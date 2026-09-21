"""硬件抽象层（HAL）统一异常定义。

写驱动时请**只抛这里的异常**，不要抛裸 ``OSError`` / ``Exception``——
上层（报警引擎、Web 服务、日志）依赖这些类型来决定"是重试、是降级、还是直接报错"。

异常层级::

    HealthMonitorError
    |-- ConfigError            配置文件/参数不合法（启动即失败，不要重试）
    |-- DeviceError            驱动相关错误的基类
    |   |-- DeviceNotFoundError    设备或驱动名不存在（装配期错误）
    |   |-- DeviceInitError        初始化失败（接线错/I2C 地址不对/权限不足）
    |   |-- DeviceIOError          读写失败（总线抖动/设备掉线，**可重试**）
    |   |-- DeviceTimeout          设备迟迟不响应（**可重试**，通常是接线松动）
    |   |-- DeviceNotReady         设备尚未初始化就被使用（编程错误）
    |   |-- DataInvalidError       读回来的数据超出物理可能范围（判为坏点）
    |   `-- UnsupportedError       本平台/本型号不支持该操作
    `-- AlarmDispatchError     报警下发失败（蜂鸣器/音箱/网络）
"""

from __future__ import annotations


class HealthMonitorError(Exception):
    """本项目所有自定义异常的基类。"""


class ConfigError(HealthMonitorError):
    """配置错误：参数缺失、类型不对、取值越界。属于启动期错误。"""


class DeviceError(HealthMonitorError):
    """设备（驱动）相关错误的基类。"""


class DeviceNotFoundError(DeviceError):
    """找不到设备名或驱动实现。"""


class DeviceInitError(DeviceError):
    """设备初始化失败（接线、地址、权限、内核模块未加载等）。"""


class DeviceIOError(DeviceError):
    """设备读写失败。上层可重试（掉线、总线冲突、瞬时错误）。"""


class DeviceTimeout(DeviceIOError):
    """设备响应超时。继承 IOError 是刻意的：上层重试策略可以只抓 IOError。"""


class DeviceNotReady(DeviceError):
    """设备未初始化就被访问。属于编程错误，不应被重试。"""


class DataInvalidError(DeviceError):
    """数据不合法（超量程、校验失败、物理上不可能）。"""


class UnsupportedError(DeviceError):
    """当前平台或型号不支持该操作（例如在 Windows 上访问 /dev/i2c-1）。"""


class AlarmDispatchError(HealthMonitorError):
    """报警下发失败（蜂鸣器、音箱、MQTT 等）。"""


__all__ = [
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
]
