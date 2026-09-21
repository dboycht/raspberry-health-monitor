"""HC-SR04 —— 超声波测距（TRIG 触发，ECHO 回波脉宽）。

模块用途
--------
给健康监护项目提供"距离"量：老人是否靠近床边/是否离开房间/跌倒后是否静止不动，
都可以用"超声波测距 + 门限"做粗判（精确跌倒检测仍靠 PIR + 规则引擎）。

⚠️ 电平与供电（最容易烧板子的一条）
----------------------------------
**HC-SR04 的 ECHO 输出是 5V 高电平，而树莓派 GPIO 只耐受 3.3V。**
必须做电平转换，二选一：

1. **TXS0102 双向电平转换模块（材料清单里有，推荐）**：A 侧 VCCA=3.3V 接树莓派
   GPIO6，B 侧 VCCB=5V 接 HC-SR04 的 ECHO，OE 通过 10kΩ 上拉到 VCCA；
2. 电阻分压：``ECHO ──1kΩ──┬── GPIO6``，``┬──2kΩ── GND``（5V×2/3 ≈ 3.33V，够安全）。

**VCC 必须接 5V**：HC-SR04 在 3.3V 下工作不稳定（实测常出现不回波/量程缩水），
所以 VCC 走物理脚 2（5V），GND 与树莓派共地。

接线表（HC-SR04 ↔ 树莓派 40-pin 物理脚号）
------------------------------------------
===================  ==============================  ===================================
HC-SR04 引脚         接到                            树莓派 40-pin 物理脚
===================  ==============================  ===================================
VCC                  5V                              **脚 2**（或脚 4）
GND                  GND                             **脚 6**（或脚 9/14/20/25/30/34/39）
TRIG                 GPIO5（输出，10µs 高电平脉冲）   **脚 29**
ECHO                 **经 TXS0102 电平转换**后接       **脚 31**（GPIO6）
                     GPIO6（输入）
===================  ==============================  ===================================

⚠️ **默认引脚刻意避开 GPIO17 / GPIO27**：那两个脚在本项目里已分别分给
HC-SR501（人体红外，GPIO17）与求救按钮（GPIO27）。早期版本的驱动文档把
本器件写成 GPIO17/GPIO27，启用拓展件时会**三方抢脚**（`config/devices.json`
里已按 GPIO5/GPIO6 配置，`tests/hal/test_pins.py` 有撞脚检查）。

⚠️ 树莓派 5 上 ``RPi.GPIO`` **不可用**（GPIO 架构从 /dev/gpiomem 换到 RP1 芯片），
请用 ``gpiozero``（底层走 ``lgpio``）：``sudo apt install -y python3-gpiozero python3-lgpio``。
本驱动真实模式**优先 gpiozero 的 ``DistanceSensor``**，不可用时退回 ``lgpio`` 手工时序。

超时策略（**本驱动选定：返回 ok=False，不抛异常**）
--------------------------------------------------
ECHO 一直不拉高（模块没接好/超出量程/物体太软吸声）时，**不抛** ``DeviceTimeout``，
而是返回::

    RangeSample(ok=False, error="回波超时：连续 3 次测距均失败（...）", echo_us=None)

理由：测距是"高频轮询 + 本来就容易失败"的量，抛异常会把上层循环打断；
``ok=False`` 让业务层记一个坏点继续跑（同时 ``status()["fault_count"]`` 会 +1，
``last_error`` 里能看到原因，**不是静默失败**）。真正需要异常语义的调用方可以自己
判断 ``sample.ok``。

设计要点
--------
1. **换算纯函数**：``echo_us_to_cm`` / ``cm_to_echo_us`` 带公式注释与已知值断言
   （20°C 时 5800µs ≈ 99.6cm），单测不碰硬件。
2. **重试取中位数**：单次测量易受抖动/多径影响，默认连测 3 次取中位数
   （:meth:`HcSr04._measure_median_cm`）；测试用子类覆写 ``_measure_once`` 注入假读数，
   于是重试/中位数/量程/超时四条逻辑都能在没有硬件时验证。
3. **量程校验**：2~400cm 之外一律 ``ok=False``（``distance_cm=None``，符合
   :class:`~health_monitor.hal.models.RangeSample` 的约定："超量程或回波超时为 None"）。
4. **温度补偿**：声速 ``v = 331.3 + 0.606*T`` m/s。默认按 20°C 算；
   若项目里已有 TMP36/DHT11，建议把实测温度传进 ``temperature_c``（例如放在
   ``core/`` 的装配代码里设置 ``hc.temperature_c = 实测值``）。

负责人占位
----------
负责人：``<填写姓名 / 学号>``；验收：``python -m pytest tests/sensors/test_hc_sr04.py -q``。
"""

from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional, Tuple

from ..hal.device import Device
from ..hal.exceptions import (
    ConfigError,
    DataInvalidError,
    DeviceInitError,
    DeviceTimeout,
    UnsupportedError,
)
from ..hal.models import DeviceKind, RangeSample
from ..hal.pins import describe_pin

# ==========================================================================
# 第一部分：纯逻辑（可单测，不碰任何硬件）
# ==========================================================================


def sound_speed_m_per_s(temperature_c: float = 20.0) -> float:
    """空气中的声速（m/s）。**纯函数**。

    公式（常用近似，0~40°C 内误差 <0.5%）::

        v(T) = 331.3 + 0.606 * T      # T 单位 °C，20°C → 343.42 m/s

    Args:
        temperature_c: 环境温度（°C），有效范围 -273.15~200。

    Raises:
        ConfigError: 温度超出有效范围。
    """
    if not -273.15 <= temperature_c <= 200.0:
        raise ConfigError(
            f"温度必须在 -273.15~200°C 之间才能算声速，收到 {temperature_c!r}"
        )
    return 331.3 + 0.606 * temperature_c


def echo_us_to_cm(echo_us: float, temperature_c: float = 20.0) -> float:
    """回波高电平时长（µs）→ 距离（cm）。**纯函数**。

    推导（HC-SR04 的 ECHO 高电平 = 声波往返时间）::

        v = 331.3 + 0.606*T          m/s      （T=20°C → 343.42 m/s = 34342 cm/s）
        v_cm_per_us = v / 10000                （1 m/s = 1e-4 cm/µs → 0.034342 cm/µs）
        d_cm = echo_us * v_cm_per_us / 2       （除以 2：去 + 回，只算单程）

    已知值（单测已断言；HC-SR04 手册给的例子正是"约 58µs/cm"）::

        echo_us_to_cm(5800, 20.0) ≈ 99.59 cm      # 5800µs ≈ 100cm
        echo_us_to_cm(1000, 20.0) ≈ 17.17 cm      # 约 58.2µs/cm
        echo_us_to_cm(0,    20.0) == 0.0

    Args:
        echo_us: ECHO 高电平持续时长（微秒），必须 >= 0。
        temperature_c: 环境温度（°C），用于声速补偿。

    Raises:
        ConfigError: ``echo_us`` 为负，或温度超出有效范围。
    """
    if echo_us < 0:
        raise ConfigError(
            f"回波时长不可能为负：{echo_us!r}（负值说明计时/接线写错了）"
        )
    speed_cm_per_us = sound_speed_m_per_s(temperature_c) / 10000.0
    return echo_us * speed_cm_per_us / 2.0


def cm_to_echo_us(distance_cm: float, temperature_c: float = 20.0) -> float:
    """:func:`echo_us_to_cm` 的反函数：距离（cm）→ 回波时长（µs）。**纯函数**。

    用途：gpiozero 只给距离、不暴露回波时长，用它可以**反推**一个 ``echo_us``
    写进样本方便排查；也用于 mock 模式造数据。

    已知值：``cm_to_echo_us(99.5871, 20.0) ≈ 5800``（与 :func:`echo_us_to_cm` 互逆）。

    Raises:
        ConfigError: ``distance_cm`` 为负，或温度超出有效范围。
    """
    if distance_cm < 0:
        raise ConfigError(
            f"距离不可能为负：{distance_cm!r}（负值说明测量/换算写错了）"
        )
    return distance_cm * 2.0 / (sound_speed_m_per_s(temperature_c) / 10000.0)


# ==========================================================================
# 第二部分：驱动（硬件访问层）
# ==========================================================================


class HcSr04(Device):
    """HC-SR04 超声波测距。

    **超时策略：返回 ``ok=False`` 的样本，不抛异常**（理由见模块文档）。

    Args:
        trig_pin: TRIG 的 BCM 编号（默认 5 = 物理脚 29；**避开 PIR 的 GPIO17 与按钮的 GPIO27**）。
        echo_pin: ECHO 的 BCM 编号（默认 6 = 物理脚 31，**必须经 TXS0102 电平转换**）。
        timeout_us: 单次测量等待回波的超时（µs，默认 30000 = 30ms ≈ 5.1m，够 4m 量程）。
        temperature_c: 用于声速补偿的环境温度（°C，默认 20.0）。
        retries: 单次 ``read()`` 内的测量次数，取中位数（默认 3）。
        bus: 总线对象（本驱动是纯 GPIO，用不到 I2C/SPI，保留以统一构造签名）。
        mock: 模拟模式。
        name: 实例名。
    """

    KIND = DeviceKind.RANGE
    NAME = "hc_sr04"

    #: HC-SR04 有效量程（cm）：2~400
    MIN_CM = 2.0
    MAX_CM = 400.0

    def __init__(
        self,
        trig_pin: int = 5,
        echo_pin: int = 6,
        timeout_us: int = 30000,
        temperature_c: float = 20.0,
        retries: int = 3,
        bus: Any = None,
        mock: bool = False,
        name: str = "",
    ) -> None:
        super().__init__(bus=bus, mock=mock, name=name)
        if not 0 <= int(trig_pin) <= 27 or not 0 <= int(echo_pin) <= 27:
            raise ConfigError(
                f"引脚必须是 BCM 编号 0~27：trig_pin={trig_pin!r}, echo_pin={echo_pin!r}"
            )
        if int(trig_pin) == int(echo_pin):
            raise ConfigError(
                f"TRIG 与 ECHO 不能是同一个引脚（都是 GPIO{trig_pin}）"
            )
        if int(timeout_us) <= 0:
            raise ConfigError(f"timeout_us 必须为正数，收到 {timeout_us!r}")
        if int(retries) < 1:
            raise ConfigError(f"retries 至少为 1，收到 {retries!r}")
        sound_speed_m_per_s(float(temperature_c))  # 顺手校验温度范围
        self.trig_pin = int(trig_pin)
        self.echo_pin = int(echo_pin)
        self.timeout_us = int(timeout_us)
        self.temperature_c = float(temperature_c)
        self.retries = int(retries)
        self._backend = "none"          # gpiozero / lgpio / none
        self._sensor: Any = None        # gpiozero DistanceSensor
        self._lgpio: Any = None         # lgpio 模块
        self._lgpio_handle: Any = None  # lgpio chip 句柄
        self._mock_tick = 0
        self._mock_distance_cm: Optional[float] = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def open(self) -> None:
        """初始化测距后端（mock 模式下什么都不做，只置位）。"""
        if self._opened:
            return
        if self.mock:
            self._opened = True
            return

        try:
            from gpiozero import DistanceSensor  # type: ignore import-not-found
        except ImportError:
            # gpiozero 不在就退回 lgpio 手工时序（树莓派 5 上 RPi.GPIO 不可用，别用它）
            self._open_lgpio()
            self._opened = True
            return

        try:
            # partial=False：超时就让 gpiozero 抛异常，由本驱动的重试逻辑兜住；
            # 若用 partial=True，超时会返回"最大量程"这种假距离，反而更难判断。
            self._sensor = DistanceSensor(
                echo=self.echo_pin,
                trigger=self.trig_pin,
                max_distance=self.MAX_CM / 100.0,  # gpiozero 用米
                queue_len=1,
                partial=False,
            )
        except Exception as exc:  # noqa: BLE001 - gpiozero 会抛各种运行时错误
            raise DeviceInitError(
                f"HC-SR04（TRIG={describe_pin(self.trig_pin)}，ECHO={describe_pin(self.echo_pin)}）"
                f"初始化失败：{exc}。排查：1) 树莓派 5 必须用 gpiozero+lgpio，RPi.GPIO 不可用；"
                "2) `sudo apt install -y python3-gpiozero python3-lgpio`；"
                "3) VCC 必须接 5V（物理脚 2）、GND 共地（物理脚 6）；"
                f"4) ECHO 必须经 TXS0102 电平转换后再接 GPIO{self.echo_pin}"
                "（ECHO 是 5V，直连会损伤 GPIO）；"
                "5) 引脚别与 LCD/按键等其他器件冲突"
            ) from exc
        self._backend = "gpiozero"
        self._opened = True

    def _open_lgpio(self) -> None:
        """退回方案：用 lgpio 手工做 TRIG/ECHO 时序。"""
        try:
            import lgpio  # type: ignore import-not-found
        except ImportError as exc:
            raise DeviceInitError(
                f"HC-SR04 需要 gpiozero（推荐）或 lgpio，两者都不可用：{exc}。"
                "树莓派上执行 `sudo apt install -y python3-gpiozero python3-lgpio`；"
                "注意树莓派 5 上 RPi.GPIO 不可用（GPIO 架构变了，必须用 gpiozero/lgpio）；"
                "接线复核：VCC=5V（物理脚 2）、GND=物理脚 6、"
                f"TRIG={describe_pin(self.trig_pin)}、"
                f"ECHO 经 TXS0102 电平转换后接 {describe_pin(self.echo_pin)}（ECHO 是 5V 不能直连）；"
                "PC 上开发请用 mock=True"
            ) from exc
        try:
            handle = lgpio.gpiochip_open(0)
            lgpio.gpio_claim_output(handle, self.trig_pin, 0)
            lgpio.gpio_claim_input(handle, self.echo_pin)
        except Exception as exc:  # noqa: BLE001
            raise DeviceInitError(
                f"lgpio 初始化失败（TRIG=GPIO{self.trig_pin}, ECHO=GPIO{self.echo_pin}）：{exc}。"
                "请确认引脚未被占用（`gpioinfo` 或换一对引脚），且当前用户在 gpio 组"
            ) from exc
        self._lgpio = lgpio
        self._lgpio_handle = handle
        self._backend = "lgpio"

    def read(self) -> RangeSample:
        """测一次距离。

        Returns:
            :class:`RangeSample`。成功时 ``distance_cm`` 为 2~400 之间的中位数、
            ``echo_us`` 为对应回波时长；**超时或超量程时 ``ok=False``、
            ``distance_cm=None``、``error`` 写明原因（不抛异常，见模块文档）**。

        Raises:
            DeviceNotReady: 未 ``open()``。
        """
        self._require_open()

        if self.mock:
            try:
                distance_cm = self._mock_distance()
            except Exception as exc:  # noqa: BLE001 - 注入参数错误也要留痕后上报
                return self._reject(str(exc), exc)
            if not self._in_range(distance_cm):
                message = self._range_message(
                    distance_cm, cm_to_echo_us(distance_cm, self.temperature_c)
                )
                return self._reject(message, DataInvalidError(message))
            return self._accept(distance_cm, cm_to_echo_us(distance_cm, self.temperature_c))

        try:
            distance_cm, echo_us = self._measure_median_cm()
        except Exception as exc:  # noqa: BLE001 - 超时/接线问题统一降级为 ok=False
            if isinstance(exc, DataInvalidError):
                message = str(exc)
            else:
                message = (
                    f"回波超时：连续 {self.retries} 次测距均失败"
                    f"（{type(exc).__name__}: {exc}）"
                )
            return self._reject(message, exc)
        return self._accept(distance_cm, echo_us)

    def close(self) -> None:
        """释放 GPIO（幂等、不抛异常）。"""
        if self._sensor is not None:
            try:
                self._sensor.close()
            except Exception:  # noqa: BLE001 - 关闭失败不应影响收尾
                pass
            self._sensor = None
        if self._lgpio_handle is not None and self._lgpio is not None:
            try:
                self._lgpio.gpio_free(self._lgpio_handle, self.trig_pin)
                self._lgpio.gpio_free(self._lgpio_handle, self.echo_pin)
            except Exception:  # noqa: BLE001
                pass
            try:
                self._lgpio.gpiochip_close(self._lgpio_handle)
            except Exception:  # noqa: BLE001
                pass
            self._lgpio_handle = None
        self._backend = "none"
        self._opened = False

    # ------------------------------------------------------------------
    # 测量：重试 + 中位数 + 量程校验
    # ------------------------------------------------------------------

    def _measure_median_cm(self) -> Tuple[float, float]:
        """连测 ``retries`` 次，取中位数，返回 ``(distance_cm, echo_us)``。

        全部失败（超时/超量程）时抛最后一次异常，由 :meth:`read` 统一降级成 ``ok=False``。
        """
        readings: List[Tuple[float, float]] = []
        last_exc: Optional[BaseException] = None
        for _ in range(self.retries):
            try:
                distance_cm, echo_us = self._measure_once()
            except Exception as exc:  # noqa: BLE001 - 单次失败是常态，重试即可
                last_exc = exc
                continue
            if not self._in_range(distance_cm):
                last_exc = DataInvalidError(self._range_message(distance_cm, echo_us))
                continue
            readings.append((distance_cm, echo_us))

        if not readings:
            raise (
                last_exc
                if last_exc is not None
                else DeviceTimeout(f"回波超时：{self.retries} 次测距均无有效读数")
            )
        # 中位数（读数个数为偶数时取偏大的那个，规则固定、可预期）
        readings.sort(key=lambda item: item[0])
        return readings[len(readings) // 2]

    def _measure_once(self) -> Tuple[float, float]:
        """真实模式：执行**一次**测距，返回 ``(distance_cm, echo_us)``。

        失败（回波超时、电平异常）请抛 :class:`DeviceTimeout`——
        重试策略由 :meth:`_measure_median_cm` 统一负责。

        单测用子类覆写本方法注入假读数，从而在没有硬件的情况下验证
        重试/中位数/量程/超时逻辑（见 ``tests/sensors/test_hc_sr04.py``）。
        """
        if self._backend == "gpiozero":
            return self._measure_gpiozero()
        if self._backend == "lgpio":
            return self._measure_lgpio()
        raise DeviceInitError("测距后端未初始化：请先 open()（或检查 gpiozero/lgpio 是否可用）")

    def _measure_gpiozero(self) -> Tuple[float, float]:
        """gpiozero ``DistanceSensor`` 路径。

        gpiozero 只暴露距离（米），不暴露回波时长，因此 ``echo_us`` 是由距离反推的
        **参考值**（温度补偿只影响这个反推值，gpiozero 内部用的是固定声速）。
        """
        try:
            distance_m = self._sensor.distance
        except Exception as exc:  # noqa: BLE001 - 超时/引脚异常都当作一次失败
            raise DeviceTimeout(f"gpiozero DistanceSensor 未收到回波：{exc}") from exc
        if distance_m is None:
            raise DeviceTimeout("gpiozero DistanceSensor 返回 None（回波超时）")
        distance_cm = float(distance_m) * 100.0
        return distance_cm, cm_to_echo_us(distance_cm, self.temperature_c)

    def _measure_lgpio(self) -> Tuple[float, float]:
        """lgpio 手工时序路径（真正测到 ECHO 脉宽，``echo_us`` 是实测值）。

        ⚠️ 纯 Python 轮询的计时精度约 ±20~50µs（≈±0.3~0.9cm），够本项目的
        "靠近/离开/静止"判定；若要更高精度，请改用 pigpio 的 DMA 计时。
        """
        lg = self._lgpio
        h = self._lgpio_handle
        # 1) TRIG 拉高 >=10µs 再拉低（sleep 在 Linux 上必然 >=10µs，满足手册要求）
        lg.gpio_write(h, self.trig_pin, 1)
        time.sleep(10e-6)
        lg.gpio_write(h, self.trig_pin, 0)

        # 2) 等 ECHO 拉高（一直为低 = 超时/模块没响应）
        t0 = time.perf_counter()
        deadline = t0 + self.timeout_us / 1e6
        while lg.gpio_read(h, self.echo_pin) == 0:
            if time.perf_counter() > deadline:
                raise DeviceTimeout(
                    f"ECHO（GPIO{self.echo_pin}）在 {self.timeout_us}µs 内一直为低："
                    "回波超时（模块未供电/超出量程/TRIG 或 ECHO 接反）"
                )
        t_rise = time.perf_counter()

        # 3) 等 ECHO 拉低（一直为高 = 接线短路/无模块）
        while lg.gpio_read(h, self.echo_pin) == 1:
            if time.perf_counter() - t_rise > self.timeout_us / 1e6:
                raise DeviceTimeout(
                    f"ECHO（GPIO{self.echo_pin}）持续为高超过 {self.timeout_us}µs："
                    "疑似接线短路或电平转换模块故障"
                )
        t_fall = time.perf_counter()

        echo_us = (t_fall - t_rise) * 1e6
        return echo_us_to_cm(echo_us, self.temperature_c), echo_us

    # ------------------------------------------------------------------
    # 纯逻辑辅助 & mock
    # ------------------------------------------------------------------

    def _in_range(self, distance_cm: float) -> bool:
        """距离是否在 HC-SR04 有效量程 2~400cm 内（含端点）。"""
        return self.MIN_CM <= distance_cm <= self.MAX_CM

    def _range_message(self, distance_cm: float, echo_us: Optional[float] = None) -> str:
        """量程外的中文说明（带原始回波时长，便于排查）。"""
        echo_text = "" if echo_us is None else f"（回波 {echo_us:.0f}µs）"
        return (
            f"距离 {distance_cm:.1f}cm{echo_text} 超出量程 "
            f"{self.MIN_CM:.0f}~{self.MAX_CM:.0f}cm"
        )

    def _accept(self, distance_cm: float, echo_us: float) -> RangeSample:
        """有效读数：计数 + 返回样本。"""
        self._note_ok()
        return RangeSample(device=self.name, distance_cm=distance_cm, echo_us=echo_us)

    def _reject(self, message: str, exc: BaseException) -> RangeSample:
        """无效读数：留痕 + 返回 ``ok=False`` 的样本（**不抛异常**）。"""
        self._note_fault(exc)
        return RangeSample(device=self.name, ok=False, error=message, echo_us=None)

    def _mock_distance(self) -> float:
        """mock 距离：30~120cm 之间的平滑正弦波动（确定性，可复现）。"""
        if self._mock_distance_cm is not None:
            return self._mock_distance_cm
        self._mock_tick += 1
        return 75.0 + 45.0 * math.sin(self._mock_tick * 0.2)

    def inject_distance(self, distance_cm: float) -> None:
        """**仅供 mock / 测试**：指定要模拟的距离（cm）。

        也用来造坏点：``inject_distance(500)`` → ``read().ok is False``（超量程）。
        """
        if not self.mock:
            raise UnsupportedError(
                "inject_distance() 只能在 mock=True 时使用（真实模式不允许伪造距离）"
            )
        if float(distance_cm) <= 0:
            raise ConfigError(f"注入距离必须为正数，收到 {distance_cm!r}")
        self._mock_distance_cm = float(distance_cm)

    # ------------------------------------------------------------------
    # 说明与状态
    # ------------------------------------------------------------------

    def describe(self) -> Dict[str, Any]:
        """接线说明（会被 ``docs`` 生成脚本读取）。"""
        return {
            "name": self.name,
            "kind": self.KIND.value,
            "mock": self.mock,
            "bus": f"GPIO 手工时序（后端：{self._backend if not self.mock else 'mock'}；无 I2C/SPI）",
            "pins": {
                "vcc": "HC-SR04 VCC → 5V，树莓派物理脚 2（⚠ 必须 5V，3.3V 下工作不稳定）",
                "gnd": "HC-SR04 GND → GND，树莓派物理脚 6（务必与树莓派共地）",
                "trig": f"HC-SR04 TRIG → {describe_pin(self.trig_pin)}",
                "echo": f"HC-SR04 ECHO → TXS0102 电平转换 → {describe_pin(self.echo_pin)}",
                "level_shifter": f"TXS0102：VCCA=3.3V（接 GPIO{self.echo_pin} 侧）、VCCB=5V（接 ECHO 侧）、"
                                 f"OE 经 10kΩ 上拉到 VCCA；或改用分压 "
                                 f"ECHO─1kΩ─┬─GPIO{self.echo_pin}、┬─2kΩ─GND",
            },
            "notes": (
                f"量程 {self.MIN_CM:.0f}~{self.MAX_CM:.0f}cm，超出或回波超时返回 ok=False（不抛异常）；"
                f"单次 read() 连测 {self.retries} 次取中位数，等待超时 {self.timeout_us}µs；"
                f"声速补偿用 {self.temperature_c:.1f}°C（v = 331.3+0.606T m/s，往返除以 2）；"
                "⚠ ECHO 输出 5V，直连 GPIO 会损坏树莓派，必须经 TXS0102 或电阻分压；"
                "⚠ 树莓派 5 上 RPi.GPIO 不可用，请用 gpiozero（底层 lgpio）或 lgpio 手工时序"
            ),
        }


__all__ = ["HcSr04", "echo_us_to_cm", "cm_to_echo_us", "sound_speed_m_per_s"]
