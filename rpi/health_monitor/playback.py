"""演示与"驱动缺席时的模拟数据源" —— 让项目在**没有任何硬件**时也能完整跑起来。

这个模块承担两件事（都很重要）：

1. **给同学做开发时的"假器件"**：某一个驱动还没写好（或没接上），
   用一个返回物理合理数据的模拟实现顶上，其它同学的模块照样能联调——
   这对"团队分工、进度不一"的场景是刚需。
   :class:`FallbackFactory` 正是干这个：**先试真驱动，失败才退回模拟**，
   并且**从不隐藏失败**（退回原因写在设备状态里，`selfcheck` / `status` 都能看到）。

2. **给答辩演示的脚本化回放**：:class:`PlaybackRuntime` + ``demo`` 命令，
   用确定性剧本把"正常 → 心率异常 → 血氧过低 → 久无活动 → 求救 → 恢复 → 传感器故障"
   整条链路演一遍，且走的是**与真机相同的业务代码**（同一套 RuleEngine/Dispatcher/Runtime）。

⚠️ 纪律：模拟数据必须**物理合理**（心率 60~90 波动、体温 36.5 附近），
绝不允许返回"永远正常"的假数据去掩盖真实故障——所以模拟器里同样支持注入失败。
"""

from __future__ import annotations

import math
import random
import time
from typing import Any, Callable, Dict, List, Optional

from .hal.device import Device, OutputDevice
from .hal.exceptions import DeviceError, UnsupportedError
from .hal.models import (
    AmbientSample,
    BeepCommand,
    ButtonAction,
    ButtonEvent,
    DeviceKind,
    DisplayCommand,
    DisplayStatus,
    LightCommand,
    MotionSample,
    MotionState,
    PrecisionTempSample,
    RangeSample,
    Sample,
    Severity,
    SpeakCommand,
    VitalSignsSample,
)
from .service import Runtime


# ==========================================================================
# 一、模拟"输入"器件（返回物理合理的数据，可注入失败）
# ==========================================================================


class _SimBase(Device):
    """模拟器件基类：不碰任何硬件，值可被脚本直接改写。"""

    NAME = "sim"

    def __init__(self, bus: Any = None, mock: bool = True, name: str = "", **_: Any) -> None:
        super().__init__(bus=bus, mock=True, name=name or self.NAME)
        self.fail_with: Optional[Exception] = None
        self.fail_once: bool = False
        self._rng = random.Random(20260921)   # 固定种子：演示可复现
        self._t0 = time.time()
        self.reason = "模拟数据源"              # 为什么退回了模拟（给同学看的原因）

    def open(self) -> None:
        self._opened = True
        self._t0 = time.time()

    def close(self) -> None:
        self._opened = False

    def _maybe_fail(self) -> None:
        if self.fail_once:
            self.fail_once = False
            raise DeviceError("模拟器件：注入的一次性失败")
        if self.fail_with is not None:
            raise self.fail_with

    def describe(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.KIND.value,
            "mock": True,
            "bus": "模拟（无硬件）",
            "pins": {},
            "notes": f"模拟数据源；原因：{self.reason}",
        }


class SimVitals(_SimBase):
    """模拟 MAX30102 心率血氧。

    ⚠️ 语义（2026-09-21 修正）：**显式设定过的心率/血氧会被固定住**，只叠加很小的噪声。
    早前版本无论设成什么值都会按时间正弦"飘回去"，导致剧本写了 `set_vitals(hr=128)`
    却仍然显示 72 —— 那是**自欺**：演示看起来"没反应"，排查半天才发现是模拟器把值盖了。
    """

    KIND = DeviceKind.VITAL
    NAME = "max30102"

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.hr: Optional[float] = 72.0
        self.spo2: Optional[float] = 98.0
        self.finger: bool = True
        self._hr_pinned: bool = True     # 初始值即"已设定"，默认保持 72
        self._spo2_pinned: bool = True

    def read(self) -> VitalSignsSample:
        self._require_open()
        self._maybe_fail()
        if not self.finger:
            # 未贴合手指时**必须**是 None（不能编一个数出来）
            return VitalSignsSample(device=self.name, heart_rate_bpm=None, spo2_percent=None, finger_detected=False)
        t = time.time() - self._t0
        if self.hr is not None:
            hr = self.hr
        else:
            hr = 72.0 + 4.0 * math.sin(t / 7.0)      # 未指定时给"缓慢波动"的合成值
        if self.spo2 is not None:
            spo2 = self.spo2
        else:
            spo2 = 98.0 - 0.3 * abs(math.sin(t / 9.0))
        return VitalSignsSample(
            device=self.name,
            heart_rate_bpm=round(hr + self._rng.uniform(-0.8, 0.8), 1),
            spo2_percent=round(min(100.0, spo2 + self._rng.uniform(-0.3, 0.3)), 1),
            finger_detected=True,
            quality=0.95,
        )


class SimBodyTemp(_SimBase):
    """模拟 TMP36 + MCP3002：36.5°C 附近波动，并给出 ADC 原始值与电压。"""

    KIND = DeviceKind.PRECISION
    NAME = "tmp36"

    def __init__(self, temperature_c: float = 36.5, vref: float = 3.3, **kw: Any) -> None:
        super().__init__(**kw)
        self.temperature_c = temperature_c
        self.vref = vref

    def read(self) -> PrecisionTempSample:
        self._require_open()
        self._maybe_fail()
        # 设定值即固定值（只叠加极小噪声）——剧本要能可靠地控制读数
        temp = self.temperature_c + self._rng.uniform(-0.05, 0.05)
        voltage = 0.75 + (temp - 25.0) * 0.010        # TMP36：10mV/°C，25°C 时 750mV
        raw = int(round(voltage / self.vref * 1023.0))
        return PrecisionTempSample(
            device=self.name, temperature_c=round(temp, 2),
            raw_adc=max(0, min(1023, raw)), voltage_v=round(voltage, 4),
        )


class SimAmbient(_SimBase):
    """模拟 DHT11：24~27°C、50~60%RH。"""

    KIND = DeviceKind.AMBIENT
    NAME = "dht11"

    def __init__(self, temperature_c: float = 24.5, humidity_percent: float = 55.0, **kw: Any) -> None:
        super().__init__(**kw)
        self.temperature_c = temperature_c
        self.humidity_percent = humidity_percent

    def read(self) -> AmbientSample:
        self._require_open()
        self._maybe_fail()
        # 同上：设定值即固定值（只叠加极小噪声），剧本可控
        return AmbientSample(
            device=self.name,
            temperature_c=round(self.temperature_c + self._rng.uniform(-0.1, 0.1), 1),
            humidity_percent=round(self.humidity_percent + self._rng.uniform(-0.5, 0.5), 1),
        )


class SimMotion(_SimBase):
    """模拟 HC-SR501：可设定"有人/无人"与"已静默多少秒"。"""

    KIND = DeviceKind.MOTION
    NAME = "hc_sr501"

    def __init__(self, state: MotionState = MotionState.DETECTED, silent_s: float = 0.0, **kw: Any) -> None:
        super().__init__(**kw)
        self.state = state
        self.silent_s = silent_s
        self._last_seen = time.time()

    def read(self) -> MotionSample:
        self._require_open()
        self._maybe_fail()
        if self.state is MotionState.DETECTED:
            self._last_seen = time.time()
            self.silent_s = 0.0
        return MotionSample(device=self.name, state=self.state, active_for_s=self.silent_s)

    def seconds_since_motion(self) -> float:
        """距上次检测到人的秒数（供规则引擎判"久无活动"）。"""
        if self.state is MotionState.DETECTED:
            return 0.0
        return max(self.silent_s, time.time() - self._last_seen)


class SimRange(_SimBase):
    """模拟 HC-SR04：默认 60cm 波动。"""

    KIND = DeviceKind.RANGE
    NAME = "hc_sr04"

    def __init__(self, distance_cm: float = 60.0, **kw: Any) -> None:
        super().__init__(**kw)
        self.distance_cm = distance_cm

    def read(self) -> RangeSample:
        self._require_open()
        self._maybe_fail()
        t = time.time() - self._t0
        cm = self.distance_cm + 3.0 * math.sin(t / 5.0)
        return RangeSample(device=self.name, distance_cm=round(cm, 1), echo_us=round(cm * 58.0, 1))


class SimMCP3002(_SimBase):
    """模拟 MCP3002：返回正弦变化的 ADC 原始值，供拓展功能使用。"""

    KIND = DeviceKind.PRECISION
    NAME = "mcp3002"

    def __init__(self, channel: int = 0, vref: float = 3.3, **kw: Any) -> None:
        super().__init__(**kw)
        self.channel = channel
        self.vref = vref

    def read_raw(self, channel: int = 0) -> int:
        self._require_open()
        self._maybe_fail()
        t = time.time() - self._t0
        return int(round((0.5 + 0.4 * math.sin(t / 8.0 + channel)) * 1023))

    def read(self) -> PrecisionTempSample:
        raw = self.read_raw(self.channel)
        return PrecisionTempSample(
            device=self.name, raw_adc=raw, voltage_v=round(raw / 1023.0 * self.vref, 4),
        )


class SimButton(_SimBase):
    """模拟按键：用 :meth:`press` 注入事件（演示"按下求救键"）。"""

    KIND = DeviceKind.BUTTON
    NAME = "button"

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self._queue: List[ButtonEvent] = []

    def press(self, action: ButtonAction = ButtonAction.CLICK) -> None:
        self._queue.append(ButtonEvent(device=self.name, action=action, pressed_for_s=0.0))

    def read(self) -> ButtonEvent:
        self._require_open()
        self._maybe_fail()
        if self._queue:
            return self._queue.pop(0)
        return ButtonEvent(device=self.name, action=ButtonAction.NONE)


# ==========================================================================
# 二、模拟"输出"器件（默认打到控制台，便于演示与无人值守测试）
# ==========================================================================


class ConsoleOutput(OutputDevice):
    """输出类模拟器件基类：把动作记录在内存里，**可选**打印到控制台。"""

    KIND = DeviceKind.DISPLAY
    NAME = "console"
    PREFIX = "[输出]"

    def __init__(self, verbose: bool = False, bus: Any = None, mock: bool = True, name: str = "", **_: Any) -> None:
        super().__init__(bus=bus, mock=True, name=name or self.NAME)
        self.verbose = verbose
        self.history: List[Any] = []

    def open(self) -> None:
        self._opened = True

    def close(self) -> None:
        self._opened = False

    def _record(self, text: str, payload: Any) -> None:
        self._require_open()
        self.history.append(payload)
        self.history = self.history[-200:]
        if self.verbose:
            print(f"{self.PREFIX} {text}")

    def read(self) -> Sample:
        self._require_open()
        self._note_ok()
        return DisplayStatus(device=self.name)

    def describe(self) -> Dict[str, Any]:
        return {
            "name": self.name, "kind": self.KIND.value, "mock": True,
            "bus": "模拟（控制台）", "pins": {}, "notes": "演示用输出器件",
        }


class ConsoleLcd(ConsoleOutput):
    """模拟 LCD1602：记录当前两行文本。"""

    KIND = DeviceKind.DISPLAY
    NAME = "lcd1602"
    PREFIX = "[LCD]"

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.current_lines = ("", "")
        self.page = 0

    def send(self, command: Any) -> None:
        if not isinstance(command, DisplayCommand):
            raise UnsupportedError(f"LCD 只接受 DisplayCommand，收到 {type(command).__name__}")
        # 与真实 LCD 一致：按 16 列截断，第二行补空格覆盖残留
        line1 = str(command.lines[0])[:16].ljust(16)
        line2 = str(command.lines[1])[:16].ljust(16) if len(command.lines) > 1 else " " * 16
        self.current_lines = (line1, line2)
        self.page = command.page
        self._record(f"第1行='{line1.strip()}' 第2行='{line2.strip()}'", command)

    def read(self) -> DisplayStatus:
        self._require_open()
        self._note_ok()
        return DisplayStatus(device=self.name, lines=self.current_lines, page=self.page)


class ConsoleSpeaker(ConsoleOutput):
    """模拟蓝牙音箱：记录播报文本（默认**不真的说话**）。"""

    KIND = DeviceKind.AUDIO
    NAME = "bt_speaker"
    PREFIX = "[语音]"

    def send(self, command: Any) -> None:
        if not isinstance(command, SpeakCommand):
            raise UnsupportedError(f"音箱只接受 SpeakCommand，收到 {type(command).__name__}")
        self._record(f"播报：{command.text}", command)

    @property
    def spoken(self) -> List[str]:
        return [c.text for c in self.history if isinstance(c, SpeakCommand)]


class ConsoleBuzzer(ConsoleOutput):
    """模拟蜂鸣器：记录鸣叫次数。"""

    KIND = DeviceKind.AUDIO
    NAME = "buzzer"
    PREFIX = "[蜂鸣]"

    def send(self, command: Any) -> None:
        if not isinstance(command, BeepCommand):
            raise UnsupportedError(f"蜂鸣器只接受 BeepCommand，收到 {type(command).__name__}")
        self._record(f"鸣叫 {command.times} 次（响 {command.on_ms}ms / 停 {command.off_ms}ms）", command)

    @property
    def total_beeps(self) -> int:
        return sum(c.times for c in self.history if isinstance(c, BeepCommand))


class ConsoleLed(ConsoleOutput):
    """模拟多色 LED：记录当前颜色（与真实驱动一样"先熄其它色"）。"""

    KIND = DeviceKind.LIGHT
    NAME = "led"
    PREFIX = "[LED]"

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.current_color = "off"

    def send(self, command: Any) -> None:
        if not isinstance(command, LightCommand):
            raise UnsupportedError(f"LED 只接受 LightCommand，收到 {type(command).__name__}")
        self.current_color = command.color
        self._record(f"颜色={command.color}" + ("（闪烁）" if command.blink else ""), command)


# ==========================================================================
# 三、驱动缺席时的"退回模拟"工厂
# ==========================================================================

#: 驱动 → 传输方式。用于决定"模拟实现需要伪造哪条总线的应答"。
_DRIVER_TRANSPORT: Dict[str, str] = {
    "max30102": "i2c",
    "dht11": "onewire",
    "tmp36": "spi",
    "mcp3002": "spi",
    "hc_sr501": "gpio",
    "hc_sr04": "gpio",
    "button": "gpio",
    "lcd1602": "i2c",
    "bt_speaker": "audio",
    "buzzer": "gpio",
    "led": "gpio",
}

_SIM_INPUTS: Dict[str, type] = {
    "max30102": SimVitals,
    "tmp36": SimBodyTemp,
    "dht11": SimAmbient,
    "hc_sr501": SimMotion,
    "hc_sr04": SimRange,
    "mcp3002": SimMCP3002,
    "button": SimButton,
}

_SIM_OUTPUTS: Dict[str, type] = {
    "lcd1602": ConsoleLcd,
    "bt_speaker": ConsoleSpeaker,
    "buzzer": ConsoleBuzzer,
    "led": ConsoleLed,
}


class FallbackFactory:
    """设备工厂：**先试真驱动，失败就退回模拟**，并如实记录原因。

    Args:
        verbose: 模拟输出器件是否打印到控制台（演示时 True，自检时 False）。
        allow_fallback: 为 False 时**绝不退回**（用于 ``selfcheck``——
            体检必须暴露"驱动没实现"，而不是用模拟数据糊过去）。
        prefer_sim: 直接使用模拟实现的驱动名集合。回放/演示用它来保证**数据完全可控**：
            真驱动的数值由总线字节决定，剧本无法直接改写（详见
            ``docs`` 里"回放模式为什么要绕开真驱动"的说明）。

    ⚠️ 这是本项目"防自欺"的关键开关：体检默认不允许退回模拟，
    否则一个没写好的驱动会被模拟数据掩盖成"一切正常"。
    """

    def __init__(self, verbose: bool = False, allow_fallback: bool = True, prefer_sim: Optional[set] = None) -> None:
        self.verbose = verbose
        self.allow_fallback = allow_fallback
        self.prefer_sim = set(prefer_sim or ())
        self.fallbacks: Dict[str, str] = {}   # 设备名 -> 退回/选用模拟的原因

    def __call__(self, driver: str, params: Optional[Dict[str, Any]] = None, mock: bool = True, name: str = "") -> Device:
        params = dict(params or {})
        name = name or driver
        if driver in self.prefer_sim:
            device = self._make_sim(driver, params, name)
            if device is not None:
                device.reason = "回放模式：按剧本使用可控的模拟数据源"
                self.fallbacks[name] = device.reason
                return device
        try:
            from .hal.registry import create_device

            return create_device(driver, params=params, mock=mock, name=name)
        except Exception as exc:  # noqa: BLE001 - 装配失败才退回；成功时这里不会执行
            if not self.allow_fallback:
                raise
            reason = f"{type(exc).__name__}: {exc}"
            device = self._make_sim(driver, params, name)
            if device is None:
                raise
            device.reason = f"真驱动不可用（{reason}），已退回模拟数据源"
            self.fallbacks[name] = device.reason
            return device

    def _make_sim(self, driver: str, params: Dict[str, Any], name: str) -> Optional[Device]:
        if driver in _SIM_OUTPUTS:
            return _SIM_OUTPUTS[driver](verbose=self.verbose, name=name)
        cls = _SIM_INPUTS.get(driver)
        if cls is None:
            return None
        safe = {k: v for k, v in params.items() if k in ("pin", "channel", "vref", "temperature_c", "humidity_percent")}
        return cls(name=name, **safe)


# ==========================================================================
# 四、可回放的运行时（演示剧本用）
# ==========================================================================


def _set_or_inject(device: Any, field: str, value: Any) -> bool:
    """给设备设定一个值：优先走驱动自带的注入接口，其次直接改可写字段。

    Returns:
        ``True`` 表示**确实设置成功**；``False`` 表示既没有注入接口、字段也不可写
        （调用方应当把这件事说出来，而不是假装改成功了——"假成功"是本项目明令禁止的）。
    """
    injector = getattr(device, f"inject_{field}", None)
    if callable(injector):
        try:
            injector(value)
            return True
        except Exception:  # noqa: BLE001 - 注入接口不适用就退回直接改字段
            pass
    for target in (field, f"_{field}"):
        try:
            setattr(device, target, value)
            return True
        except AttributeError:
            continue
    return False


#: 回放模式默认使用的模拟驱动（数据完全可控，剧本能直接改值）
#: 之所以不"先试真驱动"：真驱动的数值由总线字节决定，剧本改不动它，
#: 会让演示出现"我设了 128 bpm 但屏幕还是 72"这种自欺现象。
_PLAYBACK_SIM_DRIVERS = {
    "max30102", "tmp36", "dht11", "hc_sr501", "hc_sr04", "mcp3002", "button",
    "lcd1602", "bt_speaker", "buzzer", "led",
}


class PlaybackRuntime(Runtime):
    """用模拟设备构建的运行时，并持有它们的引用以便剧本直接改值。

    与 :class:`Runtime` 的唯一区别就是 **设备来源**（``device_factory``）；
    采集、规则、下发、HTTP、落库全部沿用同一套代码——
    所以"演示通过"与"真机运行"之间没有第二套逻辑。
    """

    def __init__(self, *args: Any, verbose_outputs: bool = True, force_real: bool = False, **kwargs: Any) -> None:
        prefer = set() if force_real else set(_PLAYBACK_SIM_DRIVERS)
        factory = FallbackFactory(verbose=verbose_outputs, allow_fallback=True, prefer_sim=prefer)
        kwargs.setdefault("mock", True)
        kwargs.setdefault("store_path", "")
        super().__init__(*args, device_factory=factory, **kwargs)
        self.factory = factory
        #: 剧本改值失败等"脚本自身的警告"（不静默：见 :func:`_set_or_inject`）
        self.script_warnings: List[str] = []

    # -- 拿到具体模拟器件（剧本用；取不到返回 None，剧本要自己能判断） --

    def sim(self, name: str) -> Any:
        return self.devices.get(name)

    # -- 剧本常用动作 --

    def set_vitals(self, heart_rate: Optional[float] = None, spo2: Optional[float] = None, finger: Optional[bool] = None) -> None:
        dev = self.devices.get("vitals")
        if dev is None:
            return
        if heart_rate is not None:
            _set_or_inject(dev, "hr", heart_rate)
        if spo2 is not None:
            _set_or_inject(dev, "spo2", spo2)
        if finger is not None:
            _set_or_inject(dev, "finger", finger)

    def set_body_temp(self, temperature_c: float) -> None:
        """设定体温（体温通道）。

        ⚠️ 真实驱动把 ``temperature_c`` 做成**只读属性**，并在内部把"要测的温度"
        换算成电压注入内层 ADC。回放模式因此**默认使用 :class:`SimBodyTemp`**
        （见 :class:`FallbackFactory` 的 ``prefer_sim``），避免与驱动的私有字段耦合；
        真驱动在场时这里退回"直接改可写字段"，改不动就如实提示（不静默失败）。
        """
        dev = self.devices.get("body_temp")
        if dev is None:
            return
        if _set_or_inject(dev, "temperature_c", temperature_c):
            return
        self.script_warnings.append(
            f"体温脚本未能改值：{type(dev).__name__} 的 temperature_c 只读且没有可用的注入接口"
        )

    def set_ambient(self, temperature_c: Optional[float] = None, humidity_percent: Optional[float] = None) -> None:
        dev = self.devices.get("ambient")
        if dev is None:
            return
        if temperature_c is not None:
            _set_or_inject(dev, "temperature_c", temperature_c)
        if humidity_percent is not None:
            _set_or_inject(dev, "humidity_percent", humidity_percent)

    def set_motion(self, state: MotionState, silent_s: float = 0.0) -> None:
        """切换"有人/无人"与其静默秒数。

        ⚠️ 真实驱动可能把 ``state`` 做成**只读属性**（内部自己按 GPIO 电平维护），
        因此这里用 ``hasattr`` 探测而不是直接赋值——剧本脚本必须能在
        "真驱动在场"与"退回模拟"两种情况下都工作。
        """
        dev = self.devices.get("motion")
        if dev is None:
            return
        if hasattr(type(dev), "state") and isinstance(getattr(type(dev), "state", None), property):
            setter = getattr(type(dev), "state").fset
            if setter is not None:
                dev.state = state  # type: ignore[misc] - 驱动提供了 setter
            else:
                self._poke_motion(dev, state, silent_s)
        else:
            try:
                dev.state = state
            except AttributeError:
                self._poke_motion(dev, state, silent_s)
        if hasattr(dev, "silent_s"):
            try:
                dev.silent_s = silent_s
            except AttributeError:
                pass

    @staticmethod
    def _poke_motion(dev: Any, state: MotionState, silent_s: float) -> None:
        """驱动把状态做成只读时，改它内部的私有字段（演示脚本的兜底手段）。"""
        for attr in ("_state", "_motion_state"):
            if hasattr(dev, attr):
                setattr(dev, attr, state)
                break
        for attr in ("_last_seen_ts", "_last_motion_ts", "_last_detected_ts"):
            if hasattr(dev, attr) and silent_s:
                import time as _time

                setattr(dev, attr, _time.time() - silent_s)
                break

    def break_sensor(self, name: str, error: str = "模拟传感器掉线") -> None:
        dev = self.devices.get(name)
        if dev is not None:
            setattr(dev, "fail_with", DeviceError(error))

    def fix_sensor(self, name: str) -> None:
        dev = self.devices.get(name)
        if dev is not None:
            setattr(dev, "fail_with", None)


__all__ = [
    "SimVitals",
    "SimBodyTemp",
    "SimAmbient",
    "SimMotion",
    "SimRange",
    "SimMCP3002",
    "SimButton",
    "ConsoleLcd",
    "ConsoleSpeaker",
    "ConsoleBuzzer",
    "ConsoleLed",
    "FallbackFactory",
    "PlaybackRuntime",
]
