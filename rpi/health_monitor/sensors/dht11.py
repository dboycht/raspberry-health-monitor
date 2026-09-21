"""温湿度传感器 DHT11 驱动（单总线 GPIO）。

模块用途
--------
读取 DHT11 的温度（℃）与相对湿度（%RH），包装成
:class:`~health_monitor.hal.models.AmbientSample` 交给业务层与报警引擎
（环境温度过高/过低、湿度过高都会触发报警）。

本文件的三条设计要点（照抄 ``sensors/button.py`` 范本）
--------------------------------------------------------
1. **纯逻辑抽出来**：量程校验（:func:`validate_reading`）与"两次读取间隔够不够 2 秒"
   （:class:`CachePolicy`）都是**不碰硬件、不依赖时钟**的纯函数/纯类，
   所以单测不需要真的 sleep 2 秒，也不会有偶发失败；
2. **DHT11 的硬件脾气要如实处理**：两次读取间隔必须 ≥2 秒，否则芯片会**返回上一次的
   陈旧数据**。本驱动自己记录上次读取时间，间隔不足时**不假装刚测过**——
   见下面"缓存策略"；
3. **mock 模式绝不碰硬件**：``mock=True`` 时不创建任何 gpiozero 对象、不打开 GPIO，
   用"缓慢波动的室内温湿度"合成数据，业务层无硬件也能完整演示。

⚠️ 缓存策略（本项目选定，答辩要能讲清）
----------------------------------------
间隔不足 2 秒时，**返回 ``ok=False`` 的样本**，同时把上一次的温度/湿度照填：
``error="距上次读取不足 2 秒（还差 1.3s），本次返回缓存值"``。
理由：业务层只要判断 ``ok=False`` 就能知道"这不是新数据"（例如不写进数据库、
不参与趋势统计），但 LCD/日志仍能显示当前环境值，比"空白"更有用。
**绝不**把缓存值包装成 ``ok=True``——那等于伪造测量时间，是本项目的一级缺陷。

接线表（DHT11 模块 → 树莓派 5 40-pin 物理脚号）
----------------------------------------------
================  ==========================  ==========================================
DHT11 模块引脚     树莓派 40-pin              说明
================  ==========================  ==========================================
VCC（或 +）        3.3V（物理脚 1 或 17）      **不要接 5V**：数据电平会被拉到 5V，伤 GPIO
DATA（或 out）     GPIO4（物理脚 7）           本驱动默认引脚；单总线双向数据
GND（或 -）        GND（物理脚 6/9/14/…）      任意一个 GND 脚
================  ==========================  ==========================================

⚠️ 三针模块（自带 10k 上拉电阻）可直接插；**裸四针传感器**必须在 DATA 与 3.3V 之间
外接一个 4.7k~10k 上拉电阻，否则读数会一直是 NaN/超时。
⚠️ 树莓派 5 上手工用 ``lgpio`` 打单总线时序很容易失败（内核调度抖动 > 微秒级精度要求），
所以本驱动**只用 gpiozero 的 ``DHT11`` 器件类**；没装 gpiozero 会抛
:class:`DeviceInitError` 并提示 ``sudo apt install -y python3-gpiozero``。

负责人（团队分工时填）
----------------------
负责人：____________（学号：__________）  验收人：____________  日期：__________
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

from ..hal.device import Device
from ..hal.exceptions import (
    ConfigError,
    DeviceInitError,
    DeviceIOError,
    DeviceTimeout,
    UnsupportedError,
)
from ..hal.models import AmbientSample, DeviceKind, now_ts
from ..hal.pins import BCM_TO_PHYSICAL, bcm_to_physical, describe_pin

# ==========================================================================
# 第一部分：纯逻辑（可单测，不碰任何硬件）
# ==========================================================================

#: DHT11 的硬件量程（数据手册）：湿度 20~90%RH，温度 0~50℃
DHT11_TEMP_MIN_C = 0.0
DHT11_TEMP_MAX_C = 50.0
DHT11_HUMI_MIN_PCT = 20.0
DHT11_HUMI_MAX_PCT = 90.0

#: DHT11 的硬件限制：两次读取间隔必须 ≥2 秒（数据手册要求 1s，实测 2s 才稳）
DHT11_MIN_INTERVAL_S = 2.0

#: BCM 编号 → 40-pin 物理脚号。**已统一到 ``hal/pins.py``（全项目单一来源）**，
#: 这里保留别名只为兼容早先的 import；新代码请直接用 ``describe_pin()``。
BCM_TO_PHYSICAL_PIN: Dict[int, int] = dict(BCM_TO_PHYSICAL)


def physical_pin(bcm: int) -> str:
    """把 BCM 编号翻成"物理脚 N"（找不到就如实说"非法引脚"）。

    ⚠️ 规矩：**不许自己写偏移公式**（``pin + 1`` 这种猜法会把 GPIO27 报成物理脚 28，
    而 28 脚其实是 HAT ID_SC）。统一走 :func:`health_monitor.hal.pins.bcm_to_physical`。
    """
    phys = bcm_to_physical(bcm)
    return f"物理脚 {phys}" if phys else "非标准 40-pin 脚位"


def validate_reading(
    temperature_c: Optional[float],
    humidity_percent: Optional[float],
    temp_limits: Tuple[float, float] = (DHT11_TEMP_MIN_C, DHT11_TEMP_MAX_C),
    humi_limits: Tuple[float, float] = (DHT11_HUMI_MIN_PCT, DHT11_HUMI_MAX_PCT),
) -> str:
    """校验一次读数是否可信，返回**空字符串表示通过**，否则返回中文原因。

    判据（可执行）：
    - ``None`` / ``NaN`` → "读数无效"（gpiozero 读失败时返回 nan）；
    - 温度超出 ``temp_limits``（默认 0~50℃）→ 超量程；
    - 湿度超出 ``humi_limits``（默认 20~90%RH）→ 超量程；
    - 湿度 < 0 或 > 100（同轴量的物理上限）→ 物理上不可能。

    **纯函数**：不碰硬件、不读时钟，边界情况（None/NaN/超量程）不抛异常。
    """
    if temperature_c is None or humidity_percent is None:
        return "读数为空（None）：传感器未应答或还没有读到数据"
    if not math.isfinite(float(temperature_c)) or not math.isfinite(float(humidity_percent)):
        return "读数为 NaN：DHT11 未应答（检查上拉电阻与接线，或距上次读取不足 1 秒）"

    t = float(temperature_c)
    h = float(humidity_percent)
    if h < 0.0 or h > 100.0:
        return f"湿度 {h:.1f}% 超出物理可能范围 0~100%"
    if not (temp_limits[0] <= t <= temp_limits[1]):
        return f"温度 {t:.1f}℃ 超出 DHT11 量程 {temp_limits[0]:.0f}~{temp_limits[1]:.0f}℃"
    if not (humi_limits[0] <= h <= humi_limits[1]):
        return f"湿度 {h:.1f}% 超出 DHT11 量程 {humi_limits[0]:.0f}~{humi_limits[1]:.0f}%"
    return ""


@dataclass
class CachePolicy:
    """DHT11 的"两次读取间隔 ≥2 秒"策略（纯逻辑，可注入时钟）。

    记录上次**真正读硬件**的时间；间隔不足时**不读硬件**，直接告诉调用方
    "你还得再等多久"（调用方用缓存值 + ``ok=False`` 如实上报）。

    Args:
        min_interval_s: 最小读取间隔（秒），DHT11 取 2.0。
        clock: 时钟函数，返回秒（默认 :func:`time.monotonic`）。
               **测试时注入假时钟**，就能"瞬时"验证 2 秒限制。
    """

    min_interval_s: float = DHT11_MIN_INTERVAL_S
    clock: Callable[[], float] = time.monotonic
    _last_read_at: Optional[float] = None

    def needs_wait(self, now: Optional[float] = None) -> Tuple[bool, float]:
        """返回 ``(是否还需等待, 还需等多久秒)``。

        - 从未读过（``_last_read_at is None``）→ ``(False, 0.0)``，可以马上读；
        - 距离上次不足 ``min_interval_s`` → ``(True, 还差多少秒)``；
        - 已经够了 → ``(False, 0.0)``。
        """
        if self._last_read_at is None:
            return False, 0.0
        current = self.clock() if now is None else now
        elapsed = current - self._last_read_at
        if elapsed >= self.min_interval_s:
            return False, 0.0
        return True, self.min_interval_s - elapsed

    def mark_read(self, now: Optional[float] = None) -> None:
        """记录"刚刚真的读了硬件"（只有真正读过才允许调用，否则就是伪造时间）。"""
        self._last_read_at = self.clock() if now is None else now

    def reset(self) -> None:
        """清空记录（``close()`` 后重开、或注入新数据时用）。"""
        self._last_read_at = None

    @property
    def last_read_at(self) -> Optional[float]:
        """上次真正读硬件的时间（None 表示还没读过）。"""
        return self._last_read_at


#: mock 模式的"房间"基准值：24~27℃、45~58%RH，缓慢波动（演示不假）
MOCK_TEMP_BASE_C = 25.5
MOCK_TEMP_SWING_C = 1.5
MOCK_HUMI_BASE_PCT = 51.0
MOCK_HUMI_SWING_PCT = 6.0
MOCK_CYCLE_S = 120.0


def mock_environment(elapsed_s: float) -> Tuple[float, float]:
    """按"已运行秒数"合成一对合理的室内温湿度（纯函数，**不碰硬件**）。

    做法：两个不同周期的正弦叠加（120s 与 47s），模拟空调/人体活动带来的缓慢波动；
    温度范围 24.0~27.0℃，湿度范围 45~57%RH——都在 DHT11 量程内，且**不会假到一条直线**。

    Args:
        elapsed_s: 打开设备以来经过的秒数。

    Returns:
        ``(温度℃, 湿度%RH)``，均已保留 1 位小数。
    """
    t = float(elapsed_s)
    temp = MOCK_TEMP_BASE_C + MOCK_TEMP_SWING_C * math.sin(2 * math.pi * t / MOCK_CYCLE_S)
    humi = MOCK_HUMI_BASE_PCT + MOCK_HUMI_SWING_PCT * math.sin(2 * math.pi * t / 47.0 + 1.1)
    return round(temp, 1), round(humi, 1)


# ==========================================================================
# 第二部分：驱动（硬件访问层）
# ==========================================================================


class Dht11(Device):
    """DHT11 温湿度传感器（单总线 GPIO）驱动。

    Args:
        pin: 数据脚 BCM 编号，默认 4（物理脚 7）。
        retries: 单次 ``read()`` 内的重试次数（默认 3）。DHT11 偶发校验失败很常见，
                 重试是最省事也最有效的补救；两次重试之间会等 0.1 秒。
        min_interval_s: 两次读取的最小间隔（秒），默认 2.0（**DHT11 硬件限制**）。
        max_temperature_c: 允许的最高温度（默认 50℃，DHT11 量程上限）。
        bus: 保留参数（单总线不走 I2C/SPI），统一构造签名用。
        mock: 模拟模式。为 True 时**不创建任何真实 GPIO 对象**。
        name: 实例名（与 ``config/devices.json`` 的键一致）。
    """

    KIND = DeviceKind.AMBIENT
    NAME = "dht11"

    def __init__(
        self,
        pin: int = 4,
        retries: int = 3,
        min_interval_s: float = DHT11_MIN_INTERVAL_S,
        max_temperature_c: float = DHT11_TEMP_MAX_C,
        bus: Any = None,
        mock: bool = False,
        name: str = "",
    ) -> None:
        super().__init__(bus=bus, mock=mock, name=name)

        if not 0 <= int(pin) <= 27:
            raise ConfigError(f"DHT11 数据脚 GPIO{int(pin)} 非法：BCM 编号应在 0~27")
        if int(retries) < 1:
            raise ConfigError(f"DHT11 的 retries={retries} 非法：至少 1 次")
        if float(min_interval_s) < 1.0:
            raise ConfigError(
                f"DHT11 的最小读取间隔 {min_interval_s}s 非法：硬件要求 ≥2s（至少不能低于 1s），"
                "否则读到的是上一次的陈旧数据"
            )
        if not (DHT11_TEMP_MIN_C < float(max_temperature_c) <= 80.0):
            raise ConfigError(
                f"DHT11 的 max_temperature_c={max_temperature_c} 非法："
                f"应大于 {DHT11_TEMP_MIN_C:.0f}℃ 且不超过 80℃"
            )

        self.pin = int(pin)
        self.retries = int(retries)
        self.min_interval_s = float(min_interval_s)
        self.max_temperature_c = float(max_temperature_c)

        self._sensor: Any = None            # gpiozero 的 DHT11 器件对象（真实模式才有）
        self._mock_opened_at = 0.0          # mock 模式的"打开时刻"（真实秒表）
        self._mock_inject_count = 0         # 注入值被真正读取的次数（测试用探针）
        self._injected: Optional[Tuple[float, float]] = None  # mock 注入值
        self._cached: Optional[Tuple[float, float, float]] = None  # (温度, 湿度, 时间戳)
        self._cache = CachePolicy(min_interval_s=float(min_interval_s))
        self._mock_fault: Optional[BaseException] = None
        self._sleep = time.sleep                # 测试可替换成"不睡"

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def open(self) -> None:
        """初始化传感器。

        mock 模式：**只置位 ``_opened`` 并准备合成数据**，绝不打开任何 GPIO；
        真实模式：用 gpiozero 的 ``DHT11`` 建对象（**故意不在构造期建**，
        否则 ``create_device()`` 一装配就会去碰硬件）。
        """
        if self._opened:
            return
        if self.mock:
            # 用真实秒表记"打开时刻"：这样合成数据的波动跟真实经过的时间对得上
            # （演示脚本连续读时，LCD 上看到的是缓慢变化，而不是每次跳一大步）。
            self._mock_opened_at = time.monotonic()
            self._opened = True
            return

        try:
            # 函数内延迟导入：PC 上开发/跑测试时不该因为没装 gpiozero 而 import 失败
            from gpiozero import DHT11 as GpioDHT11  # type: ignore import-not-found
        except ImportError as exc:
            raise DeviceInitError(
                f"DHT11（GPIO{self.pin}）初始化失败：未安装 gpiozero（{exc}）。"
                "树莓派上执行 `sudo apt install -y python3-gpiozero`；"
                "PC 上开发请用 mock=True"
            ) from exc

        try:
            # 必须显式封顶温度：DHT11 在 0℃ 以下会返回负值，gpiozero 默认会把它
            # 当成"读数无效"抛异常（NegativeTempError），显式给出量程更可控。
            self._sensor = GpioDHT11(
                self.pin, max_temperature=self.max_temperature_c
            )
        except Exception as exc:  # noqa: BLE001 - gpiozero 会抛各种运行时错误
            raise DeviceInitError(
                f"DHT11 初始化失败（GPIO{self.pin}，{physical_pin(self.pin)}）：{exc}。"
                "排查线索：① 裸传感器是否接了 4.7k~10k 上拉电阻；"
                "② VCC 是否错接成 5V；③ DATA 是否插在 GPIO4（物理脚 7）；"
                "④ 是否在 PC 上误用了 mock=False"
            ) from exc
        self._opened = True

    def read(self) -> AmbientSample:
        """读一次温湿度。

        行为（与模块开头的"缓存策略"一致）：

        - 距上次**真正读硬件**不足 ``min_interval_s`` → 返回 ``ok=False`` 的缓存样本，
          ``error`` 写明"还差多少秒"，温度/湿度照填（便于 LCD 显示）；
          **此时不会去碰硬件**（DHT11 硬性要求），因此也不会产生新的失败计数；
        - 真实读取失败 → 先 ``self._note_fault(exc)`` 再抛 :class:`DeviceIOError`；
        - 读数超出量程 / 是 NaN → 返回 ``ok=False`` 且**不覆盖缓存**，``error`` 说明原因。

        Returns:
            :class:`~health_monitor.hal.models.AmbientSample`。

        Raises:
            DeviceNotReady: 未 ``open()``。
            DeviceIOError: 重试 ``retries`` 次后仍然读失败（可重试错误）。
        """
        self._require_open()

        # ① 缓存策略：间隔不足就不碰硬件（DHT11 会返回陈旧数据，还可能损坏器件）
        #    ⚠️ 顺序很重要：这一判断必须在"故障注入检查"之前——它同时证明了
        #    "间隔不足时驱动压根没去读硬件"，这也是我们给集成测试留的探针。
        wait, remaining = self._cache.needs_wait()
        if wait:
            return self._cached_sample(
                f"距上次读取不足 {self.min_interval_s:.1f} 秒（还差 {remaining:.1f}s），"
                "本次返回缓存值"
            )

        if self._mock_fault is not None:
            exc = self._mock_fault
            self._mock_fault = None
            self._note_fault(exc)
            raise DeviceIOError(f"DHT11 读取失败（注入的故障）：{exc}") from exc

        # ② 真正读（mock 走合成数据，真实模式走 gpiozero，含重试）
        try:
            if self.mock:
                temperature_c, humidity_percent = self._read_mock()
            else:
                temperature_c, humidity_percent = self._read_hardware()
        except (DeviceIOError, DeviceTimeout) as exc:
            self._note_fault(exc)
            raise

        # ③ 校验（纯函数；温度上限取"用户配置"与"DHT11 量程"中更严的那个）
        reason = validate_reading(
            temperature_c,
            humidity_percent,
            temp_limits=(
                DHT11_TEMP_MIN_C,
                min(self.max_temperature_c, DHT11_TEMP_MAX_C),
            ),
        )
        # 注入值无论好坏都只生效一次：否则一次坏值会一直粘在注入槽里出不来
        # （这正是单测先发现的一个真实缺陷，记在这里防止以后又踩）
        self._consume_injected()
        if reason:
            # 无效读数**不覆盖缓存**：缓存里永远是最后一次可信的值
            self._note_ok()
            return AmbientSample(
                ts=now_ts(),
                device=self.name,
                ok=False,
                error=reason,
                temperature_c=None,
                humidity_percent=None,
            )

        # ④ 可信读数：更新缓存与"上次读取时间"
        stamp = now_ts()
        self._cached = (float(temperature_c), float(humidity_percent), stamp)
        self._cache.mark_read()
        self._note_ok()
        return AmbientSample(
            ts=stamp,
            device=self.name,
            ok=True,
            error=None,
            temperature_c=float(temperature_c),
            humidity_percent=float(humidity_percent),
        )

    def close(self) -> None:
        """释放 GPIO（**幂等且不抛异常**）。缓存保留，便于关掉后仍能显示最后环境值。"""
        if self._sensor is not None:
            try:
                self._sensor.close()
            except Exception:  # noqa: BLE001 - 关闭失败不应影响收尾
                pass
            self._sensor = None
        self._cache.reset()
        self._injected = None
        self._opened = False

    # ------------------------------------------------------------------
    # 内部：真实硬件访问
    # ------------------------------------------------------------------

    def _read_hardware(self) -> Tuple[float, float]:
        """读一次真实传感器（带重试）。

        Returns:
            ``(温度℃, 湿度%RH)``；数值可能是 NaN 或超量程，由调用方用纯函数判定。

        Raises:
            DeviceIOError: 重试 ``retries`` 次仍拿不到读数。
        """
        last_error = ""
        for attempt in range(1, self.retries + 1):
            try:
                temperature_c = float(self._sensor.temperature)
                humidity_percent = float(self._sensor.humidity)
            except Exception as exc:  # noqa: BLE001 - gpiozero 抛的异常类型很杂
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if not (math.isnan(temperature_c) or math.isnan(humidity_percent)):
                    return temperature_c, humidity_percent
                last_error = "读到 NaN（DHT11 未应答）"
            if attempt < self.retries:
                self._sleep(0.1)  # DHT11 需要 ≥1s 才能再次转换；重试留出余量

        raise DeviceIOError(
            f"DHT11（GPIO{self.pin}，{physical_pin(self.pin)}）连续 {self.retries} 次读取失败："
            f"{last_error}。排查线索：① 上拉电阻（4.7k~10k）有没有接；"
            "② 供电是否 3.3V；③ 读取间隔是否 ≥2 秒；④ 杜邦线是否过长/接触不良"
        )

    # ------------------------------------------------------------------
    # internal：mock 数据
    # ------------------------------------------------------------------

    def _read_mock(self) -> Tuple[float, float]:
        """mock 模式：合成一对温湿度（不碰硬件，**也绝不引用真实 GPIO 对象**）。"""
        if self._injected is not None:
            self._mock_inject_count += 1
            return self._injected
        return mock_environment(time.monotonic() - self._mock_opened_at)

    def _consume_injected(self) -> None:
        """mock 注入值只生效一次；之后再读就走正常的合成/缓存路径。"""
        if self.mock:
            self._injected = None

    # ------------------------------------------------------------------
    # mock 支持（集成演示用）
    # ------------------------------------------------------------------

    def inject(self, temperature_c: float, humidity_percent: float) -> None:
        """**仅供 mock / 测试**：指定接下来读到的温湿度。

        业务层演示脚本用它复现"环境过热报警"（例如 ``inject(35.0, 80.0)``）。
        真实模式下调用会抛 :class:`UnsupportedError`——真机上不该有"人造数据"。

        注意：
        1. 注入值同样要过 :func:`validate_reading` 的量程校验，
           ``inject(60.0, ...)`` 会读出 ``ok=False`` 的"超量程"样本（这是刻意的，
           正好用来演示数据非法路径）；
        2. 注入**同时解除 2 秒间隔限制**（``_cache.reset()``），这样演示脚本可以连续喂
           "26→33→35℃"看报警逐级升级；注入值只会生效一次。
        """
        if not self.mock:
            raise UnsupportedError("inject() 只能在 mock=True 时使用（防止污染真实读数）")
        self._injected = (float(temperature_c), float(humidity_percent))
        self._cache.reset()  # 注入后允许立刻读，方便演示脚本连续喂值

    def set_mock_fault(self, exc: Optional[BaseException] = None) -> None:
        """**仅供 mock / 测试**：让下一次 ``read()`` 抛出故障（默认 :class:`DeviceIOError`）。

        注入的是"读硬件失败"这一类故障；若此刻还没到 2 秒间隔，
        ``read()`` 会直接返回缓存值而**不会**产生故障（见 :meth:`read` 的说明）。
        """
        if not self.mock:
            raise UnsupportedError("set_mock_fault() 只能在 mock=True 时使用")
        self._mock_fault = exc or DeviceIOError("注入的故障：mock 的单总线读取失败")

    # ------------------------------------------------------------------
    # 内部：缓存样本
    # ------------------------------------------------------------------

    def _cached_sample(self, reason: str) -> AmbientSample:
        """把缓存值包装成 ``ok=False`` 的样本（**如实标注"这是缓存**）。"""
        self._note_ok()
        if self._cached is None:
            # 打开后第一次就"间隔不足"是不可能的（needs_wait 只有在读过后才为真），
            # 这里只是防御式兜底：如实说"没有缓存"，绝不瞎编一个室温。
            return AmbientSample(
                ts=now_ts(), device=self.name, ok=False,
                error=f"{reason}（当前还没有缓存值可用）",
                temperature_c=None, humidity_percent=None,
            )
        temperature_c, humidity_percent, stamp = self._cached
        return AmbientSample(
            ts=stamp,  # 时间戳用**上次真正测量**的时刻，不要伪装成现在
            device=self.name,
            ok=False,
            error=reason,
            temperature_c=temperature_c,
            humidity_percent=humidity_percent,
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
            "bus": "单总线 GPIO（无 I2C/SPI）",
            "pins": {
                "data": describe_pin(self.pin),
                "vcc": "3.3V（物理脚 1 或 17）—— 不要接 5V，数据电平会伤 GPIO",
                "gnd": "GND（物理脚 6/9/14/20/25/30/34/39 任一）",
            },
            "notes": (
                f"量程 温度 {DHT11_TEMP_MIN_C:.0f}~{DHT11_TEMP_MAX_C:.0f}℃ / "
                f"湿度 {DHT11_HUMI_MIN_PCT:.0f}~{DHT11_HUMI_MAX_PCT:.0f}%RH；"
                f"两次读取间隔必须 ≥{self.min_interval_s:.1f}s（间隔不足时返回 ok=False 的缓存值）；"
                f"重试 {self.retries} 次；四针裸传感器需外接 4.7k~10k 上拉电阻；"
                "读取用 gpiozero 的 DHT11 类（树莓派 5 上手工打时序易失败）"
            ),
        }

    def self_check(self) -> Dict[str, Any]:
        """自检：读一次并如实上报。

        两个刻意的设计：
        1. **不被 2 秒间隔限制挡住**——否则巡检脚本每跑一次都说"失败"。
           做法是临时让缓存策略"放行"，读完把原来的计时恢复，
           正常的"间隔不足返回缓存值"逻辑一点不受影响；
        2. **会自己 open()**（``close()`` 时再关掉）——真机上没有 gpiozero 器件对象
           根本读不了；mock 模式下也允许自检，便于无硬件时跑体检报告。
        """
        saved = self._cache.last_read_at
        was_open = self._opened
        self._cache.reset()
        try:
            if not was_open:
                self.open()
            sample = self.read()
        except Exception as exc:  # noqa: BLE001 - 自检要捕获一切并如实上报
            return {"ok": False, "detail": f"{type(exc).__name__}: {exc}", "name": self.name}
        finally:
            self._cache.reset()
            if saved is not None:
                self._cache.mark_read(saved)
            if not was_open:
                self.close()
        detail = (
            f"温度 {sample.temperature_c}℃，湿度 {sample.humidity_percent}%"
            if sample.ok
            else f"未读到有效值：{sample.error}"
        )
        return {"ok": bool(sample.ok), "detail": detail, "name": self.name}

    def __repr__(self) -> str:
        return (
            f"<Dht11 name={self.name!r} pin={self.pin} "
            f"mock={self.mock} opened={self._opened}>"
        )


__all__ = [
    "Dht11",
    "CachePolicy",
    "validate_reading",
    "mock_environment",
    "physical_pin",
    "BCM_TO_PHYSICAL_PIN",
    "DHT11_TEMP_MIN_C",
    "DHT11_TEMP_MAX_C",
    "DHT11_HUMI_MIN_PCT",
    "DHT11_HUMI_MAX_PCT",
    "DHT11_MIN_INTERVAL_S",
]
