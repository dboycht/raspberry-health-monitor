"""设备注册表：把"设备名"映射到"驱动实现"，并负责统一装配。

为什么要有它（团队协作的关键）
------------------------------
系统里所有"我用了哪个传感器"都只写在 ``config/devices.json`` 里，形如::

    {"vitals": {"driver": "max30102", "enabled": true, "params": {"bus": 1}}}

于是：
- **换型号不改业务代码**：把 ``"driver"`` 从 ``max30102`` 改成别的（未来支持的同
  类器件），报警引擎一行都不用动；
- **每个人只碰自己的文件**：驱动作者把自己的类加进 :data:`MANIFEST` 一行即可，
  不会与别人的代码冲突（这正是团队分工不打架的原因）。

新增一个驱动的标准流程（详见 ``docs/04-驱动开发规范与贡献指南.md``）：
1. 在 ``health_monitor/sensors/``（或 ``outputs/``）下建 ``<驱动名>.py``；
2. 类继承 :class:`~health_monitor.hal.device.Device`（输出类继承 ``OutputDevice``）；
3. 在 :data:`MANIFEST` 里加一行 ``"<驱动名>": "<模块路径>:<类名>"``；
4. 补一个 ``tests/sensors/test_<驱动名>.py``（含契约测试）；
5. 在本文件 :data:`DRIVER_DOCS` 里补上"接线/参数/负责人"三行说明。
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Type

from .device import Device
from .exceptions import ConfigError, DeviceNotFoundError
from .models import DeviceKind, Sample


@dataclass(frozen=True)
class DriverSpec:
    """一个驱动的元信息（用于装配、文档生成、自检报告）。"""

    name: str                          # 驱动名（配置里用的字符串）
    target: str                        # "模块路径:类名"
    kind: DeviceKind                   # 器件大类
    label: str                         # 中文名（给报告与界面用）
    params: tuple = ()                 # 该驱动关心的参数名（仅作文档提示）
    option: str = "核心"               # 所属题包：核心 / 温控 / 活动 / 拓展
    owner: str = ""                    # 负责人（团队分工时填名字/学号）
    notes: str = ""

    def load(self) -> Type[Device]:
        """导入并返回驱动类。导入失败会抛 :class:`DeviceNotFoundError`（含排查提示）。"""
        module_path, _, class_name = self.target.partition(":")
        if not module_path or not class_name:
            raise DeviceNotFoundError(f"驱动 {self.name} 的 target 写法不对：{self.target!r}")
        try:
            module = importlib.import_module(module_path)
        except ImportError as exc:
            raise DeviceNotFoundError(
                f"驱动 {self.name} 的模块 {module_path} 无法导入"
                f"（文件是否已创建？依赖是否已安装？）：{exc}"
            ) from exc
        try:
            cls = getattr(module, class_name)
        except AttributeError as exc:
            raise DeviceNotFoundError(
                f"模块 {module_path} 里找不到类 {class_name}"
                f"（类名拼写？是否忘了改成 Manifest 里约定的名字？）"
            ) from exc
        if not (isinstance(cls, type) and issubclass(cls, Device)):
            raise DeviceNotFoundError(
                f"{self.target} 不是 Device 的子类（驱动必须继承 hal.device.Device）"
            )
        return cls


# --------------------------------------------------------------------------
# 驱动清单：新增驱动只改这里一行
# --------------------------------------------------------------------------
# 命名规则：文件名 = 驱动名（小写，单词间用下划线），类名 = 大驼峰。
# 例如 驱动名 "max30102" -> 文件 sensors/max30102.py -> 类 Max30102

MANIFEST: Dict[str, DriverSpec] = {
    # ---- 生理 / 环境（核心） ----
    "max30102": DriverSpec(
        "max30102", "health_monitor.sensors.max30102:Max30102", DeviceKind.VITAL,
        "心率血氧传感器 MAX30102", ("bus", "address", "led_current"), "核心",
        notes="I2C 0x57；读 FIFO 后按 Spo2 算法估算心率/血氧",
    ),
    "dht11": DriverSpec(
        "dht11", "health_monitor.sensors.dht11:Dht11", DeviceKind.AMBIENT,
        "温湿度传感器 DHT11", ("pin", "retries"), "核心",
        notes="单总线；两次读取间隔必须 >= 2s（DHT11 硬件限制）",
    ),
    "tmp36": DriverSpec(
        "tmp36", "health_monitor.sensors.tmp36:Tmp36", DeviceKind.PRECISION,
        "精密温度传感器 TMP36 + MCP3002", ("spi_bus", "spi_device", "channel", "vref"), "核心",
        notes="模拟量必须经 MCP3002 ADC；10mV/°C，25°C 时 750mV",
    ),
    "hc_sr501": DriverSpec(
        "hc_sr501", "health_monitor.sensors.hc_sr501:HcSr501", DeviceKind.MOTION,
        "人体红外传感器 HC-SR501", ("pin",), "核心",
        notes="5V 供电、3.3V 电平输出，可直连 GPIO；需软件去抖",
    ),
    # ---- 拓展（可选件，默认 enabled=false） ----
    "hc_sr04": DriverSpec(
        "hc_sr04", "health_monitor.sensors.hc_sr04:HcSr04", DeviceKind.RANGE,
        "超声波测距 HC-SR04", ("trig_pin", "echo_pin", "timeout_us"), "拓展",
        notes="ECHO 为 5V，须经 TXS0102 或分压后再接 GPIO",
    ),
    "mcp3002": DriverSpec(
        "mcp3002", "health_monitor.sensors.mcp3002:Mcp3002", DeviceKind.PRECISION,
        "ADC MCP3002（SPI 模拟输入）", ("spi_bus", "spi_device", "vref"), "拓展",
        notes="被 tmp36 复用；单独使用可接电位器/光敏电阻等模拟器件",
    ),
    # ---- 输出 / 交互 ----
    "lcd1602": DriverSpec(
        "lcd1602", "health_monitor.outputs.lcd1602:Lcd1602", DeviceKind.DISPLAY,
        "LCD1602 液晶（I2C 转接板）", ("bus", "address", "cols", "rows"), "核心",
        notes="PCF8574 背包，常见地址 0x27 / 0x3F；与 MAX30102 共用 I2C 总线",
    ),
    "bt_speaker": DriverSpec(
        "bt_speaker", "health_monitor.outputs.bt_speaker:BtSpeaker", DeviceKind.AUDIO,
        "蓝牙音箱（语音播报）", ("device_name", "sink", "engine"), "核心",
        notes="用 espeak-ng 合成中文，经 bluealsa/PulseAudio 播出",
    ),
    "buzzer": DriverSpec(
        "buzzer", "health_monitor.outputs.buzzer:Buzzer", DeviceKind.AUDIO,
        "蜂鸣器", ("pin", "active_low", "gpio_chip"), "核心",
        notes="有源蜂鸣器 GPIO 直驱；无源需 PWM",
    ),
    "led": DriverSpec(
        "led", "health_monitor.outputs.led:Led", DeviceKind.LIGHT,
        "LED 状态指示（多色）", ("pins", "active_low"), "核心",
        notes="绿=正常/黄=注意/红=报警；每个 LED 串 220Ω~1kΩ 限流",
    ),
    "tft_spi": DriverSpec(
        "tft_spi", "health_monitor.outputs.tft_spi:TftSpi", DeviceKind.DISPLAY,
        "SPI TFT 彩屏（ST7735S / ST7789 / ILI9341）",
        ("controller", "spi_bus", "spi_device", "dc_pin", "reset_pin", "rotate", "bgr", "invert"),
        "核心",
        notes="课程清单里的『显示屏』；controller=auto 依次尝试三种控制器并画三色条供肉眼确认；"
              "⚠️ CS 不要与 MCP3002 抢片选（本项目 MCP3002=CE0 / TFT 默认 CE1=脚 26）",
    ),
    "button": DriverSpec(
        "button", "health_monitor.sensors.button:Button", DeviceKind.BUTTON,
        "按键（求救/消音/翻页）", ("pin", "bounce_s", "long_press_s", "pull_up"), "核心",
        notes="**参考实现**：新写驱动先照抄这个文件",
    ),
}

# 文档用：每个驱动要多写清楚的三行（负责人 / 接线 / 验收）
DRIVER_DOCS: Dict[str, Dict[str, str]] = {
    name: {"owner": spec.owner, "notes": spec.notes, "option": spec.option}
    for name, spec in MANIFEST.items()
}


# --------------------------------------------------------------------------
# 对外 API
# --------------------------------------------------------------------------


def list_drivers(kind: Optional[DeviceKind] = None, option: Optional[str] = None) -> List[DriverSpec]:
    """列出可用驱动（可按大类/题包过滤），文档与自检脚本用它。"""
    specs = list(MANIFEST.values())
    if kind is not None:
        specs = [s for s in specs if s.kind is kind]
    if option is not None:
        specs = [s for s in specs if s.option == option]
    return specs


def get_spec(name: str) -> DriverSpec:
    """按驱动名取 :class:`DriverSpec`。"""
    try:
        return MANIFEST[name]
    except KeyError as exc:
        raise DeviceNotFoundError(
            f"未知驱动 {name!r}；可用：{', '.join(sorted(MANIFEST))}"
        ) from exc


def create_device(
    driver: str,
    params: Optional[Mapping[str, Any]] = None,
    mock: bool = True,
    name: str = "",
    bus: Any = None,
) -> Device:
    """装配一个设备实例。

    Args:
        driver: 驱动名（:data:`MANIFEST` 的键）。
        params: 驱动构造参数（来自 ``config/devices.json`` 的 ``params``）。
        mock: 是否模拟模式。**默认 True**——在 PC 上开发时不要接硬件。
        name: 设备实例名（默认用驱动名）。
        bus: 显式指定总线（测试注入 MockBus 用）。为 ``None`` 时由本函数决定：
             ``mock=True`` 自动建一个 :class:`MockBus`，否则传 ``None``
             让驱动自己打开 :class:`RealBus`。

    Returns:
        已构造但**未 open()** 的设备实例（由调用方决定何时 open）。
    """
    spec = get_spec(driver)
    cls = spec.load()

    merged: Dict[str, Any] = dict(params or {})
    merged.setdefault("name", name or driver)
    merged["mock"] = bool(mock)

    if bus is None and mock:
        from .mock_bus import MockBus

        bus = MockBus()
    if bus is not None:
        merged["bus"] = bus

    try:
        instance = cls(**merged)
    except TypeError as exc:
        raise ConfigError(
            f"驱动 {driver} 不接受这些参数 {sorted(merged)}：{exc}"
            "（请对照 docs/02-接口规格说明书.md 里的参数表）"
        ) from exc

    if not isinstance(instance, Device):
        raise DeviceNotFoundError(f"{spec.target} 实例不是 Device 子类")
    return instance


def snapshot(mock: bool = True) -> Dict[str, Any]:
    """收集所有驱动的自检结果，输出"体检报告"（给 ``scripts/selfcheck.py`` 用）。

    Args:
        mock: True 时用模拟总线自检（检验代码链路），False 时访问真实硬件。
    """
    report: Dict[str, Any] = {"mock": mock, "devices": {}}
    for name, spec in sorted(MANIFEST.items()):
        entry: Dict[str, Any] = {"kind": spec.kind.value, "target": spec.target}
        try:
            device = create_device(name, mock=mock)
            entry["open"] = device.open() is None
            entry["self_check"] = device.self_check()
            entry["status"] = device.status()
            device.close()
            entry["loaded"] = True
        except Exception as exc:  # noqa: BLE001 - 体检就是要"把问题全列出来"
            entry["loaded"] = False
            entry["error"] = f"{type(exc).__name__}: {exc}"
        report["devices"][name] = entry
    return report


__all__ = [
    "DriverSpec",
    "MANIFEST",
    "DRIVER_DOCS",
    "list_drivers",
    "get_spec",
    "create_device",
    "snapshot",
]
