"""心率血氧传感器 MAX30102 驱动（I2C）。

模块用途
--------
读取 MAX30102 的 FIFO 原始红光/红外采样，估算 **心率（bpm）** 与 **血氧（SpO2 %）**，
包装成 :class:`~health_monitor.hal.models.VitalSignsSample` 交给业务层与报警引擎。

本文件的两条设计要点（照抄 ``sensors/button.py`` 范本）
--------------------------------------------------------
1. **纯逻辑与硬件访问彻底分离**：M 点移动平均去直流 → 自适应阈值峰值检测 →
   峰值间隔求心率、比值法（ratio-of-ratios）求血氧，全部放在
   :class:`PpgAlgorithm` / :func:`analyze_ppg` 里（**不碰任何硬件、不依赖总线**），
   所以可以用合成波形离线单测、可以用假时钟复现问题；
2. **失败要留痕且不静默**：``read()`` 出问题先 ``self._note_fault(exc)`` 再抛异常；
   合法但可疑的数据（没贴手指、信号太弱）返回 ``ok=False`` 的样本，
   且心率/血氧字段一律填 ``None``（**绝不填 0**，0 bpm 会被报警引擎误判成"心率过低"）。

接线表（MAX30102 模块 → 树莓派 5 40-pin 物理脚号）
--------------------------------------------------
=================  =====================  ============================================
MAX30102 模块引脚   树莓派 40-pin          说明
=================  =====================  ============================================
VIN                3.3V（物理脚 1 或 17）  **必须 3.3V**！接 5V 会烧芯片/总线
GND                GND（物理脚 6/9/14/…）  任意一个 GND 脚
SCL                GPIO3（物理脚 5）       I2C1 SCL，板上已有 1.8k 上拉
SDA                GPIO2（物理脚 3）       I2C1 SDA，板上已有 1.8k 上拉
INT                不接（可悬空）           本驱动用轮询方式读 FIFO，不用中断
=================  =====================  ============================================

⚠️ 上电前先确认：``sudo raspi-config`` → Interface Options → I2C 已启用，
然后跑 ``i2cdetect -y 1``，应能在 **0x57** 看到器件；看不到 = 接线/供电/上拉有问题。
⚠️ MAX30102 与 LCD1602（0x27/0x3F）**共用 I2C-1**，地址不冲突，可以同时接。

负责人（团队分工时填）
----------------------
负责人：____________（学号：__________）  验收人：____________  日期：__________

关键寄存器（真实器件，数据手册 MAX30102 Rev.1）
----------------------------------------------
=====  ==================  ==========================================================
地址    名称                本驱动用法
=====  ==================  ==========================================================
0x00    INT_STATUS_1        中断状态 1（本驱动不读，保留说明）
0x01    INT_STATUS_2        中断状态 2（本驱动不读，保留说明）
0x04    FIFO_WR_PTR         FIFO 写指针（0~31 环形）
0x05    OVF_COUNTER         FIFO 溢出计数（**>0 说明上层读得太慢**，要记进错误）
0x06    FIFO_RD_PTR         FIFO 读指针
0x07    FIFO_DATA           按 3 字节一组读出：``[18:16] | [15:8] | [7:0]``（18 位）
0x08    FIFO_CONFIG         采样平均 + rollover + 几乎满阈值
0x09    MODE_CONFIG         写 0x40=软复位；写 0x03=SpO2 模式（红光+红外）
0x0A    SPO2_CONFIG         ADC 量程 + 采样率 + LED 脉宽
0x0C    LED1_PA            红光 LED 电流（0x1F≈6.2mA 为芯片上电默认）
0x0D    LED2_PA            红外 LED 电流
0x11    MULTI_LED_CTRL_1   多 LED 时隙 1/2 的 LED（0x21=RED,IR）
0x12    MULTI_LED_CTRL_2   多 LED 时序（0x03=两个时隙各 1 个脉宽）
0x1F    TEMP_INT            芯片温度整数部分（℃）
0x21    PART_ID             版本号，**读回应为 0x15**（不是 0x15 → 器件/地址不对）
=====  ==================  ==========================================================
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..hal.device import Device
from ..hal.exceptions import (
    ConfigError,
    DataInvalidError,
    DeviceInitError,
    DeviceIOError,
    DeviceTimeout,
    UnsupportedError,
)
from ..hal.models import DeviceKind, VitalSignsSample, now_ts
from ..hal.pins import describe_pin

# ==========================================================================
# 常量：器件规格（真实硬件语义，改动前先查数据手册）
# ==========================================================================

#: MAX30102 的 7 位 I2C 地址（固定，不可改）
MAX30102_ADDRESS = 0x57

#: PART_ID 寄存器（0x21）的正确读回值；不对说明器件或地址有问题
MAX30102_PART_ID = 0x15

# 寄存器地址（名称与数据手册一致）
REG_INT_STATUS_1 = 0x00        # 中断状态 1
REG_INT_STATUS_2 = 0x01        # 中断状态 2
REG_FIFO_WR_PTR = 0x04         # FIFO 写指针
REG_OVF_COUNTER = 0x05         # FIFO 溢出计数
REG_FIFO_RD_PTR = 0x06         # FIFO 读指针
REG_FIFO_DATA = 0x07           # FIFO 数据（3 字节/样本）
REG_FIFO_CONFIG = 0x08         # FIFO 配置
REG_MODE_CONFIG = 0x09         # 模式配置
REG_SPO2_CONFIG = 0x0A         # SpO2 配置
REG_LED1_PA = 0x0C             # LED1（红光）电流
REG_LED2_PA = 0x0D             # LED2（红外）电流
REG_MULTI_LED_CTRL_1 = 0x11    # 多 LED 控制 1
REG_MULTI_LED_CTRL_2 = 0x12    # 多 LED 控制 2
REG_TEMP_INT = 0x1F            # 芯片温度整数部分
REG_PART_ID = 0x21             # 版本号

#: 软复位位（MODE_CONFIG 的 bit6）
MODE_RESET = 0x40
#: SpO2 模式（红光 + 红外，两个 LED 交替发光）
MODE_SPO2 = 0x03

#: 是否允许 FIFO 读完自动回绕（与 FIFO_CONFIG_VALUE 的 bit4 对应）
FIFO_ROLLOVER_EN = True

#: FIFO 采样平均点数对应的寄存器位（bit7:5）。4 点平均 = 0b010，
#: 真实效果是"硬件层面的低通"，能明显压掉电源/工频噪声，课设推荐值。
FIFO_SAMPLE_AVG_BITS = 0b010

#: LED 电流档位：字符串 → 电阻寄存器值（0x00=关，每档约 0.2mA）
LED_CURRENT_STEPS: Dict[str, int] = {
    "0mA": 0x00,
    "1.2mA": 0x06,
    "2.0mA": 0x0A,
    "3.1mA": 0x0F,
    "4.0mA": 0x14,
    "5.0mA": 0x19,
    "6.2mA": 0x1F,   # 芯片上电默认值
    "7.6mA": 0x26,   # 本项目默认（手指薄/指甲厚时也能读到足够信号）
    "10mA": 0x32,
    "12mA": 0x3C,
    "15mA": 0x4B,
    "20mA": 0x64,
    "25mA": 0x7D,
    "31mA": 0x9B,
    "50mA": 0xFF,    # 上限：**连续点亮不要用**，会发烫
}

#: 真实的 ADC 量程表：字符串 → (ADC 满量程 nA, 寄存器位)
ADC_RANGES: Dict[str, Tuple[int, int]] = {
    "2048nA": (2048, 0b00),
    "4096nA": (4096, 0b01),
    "8192nA": (8192, 0b10),
    "16384nA": (16384, 0b11),
}

#: 真实的采样率表：字符串 → (每秒样本数, 寄存器位)
SAMPLE_RATES: Dict[str, Tuple[int, int]] = {
    "50": (50, 0b000),
    "100": (100, 0b001),
    "200": (200, 0b010),
    "400": (400, 0b011),
    "800": (800, 0b100),
    "1000": (1000, 0b101),
    "1600": (1600, 0b110),
    "3200": (3200, 0b111),
}

#: 支持的 LED 脉宽：字符串 → (微秒, 寄存器位)
LED_PULSE_WIDTHS: Dict[str, Tuple[int, int]] = {
    "69": (69, 0b00),    # 15 位
    "118": (118, 0b01),  # 16 位
    "215": (215, 0b10),  # 17 位
    "411": (411, 0b11),  # 18 位（本项目默认，精度最高）
}

#: FIFO 深度（32 个样本，硬件固定）
FIFO_DEPTH = 32

#: FIFO 配置寄存器（0x08）的取值：4 点平均 (bit7:5=0b010) | 允许回绕 (bit4=1)
#: | 几乎满阈值 15 (bit3:0=0x0F)。阈值决定 INT 何时拉低，配置正确时钟就够用。
FIFO_CONFIG_VALUE = (FIFO_SAMPLE_AVG_BITS << 5) | ((1 if FIFO_ROLLOVER_EN else 0) << 4) | 0x0F

#: SpO2 配置寄存器（0x0A）的取值：ADC 量程 4096nA (bit6:5) | 采样率 (bit4:2)
#: | LED 脉宽 411us/18bit (bit1:0)。这里默认按 100Hz + 18 位精度配置。
SPO2_CONFIG_VALUE = (
    (ADC_RANGES["4096nA"][1] << 5)
    | (SAMPLE_RATES["100"][1] << 2)
    | LED_PULSE_WIDTHS["411"][1]
)


# ==========================================================================
# 第一部分：纯逻辑（可单测，不碰任何硬件）
# ==========================================================================


@dataclass(frozen=True)
class PpgAnalysis:
    """一次 PPG（光电容积脉搏波）分析的结论。"""

    finger_detected: bool = False
    heart_rate_bpm: Optional[float] = None
    spo2_percent: Optional[float] = None
    quality: float = 0.0
    beats: int = 0          # 检出的脉搏波个数（峰值数）
    samples: int = 0        # 参与计算的样本数
    dc: float = 0.0         # 红外直流分量（"手指在不在"的判据）
    reason: str = ""        # 没给出数值时的原因（中文，便于日志排查）


@dataclass(frozen=True)
class PpgParams:
    """算法阈值（集中放这里，答辩时好解释，也好调）。"""

    hr_min_bpm: float = 30.0        # 低于 30 bpm 判为噪声
    hr_max_bpm: float = 220.0       # 高于 220 bpm 判为噪声（运动伪迹）
    finger_dc_min: float = 5000.0   # 红外直流分量下限：低于此值视为没贴手指
    min_samples_s: float = 5.0      # 至少要 5 秒数据才给结论（约 5 个脉搏波，估得稳）
    min_beats: int = 3              # 至少要 3 个波峰才够算心率
    peak_min_s: float = 0.3         # 两个波峰最小间隔（0.3s ≈ 200bpm，抗重复检出）
    peak_max_s: float = 2.0         # 两个波峰最大间隔（2.0s ≈ 30bpm）
    smooth_s: float = 0.10          # 快窗移动平均长度（秒）：跟得上脉搏波包络
    baseline_s: float = 0.40        # 慢窗移动平均长度（秒）：估算漂移基线
    ac_floor: float = 1.0           # 交流有效值下限（低于此值视为无脉搏波）
    quality_min: float = 0.30       # 质量低于此值不输出心率（宁可空着也不给坏数据）


def _moving_average(values: Sequence[float], window: int) -> List[float]:
    """居中移动平均（DC 基线估计）。

    ``window`` 为样本数；输出与输入等长，边界用可用的部分求平均。
    **纯函数**：不碰硬件，空输入返回空列表。
    """
    n = len(values)
    if n == 0:
        return []
    window = max(1, min(int(window), n))
    half = window // 2
    out: List[float] = []
    for idx in range(n):
        lo = max(0, idx - half)
        hi = min(n, idx + half + 1)
        out.append(sum(values[lo:hi]) / (hi - lo))
    return out


def _median(values: Sequence[float]) -> float:
    """中位数（对漏检/多检造成的个别异常间隔不敏感）。"""
    ordered = sorted(values)
    n = len(ordered)
    if n == 0:
        return 0.0
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _rms(values: Sequence[float]) -> float:
    """均方根（用来做自适应阈值：不依赖量程、不依赖 LED 电流）。"""
    if not values:
        return 0.0
    return math.sqrt(sum(v * v for v in values) / len(values))


def _bandpass(
    values: Sequence[float], sample_rate: float, params: Optional[PpgParams] = None
) -> List[float]:
    """简单的"带通"预处理：去直流 + 两级移动平均低通（做法朴素但可解释）。

    电子课设够用的做法：
    1. 先用**较长**的移动平均（``baseline_s``）估出低频基线并减掉
       （去掉呼吸/体温造成的漂移）；
    2. 再用**较短**的移动平均（``smooth_s``）做一次低通（压掉传感器高频噪声）；
    3. 把两级低通的输出相减——短窗跟得上脉搏跳动的包络，长窗跟不上，
       两者之差正好把"只有脉搏波才有的频段"留下（等价于一个粗糙的带通）。

    ⚠️ 这一步很关键：只做一次平滑的话，叠加在峰顶上的高频噪声会形成
    "双峰"，峰值间隔法会数出双倍的心跳（这是本项目实测踩过的坑）。

    Args:
        values: 原始红外采样（整数计数值）。
        sample_rate: 采样率（Hz）。
        params: 算法阈值（用到 ``smooth_s`` / ``baseline_s`` 两个窗长）。

    Returns:
        与输入等长的交流分量序列（无脉搏波时接近全 0）。
    """
    if not values:
        return []
    p = params or PpgParams()
    fast = _moving_average(values, max(1, int(round(p.smooth_s * sample_rate))))
    slow = _moving_average(values, max(1, int(round(p.baseline_s * sample_rate))))
    return [f - s for f, s in zip(fast, slow)]


def _is_local_peak(ac: Sequence[float], idx: int) -> bool:
    """判断 ``idx`` 是否为 (局部) 极大值点。"""
    prev_v = ac[idx - 1] if idx > 0 else -math.inf
    next_v = ac[idx + 1] if idx + 1 < len(ac) else -math.inf
    return ac[idx] > prev_v and ac[idx] > next_v


def _refine_peak(ac: Sequence[float], idx: int) -> float:
    """用抛物线插值把波峰位置细化到**亚样本精度**，返回浮点下标。

    为什么要这一步：采样率 100Hz 时，峰值下标只能落在 0.01s 网格上，
    相邻两拍的间隔会在 ±0.01s 之间跳（1000 个样本里量化为 ±0.25 bpm 的抖动）；
    用峰顶左右三个点拟合抛物线取顶点，可以得到远小于一个采样周期的精度。
    这也是"峰值间隔法"在低采样率下仍然准的关键（课设实测数据可复现）。
    """
    if idx <= 0 or idx + 1 >= len(ac):
        return float(idx)
    left, center, right = ac[idx - 1], ac[idx], ac[idx + 1]
    denom = left - 2.0 * center + right
    if denom == 0:
        return float(idx)
    offset = 0.5 * (left - right) / denom
    if not math.isfinite(offset):
        return float(idx)
    return float(idx) + max(-0.5, min(0.5, offset))


def _detect_peaks(ac: Sequence[float], sample_rate: float, params: PpgParams) -> List[int]:
    """在交流分量上用**自适应阈值 + 最小间距**检测脉搏波峰，返回样本下标。

    "M 点平均" 与峰值间隔法的经典组合：
    1. 阈值取 AC 有效值的 0.5 倍——信号强阈值自动抬高，信号弱自动降低；
    2. 以**上升沿**为触发点：先记住最后一个低于阈值的点 ``low``（真正的谷底），
       再从它往后找**第一个局部极大值**当作这一拍的峰。触发点固定在谷底，
       峰顶附近的高频抖动就不会额外制造出第二个峰；
    3. 两个峰之间必须至少隔 ``peak_min_s``（默认 0.3s ≈ 200bpm）：
       落在窗口内的重复检出会被丢弃——**这是抗"双峰"的最后一道保险**，
       只做平滑不做去抖时，噪声会数出双倍心跳（本项目实测踩过）；
    4. 间隔是否**过大**（漏检）不在这里判，交给 :func:`analyze_ppg` 的
       ``peak_min_s ~ peak_max_s`` 区间过滤统一处理，避免两处逻辑打架。

    空数据、全 0 数据、越界数据都只会返回空列表，**不会抛异常**。
    """
    n = len(ac)
    if n < 3 or sample_rate <= 0:
        return []
    level = _rms(ac)
    if level < params.ac_floor:
        return []
    threshold = 0.5 * level
    min_gap = max(1, int(round(params.peak_min_s * sample_rate)))

    peaks: List[int] = []
    low = 0  # 最近一个"低于阈值"的位置（谷底候选）
    for idx in range(n):
        value = ac[idx]
        if value < threshold:
            low = idx  # 跌破阈值：记录下来作为下一拍的谷底候选
            continue
        # 只在"谷底之后"的上升段里找第一个局部极大值，且与上一个峰拉开最小间距
        if idx > low and _is_local_peak(ac, idx) and (not peaks or idx - peaks[-1] >= min_gap):
            peaks.append(idx)
    return peaks


def _dc_ac(signal: Sequence[float]) -> Tuple[float, float]:
    """取一段信号的直流分量（均值）与交流有效值（AC RMS）。"""
    if not signal:
        return 0.0, 0.0
    dc = sum(signal) / len(signal)
    ac = [float(v) - dc for v in signal]
    return dc, _rms(ac)


def _to_18bit(group: bytes) -> int:
    """把 FIFO 里连续的 3 字节拼成一个 18 位计数值（高位先出）。

    MAX30102 的 ADC 是 18 位，数据手册规定先传高 8 位、再中 8 位、最后低 8 位，
    所以拼装顺序是 ``data[0] << 16 | data[1] << 8 | data[2]``，再掩掉无效高位。
    """
    if len(group) != 3:
        raise DataInvalidError(f"FIFO 通道数据必须是 3 字节，实际 {len(group)} 字节")
    return ((group[0] << 16) | (group[1] << 8) | group[2]) & 0x03FFFF


def _spo2_from_ratio(ir_dc: float, ir_ac: float, red_dc: float, red_ac: float) -> Optional[float]:
    """比值法（ratio-of-ratios）估算血氧。

    ``R = (AC_red / DC_red) / (AC_ir / DC_ir)``，再按 MAX30102 数据手册给出的
    经验式 ``SpO2 = 104 - 17 * R`` 估算。R≈0.5 → 约 95%。

    输入非法（分母为 0、直流太小）时返回 ``None``——**不编造数值**。
    """
    if ir_dc <= 0 or red_dc <= 0 or ir_ac <= 0 or red_ac <= 0:
        return None
    ratio = (red_ac / red_dc) / (ir_ac / ir_dc)
    if not math.isfinite(ratio) or ratio <= 0:
        return None
    spo2 = 104.0 - 17.0 * ratio
    if spo2 >= 100.0:
        # 公式在 R 很小时会给出 >100%，物理上不可能：夹到 100
        return 100.0
    return spo2


def analyze_ppg(
    ir_samples: Sequence[float],
    sample_rate: float,
    red_samples: Optional[Sequence[float]] = None,
    params: Optional[PpgParams] = None,
) -> PpgAnalysis:
    """把一段红外（可选红光）原始采样算成心率/血氧（**纯函数**）。

    算法（课程设计够用且可解释）：
    1. **手指检测**：红外直流分量均值 ``dc`` 超过 ``finger_dc_min`` 才算贴了手指。
       没贴手指直接返回 ``finger_detected=False``、心率血氧为 ``None``。
    2. **去直流 + 平滑**：M 点移动平均估基线并相减，得到脉搏波交流分量。
    3. **峰值检测**：自适应阈值（AC 有效值的 0.5 倍）+ 最小/最大间隔约束，
       峰顶位置再用抛物线插值细化到亚样本精度。
    4. **心率**：取所有相邻峰间隔的**中位数**换成 bpm（中位数抗漏检/多检）。
    5. **血氧**：红光与红外的 AC/DC 比值法。
    6. **质量**：由波峰间隔的变异系数与信号强度共同给出 0.0~1.0；
       数据不足 5 秒或质量过低时**只报手指状态、不给心率**（宁可空着也不给坏数据）。

    Args:
        ir_samples: 红外（IR）原始采样序列。
        sample_rate: 采样率（Hz）。
        red_samples: 红光原始采样序列（与 ``ir_samples`` 等长且一一对应）。可选。
        params: 算法阈值，默认 :class:`PpgParams`。

    Returns:
        :class:`PpgAnalysis`。空数据/全 0/全越界数据都返回"无结论"而不抛异常。
    """
    p = params or PpgParams()
    ir = [float(v) for v in ir_samples]
    n = len(ir)

    if n == 0 or sample_rate <= 0:
        return PpgAnalysis(samples=n, reason="没有采样数据")

    dc = sum(ir) / n
    if dc < p.finger_dc_min:
        return PpgAnalysis(
            finger_detected=False, samples=n, dc=dc,
            reason=f"红外直流分量 {dc:.0f} 低于阈值 {p.finger_dc_min:.0f}：未检测到手指",
        )

    if n < int(p.min_samples_s * sample_rate):
        return PpgAnalysis(
            finger_detected=True, samples=n, dc=dc,
            reason=f"采样时长不足 {p.min_samples_s:.1f}s（当前 {n / sample_rate:.1f}s）",
        )

    ac = _bandpass(ir, sample_rate, p)
    level = _rms(ac)
    if level < p.ac_floor:
        return PpgAnalysis(
            finger_detected=True, samples=n, dc=dc,
            reason=f"交流分量 {level:.2f} 太小：手指没贴稳或 LED 电流太低",
        )

    peaks = _detect_peaks(ac, sample_rate, p)
    if len(peaks) < p.min_beats:
        return PpgAnalysis(
            finger_detected=True, samples=n, dc=dc, beats=len(peaks),
            reason=f"只检出 {len(peaks)} 个脉搏波（至少需要 {p.min_beats} 个）",
        )

    # 波峰位置细化到亚样本精度后再算间隔（降低量化抖动对心率的污染）
    refined = [_refine_peak(ac, idx) for idx in peaks]
    intervals = [(refined[i + 1] - refined[i]) / sample_rate for i in range(len(refined) - 1)]
    valid = [dt for dt in intervals if p.peak_min_s <= dt <= p.peak_max_s]
    if not valid:
        return PpgAnalysis(
            finger_detected=True, samples=n, dc=dc, beats=len(peaks),
            reason="波峰间隔全部超出 30~200 bpm 合理区间",
        )

    period = _median(valid)
    hr = 60.0 / period
    if not (p.hr_min_bpm <= hr <= p.hr_max_bpm):
        return PpgAnalysis(
            finger_detected=True, samples=n, dc=dc, beats=len(peaks),
            reason=f"心率 {hr:.1f} bpm 不在 {p.hr_min_bpm:.0f}~{p.hr_max_bpm:.0f} 区间内",
        )

    quality = _quality(valid, period, level, len(valid))

    # 血氧用**原始信号**的 AC/DC 比值：与心率共用同一段窗，但必须用各自通道
    # 自己的均值做直流（_dc_ac），不能拿红外的电平当红光的分母（曾踩过这个坑）。
    spo2: Optional[float] = None
    if red_samples is not None and len(red_samples) == n:
        red = [float(v) for v in red_samples]
        ir_dc_raw, ir_ac_raw = _dc_ac(ir)
        red_dc, red_ac = _dc_ac(red)
        spo2 = _spo2_from_ratio(ir_dc_raw, ir_ac_raw, red_dc, red_ac)

    reason = ""
    if quality < p.quality_min:
        reason = f"数据质量 {quality:.2f} 低于 {p.quality_min:.2f}：信号不稳（手指动了？）"

    return PpgAnalysis(
        finger_detected=True,
        heart_rate_bpm=round(hr, 1),
        spo2_percent=None if spo2 is None else round(spo2, 1),
        quality=round(quality, 3),
        beats=len(peaks),
        samples=n,
        dc=dc,
        reason=reason,
    )


def _quality(intervals: Sequence[float], period: float, level: float, beats: int) -> float:
    """给出 0.0~1.0 的数据质量评分（给业务层丢弃坏点用）。

    三个因子相乘：
    - **节律一致性**：波峰间隔的变异系数越小越好（跳动的心率节拍应当稳定）；
    - **信号强度**：交流分量越大越可信（50 计数以上记满分）；
    - **波数充分性**：波峰越多，估计越稳（5 个波以上记满分）。
    """
    if period <= 0 or not intervals:
        return 0.0
    mean = sum(intervals) / len(intervals)
    if mean <= 0:
        return 0.0
    var = sum((dt - mean) ** 2 for dt in intervals) / len(intervals)
    cv = math.sqrt(var) / mean
    consistency = max(0.0, 1.0 - cv / 0.25)
    strength = max(0.0, min(1.0, level / 50.0))
    count = max(0.0, min(1.0, beats / 5.0))
    return max(0.0, min(1.0, consistency * strength * count))


# ==========================================================================
# 第二部分：驱动（硬件访问层）
# ==========================================================================


class Max30102(Device):
    """MAX30102 心率血氧传感器（I2C 0x57）驱动。

    Args:
        bus: I2C 总线号（默认 1 → ``/dev/i2c-1``）。
             若想注入自定义总线对象（MockBus 或 RealBus），请用关键字 ``bus=`` 传入。
        address: 7 位 I2C 地址，默认 0x57（MAX30102 固定地址）。
        led_current: LED 电流档位字符串，见 :data:`LED_CURRENT_STEPS`，默认 ``"7.6mA"``。
        sample_rate: 采样率（Hz）字符串/整数，默认 100。
        quality_min: 数据质量门槛（0.0~1.0），低于它就不输出心率/血氧。
            业务层若要求更严（例如只在静息时测），可以调高到 0.5~0.6。
        mock: 模拟模式。**为 True 时绝不打开真实 I2C，用内存波形合成数据。**
        mock_auto_wave: mock 模式下 ``read()`` 是否自动推进合成波形（默认 True）。
            为 False 时只分析已有缓冲——测试或演示脚本可以先用 :meth:`inject`
            喂一段**确定的**波形，再 ``read()`` 看算法结论，不被自动波形混进来。
        name: 实例名（与 ``config/devices.json`` 的键一致）。

    Raises:
        ConfigError: 参数取值不在支持表里（电流档位/采样率/地址非法）。
    """

    KIND = DeviceKind.VITAL
    NAME = "max30102"

    def __init__(
        self,
        bus: Any = None,
        address: int = MAX30102_ADDRESS,
        led_current: str = "7.6mA",
        sample_rate: int = 100,
        quality_min: float = PpgParams.quality_min,
        mock: bool = False,
        mock_auto_wave: bool = True,
        name: str = "",
    ) -> None:
        super().__init__(bus=bus, mock=mock, name=name)
        if not 0.0 <= float(quality_min) <= 1.0:
            raise ConfigError(f"quality_min={quality_min} 非法：应在 0.0~1.0 之间")
        self.quality_min = float(quality_min)
        self.mock_auto_wave = bool(mock_auto_wave)
        self._params = PpgParams(quality_min=self.quality_min)

        # 参数校验放在构造期（启动即失败，别等跑起来才炸）
        if not 0x03 <= int(address) <= 0x77:
            raise ConfigError(f"MAX30102 的 I2C 地址 0x{int(address):02X} 非法（应在 0x03~0x77）")
        if led_current not in LED_CURRENT_STEPS:
            raise ConfigError(
                f"不支持的 LED 电流档位 {led_current!r}；可用："
                f"{', '.join(LED_CURRENT_STEPS)}"
                f"（对照 MAX30102 数据手册的 LED_PA 寄存器）"
            )
        rate_key = str(int(sample_rate))
        if rate_key not in SAMPLE_RATES:
            raise ConfigError(
                f"不支持的采样率 {sample_rate!r}；可用：{', '.join(SAMPLE_RATES)}"
            )

        # ``bus`` 既可以是总线号（int），也可以是注入的总线对象（MockBus/RealBus）
        self.i2c_bus = int(bus) if isinstance(bus, int) else 1
        self._bus: Any = None if isinstance(bus, int) else bus
        self.address = int(address)
        self.led_current = led_current
        self.sample_rate = int(rate_key)

        # 运行期状态
        self._ir_buf: List[int] = []                 # 红外采样环形缓冲（分析窗）
        self._red_buf: List[int] = []                # 红光采样环形缓冲（与上面一一对应）
        self._window_samples = self.sample_rate * 10  # 分析窗长：10 秒
        self._mock_index = 0                         # mock 波形相位（样本序号）
        self._mock_ready = False                     # mock 波形是否已初始化
        self._mock_fault: Optional[BaseException] = None  # 测试注入的故障
        self._ovf_total = 0                          # FIFO 溢出累计（**读太慢的凭据**）

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def open(self) -> None:
        """初始化 MAX30102。

        mock 模式：**只置位 ``_opened`` 并准备内存合成波形**，绝不打开 ``/dev/i2c-*``；
        真实模式：复位 → 等 RESET 清零 → 清 FIFO 指针 → 配 FIFO/SpO2 → 设 LED 电流
        → 进 SpO2 模式，任何一步失败都抛 :class:`DeviceInitError`（含排查线索）。
        """
        if self._opened:
            return
        if self.mock:
            self._mock_ready = True
            self._opened = True
            return

        if self._bus is None:
            from ..hal.mock_bus import RealBus

            self._bus = RealBus(self.i2c_bus)

        try:
            self._reset_device()
            self._init_sequence()
        except (DeviceIOError, DeviceTimeout) as exc:
            self._note_fault(exc)
            raise DeviceInitError(
                f"MAX30102 初始化失败（I2C-{self.i2c_bus} 地址 0x{self.address:02X}）：{exc}。"
                "排查线索：① `i2cdetect -y 1` 看 0x57 是否出现；"
                "② 确认 VIN 接的是 3.3V 而不是 5V；③ 确认 SDA=物理脚3/SCL=物理脚5 没接反；"
                "④ `sudo raspi-config` 里 I2C 是否启用；⑤ 是否需要用 sudo 或有 i2c 组权限"
            ) from exc

        self._opened = True

    def _reset_device(self) -> None:
        """软复位（MODE_CONFIG 写 0x40），然后等 RESET 位自己清零。

        真实器件会在几毫秒内清零；这里最多等 200ms，超时抛 :class:`DeviceTimeout`。
        """
        import time

        self._write_reg(REG_MODE_CONFIG, MODE_RESET)
        for _ in range(20):
            time.sleep(0.01)
            if not (self._read_reg(REG_MODE_CONFIG) & MODE_RESET):
                return
        raise DeviceTimeout(
            "MAX30102 复位位 200ms 内未清零（供电不足或器件未正常应答，检查 3.3V 供电）"
        )

    def _init_sequence(self) -> None:
        """按数据手册顺序完成初始化，并记录读回值以便留痕。"""
        part_id = self._read_reg(REG_PART_ID)
        if part_id != MAX30102_PART_ID:
            raise DeviceInitError(
                f"MAX30102 版本号寄存器(0x21)读回 0x{part_id:02X}，应为 0x{MAX30102_PART_ID:02X}："
                "可能地址不对（LCD1602 在 0x27/0x3F）、器件没焊好，或线太长导致 I2C 误码"
            )

        # 清 FIFO 指针与溢出计数
        self._write_reg(REG_FIFO_WR_PTR, 0x00)
        self._write_reg(REG_FIFO_RD_PTR, 0x00)
        self._write_reg(REG_OVF_COUNTER, 0x00)

        # FIFO 配置：4 点平均 + rollover + 几乎满阈值 15
        self._write_reg(REG_FIFO_CONFIG, FIFO_CONFIG_VALUE)

        # SpO2 配置：ADC 量程 4096nA + 采样率（用户可选）+ LED 脉宽 411us（18 位）
        rate_bits = SAMPLE_RATES[str(self.sample_rate)][1]
        pulse_bits = LED_PULSE_WIDTHS["411"][1]
        range_bits = ADC_RANGES["4096nA"][1]
        self._write_reg(
            REG_SPO2_CONFIG,
            (range_bits << 5) | (rate_bits << 2) | pulse_bits,
        )

        # LED 电流（红光 LED1 / 红外 LED2）
        current = LED_CURRENT_STEPS[self.led_current]
        self._write_reg(REG_LED1_PA, current)
        self._write_reg(REG_LED2_PA, current)

        # 多 LED 控制：时隙1=红光(0x21 的低两位=RED)，时隙2=红外
        self._write_reg(REG_MULTI_LED_CTRL_1, 0x21)
        self._write_reg(REG_MULTI_LED_CTRL_2, 0x03)

        # 进入 SpO2 模式（每槽一个 LED：红光、红外）
        self._write_reg(REG_MODE_CONFIG, MODE_SPO2)

    def read(self) -> VitalSignsSample:
        """读一次 FIFO 并给出心率/血氧结论。

        Returns:
            没贴手指 / 数据不足 / 质量太差时 ``ok=False`` 且心率血氧为 ``None``
            （**绝不填 0**）。质量不达标时**保留数值但 ``ok=False``**，
            并在 ``error`` 里说明原因——业务层只看 ``ok`` 就不会被坏数据骗到。

        Raises:
            DeviceNotReady: 未 ``open()``。
            DeviceIOError / DeviceTimeout: I2C 读失败（FIFO 一直空）。
        """
        self._require_open()

        try:
            if self.mock:
                # 测试用故障注入（模拟"这一次 I2C 读失败了"），真实模式永远为 None
                if self._mock_fault is not None:
                    exc = self._mock_fault
                    self._mock_fault = None
                    raise DeviceIOError(f"MAX30102 读取失败（注入的故障）：{exc}") from exc
                if self.mock_auto_wave:
                    self._pull_mock_samples()
            else:
                self._drain_fifo()
        except (DeviceIOError, DeviceTimeout) as exc:
            self._note_fault(exc)
            raise

        self._note_ok()
        return self._analyze_buffer()

    def close(self) -> None:
        """关闭传感器：尽量关掉两个 LED，然后释放资源（**幂等、不抛异常**）。"""
        if self._bus is not None and not self.mock and self._opened:
            try:
                # 关灯：避免器件持续发热（对手指测温/功耗都不好）
                self._write_reg(REG_LED1_PA, 0x00)
                self._write_reg(REG_LED2_PA, 0x00)
                self._write_reg(REG_MODE_CONFIG, 0x80)  # shutdown 位
            except Exception:  # noqa: BLE001 - 关闭失败不应影响收尾
                pass
        self._ir_buf.clear()
        self._red_buf.clear()
        self._mock_ready = False
        self._opened = False

    # ------------------------------------------------------------------
    # mock 支持（集成演示用）
    # ------------------------------------------------------------------

    def inject(self, ir_samples: Sequence[int], red_samples: Optional[Sequence[int]] = None) -> None:
        """**仅供 mock / 测试**：人为塞入一段原始 FIFO 采样供 ``read()`` 分析。

        真实模式下调用会抛 :class:`UnsupportedError`——真机上不该有"人造数据"。
        业务层的集成演示脚本用它复现"某段波形算出来是多少 bpm"。
        """
        if not self.mock:
            raise UnsupportedError("inject() 只能在 mock=True 时使用（防止污染真实读数）")
        reds = list(red_samples) if red_samples is not None else [0] * len(ir_samples)
        if len(reds) != len(ir_samples):
            raise ConfigError("inject() 的红光样本数必须与红外样本数相同")
        self._ir_buf.extend(int(v) for v in ir_samples)
        self._red_buf.extend(int(v) for v in reds)
        self._trim_buffers()

    def set_mock_fault(self, exc: Optional[BaseException] = None) -> None:
        """**仅供 mock / 测试**：让下一次 ``read()`` 抛出故障（默认 :class:`DeviceIOError`）。"""
        if not self.mock:
            raise UnsupportedError("set_mock_fault() 只能在 mock=True 时使用")
        self._mock_fault = exc or DeviceIOError("注入的故障：mock 的 I2C 读取失败")

    # ------------------------------------------------------------------
    # 内部：真实硬件访问
    # ------------------------------------------------------------------

    def _read_reg(self, reg: int) -> int:
        """读一个寄存器（写寄存器地址后读 1 字节）。"""
        data = self._bus.i2c_write_read(self.i2c_bus, self.address, bytes([reg & 0x7F]), 1)
        return int(data[0])

    def _write_reg(self, reg: int, value: int) -> None:
        """写一个寄存器。"""
        self._bus.i2c_write(self.i2c_bus, self.address, bytes([reg & 0x7F, value & 0xFF]))

    def _read_fifo(self, count: int) -> List[Tuple[int, int]]:
        """从 FIFO_DATA(0x07) 连读 ``count`` 组样本 → ``[(红外, 红光), ...]``。

        ⚠️ **每组样本是 6 字节，不是 3 字节**：MAX30102 工作在 SpO2 模式时，
        每个采样点会依次把**红光 3 字节 + 红外 3 字节**推进 FIFO
        （数据手册 "FIFO Data (0x07)" 一节的时序：SLOT1 先、SLOT2 后）。
        每组 3 字节都是 18 位有效（高 6 位是 0），拼装时要先左移再掩码 0x03FFFF。
        """
        size = 6 * count
        raw = self._bus.i2c_write_read(
            self.i2c_bus, self.address, bytes([REG_FIFO_DATA]), size
        )
        if len(raw) != size:
            raise DeviceIOError(
                f"MAX30102 FIFO 期望 {size} 字节（{count} 组×6 字节：红光+红外），"
                f"实收 {len(raw)} 字节（I2C 误码或器件掉线）"
            )
        out: List[Tuple[int, int]] = []
        for i in range(count):
            base = 6 * i
            red = _to_18bit(raw[base:base + 3])
            ir = _to_18bit(raw[base + 3:base + 6])
            out.append((ir, red))
        return out

    def _drain_fifo(self) -> None:
        """把 FIFO 里所有新样本读进分析窗；FIFO 一直空则抛 :class:`DeviceTimeout`。"""
        import time

        stashed: Optional[List[Tuple[int, int]]] = None
        for attempt in range(3):
            wr = self._read_reg(REG_FIFO_WR_PTR) & 0x1F
            rd = self._read_reg(REG_FIFO_RD_PTR) & 0x1F
            count = (wr - rd) % FIFO_DEPTH
            if count:
                if stashed is not None:
                    self._ingest(stashed)
                    stashed = None
                self._ingest(self._read_fifo(count))
                return
            if attempt == 0 and stashed is None:
                stashed = self._read_fifo(1)  # 先读一组，逼器件把新样本推出来
                continue
            time.sleep(0.02)

        ovf = self._read_reg(REG_OVF_COUNTER)
        if ovf:
            self._ovf_total += ovf
            raise DeviceIOError(
                f"MAX30102 FIFO 溢出计数 {ovf}（累计 {self._ovf_total}）："
                "上层读得太慢，请缩短采样周期或减小 FIFO 平均点数"
            )
        raise DeviceTimeout(
            "MAX30102 FIFO 连续无新数据：检查手指是否贴上、LED 电流是否太小、"
            "I2C 是否被 LCD1602 抢占总线"
        )

    def _ingest(self, samples: Sequence[Tuple[int, int]]) -> None:
        """把一组 ``[(红外, 红光), ...]`` 追加进分析窗（超长自动丢最旧）。"""
        for ir, red in samples:
            self._ir_buf.append(int(ir))
            self._red_buf.append(int(red))
        self._trim_buffers()

    def _trim_buffers(self) -> None:
        """分析窗只保留最近 ``_window_samples`` 个样本。"""
        if len(self._ir_buf) > self._window_samples:
            del self._ir_buf[: len(self._ir_buf) - self._window_samples]
        if len(self._red_buf) > self._window_samples:
            del self._red_buf[: len(self._red_buf) - self._window_samples]

    # ------------------------------------------------------------------
    # 内部：mock 波形（不碰任何硬件）
    # ------------------------------------------------------------------

    def _pull_mock_samples(self) -> None:
        """模拟模式：按时间推进合成一段"手指贴着"的脉搏波，喂给分析窗。

        合成规则（答辩时可以现场解释）：红外直流 60000 计数、交流基波幅度 600 计数；
        红光直流 58000 计数、交流基波幅度 307 计数——红光/红外的 AC-DC 比约为 0.53，
        比值法 R≈0.53 → SpO2≈95%，正落在健康老人的合理区间（不假、也不报警）。
        """
        if not self._mock_ready:
            self._mock_ready = True
        # 每次 read() 推进 0.5 秒的样本（相当于轮询周期 0.5s、器件在 FIFO 里攒了 50 个样本），
        # 这样业务层每秒轮询两次也能在几秒内攒满 10 秒分析窗。
        step = max(1, self.sample_rate // 2)
        for _ in range(step):
            t = self._mock_index / float(self.sample_rate)
            phase = 2.0 * math.pi * 1.0 * t          # 60 bpm 的基波（1 Hz）
            ir = 60000 + 600 * math.sin(phase) + 180 * math.sin(2 * phase)
            red = 58000 + 307 * math.sin(phase) + 92 * math.sin(2 * phase)
            self._ir_buf.append(int(ir))
            self._red_buf.append(int(red))
            self._mock_index += 1
        self._trim_buffers()

    # ------------------------------------------------------------------
    # 内部：把分析窗交给纯算法
    # ------------------------------------------------------------------

    def _analyze_buffer(self) -> VitalSignsSample:
        """调用纯算法 :func:`analyze_ppg` 并包装成样本。"""
        analysis = analyze_ppg(
            self._ir_buf, self.sample_rate, self._red_buf, self._params
        )
        if not analysis.finger_detected:
            # 没贴手指：清空缓冲，避免手指再贴上来时把"旧手指"的数据算进去
            self._ir_buf.clear()
            self._red_buf.clear()
            if self.mock:
                self._mock_index = 0
            return VitalSignsSample(
                ts=now_ts(), device=self.name, ok=False,
                error="未检测到手指（红外直流分量过低），请把指腹完全覆盖传感器窗口",
                heart_rate_bpm=None, spo2_percent=None,
                finger_detected=False, quality=0.0,
            )

        enough = (
            analysis.heart_rate_bpm is not None
            and analysis.spo2_percent is not None
            and not analysis.reason
        )
        return VitalSignsSample(
            ts=now_ts(),
            device=self.name,
            ok=enough,
            error=None if enough else _reason_to_error(analysis.reason),
            heart_rate_bpm=analysis.heart_rate_bpm,
            spo2_percent=analysis.spo2_percent,
            finger_detected=True,
            quality=analysis.quality,
        )

    # ------------------------------------------------------------------
    # 可选覆盖
    # ------------------------------------------------------------------

    def describe(self) -> Dict[str, Any]:
        """接线说明（会被 ``docs`` 生成脚本读取）。"""
        return {
            "name": self.name,
            "kind": self.KIND.value,
            "mock": self.mock,
            "bus": f"I2C-{self.i2c_bus}（/dev/i2c-{self.i2c_bus}），地址 0x{self.address:02X}",
            "pins": {
                "vin": "3.3V（物理脚 1 或 17）—— 必须是 3.3V，接 5V 会烧",
                "gnd": "GND（物理脚 6/9/14/20/25/30/34/39 任一）",
                "scl": describe_pin(3),
                "sda": describe_pin(2),
                "int": "不接（悬空即可，本驱动用轮询读 FIFO）",
            },
            "notes": (
                f"LED 电流 {self.led_current}；采样率 {self.sample_rate}Hz；"
                f"分析窗 {self._window_samples / self.sample_rate:.0f}s；"
                "算法=M 点平均去直流 + 自适应阈值峰值间隔 + 抛物线亚样本细化；"
                "与 LCD1602(0x27/0x3F) 可共用 I2C-1"
                + ("；mock 自动合成波形（可用 inject 覆盖）" if self.mock else "")
            ),
        }

    def self_check(self) -> Dict[str, Any]:
        """自检：mock 模式做一次合成读数；真实模式读 PART_ID（应为 0x15）。

        ⚠️ mock 模式下心率需要攒够 5 秒分析窗才有结论，所以这里会连续合成
        若干轮（**只是内存里的波形，不碰任何硬件**），让体检报告能给出具体数值。
        """
        detail: Dict[str, Any] = {"name": self.name, "mock": self.mock}
        was_open = self._opened
        try:
            if not was_open:
                self.open()
            if self.mock:
                # 攒够分析窗：每次 _pull_mock_samples() 推进 0.5 秒
                for _ in range(int(self._window_samples / max(1, self.sample_rate // 2)) + 1):
                    self._pull_mock_samples()
                sample = self.read()
                return {
                    "ok": bool(sample.ok),
                    "detail": (
                        f"mock 合成读数：心率={sample.heart_rate_bpm}，"
                        f"血氧={sample.spo2_percent}（{sample.error or '正常'}）"
                    ),
                    **detail,
                }
            part = self._read_reg(REG_PART_ID)
        except Exception as exc:  # noqa: BLE001 - 自检要捕获一切并如实上报
            return {"ok": False, "detail": f"{type(exc).__name__}: {exc}", **detail}
        finally:
            if not was_open:
                self.close()
        ok = part == MAX30102_PART_ID
        return {
            "ok": ok,
            "detail": f"PART_ID=0x{part:02X}（应为 0x{MAX30102_PART_ID:02X}）",
            **detail,
        }

    def __repr__(self) -> str:
        return (
            f"<Max30102 name={self.name!r} i2c={self.i2c_bus} addr=0x{self.address:02X} "
            f"mock={self.mock} opened={self._opened}>"
        )


def _reason_to_error(reason: str) -> str:
    """把算法的中文原因翻成"给业务层看的一行错误"，空原因给个兜底文案。"""
    if reason:
        return f"本次未得出心率/血氧：{reason}"
    return "本次未得出心率/血氧（数据不足）"


__all__ = [
    "Max30102",
    "PpgAnalysis",
    "PpgParams",
    "analyze_ppg",
    "LED_CURRENT_STEPS",
    "ADC_RANGES",
    "SAMPLE_RATES",
    "LED_PULSE_WIDTHS",
    "MAX30102_ADDRESS",
    "MAX30102_PART_ID",
]
