"""HAL 数据模型：所有驱动与业务层之间的**唯一数据契约**。

凡是驱动读到的数据，都必须包装成这里的某个 ``Sample`` 子类返回；
凡是业务层下发的指令，都必须用 ``Command``；
凡是报警，都必须用 ``AlarmEvent``。

⚠️ 修改本文件 = 修改所有人的接口契约。改之前先看
``docs/02-接口规格说明书.md``，并同步更新 ``docs/CHANGELOG-接口.md``。

所有时间戳统一为 **Unix epoch 秒（float，UTC）**，由 :func:`now_ts` 提供，
不要在驱动里用 ``time.time()`` 之外的时钟（也**不要**用本地时间字符串）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional, Tuple

# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------


def now_ts() -> float:
    """返回当前 Unix 时间戳（秒，float）。全项目唯一的时间来源。"""
    return time.time()


# --------------------------------------------------------------------------
# 枚举
# --------------------------------------------------------------------------


class DeviceKind(str, Enum):
    """器件大类。用于自动发现、配置分组与文档生成。"""

    VITAL = "vital"          # 生理：心率/血氧
    AMBIENT = "ambient"      # 环境：温湿度
    PRECISION = "precision"  # 精密测温（模拟量 + ADC）
    MOTION = "motion"        # 人体活动
    RANGE = "range"          # 距离
    DISPLAY = "display"      # 显示
    AUDIO = "audio"          # 声音
    LIGHT = "light"          # 灯光指示
    BUTTON = "button"        # 按键


class Severity(int, Enum):
    """报警等级。数值越大越严重，可直接比较：``sev >= Severity.WARNING``。"""

    NORMAL = 0    # 正常，无需动作
    NOTICE = 1    # 提示（LCD/LED 提示，不响铃）
    WARNING = 2   # 警告（LED 黄/红 + 蜂鸣器间歇 + 语音播报）
    CRITICAL = 3  # 紧急（蜂鸣器长鸣 + 语音 + 立即推手机）


class AlarmCode(str, Enum):
    """报警/事件类型码。规则引擎与安卓端都按这些字符串判定，**不要随意改名**。

    新增类型时：在本枚举加成员 + 同步 ``docs/03-报警规则表.md`` + 同步安卓端文案表。
    """

    HR_TOO_HIGH = "hr_too_high"            # 心率过高
    HR_TOO_LOW = "hr_too_low"              # 心率过低
    SPO2_TOO_LOW = "spo2_too_low"          # 血氧过低
    BODY_TEMP_HIGH = "body_temp_high"      # 体温偏高
    BODY_TEMP_LOW = "body_temp_low"        # 体温偏低
    AMBIENT_TEMP_HIGH = "ambient_temp_high"  # 环境温度过高
    AMBIENT_TEMP_LOW = "ambient_temp_low"    # 环境温度过低
    HUMIDITY_HIGH = "humidity_high"        # 湿度过高
    NO_MOTION_TOO_LONG = "no_motion_too_long"  # 长时间无活动（疑似跌倒/昏迷）
    NIGHT_FREQUENT_WAKE = "night_frequent_wake"  # 夜间起夜过于频繁
    SOS_PRESSED = "sos_pressed"            # 用户按下求救按钮
    SENSOR_FAULT = "sensor_fault"          # 传感器故障
    DEVICE_OFFLINE = "device_offline"      # 设备离线
    SYSTEM_START = "system_start"          # 系统启动（信息类）
    ALL_CLEAR = "all_clear"                # 恢复正常（解除报警）


class MotionState(str, Enum):
    """人体红外（PIR）状态。"""

    IDLE = "idle"          # 无人
    DETECTED = "detected"  # 检测到人
    UNKNOWN = "unknown"    # 读取失败/未就绪（**诚实上报，不要假装 IDLE**）


class ButtonAction(str, Enum):
    """按键动作类型。"""

    NONE = "none"            # 本次轮询没有任何事件（**不要用 RELEASE 冒充"无事件"**）
    PRESS = "press"          # 按下（沿）
    RELEASE = "release"      # 抬起
    LONG_PRESS = "long_press"  # 长按
    CLICK = "click"          # 短按一次（驱动层去抖后判定）


class CommandType(str, Enum):
    """业务层 → 输出器件 的指令类型。"""

    DISPLAY_PAGE = "display_page"  # 显示翻页
    DISPLAY_TEXT = "display_text"  # 显示任意两行文本
    SPEAK = "speak"                # 语音播报
    BEEP = "beep"                  # 蜂鸣器鸣叫
    LIGHT = "light"                # LED 状态
    SILENCE = "silence"            # 消音（本轮报警静音）


# --------------------------------------------------------------------------
# 采样数据（驱动 → 业务层）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Sample:
    """所有采样值的基类。

    Attributes:
        ts: 采样时间（Unix 秒，UTC）。
        device: 产生该数据的设备名（与注册表里的名字一致）。
        ok: 本次采样是否有效。``False`` 时 ``error`` 必须说明原因，其余字段可为空。
        error: 失败原因（``ok=False`` 时必填）。
    """

    ts: float = field(default_factory=now_ts)
    device: str = ""
    ok: bool = True
    error: Optional[str] = None


@dataclass(frozen=True)
class VitalSignsSample(Sample):
    """心率血氧（MAX30102）。

    Attributes:
        heart_rate_bpm: 心率，次/分。未测出时为 ``None``（**不要填 0**）。
        spo2_percent: 血氧饱和度 %。未测出时为 ``None``。
        finger_detected: 是否检测到手指/皮肤贴合（MAX30102 有该状态位）。
        quality: 数据质量 0.0~1.0（由驱动根据信号强度给出，业务层可据此丢弃坏点）。
    """

    heart_rate_bpm: Optional[float] = None
    spo2_percent: Optional[float] = None
    finger_detected: bool = False
    quality: float = 0.0


@dataclass(frozen=True)
class AmbientSample(Sample):
    """环境温湿度（DHT11）。

    Attributes:
        temperature_c: 摄氏温度。
        humidity_percent: 相对湿度 %。
        is_cached: 本值是否来自**缓存**（DHT11 硬件限制：两次读取间隔必须 ≥2 秒，
            间隔不足时驱动返回上次的值并置此标志为 ``True``）。
            业务层据此区分"刚测到"与"沿用旧值"——**缓存值可以用于显示，
            但不应当成"刚测到的新数据"参与判定**。
    """

    temperature_c: Optional[float] = None
    humidity_percent: Optional[float] = None
    is_cached: bool = False


@dataclass(frozen=True)
class PrecisionTempSample(Sample):
    """精密温度（TMP36 + MCP3002）。

    Attributes:
        temperature_c: 换算后的摄氏温度。
        raw_adc: ADC 原始值（0~1023），**务必保留**——报告里要用它写换算推导。
        voltage_v: 由原始值反推的电压（V），同样保留以便答辩时展示"电压→温度"的换算。
    """

    temperature_c: Optional[float] = None
    raw_adc: Optional[int] = None
    voltage_v: Optional[float] = None


@dataclass(frozen=True)
class RangeSample(Sample):
    """距离（HC-SR04）。

    Attributes:
        distance_cm: 距离，厘米。超出量程或回波超时为 ``None``。
        echo_us: 回波高电平持续时间（微秒），保留用于疑难排查。
    """

    distance_cm: Optional[float] = None
    echo_us: Optional[float] = None


@dataclass(frozen=True)
class MotionSample(Sample):
    """人体活动（HC-SR501）。

    Attributes:
        state: 见 :class:`MotionState`。
        active_for_s: 已连续处于该状态的秒数（由驱动维护，业务层直接用）。
    """

    state: MotionState = MotionState.UNKNOWN
    active_for_s: float = 0.0

    @property
    def detected(self) -> bool:
        """是否检测到人。``UNKNOWN`` 一律返回 ``False``（不要猜）。"""
        return self.state is MotionState.DETECTED


@dataclass(frozen=True)
class ButtonEvent(Sample):
    """按键事件（按钮）。"""

    button: str = ""
    action: ButtonAction = ButtonAction.PRESS
    pressed_for_s: float = 0.0


@dataclass(frozen=True)
class DisplayStatus(Sample):
    """显示器件回读状态（LCD1602）。

    Attributes:
        lines: 当前屏幕上两行文本（便于测试与远程查看，**不是**像素级回读）。
        page: 当前页号。
    """

    lines: Tuple[str, str] = ("", "")
    page: int = 0


# --------------------------------------------------------------------------
# 报警（业务层产出）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AlarmEvent:
    """一次报警事件。既用于本地播报，也用于推送安卓端。

    Attributes:
        ts: 事件时间（Unix 秒）。
        code: 事件类型，见 :class:`AlarmCode`。
        severity: 严重度。
        message: 给人看的中文短句（**面向用户可见，禁止写 markdown 标记**）。
        value: 触发时的数值（可选，便于手机端显示"心率 128"）。
        unit: 数值单位（可选）。
        source: 触发来源设备名（可选）。
        detail: 附加信息（可选，写进日志/数据库，不直接展示给用户）。
    """

    ts: float = field(default_factory=now_ts)
    code: AlarmCode = AlarmCode.SYSTEM_START
    severity: Severity = Severity.NOTICE
    message: str = ""
    value: Optional[float] = None
    unit: str = ""
    source: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """转成可直接 JSON 序列化的字典（供 HTTP API / MQTT / SQLite 使用）。"""
        return {
            "ts": self.ts,
            "code": self.code.value,
            "severity": int(self.severity),
            "message": self.message,
            "value": self.value,
            "unit": self.unit,
            "source": self.source,
            "detail": dict(self.detail),
        }


# --------------------------------------------------------------------------
# 指令（业务层 → 输出器件）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Command:
    """下发给输出器件（显示/音箱/蜂鸣器/LED）的指令基类。"""

    ts: float = field(default_factory=now_ts)


@dataclass(frozen=True)
class SpeakCommand(Command):
    """语音播报。``text`` 为中文短句。"""

    text: str = ""
    priority: Severity = Severity.NOTICE


@dataclass(frozen=True)
class BeepCommand(Command):
    """蜂鸣器鸣叫。

    Attributes:
        times: 鸣叫次数。
        on_ms / off_ms: 响/停的毫秒数。
    """

    times: int = 1
    on_ms: int = 200
    off_ms: int = 200


@dataclass(frozen=True)
class LightCommand(Command):
    """LED 状态。颜色由业务层决定，驱动只负责把对应颜色的灯点亮。"""

    color: str = "green"   # green / yellow / red / blue / off
    blink: bool = False


@dataclass(frozen=True)
class DisplayCommand(Command):
    """LCD 显示内容。``lines`` 最多两行，每行最多 16 字符（驱动负责截断）。"""

    lines: Tuple[str, str] = ("", "")
    page: int = 0


__all__ = [
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
]
