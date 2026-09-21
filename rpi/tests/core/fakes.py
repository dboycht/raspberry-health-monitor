"""测试用的假设备（Fake）集合。

⚠️ 这些 Fake **放在 tests 目录里**，不进 ``health_monitor`` 包——
产品代码绝不允许依赖测试替身。

用途：给采集调度 / 报警链路做**确定性**测试：值可设定、可注入失败、
时间可完全由测试控制，不需要真实硬件也不需要 ``time.sleep``。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from health_monitor.hal import (
    Device,
    DeviceError,
    DeviceInitError,
    DeviceKind,
    MotionSample,
    MotionState,
    PrecisionTempSample,
    RangeSample,
    VitalSignsSample,
)
from health_monitor.hal.models import AmbientSample


class _FakeBase(Device):
    """Fake 公共部分：可注入"打开失败"与"读取失败一次/一直失败"。"""

    KIND = DeviceKind.VITAL
    NAME = "fake"

    def __init__(
        self,
        bus: Any = None,
        mock: bool = True,
        name: str = "",
        open_error: Optional[Exception] = None,
    ) -> None:
        super().__init__(bus=bus, mock=mock, name=name or self.NAME)
        self.open_error = open_error
        self.fail_with: Optional[Exception] = None      # 非 None 时 read() 一直抛它
        self.fail_once: bool = False                    # 为 True 时下一次 read() 抛错然后自动复位

    def open(self) -> None:
        if self.open_error is not None:
            raise self.open_error
        self._opened = True

    def close(self) -> None:
        self._opened = False

    def _maybe_fail(self) -> None:
        if self.fail_once:
            self.fail_once = False
            raise DeviceError("注入的一次性失败")
        if self.fail_with is not None:
            raise self.fail_with


class FakeVitalSensor(_FakeBase):
    """假心率血氧传感器。"""

    KIND = DeviceKind.VITAL
    NAME = "max30102"

    def __init__(self, hr: Optional[float] = 72.0, spo2: Optional[float] = 98.0, finger: bool = True, **kw: Any) -> None:
        super().__init__(**kw)
        self.hr = hr
        self.spo2 = spo2
        self.finger = finger

    def read(self) -> VitalSignsSample:
        self._require_open()
        self._maybe_fail()
        return VitalSignsSample(
            device=self.name, heart_rate_bpm=self.hr, spo2_percent=self.spo2,
            finger_detected=self.finger, quality=1.0,
        )


class FakeAmbientSensor(_FakeBase):
    """假温湿度传感器（周期必须 ≥2s 由配置层保证）。"""

    KIND = DeviceKind.AMBIENT
    NAME = "dht11"

    def __init__(self, temperature_c: float = 26.0, humidity_percent: float = 55.0, **kw: Any) -> None:
        super().__init__(**kw)
        self.temperature_c = temperature_c
        self.humidity_percent = humidity_percent

    def read(self) -> AmbientSample:
        self._require_open()
        self._maybe_fail()
        return AmbientSample(
            device=self.name, temperature_c=self.temperature_c, humidity_percent=self.humidity_percent
        )


class FakeBodyTempSensor(_FakeBase):
    """假精密体温（TMP36 + MCP3002）。"""

    KIND = DeviceKind.PRECISION
    NAME = "tmp36"

    def __init__(self, temperature_c: float = 36.5, **kw: Any) -> None:
        super().__init__(**kw)
        self.temperature_c = temperature_c

    def read(self) -> PrecisionTempSample:
        self._require_open()
        self._maybe_fail()
        raw = int(round(self.temperature_c * 20.48 + 341.3))  # 仅用于让 raw 看起来合理
        return PrecisionTempSample(
            device=self.name, temperature_c=self.temperature_c, raw_adc=raw, voltage_v=0.75
        )


class FakeMotionSensor(_FakeBase):
    """假人体红外。"""

    KIND = DeviceKind.MOTION
    NAME = "hc_sr501"

    def __init__(self, state: MotionState = MotionState.DETECTED, silent_s: float = 0.0, **kw: Any) -> None:
        super().__init__(**kw)
        self.state = state
        self.silent_s = silent_s

    def read(self) -> MotionSample:
        self._require_open()
        self._maybe_fail()
        return MotionSample(device=self.name, state=self.state, active_for_s=self.silent_s)

    def seconds_since_motion(self) -> float:
        return self.silent_s


class FakeRangeSensor(_FakeBase):
    """假超声波（拓展件）。"""

    KIND = DeviceKind.RANGE
    NAME = "hc_sr04"

    def __init__(self, distance_cm: Optional[float] = 60.0, **kw: Any) -> None:
        super().__init__(**kw)
        self.distance_cm = distance_cm

    def read(self) -> RangeSample:
        self._require_open()
        self._maybe_fail()
        return RangeSample(device=self.name, distance_cm=self.distance_cm, echo_us=3500.0)


#: 一次性构造一整套假设备（集成测试与演示脚本用）
def build_all(mock: bool = True) -> Dict[str, Device]:
    return {
        "vitals": FakeVitalSensor(mock=mock),
        "body_temp": FakeBodyTempSensor(mock=mock),
        "ambient": FakeAmbientSensor(mock=mock),
        "motion": FakeMotionSensor(mock=mock),
    }


__all__ = [
    "FakeVitalSensor",
    "FakeAmbientSensor",
    "FakeBodyTempSensor",
    "FakeMotionSensor",
    "FakeRangeSensor",
    "build_all",
]
