"""配置加载与校验。

配置文件：``config/devices.json``（设备与阈值），示例见 ``config/devices.example.json``。

设计原则
--------
1. **配置错误要在启动时炸掉**（``ConfigError``），不要跑起来才发现阈值反了；
2. **配置里只有"驱动名 + 参数"**，没有具体类名 —— 换器件不改代码；
3. **未启用的器件允许"参数不全"**（只是不跑），启用的器件必须能装配成功。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from ..hal.exceptions import ConfigError
from ..hal.models import DeviceKind

# --------------------------------------------------------------------------
# 默认阈值（健康监护常用经验值；报告里请写清"这些是工程默认值，非医学诊断标准"）
# --------------------------------------------------------------------------


@dataclass
class Thresholds:
    """报警阈值集合。

    ⚠️ 本项目的报警是**看护提示**，不是医疗诊断。文档与界面都不得宣称医疗结论。
    """

    # 心率（次/分）
    hr_min: float = 50.0
    hr_max: float = 110.0
    # 血氧（%）
    spo2_min: float = 93.0
    # 精密体温 / 环境温度（摄氏度）
    body_temp_min: float = 35.5
    body_temp_max: float = 37.5
    ambient_temp_min: float = 16.0
    ambient_temp_max: float = 30.0
    humidity_max: float = 80.0
    # 久无活动（秒）：超过则告警"疑似跌倒/长时间静止"
    no_motion_timeout_s: float = 1800.0
    # 夜间频繁起夜：窗口与次数
    night_start_hour: int = 22
    night_end_hour: int = 6
    night_wake_count: int = 5
    night_window_s: float = 3600.0
    # 传感器连续失败多少次后报"传感器故障"
    sensor_fault_after: int = 3
    # 报警重复抑制：同一报警码在多少秒内只报一次（防止刷屏/吵人）
    repeat_cooldown_s: float = 300.0
    # 数值回落的迟滞（防止在阈值附近反复抖动）
    hysteresis: float = 3.0

    def validate(self) -> None:
        """检查阈值自洽性。**启动期必须调用**，错误直接抛 ``ConfigError``。"""
        pairs = [
            ("hr_min", "hr_max"),
            ("body_temp_min", "body_temp_max"),
            ("ambient_temp_min", "ambient_temp_max"),
        ]
        for lo, hi in pairs:
            if getattr(self, lo) >= getattr(self, hi):
                raise ConfigError(f"阈值不合理：{lo}({getattr(self, lo)}) 必须小于 {hi}({getattr(self, hi)})")
        if not (0 < self.spo2_min <= 100):
            raise ConfigError(f"spo2_min 应在 (0, 100] 内，当前 {self.spo2_min}")
        if self.no_motion_timeout_s <= 0:
            raise ConfigError("no_motion_timeout_s 必须为正数")
        if not (0 <= self.night_start_hour <= 23 and 0 <= self.night_end_hour <= 23):
            raise ConfigError("夜间时段小时数必须在 0~23 之间")
        if self.repeat_cooldown_s < 0:
            raise ConfigError("repeat_cooldown_s 不能为负")
        if self.sensor_fault_after < 1:
            raise ConfigError("sensor_fault_after 至少为 1")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Thresholds":
        """从字典构造，**未知键一律报错**（防止拼错键静默失效）。"""
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        unknown = set(data) - known
        if unknown:
            raise ConfigError(
                f"阈值配置里有未知字段 {sorted(unknown)}；可用字段：{sorted(known)}"
                "（拼错键不会报错、只会静默失效，所以这里刻意严格）"
            )
        obj = cls(**{k: v for k, v in data.items()})
        obj.validate()
        return obj


# --------------------------------------------------------------------------
# 设备配置
# --------------------------------------------------------------------------


@dataclass
class DeviceConfig:
    """单个设备的配置项。

    Attributes:
        name: 配置里的键名（也是设备实例名），如 ``"vitals"`` / ``"ambient"``。
        driver: 驱动名（``hal.registry.MANIFEST`` 的键），如 ``"max30102"``。
        enabled: 是否启用。未启用的设备不装配、不读取。
        read_interval_s: 读取周期（秒）。默认 1 秒；DHT11 必须 ≥ 2 秒。
        params: 传给驱动构造函数的参数。
        optional: 为 True 时装配失败只记警告（用于"拓展件没接也能跑"）。
    """

    name: str
    driver: str
    enabled: bool = True
    read_interval_s: float = 1.0
    params: Dict[str, Any] = field(default_factory=dict)
    optional: bool = False

    def validate(self) -> None:
        if not self.name:
            raise ConfigError("设备配置缺少 name")
        if not self.driver:
            raise ConfigError(f"设备 {self.name} 缺少 driver")
        if self.read_interval_s <= 0:
            raise ConfigError(f"设备 {self.name} 的 read_interval_s 必须为正数")
        # DHT11 的硬件限制：两次读取间隔必须 ≥ 2 秒
        if self.driver == "dht11" and self.read_interval_s < 2.0:
            raise ConfigError(
                f"设备 {self.name}（dht11）的读取周期必须 ≥ 2 秒，当前 {self.read_interval_s}"
                "（DHT11 硬件限制，读太快会拿到陈旧值甚至读失败）"
            )


@dataclass
class AppConfig:
    """整个应用的配置。"""

    thresholds: Thresholds = field(default_factory=Thresholds)
    devices: List[DeviceConfig] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)
    #: MQTT 上云配置（可选，默认关闭）。用 ``dict`` 承载以避免 core 依赖 net 层；
    #: 需要时由 net.mqtt.MqttConfig.from_dict 解析。
    mqtt: Dict[str, Any] = field(default_factory=dict)
    #: OneNET（中国移动物联网平台）配置（可选，默认关闭）。
    #: 本项目实际采用的上云方式 = OneNET 旧版「MQTT物联网套件」数据流-数据点，
    #: 再由 OneNET 规则引擎转发回本机/App（云云对接）。
    #: 字段级解析见 ``net.onenet.OneNetConfig.from_dict``。
    onenet: Dict[str, Any] = field(default_factory=dict)

    def enabled_devices(self) -> List[DeviceConfig]:
        return [d for d in self.devices if d.enabled]

    def device(self, name: str) -> Optional[DeviceConfig]:
        for d in self.devices:
            if d.name == name:
                return d
        return None

    def by_driver(self, driver: str) -> Optional[DeviceConfig]:
        for d in self.devices:
            if d.driver == driver and d.enabled:
                return d
        return None

    def validate(self) -> None:
        self.thresholds.validate()
        seen: set = set()
        for dev in self.devices:
            dev.validate()
            if dev.name in seen:
                raise ConfigError(f"设备名重复：{dev.name}（名字必须唯一，否则状态会互相覆盖）")
            seen.add(dev.name)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AppConfig":
        known = {"thresholds", "devices", "mqtt", "onenet"}
        unknown = set(data) - known
        if unknown:
            raise ConfigError(f"配置里有未知顶层字段 {sorted(unknown)}；只支持 {sorted(known)}")
        thresholds = Thresholds.from_dict(data.get("thresholds", {}))
        raw_devices = data.get("devices", {})
        if not isinstance(raw_devices, Mapping):
            raise ConfigError("devices 必须是一个对象（设备名 -> 设备配置）")
        devices: List[DeviceConfig] = []
        for name, item in raw_devices.items():
            if not isinstance(item, Mapping):
                raise ConfigError(f"设备 {name} 的配置必须是对象")
            item_known = {"driver", "enabled", "read_interval_s", "params", "optional"}
            bad = set(item) - item_known
            if bad:
                raise ConfigError(f"设备 {name} 有未知字段 {sorted(bad)}；可用 {sorted(item_known)}")
            devices.append(
                DeviceConfig(
                    name=name,
                    driver=str(item.get("driver", "")),
                    enabled=bool(item.get("enabled", True)),
                    read_interval_s=float(item.get("read_interval_s", 1.0)),
                    params=dict(item.get("params", {})),
                    optional=bool(item.get("optional", False)),
                )
            )
        # MQTT / OneNET 配置只做"是不是对象"的粗校验；
        # 字段级校验在 net.mqtt.MqttConfig.from_dict / net.onenet.OneNetConfig.from_dict
        mqtt_raw = data.get("mqtt", {})
        if not isinstance(mqtt_raw, Mapping):
            raise ConfigError("mqtt 必须是一个对象")
        onenet_raw = data.get("onenet", {})
        if not isinstance(onenet_raw, Mapping):
            raise ConfigError("onenet 必须是一个对象")
        cfg = cls(
            thresholds=thresholds, devices=devices, raw=dict(data),
            mqtt=dict(mqtt_raw), onenet=dict(onenet_raw),
        )
        cfg.validate()
        return cfg


# --------------------------------------------------------------------------
# 读写
# --------------------------------------------------------------------------


def default_config_path() -> Path:
    """默认配置文件路径：``rpi/config/devices.json``。"""
    return Path(__file__).resolve().parents[2] / "config" / "devices.json"


def example_config_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "devices.example.json"


def load_config(path: Optional[str | Path] = None) -> AppConfig:
    """从 JSON 文件加载配置。

    Args:
        path: 配置文件路径。为 ``None`` 时用 :func:`default_config_path`；
              若该文件不存在则回退到示例配置（便于"第一次跑起来"）。

    Raises:
        ConfigError: 文件不存在、JSON 语法错误、字段未知、阈值不合理。
    """
    target = Path(path) if path else default_config_path()
    if not target.exists():
        if path is None and example_config_path().exists():
            target = example_config_path()
        else:
            raise ConfigError(
                f"配置文件不存在：{target}\n"
                f"请先复制示例：copy config\\devices.example.json config\\devices.json"
            )
    try:
        # 显式 UTF-8：本项目所有文本一律 UTF-8，绝不使用系统默认编码
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"读取配置失败：{target}：{exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件 JSON 语法错误：{target} 第 {exc.lineno} 行：{exc.msg}") from exc
    if not isinstance(data, Mapping):
        raise ConfigError(f"配置文件顶层必须是对象：{target}")
    return AppConfig.from_dict(data)


def dump_example(path: Optional[str | Path] = None) -> Path:
    """把内置的示例配置写到磁盘（``scripts/init_config.py`` 用它生成初始配置）。"""
    target = Path(path) if path else default_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


#: 内置默认配置（= ``config/devices.example.json`` 的内容，单一来源）
DEFAULT_CONFIG: Dict[str, Any] = {
    "thresholds": {
        "hr_min": 50,
        "hr_max": 110,
        "spo2_min": 93,
        "body_temp_min": 35.5,
        "body_temp_max": 37.5,
        "ambient_temp_min": 16,
        "ambient_temp_max": 30,
        "humidity_max": 80,
        "no_motion_timeout_s": 1800,
        "night_start_hour": 22,
        "night_end_hour": 6,
        "night_wake_count": 5,
        "night_window_s": 3600,
        "sensor_fault_after": 3,
        "repeat_cooldown_s": 300,
        "hysteresis": 3,
    },
    "devices": {
        "vitals": {
            "driver": "max30102",
            "enabled": True,
            "read_interval_s": 1.0,
            "params": {"bus": 1, "address": 87, "led_current": "7.6mA"},
        },
        "body_temp": {
            "driver": "tmp36",
            "enabled": True,
            "read_interval_s": 2.0,
            "params": {
                "spi_bus": 0,
                "spi_device": 0,
                "channel": 0,
                "vref": 3.3,
                "calibration_offset_c": 0.0,
            },
        },
        "ambient": {
            "driver": "dht11",
            "enabled": True,
            "read_interval_s": 3.0,
            "params": {"pin": 4},
        },
        "motion": {
            "driver": "hc_sr501",
            "enabled": True,
            "read_interval_s": 0.5,
            "params": {"pin": 17},
        },
        "display": {
            "driver": "lcd1602",
            "enabled": True,
            "read_interval_s": 1.0,
            "params": {"bus": 1, "address": 0, "cols": 16, "rows": 2},
        },
        "tft": {
            "driver": "tft_spi",
            "enabled": False,
            "optional": True,
            "read_interval_s": 2.0,
            "params": {
                "controller": "auto",
                "spi_bus": 0,
                "spi_device": 1,
                "dc_pin": 24,
                "reset_pin": 25,
                "backlight_pin": -1,
                "rotate": 90,
                "baudrate": 24000000,
            },
        },
        "speaker": {
            "driver": "bt_speaker",
            "enabled": True,
            "read_interval_s": 1.0,
            "params": {"engine": "espeak-ng", "rate": 150},
        },
        "alarm_buzzer": {
            "driver": "buzzer",
            "enabled": True,
            "read_interval_s": 1.0,
            "params": {"pin": 18},
        },
        "status_led": {
            "driver": "led",
            "enabled": True,
            "read_interval_s": 1.0,
            "params": {"pins": {"green": 22, "yellow": 23, "red": 24}},
        },
        "sos_button": {
            "driver": "button",
            "enabled": True,
            "read_interval_s": 0.2,
            "params": {"pin": 27, "long_press_s": 1.0},
        },
        "distance": {
            "driver": "hc_sr04",
            "enabled": False,
            "optional": True,
            "read_interval_s": 5.0,
            "params": {"trig_pin": 5, "echo_pin": 6},
        },
    },
    # 上云（可选，默认关闭）。填好 broker 信息并把 enabled 改 true 即可。
    # ⚠️ password 请留空并用环境变量 HEALTH_MQTT_PASSWORD，或写在自己的 devices.json 里
    #    （devices.json 已被 .gitignore 排除，不会进仓库）。
    "mqtt": {
        "enabled": False,
        "host": "",
        "port": 1883,
        "topic_prefix": "health/room1",
        "client_id": "raspi-health-monitor",
        "username": "",
        "password": "",
        "interval_s": 30.0,
        "qos": 0,
        "keepalive": 60,
    },
    # OneNET（中国移动物联网平台）—— 本项目实际使用的云平台，默认关闭。
    # 完整点击步骤见 docs/11-OneNET云端接入与云云对接.md；
    # 密钥请用环境变量 HEALTH_ONENET_KEY，不要写进仓库。
    "onenet": {
        "enabled": False,
        # 平台版本：**legacy = 旧版 MQTT物联网套件（数据流-数据点）← 本项目用这套**
        # 另一套 OneNET Studio（物模型 OneJSON）不支持，填了会明确报错并说明差异。
        "platform": "legacy",
        "product_id": "",
        "device_name": "",
        "access_key": "",
        "method": "sha256",
        "token_ttl_s": 86400,
        "host": "",
        "port": 0,
        "tls": False,
        "keepalive": 120,
        "interval_s": 30.0,
        "topic_template": "$sys/{pid}/{device}/dp/post/json",
        "subscribe_result": True,
        "qos": 1,
        "publish_alarm": True,
        "publish_status": True,
    },
}


def kind_of(driver: str) -> DeviceKind:
    """取驱动对应的器件大类（供文档生成脚本用；注册表里没有则报错）。"""
    from ..hal.registry import get_spec

    return get_spec(driver).kind


__all__ = [
    "Thresholds",
    "DeviceConfig",
    "AppConfig",
    "DEFAULT_CONFIG",
    "load_config",
    "dump_example",
    "default_config_path",
    "example_config_path",
    "kind_of",
]
