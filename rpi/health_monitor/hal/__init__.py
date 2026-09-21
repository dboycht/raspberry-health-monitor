"""硬件抽象层（HAL）—— 团队协作的"地基"，改这里等于改所有人的接口。

导出的公共名字（其他层的代码只允许从这里 import）::

    from health_monitor.hal import Device, OutputDevice, MockBus, create_device
    from health_monitor.hal import VitalSignsSample, AlarmEvent, Severity, AlarmCode

⚠️ 不要 ``from health_monitor.hal.models import ...`` 之外的内部模块
（例如直接 import ``registry`` 里的私有函数）——那些不是契约的一部分。
"""

from .device import Device, OutputDevice
from .exceptions import (
    AlarmDispatchError,
    ConfigError,
    DataInvalidError,
    DeviceError,
    DeviceInitError,
    DeviceIOError,
    DeviceNotFoundError,
    DeviceNotReady,
    DeviceTimeout,
    HealthMonitorError,
    UnsupportedError,
)
from .mock_bus import MockBus, RealBus
from .models import (
    AlarmCode,
    AlarmEvent,
    AmbientSample,
    BeepCommand,
    ButtonAction,
    ButtonEvent,
    Command,
    CommandType,
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
    now_ts,
)
from .registry import (
    DRIVER_DOCS,
    MANIFEST,
    DriverSpec,
    create_device,
    get_spec,
    list_drivers,
    snapshot,
)
from .pins import (
    BCM_ROLE,
    BCM_TO_PHYSICAL,
    bcm_to_physical,
    describe_pin,
    find_conflicts,
    full_table,
    is_reserved,
    physical_to_bcm,
    reserved_pin_warnings,
)

__all__ = [
    # 基类
    "Device",
    "OutputDevice",
    # 总线
    "MockBus",
    "RealBus",
    # 异常
    "HealthMonitorError",
    "ConfigError",
    "DeviceError",
    "DeviceNotFoundError",
    "DeviceInitError",
    "DeviceIOError",
    "DeviceTimeout",
    "DeviceNotReady",
    "DataInvalidError",
    "UnsupportedError",
    "AlarmDispatchError",
    # 数据模型
    "now_ts",
    "DeviceKind",
    "Severity",
    "AlarmCode",
    "MotionState",
    "ButtonAction",
    "CommandType",
    "Sample",
    "VitalSignsSample",
    "AmbientSample",
    "PrecisionTempSample",
    "RangeSample",
    "MotionSample",
    "ButtonEvent",
    "DisplayStatus",
    "AlarmEvent",
    "Command",
    "SpeakCommand",
    "BeepCommand",
    "LightCommand",
    "DisplayCommand",
    # 注册表
    "DriverSpec",
    "MANIFEST",
    "DRIVER_DOCS",
    "list_drivers",
    "get_spec",
    "create_device",
    "snapshot",
    # 引脚映射（物理脚号的单一来源；驱动写 describe() 必须用它）
    "BCM_TO_PHYSICAL",
    "BCM_ROLE",
    "bcm_to_physical",
    "physical_to_bcm",
    "describe_pin",
    "is_reserved",
    "find_conflicts",
    "reserved_pin_warnings",
    "full_table",
]
