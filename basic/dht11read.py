#!/usr/bin/env python3
"""基础版自带的一小块 DHT11 读取代码（**自包含，不 import 主项目**）。

为什么要在这里再写一份（不是复制粘贴凑数）
------------------------------------------
基础版（`basic/`）是给"只做大作业"的场景准备的：**一个文件夹、三条命令就能跑**，
所以它不 import 主项目那套 HAL（`rpi/health_monitor/`），否则"基础版"就要跟着
整套项目一起搬。代价是这里要自带一段读取代码，好在 DHT11 的单总线并不长。

三种后端（按顺序尝试，谁在就用谁）
----------------------------------
1. ``lgpio``（**树莓派上推荐**）：用内核级**边沿时间戳**，纳秒精度。原因见下；
2. ``gpiozero``：只有 **gpiozero < 2.0** 才有 ``DHT11`` 类。
   ⚠️ Debian 13（trixie）的 ``python3-gpiozero 2.0.1`` **已经删掉了 DHT11/DHT22**
   （源码里没有、apt 里也没有任何 dht 包），所以这条路径在较新的系统上通常不可用；
3. ``mock``：完全不碰硬件，用缓慢波动的合成数据，**没有树莓派也能把程序跑通**。

为什么坚持用"边沿时间戳"而不是在 Python 里紧循环读电平
----------------------------------------------------
DHT11 用**高电平的宽度**表示 bit：约 26 µs = 0，约 70 µs = 1。
Python 紧循环的抖动常常就大于 26 µs，会大量误码；而 ``lgpio`` 的回调自带时间戳，
两个边沿相减就能得到稳定的宽度。判据：**能在硬件侧拿到时间戳，就不要在 Python 侧数时间**。

协议速览（解码代码就是照这个写的）
----------------------------------
主机拉低 ≥18 ms → 释放总线 → 传感器先应答（低 80 µs + 高 80 µs）
→ 40 个 bit（每 bit = 低 50 µs + 高 26 µs 表示 0 / 高 70 µs 表示 1）
→ 收尾低电平。40 bit = 湿度整数 + 湿度小数 + 温度整数 + 温度小数 + 校验和。

⚠️ 两个已经踩过的坑（写这份代码前先看）
--------------------------------------
1. **应答脉冲不能靠宽度阈值过滤**：应答的高电平约 80 µs，而数据位"1"约 70 µs，几乎同宽。
   正确做法是**按位置丢掉第一个高电平段**（应答恒在最前），见 :func:`decode_frame`。
2. **一帧的边沿数要对上**：回调是在"主机释放总线之后"才注册的，所以看到的是
   应答高 + 40×(低+高) + 收尾低 = **82 个边沿**（41 个高电平段 = 应答 + 40 个数据位）；
   少算一对就只解出 39 bit（表现为"校验和不符"，很难查）。
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

_LOG = logging.getLogger(__name__)

# ==========================================================================
# 一、协议常量与纯解码（**不碰硬件，可直接用合成时序单测**）
# ==========================================================================

#: 主机起始信号：拉低至少 18 ms（数据手册要求，这里留到 20 ms 更稳）
START_LOW_S = 0.020

#: 等待读取一帧的时间：应答 + 40 bit ≈ 4 ms，给足 40 ms 余量
FRAME_WAIT_S = 0.040

#: 高电平宽度判 0 / 1 的分界（µs）：26 µs 左右是 0，70 µs 左右是 1
BIT_THRESHOLD_US = 50.0

#: 宽度上限（µs）：超过它说明采样里混入了中断延迟/噪声，宁可报错也不解出脏数据
MAX_BIT_WIDTH_US = 200.0

#: 一帧最少要有多少个边沿才可能解出 40 bit（82 为实测值，留 ~4% 余量）
MIN_EDGES = 79

#: DHT11 的量程（数据手册）：湿度 20~90 %RH、温度 0~50 ℃
TEMP_MIN_C, TEMP_MAX_C = 0.0, 50.0
HUMI_MIN_PCT, HUMI_MAX_PCT = 20.0, 90.0

#: 同一传感器两次读取的最小间隔（秒）。DHT11 要求 ≥1 s，实测 2 s 才稳，
#: 间隔不够时会**返回上一次的陈旧数据**——基础版直接等够时间，不拿旧值糊弄。
MIN_INTERVAL_S = 2.0

#: 间隔门禁的容差比例：**只在"设定间隔贴着硬件下限"时才生效**（见 `interval_tolerance`）。
#: ⚠️ 2026-09-25 真机实测的完整结论（ERROR.md E38）：`run.py` 曾经把**采样周期**当成
#: 门禁下限传进来（`min_interval_s=max(args.interval, 2.0)`），于是"3 秒采样 + 读/存/画
#: 花掉 60ms"⇒ 真实间隔 2.94 秒 < 3.0 秒 ⇒ **隔一次被拒**（4 次里失败 2 次）。
#: 现在改成：**门禁 = 硬件下限 2 秒，节奏 = 主循环的采样周期 3 秒**，两者不再互相打架。
INTERVAL_TOLERANCE_FRACTION = 0.15

#: 一次读取内部重试之间的等待（秒）。DHT11 转换一次约 1 s，等太短会连续失败。
RETRY_DELAY_S = 0.2


class Dht11Error(RuntimeError):
    """读取失败（无应答 / 时序异常 / 校验和不符 / 数值超量程）。"""


def decode_frame(edges: Sequence[Tuple[int, int]]) -> Tuple[float, float]:
    """把一串"边沿（电平, 纳秒时间戳）"解码成 ``(温度℃, 湿度%)``。

    **纯函数**：不碰硬件、不读时钟，所以可以喂**合成时序**做单测
    （合成报文 → 解码 → 断言原文，是协议类代码最快的查错方式）。

    Args:
        edges: 边沿序列，从**传感器应答的上升沿**开始（真机上 lgpio 回调就是这样看到的；
               也可以在开头多带一个"主机拉低"的下降沿，解码会自行跳过非数据段）。

    Returns:
        ``(温度℃, 湿度%)``。

    Raises:
        Dht11Error: 边沿太少（器件没应答）/ 没有应答脉冲 / 数据位不足 / 校验和不符。
    """
    if len(edges) < MIN_EDGES:
        raise Dht11Error(
            f"只捕获到 {len(edges)} 个边沿（完整一帧约 83 个）：**传感器没有应答**。"
            "先查：① 供电是不是 3.3 V；② 数据线是不是 GPIO4（物理脚 7）；"
            "③ 裸四针传感器有没有接 4.7kΩ~10kΩ 上拉电阻；④ 杜邦线是否过长/接触不良"
        )

    # ① 先按协议结构切出"高电平段的宽度"。
    #    相邻的 (高, 低) 才构成一个完整的高电平段；起点按位置找，不按宽度猜。
    widths_us: List[float] = []
    index = 0
    while index + 1 < len(edges):
        level, start_ns = edges[index]
        next_level, end_ns = edges[index + 1]
        if level == 1 and next_level == 0:
            widths_us.append((end_ns - start_ns) / 1000.0)
            index += 2
        else:
            index += 1

    if not widths_us:
        raise Dht11Error("捕获到的边沿里没有任何完整的高电平段（多为缺上拉电阻或时序抖动）")

    # ② ★ 丢掉**第一个**高电平段：它是传感器的应答脉冲（约 80 µs），
    #    宽度和数据位"1"几乎一样，**只能按位置丢**。
    data_widths = [w for w in widths_us[1:] if 0.0 < w < MAX_BIT_WIDTH_US]
    if len(data_widths) < 40:
        raise Dht11Error(
            f"数据位不足：只解出 {len(data_widths)}/40 个 bit（多为上拉缺失或时序抖动，可多试几次）"
        )

    bits = [1 if width > BIT_THRESHOLD_US else 0 for width in data_widths[:40]]

    # ③ 40 bit → 5 字节（湿度整数、湿度小数、温度整数、温度小数、校验和）
    data = bytearray()
    for byte_index in range(5):
        value = 0
        for bit in bits[byte_index * 8:(byte_index + 1) * 8]:
            value = (value << 1) | bit
        data.append(value)

    checksum = sum(data[:4]) & 0xFF
    if checksum != data[4]:
        raise Dht11Error(
            f"校验和不符：算出 0x{checksum:02X}，收到 0x{data[4]:02X}"
            "（时序抖动导致误码；查上拉电阻与线长，并确保读取间隔 ≥2 秒）"
        )

    humidity = float(data[0]) + float(data[1]) / 10.0
    temperature = float(data[2]) + float(data[3]) / 10.0
    return temperature, humidity


def check_range(temperature_c: float, humidity_percent: float) -> Optional[str]:
    """量程校验：通过返回 ``None``，不通过返回中文原因（**纯函数**）。"""
    if humidity_percent < HUMI_MIN_PCT or humidity_percent > HUMI_MAX_PCT:
        return (
            f"湿度 {humidity_percent:.1f}% 超出 DHT11 量程 "
            f"{HUMI_MIN_PCT:.0f}~{HUMI_MAX_PCT:.0f}%RH"
        )
    if temperature_c < TEMP_MIN_C or temperature_c > TEMP_MAX_C:
        return (
            f"温度 {temperature_c:.1f}℃ 超出 DHT11 量程 "
            f"{TEMP_MIN_C:.0f}~{TEMP_MAX_C:.0f}℃"
        )
    return None


def synth_frame(temperature_c: float, humidity_percent: float) -> List[Tuple[int, int]]:
    """按协议**合成**一帧边沿序列（单测/自查用，不碰硬件）。

    ⚠️ 合成的形状必须与**真机上 lgpio 回调看到的**一模一样，否则测试就是自欺：
    回调是在主机释放总线**之后**才注册的，所以捕获不到"主机拉低"那个下降沿，
    序列从**应答的上升沿**开始，形状是：

        高（应答 80µs）→ 低 → [每 bit：高（26/70µs）→ 低] × 40

    即 **82 个边沿**（41 个高电平段 = 应答 + 40 位；文末不做"人为闭合"，
    与真机一致：最后一个下降沿之后不再有边沿）。

    Args:
        temperature_c: 想让它"读出来"的温度（整数或一位小数）。
        humidity_percent: 想让它"读出来"的湿度。

    Returns:
        ``[(电平, 纳秒时间戳), ...]``，可直接喂给 :func:`decode_frame`。
    """
    humidity_int = int(humidity_percent)
    humidity_dec = int(round((humidity_percent - humidity_int) * 10))
    temperature_int = int(temperature_c)
    temperature_dec = int(round((temperature_c - temperature_int) * 10))
    payload = [
        humidity_int, humidity_dec, temperature_int, temperature_dec,
        (humidity_int + humidity_dec + temperature_int + temperature_dec) & 0xFF,
    ]
    bits: List[int] = []
    for byte in payload:
        for shift in range(7, -1, -1):
            bits.append((byte >> shift) & 1)

    edges: List[Tuple[int, int]] = []
    t = 0

    def edge(level: int, duration_us: float) -> None:
        """记一个边沿（电平 level），再前进 duration_us。"""
        nonlocal t
        edges.append((level, t))
        t += int(duration_us * 1000)

    edge(1, 80)          # ① 传感器应答：高 80 µs（回调从这里开始看到）
    edge(0, 50)          # ② 应答结束 → 进入 40 bit
    for bit in bits:
        edge(1, 70 if bit else 26)   # 每个 bit 的高电平宽度决定 0/1
        edge(0, 50)                  # bit 之间固定的低电平
    return edges


# ==========================================================================
# 二、三种后端（真硬件两种 + mock 一种）
# ==========================================================================


class LgpioBackend:
    """用 ``lgpio`` 的边沿时间戳读 DHT11（树莓派上推荐）。"""

    name = "lgpio"

    def __init__(self, pin: int) -> None:
        import lgpio  # 延迟导入：PC 上没装也能 import 本模块

        self._lgpio = lgpio
        # gpiochip_open(0) 会让 lgpio 自己选中正确的 gpiochip
        # （树莓派 5 的 40-pin 在 gpiochip4，写死反而出错）
        self._handle = lgpio.gpiochip_open(0)
        self._pin = int(pin)

    def read_once(self) -> Tuple[float, float]:
        lg, handle, pin = self._lgpio, self._handle, self._pin
        edges: List[Tuple[int, int]] = []
        callback = None
        try:
            # ① 主机拉低 20 ms 作为起始信号
            lg.gpio_claim_output(handle, pin, 0)
            time.sleep(START_LOW_S)
            # ② 释放总线：改成带内部上拉的输入，并注册**双边沿**回调拿时间戳
            lg.gpio_free(handle, pin)
            lg.gpio_claim_alert(handle, pin, lg.BOTH_EDGES, lg.SET_PULL_UP)
            callback = lg.callback(
                handle, pin, lg.BOTH_EDGES,
                lambda chip, gpio, level, timestamp: edges.append((level, timestamp)),
            )
            # ③ 等一帧走完（应答 + 40 bit ≈ 4 ms，给到 40 ms）
            time.sleep(FRAME_WAIT_S)
        finally:
            if callback is not None:
                try:
                    callback.cancel()
                except Exception:  # noqa: BLE001 - 收尾失败不影响已采到的边沿
                    pass
            try:
                lg.gpio_free(handle, pin)
            except Exception:  # noqa: BLE001
                pass
        return decode_frame(edges)

    def close(self) -> None:
        try:
            self._lgpio.gpiochip_close(self._handle)
        except Exception:  # noqa: BLE001
            pass


class GpiozeroBackend:
    """用 ``gpiozero`` 的 ``DHT11`` 类读（**只有 gpiozero < 2.0 才有**）。"""

    name = "gpiozero"

    def __init__(self, pin: int) -> None:
        from gpiozero import DHT11 as GpioDHT11  # 新版本没有这个类 → ImportError

        self._sensor = GpioDHT11(int(pin), max_temperature=TEMP_MAX_C)

    def read_once(self) -> Tuple[float, float]:
        return float(self._sensor.temperature), float(self._sensor.humidity)

    def close(self) -> None:
        try:
            self._sensor.close()
        except Exception:  # noqa: BLE001
            pass


class MockBackend:
    """不碰硬件的合成数据源（**没有树莓派也能把作业跑通/演示**）。

    数据做法：两个不同周期的正弦叠加（120 s 与 47 s），温度 24~27 ℃、湿度 45~57 %，
    既在 DHT11 量程内，又不会是一条直线（曲线好看，也更像真实环境）。
    用 ``seed`` 固定随机相位，测试里可复现。
    """

    name = "mock"

    def __init__(
        self,
        explicit: Optional[Tuple[float, float]] = None,
        seed: int = 20260924,
        clock=time.monotonic,
    ) -> None:
        self._clock = clock
        self._started = float(clock())
        self._explicit = explicit
        self._phase = random.Random(seed).uniform(0, 6.28)

    def read_once(self) -> Tuple[float, float]:
        import math

        if self._explicit is not None:
            return self._explicit
        elapsed = float(self._clock()) - self._started
        temperature = 25.5 + 1.5 * math.sin(2 * math.pi * elapsed / 120.0 + self._phase)
        humidity = 51.0 + 6.0 * math.sin(2 * math.pi * elapsed / 47.0 + 1.1 + self._phase)
        return round(temperature, 1), round(humidity, 1)

    def close(self) -> None:
        return None


# ==========================================================================
# 三、对外的读取器（带 2 秒间隔与重试）
# ==========================================================================


@dataclass
class ReadResult:
    """一次读取的结果（成功/失败都如实描述，**失败时不编造数值**）。"""

    ok: bool
    temperature_c: Optional[float] = None
    humidity_percent: Optional[float] = None
    note: str = ""
    cached: bool = False     # 是否为"间隔不足"返回的缓存值（基础版默认不用缓存，留作说明）

    def summary(self) -> str:
        if self.ok:
            return f"温度: {self.temperature_c:.1f} ℃，湿度: {self.humidity_percent:.0f} %"
        return f"读取失败：{self.note}"


class Dht11Reader:
    """DHT11 读取器：负责选后端、控间隔、重试，把结果如实包成 :class:`ReadResult`。

    Args:
        pin: 数据脚 BCM 编号（默认 4 = 物理脚 7）。
        mock: 强制使用合成数据（不碰任何 GPIO）。
        retries: 单次读取内的重试次数（DHT11 偶发校验失败很常见，默认 3）。
        min_interval_s: 两次读取的最小间隔（默认 2.0 s，DHT11 硬件限制）。
        explicit_mock: 指定 mock 读到的固定值（测试/演示用）。
        sleep: 可注入的 sleep 函数（测试时换成"不睡"）。
        clock: 可注入的时钟（测试时用假时钟让"2 秒间隔"瞬时验证）。
    """

    def __init__(
        self,
        pin: int = 4,
        mock: bool = False,
        retries: int = 3,
        min_interval_s: float = MIN_INTERVAL_S,
        explicit_mock: Optional[Tuple[float, float]] = None,
        sleep=time.sleep,
        clock=time.monotonic,
    ) -> None:
        if not 0 <= int(pin) <= 27:
            raise ValueError(f"DHT11 数据脚 GPIO{int(pin)} 非法：BCM 编号应在 0~27")
        if float(min_interval_s) < 1.0:
            raise ValueError(
                f"读取间隔 {min_interval_s}s 太小：DHT11 要求 ≥1 s（实测 ≥2 s 才稳），"
                "间隔不足会读到上一次的陈旧数据"
            )
        self.pin = int(pin)
        self.mock = bool(mock)
        self.retries = max(1, int(retries))
        self.min_interval_s = float(min_interval_s)
        self._sleep = sleep
        self._clock = clock
        self._last_read_at: Optional[float] = None
        self._backend: Optional[object] = None
        self._explicit_mock = explicit_mock
        self.consecutive_failures = 0
        self.read_count = 0

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    @property
    def backend_name(self) -> str:
        """当前用的后端名（``lgpio`` / ``gpiozero`` / ``mock`` / ``none``）。"""
        return getattr(self._backend, "name", "none")

    def open(self) -> str:
        """选后端并打开；返回后端名。

        顺序（每一步失败都往下退，并把原因记进日志——**不静默降级**）：

        1. ``--mock`` 或显式指定了 mock 值 → 合成数据；
        2. ``lgpio`` 可用 → 用它（树莓派上最稳）；
        3. ``gpiozero`` 且有 ``DHT11`` 类 → 用它（旧系统）；
        4. 都不可用 → 抛 :class:`Dht11Error`，并在信息里给出**可执行的排查命令**。
        """
        if self._backend is not None:
            return self.backend_name
        if self.mock or self._explicit_mock is not None:
            # 把可注入的时钟一并交给 mock 后端：这样单测里能让"时间真的流逝 10 秒"
            self._backend = MockBackend(self._explicit_mock, clock=self._clock)
            return self.backend_name

        errors: List[str] = []
        for factory in (LgpioBackend, GpiozeroBackend):
            try:
                self._backend = factory(self.pin)
                _LOG.info("DHT11 使用 %s 后端（GPIO%d）", self.backend_name, self.pin)
                return self.backend_name
            except Exception as exc:  # noqa: BLE001 - 后端不可用是预期情况
                errors.append(f"{factory.name}: {type(exc).__name__}: {exc}")
        raise Dht11Error(
            "没有可用的 DHT11 读取后端：\n  - "
            + "\n  - ".join(errors)
            + "\n排查：① 树莓派上装 lgpio：`sudo apt install -y python3-lgpio`；"
            "② 只想看程序效果就用 `--mock`；"
            "③ 在电脑上跑必须加 `--mock`（电脑没有树莓派的 GPIO）"
        )

    def close(self) -> None:
        if self._backend is not None:
            try:
                self._backend.close()
            except Exception:  # noqa: BLE001
                pass
        self._backend = None

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    @property
    def last_read_at(self) -> Optional[float]:
        """上次**成功**读取的时刻（``None`` 表示还没成功过）。可赋值，便于测试与恢复。"""
        return self._last_read_at

    @last_read_at.setter
    def last_read_at(self, value: Optional[float]) -> None:
        self._last_read_at = value

    def interval_tolerance(self) -> float:
        """本实例允许的"还差多少秒也算到点"。

        规则：**只有设定间隔贴着硬件下限时才给容差**（15%），否则严格按设定间隔判。

        ⚠️ 但真正解决"隔次被拒"的不是容差，而是**别把采样周期当门禁下限**：
        `run.py` 现在传的是硬件下限 2.0 秒，采样周期 3.0 秒只在主循环里控节奏
        （见 `INTERVAL_TOLERANCE_FRACTION` 的注释与 ERROR.md E38）。
        """
        slack = self.min_interval_s - MIN_INTERVAL_S
        tolerance = self.min_interval_s * INTERVAL_TOLERANCE_FRACTION
        return tolerance if slack < tolerance else 0.0

    def wait_remaining(self, now: float) -> float:
        """距"可以再次读取"还需要等多少秒（0 表示现在就能读）。

        ⚠️ **带容差**（2026-09-25 真机实测，ERROR.md E38）：
        `run.py` 默认采样间隔 3.0 秒，两次读取之间还夹着"读传感器 + 存 CSV + 刷曲线"
        的时间波动，真实经过时间常是 2.9 秒 ⇒ 原来判"还差 0.1s"把这一轮拒掉 ⇒ 真机上
        **隔一次就失败一次**（实测 4 次采样成功 2 次失败 2 次）。
        容差只对"贴着硬件下限的间隔"生效，见 :meth:`interval_tolerance`。
        """
        if self._last_read_at is None:
            return 0.0
        remaining = self.min_interval_s - (now - self._last_read_at)
        tolerance = self.interval_tolerance()
        if remaining <= tolerance:
            return 0.0
        # 报"还差多少"时扣掉容差，这样提示里的数字与门禁实际口径一致
        return remaining - tolerance

    def read(self) -> ReadResult:
        """读一次；失败返回 ``ok=False`` 的结果（**不抛异常、不编造数值**）。

        三条刻意的行为：

        1. **距上次成功读取不足** ``min_interval_s`` **就拒绝**，返回
           ``ok=False`` 并说明还差几秒 —— DHT11 读太快会返回**上一次的陈旧数据**，
           基础版不认识"缓存样本"这种形态，宁可如实报"太早"也不拿旧值冒充新值；
           （主循环默认 3 秒一次，所以正常跑不会碰到这条；它防的是"手滑改小了间隔"）
        2. 失败时**不刷新"上次成功时间"**，下一轮可以立刻重试（失败要尽快重试，只有成功才需要限速）；
        3. 重试之间等 ``RETRY_DELAY_S``，避免把总线打爆。
        """
        if self._backend is None:
            self.open()
        remaining = self.wait_remaining(self._clock())
        if remaining > 0:
            return ReadResult(
                False,
                note=(
                    f"距上次成功读取不足 {self.min_interval_s:.1f} 秒（还差 {remaining:.1f}s）："
                    "DHT11 读太快会返回陈旧数据，本次不读（请把采样间隔设为 ≥2 秒）"
                ),
            )
        last_error = ""
        for attempt in range(1, self.retries + 1):
            try:
                temperature, humidity = self._backend.read_once()  # type: ignore[attr-defined]
            except Dht11Error as exc:
                last_error = str(exc)
                if attempt < self.retries:
                    self._sleep(RETRY_DELAY_S)
                continue
            except Exception as exc:  # noqa: BLE001 - 硬件层异常很杂，一律转成失败
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self.retries:
                    self._sleep(RETRY_DELAY_S)
                continue
            reason = check_range(temperature, humidity)
            if reason:
                last_error = reason
                continue
            self._last_read_at = float(self._clock())
            self.read_count += 1
            self.consecutive_failures = 0
            return ReadResult(True, round(temperature, 1), round(humidity, 1))
        self.consecutive_failures += 1
        return ReadResult(False, note=f"{last_error}（已重试 {self.retries} 次）")

    def describe(self) -> str:
        """一行人类可读说明（写进日志/小结，便于答辩时说明"数据从哪来"）。

        ⚠️ `describe_pin()` 的返回值**已经含 `GPIO{n}（…）` 整段**，别再套一层括号：
        2026-09-25 真机实测曾打印成 `GPIO4（GPIO4（物理脚 7，…））`（见 ERROR.md E35）。
        """
        from .pins import describe_pin

        if self.mock or self._explicit_mock is not None:
            return "模拟数据源（--mock：不接硬件也能跑，数据为合成值）"
        return f"DHT11 · {describe_pin(self.pin)} · 后端 {self.backend_name}"


__all__ = [
    "Dht11Reader",
    "Dht11Error",
    "ReadResult",
    "LgpioBackend",
    "GpiozeroBackend",
    "MockBackend",
    "decode_frame",
    "synth_frame",
    "check_range",
    "MIN_INTERVAL_S",
    "BIT_THRESHOLD_US",
]
