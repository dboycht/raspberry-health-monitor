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
0x20    TEMP_FRAC           芯片温度小数部分（每档 1/16 ℃）
0x21    PART_ID *或* TEMP_EN  **两种已知布局**（见下），不是"唯一真相"
0xFF    PART_ID（备选布局）  MAX30105 布局下版本号在这里，读回同样应为 0x15
0xFE    REV_ID              修订号（MAX3010x 实测读回 0x03）
=====  ==================  ==========================================================

⚠️ **PART_ID 有两种已知布局，只查 0x21 会把可用器件误判成"器件可疑"**
（2026-09-29 真机实测，见 `ERROR.md` **E49**）：

====================  ==========================  ==============================
布局                   PART_ID 位置                 0x21 是什么
====================  ==========================  ==============================
MAX30102/MAX30101     0x21（读回 0x15）             PART_ID
MAX30105（SparkFun）  0xFF（读回 0x15）             TEMP_CONFIG（TEMP_EN）
====================  ==========================  ==============================

**怎么区分（有区分力的一句判据）**：往 `0x21` 写 `0x01` 再读 `0x1F`/`0x20` ——
若立刻报出一个**像室温的温度**（本机实测 `23 + 15/16 = 23.94 ℃`，与 DHT11 同时刻的
23.8~24.2 ℃ 吻合），说明 `0x21` 是 `TEMP_EN`（MAX30105 布局），PART_ID 要去 `0xFF` 读；
真 MAX30102 的 `0x21` 是只读 PART_ID，写它不会"使能出温度"。

⚠️ **FIFO 的结构与速率（2026-09-29 真机实测，`ERROR.md` E50/E51）**

用"只点亮红光 LED、把红外 LED 关掉"的办法让相邻样本差异悬殊，一次事务突发读 30 字节：

    2179    25   2172    29   2162    24   2174    27   2166    31
    ↑红光亮 ↑红外关  ↑红光亮 ↑红外关  ……（严格交替）

由此钉死三件事（**都是实测，不是查手册抄来的推断**）：

1. **每个 FIFO 单元 = 一个 LED 的 3 字节**，顺序固定 **先红光、后红外**；
   ⇒ 驱动按 `(前 3 字节=红光, 后 3 字节=红外)` 拼装是**对的**；
2. **读写指针按"组"（红光+红外=6 字节）计数**：一次读 30 字节后 `RD_PTR` 正好 +5；
   ⇒ `FIFO_DEPTH = 32` 与 `count = (WR_PTR - RD_PTR) % 32` 按组算也是**对的**；
3. **有效采样率 = `SPO2_SR` ÷ 平均点数**：`SR=100 + 4 点平均` 实测 **24.6 组/秒**
   （关掉平均则 98.7 组/秒）⇒ **分析的时间基数必须是 `sample_rate / FIFO_SAMPLE_AVG`**，
   直接用 `sample_rate` 会把心率算成约 4 倍（旧实现就是这个问题，E51）。

另外两条同源实测（E50）：`smbus2` 单次块读**上限 32 字节**（超了抛 `ValueError`，
**不是** OSError），所以 FIFO 必须**分块读**；且**器件被留在 shutdown / multi-LED 等状态后，
只写一次软复位不一定能恢复采样** —— 诊断脚本恢复时先写 `0x09=0x80`（shutdown）再复位更稳。
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

#: 所有 MAX3010x 系器件通用的 PART_ID 期望值（两种布局都一样）
MAX30102_PART_ID = 0x15

#: PART_ID 的**两种已知寄存器布局**（任一处读到 0x15 都算对上）：
#:   - ``0x21``：MAX30102 / MAX30101 数据手册布局（本驱动最初的实现只认这一处）
#:   - ``0xFF``：MAX30105 布局（SparkFun MAX3010x 库的 ``REG_PART_ID`` 就是 0xFF）
#: ⚠️ 2026-09-29 真机实测（`ERROR.md` **E49**）：本机模块的 PART_ID 报在 ``0xFF``，
#: 而 ``0x21`` 是 ``TEMP_CONFIG``（读回 0x00）⇒ 旧实现把**可用器件**判成"器件可疑"。
PART_ID_REGISTERS: Tuple[int, ...] = (0x21, 0xFF)

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
REG_TEMP_FRAC = 0x20           # 芯片温度小数部分（1/16 ℃ 一档）
REG_TEMP_CONFIG = 0x21         # 温度使能（MAX30105 布局下 0x21 是这个）
REG_PART_ID = 0x21             # 版本号（主布局）
REG_PART_ID_ALT = 0xFF         # 版本号（MAX30105 备选布局）
REG_REV_ID = 0xFE              # 修订号

#: 软复位位（MODE_CONFIG 的 bit6）
MODE_RESET = 0x40
#: 关断位（MODE_CONFIG 的 bit7）；复位前先置它，见 `_reset_device()` 的说明（E51 补记）
MODE_SHUTDOWN = 0x80
#: SpO2 模式（红光 + 红外，两个 LED 交替发光）
MODE_SPO2 = 0x03

#: 是否允许 FIFO 读完自动回绕（与 FIFO_CONFIG_VALUE 的 bit4 对应）
FIFO_ROLLOVER_EN = True

#: FIFO 深度（**以 3 字节样本为单位**，硬件固定 32）
FIFO_DEPTH = 32

#: `smbus2` 单次块读的硬上限：**32 字节**（SMBus 规范）。
#: 超过就抛 ``ValueError: Desired block length over 32 bytes``（见 `ERROR.md` E50）。
MAX_I2C_BLOCK_BYTES = 32

#: 一次 I2C 事务最多能读几组样本：32 // 6 = **5 组（30 字节）**。
#: ⚠️ 少了它就会"一次读 25 组 = 150 字节"直接崩（E50）。
FIFO_SAMPLES_PER_READ = MAX_I2C_BLOCK_BYTES // 6

#: PPG 带通的切点（Hz）—— **必须切掉呼吸**：呼吸引起的基线波动在 0.15~0.5 Hz
#: （9~30 次/分），幅度常比脉搏波还大；旧实现（两个滑动平均）的高通拐点太低，
#: 放它过去 ⇒ 心率被带偏（真机实测：真实 66 bpm 被算成 116~126，见 `ERROR.md` **E52**）。
#: 0.7 Hz ≈ 42 bpm，覆盖老年人静息心率；4 Hz ≈ 240 bpm，留足余量。
PPG_BAND_LOW_HZ = 0.7
PPG_BAND_HIGH_HZ = 4.0

#: 频谱法找心率的**搜索下限**（bpm）：0.7 Hz 高通以下的残留不该被当成心率。
#: 45 bpm ≈ 0.75 Hz，与上面的高通切点配套（`ERROR.md` E52：不带这个下限时，
#: 带边残留会把结果往下带，真机的 66 bpm 会被"谐波校验"错误地对半砍成 33）。
PPG_HR_SEARCH_MIN_BPM = 45.0

#: 频谱主峰 SNR（主峰 / 带内本底）换算成质量分的两端：2.0 记 0 分、5.0 记满分。
#: 标定依据：真机可用波形 4.09、干净合成 35~51、纯噪声/呼吸 2.12（E52）。
PPG_SPEC_SNR_FLOOR = 2.0
PPG_SPEC_SNR_FULL = 5.0

#: FIFO 采样平均点数对应的寄存器位（bit7:5）。4 点平均 = 0b010，
#: 真实效果是"硬件层面的低通"，能明显压掉电源/工频噪声，课设推荐值。
FIFO_SAMPLE_AVG_BITS = 0b010

#: 平均点数（由上面的寄存器位换算）。
#: ⚠️⚠️ **平均会同比降低 FIFO 的产出速率**（2026-09-29 真机实测，`ERROR.md` **E51**）：
#: SPO2_SR=100 + 4 点平均 ⇒ 实测 FIFO **只有 24.6 组/秒**（= 100/4）；
#: 把平均关掉（AVG=1）⇒ 实测 98.7 组/秒（= 100/1）。
#: 也就是说"配置里的采样率"**不是**分析该用的时间基数，必须除以平均点数 ——
#: 否则心率会被算成真实值的 4 倍（旧实现就是这个问题）。
FIFO_SAMPLE_AVG = 1 << FIFO_SAMPLE_AVG_BITS

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
    #: 这个心率是用哪条路子得到的：``"自相关"``（主）/ ``"峰值间隔"``（兜底），便于排查
    source: str = ""
    #: 自相关主峰的相关度（0~1）：越高说明波形周期性越好
    acf_score: float = 0.0


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
    ac_floor: float = 1.0           # 交流有效值下限（低于此值视为无脉搏波）
    quality_min: float = 0.30       # 质量低于此值不输出心率（宁可空着也不给坏数据）
    #: 自相关主峰的相关度门限：低于它就认为"这段波形没有可靠周期"（不编数值）。
    #: 0.35 是实测调出来的：真机那份可用波形在 0.5 上下，而纯噪声段在 0.2 以下。
    acf_min_score: float = 0.35


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


def _detrend_linear(values: Sequence[float]) -> List[float]:
    """去掉**线性趋势**（最小二乘拟合直线后相减），返回与输入等长的新列表。

    为什么必须在滤波前做（2026-09-29 真机实测，`ERROR.md` **E52**）：
    PPG 原始信号带着几万计数的直流 + 缓慢漂移，而 IIR 高通在"从 0 跳到直流电平"
    的开头会产生**巨大瞬态**，把整段波形淹没（实测 RMS 抬到 4~8 倍、峰值检测只剩 1~2 个波）。
    先去趋势就等价于把滤波器的初始条件对齐到信号电平，瞬态随之消失。
    """
    n = len(values)
    if n < 2:
        return [float(v) for v in values]
    sx = n * (n - 1) / 2.0
    sy = sum(float(v) for v in values)
    sxx = (n - 1) * n * (2 * n - 1) / 6.0
    sxy = sum(i * float(v) for i, v in enumerate(values))
    denom = n * sxx - sx * sx
    if denom == 0:  # pragma: no cover - n<2 已经挡掉
        mean = sy / n
        return [float(v) - mean for v in values]
    slope = (n * sxy - sx * sy) / denom
    intercept = (sy - slope * sx) / n
    return [float(v) - (slope * i + intercept) for i, v in enumerate(values)]


def _biquad_coeffs(kind: str, f0: float, q: float, sample_rate: float) -> Tuple[float, float, float, float, float]:
    """双二次（biquad）滤波器系数 —— **RBJ Audio EQ Cookbook** 的标准公式。

    为什么不用 ``scipy.signal.butter``：本项目的驱动是**纯标准库**（树莓派上不额外装 scipy），
    而二阶 Butterworth 用 ``Q = 1/sqrt(2)`` 的双二次就能精确表达：三十行、可解释、可单测。
    """
    w0 = 2.0 * math.pi * f0 / sample_rate
    cos_w0 = math.cos(w0)
    sin_w0 = math.sin(w0)
    alpha = sin_w0 / (2.0 * q)
    if kind == "lowpass":
        b0 = (1.0 - cos_w0) / 2.0
        b1 = 1.0 - cos_w0
        b2 = b0
    elif kind == "highpass":
        b0 = (1.0 + cos_w0) / 2.0
        b1 = -(1.0 + cos_w0)
        b2 = b0
    else:  # pragma: no cover - 只在本文件内部调用
        raise ValueError(f"未知滤波器类型 {kind!r}")
    a0 = 1.0 + alpha
    a1 = -2.0 * cos_w0
    a2 = 1.0 - alpha
    return (b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0)


def _apply_biquad(
    values: Sequence[float], coeffs: Tuple[float, float, float, float, float]
) -> List[float]:
    """按 Direct Form I 跑一遍双二次（零初始状态）。"""
    b0, b1, b2, a1, a2 = coeffs
    x1 = x2 = y1 = y2 = 0.0
    out: List[float] = []
    for x in values:
        y = b0 * x + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        x2, x1 = x1, x
        y2, y1 = y1, y
        out.append(y)
    return out


def _bandpass(
    values: Sequence[float], sample_rate: float, params: Optional[PpgParams] = None
) -> List[float]:
    """带通预处理：**0.7~4 Hz 二阶 Butterworth（前向-反向各滤一遍，零相位）**。

    ⚠️ **为什么从"两个滑动平均"换成真正的带通**（2026-09-29 真机实测，`ERROR.md` **E52**）：
    旧实现用 ``smooth_s=0.1s`` 与 ``baseline_s=0.4s`` 两个滑动平均相减，**高通拐点太低**
    （0.4s 窗对 0.3Hz 分量的增益仍有 0.99）⇒ **呼吸引起的基线波动（0.15~0.5 Hz，即
    9~30 次/分）几乎原样通过**。实测那份手指波形里，频谱主导能量在 5 / 20.6 / 29.4 bpm
    （漂移 + 呼吸），真正 66 bpm 的脉搏峰只有它们的 1/2.4 ⇒ 峰值间隔法锁错，
    报出 116~126 bpm、质量分 0.00。换成 0.7 Hz 高通后，0.3 Hz 被压掉约 16 dB（≈6 倍），
    脉搏成为带内主成分。

    切点参考：HeartPy（**MIT**，© 2021 Paul van Gent，见
    https://github.com/paulvangentcom/heartrate_analysis_python 的
    ``filter_signal(cutoff=[0.75, 3.5], order=3, filtertype='bandpass')``）与文献常用
    0.5~4 Hz / 0.7~4 Hz；本项目取 0.7~4 Hz（0.7 Hz ≈ 42 bpm，覆盖老年人静息心率；
    4 Hz ≈ 240 bpm）。

    Args:
        values: 原始红外（或红光）采样（整数计数值）。
        sample_rate: 采样率（Hz）；很低时自动收敛切点，绝不让 ``low >= high``。
        params: 保留参数位（切点由模块常量决定，不再依赖两个窗长）。

    Returns:
        与输入等长的交流分量序列（无脉搏波时接近全 0）。
    """
    if not values or sample_rate <= 0:
        return []
    rate = float(sample_rate)
    # ★ **先线性去趋势（去掉直流与缓慢漂移）再滤波** —— 不能省！
    #   零初始状态的高通遇到"信号从 0 跳到直流电平"会在开头炸出一大段瞬态：
    #   实测那份真机波形首点冲到 19982（RMS 从 1893 被抬到 7145），
    #   干净合成波形的 RMS 也被抬到 5702（本该 ~340）⇒ 峰值检测只剩 1~2 个波、
    #   自相关被污染成一条长周期余弦。去趋势后首点瞬态降到 3144、波峰恢复正常。
    values = _detrend_linear(values)
    nyquist = rate / 2.0
    low = min(PPG_BAND_LOW_HZ, nyquist * 0.4)
    high = min(PPG_BAND_HIGH_HZ, nyquist * 0.9)
    if low >= high:
        # 采样率实在太低（连一个倍频程都放不下）：退化成"只去直流"，至少不编数据
        mean = sum(values) / len(values)
        return [float(v) - mean for v in values]
    q = 1.0 / math.sqrt(2.0)          # 二阶 Butterworth
    hp = _biquad_coeffs("highpass", low, q, rate)
    lp = _biquad_coeffs("lowpass", high, q, rate)
    forward = _apply_biquad(values, hp)
    forward = _apply_biquad(forward, lp)
    # 反向再滤一遍：零相位（不会把波峰挪位置），代价是两端各有一次瞬态
    backward = _apply_biquad(list(reversed(forward)), lp)
    backward = _apply_biquad(backward, hp)
    return list(reversed(backward))


def _spectral_snr_score(snr: float) -> float:
    """把"频谱主峰 / 带内本底"换算成 0~1 的质量分。

    阈值由实测标定（`ERROR.md` **E52**，2026-09-29）：
    * 那份真机手指波形 SNR=**4.09**（频谱主峰 66 bpm 清楚可见）；
    * 干净合成波形 SNR=**35~51**；
    * **纯噪声 / 平坦 / 只有呼吸** SNR 都卡在 **2.12** 附近。
    所以 2.0 记 0 分、5.0 记满分：真机 ≈0.70、噪声 ≈0.04，分离得很干净。
    """
    return max(0.0, min(1.0, (float(snr) - PPG_SPEC_SNR_FLOOR) / (PPG_SPEC_SNR_FULL - PPG_SPEC_SNR_FLOOR)))


def _hr_from_spectrum(
    signal: Sequence[float], sample_rate: float, params: PpgParams
) -> Tuple[Optional[float], float]:
    """用**频谱主峰**估计心率（返回 ``(bpm 或 None, SNR)``）。

    为什么它是主估计器（2026-09-29 真机实测，`ERROR.md` **E52**）：
    在带通后的真机波形上，"数波峰取间隔中位数"给出 81.8 bpm、自相关给出 214 bpm，
    而**频谱主峰稳定给出 66.1 bpm**（换个窗口 65.7/66.5，抖动 <1 bpm），
    且与呼吸/漂移的强分量分得很开。实现要点：

    * 候选频率从 :data:`PPG_HR_SEARCH_MIN_BPM`（45）起 —— 0.7 Hz 高通以下的残留
      不该被当成"心率"，否则带边残留会把结果往低处带；
    * **加 Hann 窗**抑制频谱泄漏（不加窗时漂移的台阶会把整条谱抬起来）；
    * **谐波校验**：若"一半频率"处也有不低于主峰 80% 的能量，说明主峰是二次谐波，
      取半频为真值（否则一次心跳被数成两次）。

    Returns:
        ``(心率 bpm, SNR)``；SNR = 主峰幅度 / 带内本底均值。信号不可用时返回 ``(None, 0.0)``。
    """
    n = len(signal)
    if n < 8 or sample_rate <= 0:
        return None, 0.0
    bpm_lo = max(PPG_HR_SEARCH_MIN_BPM, params.hr_min_bpm)
    bpm_hi = params.hr_max_bpm
    if bpm_hi <= bpm_lo:
        return None, 0.0
    mean = sum(signal) / n
    window = [0.5 - 0.5 * math.cos(2.0 * math.pi * i / (n - 1)) for i in range(n)]
    x = [(float(signal[i]) - mean) * window[i] for i in range(n)]

    steps = max(40, int((bpm_hi - bpm_lo)))          # 每 1 bpm 一个候选，够细
    curve: List[Tuple[float, float]] = []
    for k in range(steps + 1):
        bpm = bpm_lo + (bpm_hi - bpm_lo) * k / steps
        omega = 2.0 * math.pi * (bpm / 60.0) / sample_rate
        re = 0.0
        im = 0.0
        for i in range(n):
            angle = omega * i
            re += x[i] * math.cos(angle)
            im += x[i] * math.sin(angle)
        curve.append((bpm, math.hypot(re, im) / n))
    peak_bpm, peak_mag = max(curve, key=lambda item: item[1])
    if peak_mag <= 0.0:
        return None, 0.0

    # 谐波校验：半频处若也有接近的能量，真周期是它的两倍
    half_bpm = peak_bpm / 2.0
    if half_bpm >= bpm_lo:
        half = [mag for bpm, mag in curve if abs(bpm - half_bpm) <= 4.0]
        if half and max(half) >= 0.8 * peak_mag:
            peak_bpm, peak_mag = half_bpm, max(half)

    floor_values = [mag for bpm, mag in curve if abs(bpm - peak_bpm) > 12.0]
    floor = (sum(floor_values) / len(floor_values)) if floor_values else 0.0
    snr = (peak_mag / floor) if floor > 0 else float("inf") if peak_mag > 0 else 0.0
    return peak_bpm, snr


def _hr_from_autocorr(
    signal: Sequence[float], sample_rate: float, params: PpgParams
) -> Tuple[Optional[float], float]:
    """用**归一化自相关**估计心率（返回 ``(bpm 或 None, 主峰相关度 0~1)``）。

    为什么它是主估计器（2026-09-29 真机实测 + 文献，见 `ERROR.md` **E52**）：
    "数波峰 + 取间隔中位数"在伪迹上很脆 —— 呼吸/漂移会让它锁到错的位置；
    而**自相关找的是"这段波形自己跟自己最像的周期"**，对非周期的噪声/伪迹天然不敏感，
    这正是 PPG 心率估计里最常用的稳健做法（HeartPy 一类工具也是这套思路）。

    两条防错：
    * **相关度门限**：主峰相关度低于 ``params.acf_min_score`` ⇒ 认为"没有可靠周期"，返回 None
      （宁可空着，也不给出编造的数值）；
    * **次谐波校验**：若 2 倍或 3 倍周期的相关度也不低于主峰的 90%，就取更长的周期 ——
      否则一次心跳会被数成两次（心率翻倍）。

    Returns:
        ``(心率 bpm, 主峰相关度)``；没有可靠周期时第一项为 ``None``。
    """
    n = len(signal)
    if n < 4 or sample_rate <= 0:
        return None, 0.0
    lag_lo = max(1, int(round(sample_rate * 60.0 / params.hr_max_bpm)))
    lag_hi = min(n // 2, int(round(sample_rate * 60.0 / params.hr_min_bpm)))
    if lag_hi <= lag_lo:
        return None, 0.0
    mean = sum(signal) / n
    x = [float(v) - mean for v in signal]
    scores: Dict[int, float] = {}
    for lag in range(lag_lo, lag_hi + 1):
        num = 0.0
        for i in range(n - lag):
            num += x[i] * x[i + lag]
        left = sum(v * v for v in x[: n - lag])
        right = sum(v * v for v in x[lag:])
        den = math.sqrt(left * right)
        scores[lag] = (num / den) if den > 0 else 0.0
    best_lag = max(scores, key=lambda lag: scores[lag])
    best = scores[best_lag]
    if best < params.acf_min_score:
        return None, max(0.0, best)
    # 次谐波校验：真正的周期可能是它的整数倍（否则一次心跳数成两次）
    for multiple in (2, 3):
        longer = best_lag * multiple
        if longer <= lag_hi and scores.get(longer, -1.0) >= best * 0.9:
            best_lag, best = longer, scores[longer]
            break
    return 60.0 * sample_rate / best_lag, best


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
    2. **带通 0.7~4 Hz**：二阶 Butterworth（前向-反向各滤一遍，零相位）。
       ⚠️ 必须切掉 0.15~0.5 Hz 的**呼吸引起的基线波动**（它常比脉搏波还大）。
    3. **心率 = 归一化自相关主峰**（含次谐波校验，防一次心跳数成两次）；
       若自相关给不出可靠周期（相关度低于 ``acf_min_score``），
       退回**峰值间隔中位数**（自适应阈值 + 抛物线亚样本细化）。
    4. **质量分**：节律一致性 × 信号强度 × 波数充分性 × 自相关主峰相关度。
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
    # 波峰位置细化到亚样本精度后再算间隔（降低量化抖动对心率的污染）
    refined = [_refine_peak(ac, idx) for idx in peaks]
    intervals = [(refined[i + 1] - refined[i]) / sample_rate for i in range(len(refined) - 1)]
    valid = [dt for dt in intervals if p.peak_min_s <= dt <= p.peak_max_s]

    # ---- 心率：**频谱主峰 → 自相关 → 峰值间隔**（2026-09-29 重排，见 ERROR.md E52）----
    # 三条路的实测表现（同一份真机波形，真实心率 ~66 bpm）：
    #   频谱主峰 66.1（稳） / 自相关 214（被瞬态污染） / 峰值间隔 81.8（伪迹带偏）。
    # 所以频谱为主；另两条留作兜底，并把"用了哪条路"记进 source 便于排查。
    spec_hr, spec_snr = _hr_from_spectrum(ac, sample_rate, p)
    acf_hr, acf_score = _hr_from_autocorr(ac, sample_rate, p)
    peak_hr = (60.0 / _median(valid)) if valid else None

    if spec_hr is not None and spec_snr >= PPG_SPEC_SNR_FLOOR:
        hr, source, periodicity = spec_hr, "频谱主峰", _spectral_snr_score(spec_snr)
    elif acf_hr is not None:
        hr, source, periodicity = acf_hr, "自相关", max(0.0, min(1.0, acf_score))
    elif peak_hr is not None and len(peaks) >= p.min_beats:
        hr, source, periodicity = peak_hr, "峰值间隔", 0.5   # 兜底：质量按中等计
    else:
        return PpgAnalysis(
            finger_detected=True, samples=n, dc=dc, beats=len(peaks),
            acf_score=acf_score,
            reason=(
                f"没有找到可靠的心率周期（频谱 SNR {spec_snr:.2f} 偏低，"
                f"自相关相关度 {acf_score:.2f}，只检出 {len(peaks)} 个脉搏波）"
            ),
        )

    if not (p.hr_min_bpm <= hr <= p.hr_max_bpm):
        return PpgAnalysis(
            finger_detected=True, samples=n, dc=dc, beats=len(peaks), acf_score=acf_score,
            reason=f"心率 {hr:.1f} bpm 不在 {p.hr_min_bpm:.0f}~{p.hr_max_bpm:.0f} 区间内",
        )

    period = 60.0 / hr
    quality = _quality(level, periodicity)

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
        source=source,
        acf_score=round(acf_score, 3),
    )


def _quality(level: float, periodicity: float) -> float:
    """给出 0.0~1.0 的数据质量评分（给业务层丢弃坏点用）。

    两个因子相乘：

    * **信号强度** ``level``：交流有效值，50 计数以上记满分（太弱说明手指没贴实）；
    * **周期性/纯净度** ``periodicity``：频谱主峰的 SNR 分（或自相关/兜底路径的对应分），
      越高说明这段波形越像"有规律地在跳"，而不是噪声凑出来的一个峰。

    ⚠️ **为什么不再把"波峰间隔一致性"和"波数"算进去**（2026-09-29 改，`ERROR.md` **E52**）：
    实测那份**可用**的真机波形里，波峰间隔一致性是 **0.00**、ACF 相关度只有 0.19，
    而频谱主峰 SNR 是 4.09（66 bpm 清清楚楚）。把前两者算进乘积会把好数据一票否决
    （质量分 0.00），而它们在**纯噪声**上反倒不低（ACF 0.37）—— 两边的判别力都是反的。
    判据只能用**数据选出来的**那个（SNR），这也正是本项目反复强调的"判据要有区分力"。
    """
    strength = max(0.0, min(1.0, float(level) / 50.0))
    return max(0.0, min(1.0, strength * max(0.0, min(1.0, float(periodicity)))))


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
        self.sample_rate = int(rate_key)             # **配置给器件的 ADC 采样率**（写进 SPO2_SR）
        # 分析真正该用的时间基数：FIFO 的实际产出速率。
        # ⚠️ 4 点平均会让器件每秒只推出 sample_rate/4 组（实测，见 FIFO_SAMPLE_AVG 注释），
        # 直接用 sample_rate 当时间基数会把心率算成 4 倍（ERROR.md E51）。
        self.analysis_rate = self.sample_rate / float(FIFO_SAMPLE_AVG)

        # 运行期状态
        self._ir_buf: List[int] = []                 # 红外采样环形缓冲（分析窗）
        self._red_buf: List[int] = []                # 红光采样环形缓冲（与上面一一对应）
        self._window_samples = int(round(self.analysis_rate * 10))  # 分析窗长：10 秒（按 FIFO 速率）
        self._mock_index = 0                         # mock 波形相位（样本序号）
        self._mock_ready = False                     # mock 波形是否已初始化
        self._mock_fault: Optional[BaseException] = None  # 测试注入的故障
        self._ovf_total = 0                          # FIFO 溢出累计（**读太慢的凭据**）
        # PART_ID 命中在哪一处布局（0x21 / 0xFF），以及读到的 REV_ID（留痕用）
        self._part_id_reg: Optional[int] = None
        self._rev_id: Optional[int] = None

    # ------------------------------------------------------------------
    # 器件识别（两种已知布局）
    # ------------------------------------------------------------------

    def read_part_id(self) -> Tuple[Optional[int], int, int]:
        """按**两种已知布局**读 PART_ID，返回 ``(命中的寄存器, 0x21 的读回值, 0xFF 的读回值)``。

        - 命中（读到 ``0x15``）时第一项是该寄存器地址，**哪一处命中就说明是哪种布局**；
        - 两处都不是 ``0x15`` 时第一项为 ``None``，但仍然把两处的读回值一并带回来，
          好让报错/自检能一次说清"两个地址分别读到了什么"（而不是只甩一个 0x00）；
        - 读失败（器件没应答）记 ``-1``，不在这里抛异常 —— 交由调用方决定怎么报。

        ⚠️ **为什么不只查 0x21**：见本文件头部与 `ERROR.md` **E49** ——
        MAX30105 布局的 ``0x21`` 是 ``TEMP_CONFIG``，读回 0x00，只查它会把可用器件误判成坏件。
        """
        values: Dict[int, int] = {}
        for reg in PART_ID_REGISTERS:
            try:
                values[reg] = self._read_reg(reg)
            except (DeviceIOError, DeviceTimeout):
                values[reg] = -1
        hit = next((r for r in PART_ID_REGISTERS if values[r] == MAX30102_PART_ID), None)
        return hit, values[PART_ID_REGISTERS[0]], values[PART_ID_REGISTERS[1]]

    @staticmethod
    def part_id_layout(reg: Optional[int]) -> str:
        """把命中的寄存器地址翻成"人话"的布局名（给日志与自检报告用）。"""
        if reg == 0x21:
            return "MAX30102/MAX30101 布局（PART_ID@0x21）"
        if reg == 0xFF:
            return "MAX30105 布局（PART_ID@0xFF，0x21 是 TEMP_CONFIG）"
        return "未识别布局"

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

        ⚠️ **先写一次 shutdown(0x80) 再复位**（2026-09-29，`ERROR.md` **E51 补记**）：
        实测观察到"器件被留在 shutdown / multi-LED 模式后，**只写一次 RESET 不一定恢复采样**
        （寄存器都读回正常、`MODE_CONFIG=0x03` 也在，但 `FIFO_WR_PTR` 一直不动）"；
        而"先 `0x09=0x80` 再 `0x09=0x40`"能稳定恢复。⚠️ 机制**未完全隔离**（属实测现象 + 推断），
        代价只是多一次寄存器写，所以照做以求稳。
        """
        import time

        try:
            self._write_reg(REG_MODE_CONFIG, MODE_SHUTDOWN)
            time.sleep(0.05)
        except (DeviceIOError, DeviceTimeout):  # 器件本来就没开着，继续走复位
            pass
        self._write_reg(REG_MODE_CONFIG, MODE_RESET)
        for _ in range(20):
            time.sleep(0.01)
            if not (self._read_reg(REG_MODE_CONFIG) & MODE_RESET):
                return
        raise DeviceTimeout(
            "MAX30102 复位位 200ms 内未清零（供电不足或器件未正常应答，检查 3.3V 供电）"
        )

    def _init_sequence(self) -> None:
        """按数据手册顺序完成初始化，并记录读回值以便留痕。

        ⚠️ **PART_ID 查两处**（0x21 与 0xFF）：MAX3010x 系器件存在两套已知布局，
        只查 0x21 会把 MAX30105 布局的可用模块误判成坏件（见 `ERROR.md` **E49**）。
        **判据没有放宽**：仍然必须真的读到 ``0x15`` 才放行，只是知道它可能在哪一处。
        """
        hit, value_21, value_ff = self.read_part_id()
        if hit is None:
            raise DeviceInitError(
                f"MAX3010x 版本号寄存器读回不对：0x21=0x{value_21:02X}、0xFF=0x{value_ff:02X}，"
                f"两处应有一处为 0x{MAX30102_PART_ID:02X}"
                f"（0x21=MAX30102/MAX30101 布局，0xFF=MAX30105 布局）："
                "可能地址不对（LCD1602 在 0x27/0x3F）、器件没焊好，或线太长导致 I2C 误码"
            )
        self._part_id_reg = hit
        try:
            self._rev_id = self._read_reg(REG_REV_ID)
        except (DeviceIOError, DeviceTimeout):  # 留痕失败不该拦住开机
            self._rev_id = None

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
        """读一个寄存器（写寄存器地址后读 1 字节）。

        ⚠️ **寄存器地址是 8 位**，所以这里用 ``& 0xFF`` 而不是 ``& 0x7F``：
        旧实现写成 ``& 0x7F``（把 7 位**从机地址**的掩码误用到了寄存器地址上），
        导致 ``0xFF`` 被当成 ``0x7F`` 发出去 —— 支持 MAX30105 布局的 ``0xFF``
        之前必须先修掉它（见 `ERROR.md` **E49**）。
        """
        data = self._bus.i2c_write_read(self.i2c_bus, self.address, bytes([reg & 0xFF]), 1)
        return int(data[0])

    def _write_reg(self, reg: int, value: int) -> None:
        """写一个寄存器（同样：寄存器地址是 8 位，不能被截成 7 位）。"""
        self._bus.i2c_write(self.i2c_bus, self.address, bytes([reg & 0xFF, value & 0xFF]))

    def _read_fifo(self, count: int) -> List[Tuple[int, int]]:
        """从 FIFO_DATA(0x07) 连读 ``count`` 组样本 → ``[(红外, 红光), ...]``。

        ⚠️ **每组样本是 6 字节，不是 3 字节**：MAX30102 工作在 SpO2 模式时，
        每个采样点会依次把**红光 3 字节 + 红外 3 字节**推进 FIFO
        （数据手册 "FIFO Data (0x07)" 一节的时序：SLOT1 先、SLOT2 后）。
        每组 3 字节都是 18 位有效（高 6 位是 0），拼装时要先左移再掩码 0x03FFFF。

        ⚠️⚠️ **必须分块读**（2026-09-29 真机实测，`ERROR.md` **E50**）：
        `smbus2` 的一次块读**最多 32 字节**（SMBus 规范），超了直接抛
        ``ValueError: Desired block length over 32 bytes``（**不是** OSError，
        所以以前连"翻译成人话"的机会都没有）。而实测这颗器件**每秒往 FIFO 推约 25 组**
        （= 150 字节），一次读不完 ⇒ 旧实现**每次 read() 都在第三步崩**，
        手指贴上去也永远算不出心率。修法：按 5 组（30 字节）一块分批读完。
        """
        out: List[Tuple[int, int]] = []
        remaining = max(0, int(count))
        while remaining > 0:
            chunk = min(remaining, FIFO_SAMPLES_PER_READ)
            size = 6 * chunk
            raw = self._bus.i2c_write_read(
                self.i2c_bus, self.address, bytes([REG_FIFO_DATA]), size
            )
            if len(raw) != size:
                raise DeviceIOError(
                    f"MAX30102 FIFO 期望 {size} 字节（{chunk} 组×6 字节：红光+红外），"
                    f"实收 {len(raw)} 字节（I2C 误码或器件掉线）"
                )
            for i in range(chunk):
                base = 6 * i
                red = _to_18bit(raw[base:base + 3])
                ir = _to_18bit(raw[base + 3:base + 6])
                out.append((ir, red))
            remaining -= chunk
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
        # 每次 read() 推进 0.5 秒的样本（相当于轮询周期 0.5s、器件在 FIFO 里攒了若干组），
        # 这样业务层每秒轮询两次也能在几秒内攒满 10 秒分析窗。
        # ⚠️ 用 **analysis_rate**（FIFO 的实际产出速率）而不是配置里的 sample_rate：
        #    合成波形的"每秒样本数"必须与真机一致，否则 mock 出的 60 bpm 会对不上
        #    （ERROR.md E51；这也正是 mock 全绿而真机算不对的原因之一）。
        step = max(1, int(self.analysis_rate // 2))
        for _ in range(step):
            t = self._mock_index / float(self.analysis_rate)
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
            self._ir_buf, self.analysis_rate, self._red_buf, self._params
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

    def raw_window(self) -> Tuple[List[int], List[int]]:
        """**只读诊断**：返回当前分析窗的原始采样副本 ``(红外, 红光)``。

        为什么要开这个口子（2026-09-29 T7 真机）：现场只看到"质量分 0.00"，
        但**质量分是三个因子相乘**，光看结论分不清是"信号太弱/ADC 饱和"还是"节律乱"
        —— 必须拿原始波形说话（判据要有区分力）。返回的是副本，改它不影响驱动状态。
        """
        return list(self._ir_buf), list(self._red_buf)

    def window_stats(self) -> Dict[str, Any]:
        """**只读诊断**：当前分析窗的统计量（样本数 / 直流 / 交流均方根）。

        ``ir_dc`` 接近满量程（18 位 = 262143）⇒ **ADC 饱和**，要降 LED 电流或放大 ADC 量程；
        ``ir_ac_rms`` 很小（个位数）⇒ **信号太弱**（手指没贴实 / 电流太低 / 漏光）。
        这两条是"波形质量差"的两种**不同**原因，必须能分开（`ERROR.md` E52）。
        """
        ir = [float(v) for v in self._ir_buf]
        red = [float(v) for v in self._red_buf]
        if not ir:
            return {"samples": 0, "ir_dc": None, "ir_ac_rms": None,
                    "red_dc": None, "red_ac_rms": None}
        ac = _bandpass(ir, self.analysis_rate, self._params)
        return {
            "samples": len(ir),
            "ir_dc": sum(ir) / len(ir),
            "ir_ac_rms": _rms(ac),
            "ir_min": min(ir),
            "ir_max": max(ir),
            "red_dc": (sum(red) / len(red)) if red else None,
            "red_ac_rms": _rms(_bandpass(red, self.analysis_rate, self._params)) if red else None,
        }

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
                f"LED 电流 {self.led_current}；采样率 {self.sample_rate}Hz"
                f"（4 点平均 ⇒ FIFO 实际 {self.analysis_rate:g} 组/秒，算法按它计时）；"
                f"分析窗 {self._window_samples / self.analysis_rate:.0f}s；"
                "算法=M 点平均去直流 + 自适应阈值峰值间隔 + 抛物线亚样本细化；"
                "与 LCD1602(0x27/0x3F) 可共用 I2C-1"
                + (
                    f"；PART_ID 命中 {self.part_id_layout(self._part_id_reg)}"
                    + (f"，REV_ID=0x{self._rev_id:02X}" if self._rev_id is not None else "")
                    if self._part_id_reg is not None
                    else ""
                )
                + ("；mock 自动合成波形（可用 inject 覆盖）" if self.mock else "")
            ),
        }

    def self_check(self) -> Dict[str, Any]:
        """自检：mock 模式做一次合成读数；真实模式按两种布局读 PART_ID（应为 0x15）。

        ⚠️ mock 模式下心率需要攒够 5 秒分析窗才有结论，所以这里会连续合成
        若干轮（**只是内存里的波形，不碰任何硬件**），让体检报告能给出具体数值。

        ⚠️ 真实模式**两处都读**（0x21 与 0xFF），报告里写明命中哪一种布局 ——
        只报一个 0x00 会让"可用器件"看起来像坏件（`ERROR.md` **E49**）。
        """
        detail: Dict[str, Any] = {"name": self.name, "mock": self.mock}
        was_open = self._opened
        try:
            if not was_open:
                self.open()
            if self.mock:
                # 攒够分析窗：每次 _pull_mock_samples() 推进 0.5 秒
                per_call = max(1, int(self.analysis_rate // 2))
                for _ in range(int(self._window_samples / per_call) + 1):
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
            hit, value_21, value_ff = self.read_part_id()
        except Exception as exc:  # noqa: BLE001 - 自检要捕获一切并如实上报
            return {"ok": False, "detail": f"{type(exc).__name__}: {exc}", **detail}
        finally:
            if not was_open:
                self.close()
        readings = f"0x21=0x{value_21:02X}、0xFF=0x{value_ff:02X}"
        if hit is None:
            return {
                "ok": False,
                "detail": f"PART_ID 两处都不是 0x{MAX30102_PART_ID:02X}（{readings}）",
                **detail,
            }
        return {
            "ok": True,
            "detail": (
                f"PART_ID=0x{MAX30102_PART_ID:02X} 命中 {self.part_id_layout(hit)}"
                f"（{readings}）；REV_ID="
                + (f"0x{self._rev_id:02X}" if self._rev_id is not None else "读不到")
            ),
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
    "PART_ID_REGISTERS",
    "REG_PART_ID",
    "REG_PART_ID_ALT",
    "REG_REV_ID",
    "MAX_I2C_BLOCK_BYTES",
    "FIFO_SAMPLES_PER_READ",
    "FIFO_SAMPLE_AVG",
]
