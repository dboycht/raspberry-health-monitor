"""报警下发：把 :class:`AlarmEvent` 翻译成各输出器件的具体动作。

分工与纪律
----------
- **翻译成什么样**（说什么话、响几声、什么颜色、LCD 显示什么）—— 全在本文件，
  属于业务策略，改这里不影响任何驱动；
- **怎么执行**（怎么发音、怎么点灯）—— 由 ``outputs/`` 下的驱动负责；
- 本文件**不认识任何具体输出类**（不 import ``Lcd1602`` 等），只按 ``DeviceKind``
  派发指令，因此换显示屏/换音箱都不用改这里（这是分层的意义所在）。

三条防吵人措施
--------------
1. **一句话 10 秒内不重复播报**（``speak_repeat_s``）；
2. **消音生效**：``SILENCE`` 后一段时间内只保留 LED 提示，不响铃、不播报；
3. **蜂鸣器鸣叫有总时长上限**（由驱动再兜一层，防止代码 bug 导致长鸣）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..hal.device import OutputDevice
from ..hal.exceptions import AlarmDispatchError
from ..hal.models import (
    AlarmCode,
    AlarmEvent,
    BeepCommand,
    DeviceKind,
    DisplayCommand,
    LCD_DEBUG_PAGE,
    LightCommand,
    Severity,
    SpeakCommand,
)

_LOG = logging.getLogger(__name__)


@dataclass
class AlarmPresentation:
    """一次报警的"呈现方案"（语音文本 / 蜂鸣模式 / 灯色 / LCD 两行）。

    这是**纯数据**，因此可以被单测直接断言，不需要任何硬件。
    """

    speak: Optional[str] = None
    beep_times: int = 0
    beep_on_ms: int = 200
    beep_off_ms: int = 200
    light: str = "green"
    blink: bool = False
    lcd_lines: tuple = ("", "")


#: 报警码 → 语音文本 + LED 颜色 + 蜂鸣模式。文案面向用户，**禁止包含 markdown 标记**。
PRESENTATION_TABLE: Dict[AlarmCode, AlarmPresentation] = {
    AlarmCode.SOS_PRESSED: AlarmPresentation(
        speak="已收到紧急求助，请立即查看", beep_times=5, beep_on_ms=150, beep_off_ms=100,
        light="red", blink=True, lcd_lines=("SOS! HELP NEEDED", "PLEASE CHECK NOW"),
    ),
    AlarmCode.HR_TOO_HIGH: AlarmPresentation(
        speak="心率偏高，请注意休息", beep_times=3, beep_on_ms=200, beep_off_ms=150,
        light="yellow", blink=True, lcd_lines=("ALARM: HR HIGH", ""),
    ),
    AlarmCode.HR_TOO_LOW: AlarmPresentation(
        speak="心率偏低，请确认老人状态", beep_times=3, beep_on_ms=300, beep_off_ms=150,
        light="yellow", blink=True, lcd_lines=("ALARM: HR LOW", ""),
    ),
    AlarmCode.SPO2_TOO_LOW: AlarmPresentation(
        speak="血氧偏低，请立即查看", beep_times=4, beep_on_ms=250, beep_off_ms=120,
        light="red", blink=True, lcd_lines=("ALARM: SPO2 LOW", ""),
    ),
    AlarmCode.BODY_TEMP_HIGH: AlarmPresentation(
        speak="体温偏高，请注意观察", beep_times=2, beep_on_ms=200, beep_off_ms=200,
        light="yellow", blink=False, lcd_lines=("ALARM: TEMP HIGH", ""),
    ),
    AlarmCode.BODY_TEMP_LOW: AlarmPresentation(
        speak="体温偏低，请注意保暖", beep_times=2, beep_on_ms=200, beep_off_ms=200,
        light="yellow", blink=False, lcd_lines=("ALARM: TEMP LOW", ""),
    ),
    AlarmCode.AMBIENT_TEMP_HIGH: AlarmPresentation(
        speak="室温偏高，建议通风", beep_times=1, light="yellow", blink=False,
        lcd_lines=("ROOM TEMP HIGH", ""),
    ),
    AlarmCode.AMBIENT_TEMP_LOW: AlarmPresentation(
        speak="室温偏低，建议取暖", beep_times=1, light="yellow", blink=False,
        lcd_lines=("ROOM TEMP LOW", ""),
    ),
    AlarmCode.HUMIDITY_HIGH: AlarmPresentation(
        speak="湿度偏高，建议通风", beep_times=0, light="yellow", blink=False,
        lcd_lines=("HUMIDITY HIGH", ""),
    ),
    AlarmCode.NO_MOTION_TOO_LONG: AlarmPresentation(
        speak="长时间没有检测到活动，请确认老人是否安全", beep_times=4,
        beep_on_ms=400, beep_off_ms=150, light="red", blink=True,
        lcd_lines=("NO MOTION ALERT", "CHECK PLEASE"),
    ),
    AlarmCode.NIGHT_FREQUENT_WAKE: AlarmPresentation(
        speak="夜间起夜次数较多，请注意休息", beep_times=0, light="yellow", blink=False,
        # ⚠️ 必须 ≤16 字符：LCD1602 每行 16 字符、TFT 在 1 倍字号下也是 16 字符。
        #    旧文案 "WAKE UP TOO OFTEN" 是 17 个字符 ⇒ 两块屏上都被截成 "WAKE UP TOO OFT"
        #    （2026-09-29 查 TFT 显示时发现，见 ERROR.md E53）。这里缩短到 14 个字符。
        lcd_lines=("WAKE TOO OFTEN", ""),
    ),
    AlarmCode.SENSOR_FAULT: AlarmPresentation(
        speak="设备异常，请检查传感器接线", beep_times=2, beep_on_ms=120, beep_off_ms=120,
        light="yellow", blink=True, lcd_lines=("SENSOR FAULT", "CHECK WIRING"),
    ),
    AlarmCode.DEVICE_OFFLINE: AlarmPresentation(
        speak="设备已离线", beep_times=1, light="yellow", blink=False,
        lcd_lines=("DEVICE OFFLINE", ""),
    ),
    AlarmCode.ALL_CLEAR: AlarmPresentation(
        speak="", beep_times=0, light="green", blink=False, lcd_lines=("STATUS: NORMAL", ""),
    ),
    AlarmCode.SYSTEM_START: AlarmPresentation(
        speak="监护系统已启动", beep_times=0, light="green", blink=False,
        lcd_lines=("SYSTEM READY", ""),
    ),
}


class AlarmDispatcher:
    """把报警事件送到各个输出器件。

    Args:
        outputs: ``{设备名: OutputDevice}``（由服务层装配，本类不关心怎么造出来）。
        enabled: 是否真的发声/显示。``False`` 时只记录（演示或夜间静音模式）。
        speak_repeat_s: 同一句话在该秒数内不重复播报（防吵人）。
        silence_after_s: 调用 :meth:`silence` 后，多少秒内只亮灯不出声。
    """

    def __init__(
        self,
        outputs: Optional[Dict[str, OutputDevice]] = None,
        enabled: bool = True,
        speak_repeat_s: float = 10.0,
        silence_after_s: float = 300.0,
    ) -> None:
        self.outputs: Dict[str, OutputDevice] = dict(outputs or {})
        self.enabled = bool(enabled)
        self.speak_repeat_s = float(speak_repeat_s)
        self.silence_after_s = float(silence_after_s)
        self._last_spoken: Dict[str, float] = {}
        self._silenced_until: float = 0.0
        self.dispatched: List[Dict[str, Any]] = []   # 下发流水（供手机端/日志查看）
        self.realerts: int = 0                       # "报警持续提醒"的重发次数（诊断用）
        self.errors: List[str] = []
        self._send_failures: Dict[str, int] = {}     # 器件名 -> 连续下发失败次数
        self._quiet_after = 3                        # 连续失败达到该次数后停止刷屏

    # ------------------------------------------------------------------
    # 对外主入口
    # ------------------------------------------------------------------

    def dispatch(self, event: AlarmEvent, now: float) -> AlarmPresentation:
        """下发一条报警，返回实际采用的呈现方案（便于测试与显示）。"""
        plan = self.plan(event)
        record = {
            "ts": now,
            "code": event.code.value,
            "severity": int(event.severity),
            "light": plan.light,
            "blink": plan.blink,
            "speak": plan.speak or "",
            "beep_times": 0,
            "silenced": self.is_silenced(now),
        }

        # 灯：**任何情况下都要亮**（静音也要让人看得见状态）
        self._send_kind(DeviceKind.LIGHT, LightCommand(color=plan.light, blink=plan.blink, ts=now))

        # LCD：总是更新（静音不影响显示）
        self._send_kind(DeviceKind.DISPLAY, DisplayCommand(lines=plan.lcd_lines, ts=now))

        if not self.enabled or self.is_silenced(now):
            self.dispatched.append(record)
            return plan

        if plan.beep_times > 0:
            self._send_kind(
                DeviceKind.AUDIO,
                BeepCommand(times=plan.beep_times, on_ms=plan.beep_on_ms, off_ms=plan.beep_off_ms, ts=now),
                only_driver="buzzer",
            )
            record["beep_times"] = plan.beep_times

        if plan.speak and self._speak_allowed(plan.speak, now):
            self._send_kind(
                DeviceKind.AUDIO,
                SpeakCommand(text=plan.speak, priority=event.severity, ts=now),
                only_driver="bt_speaker",
            )

        self.dispatched.append(record)
        return plan

    @staticmethod
    def plan(event: AlarmEvent) -> AlarmPresentation:
        """把报警事件翻译成呈现方案（**纯函数**，可直接单测）。

        未知报警码有兜底：按严重度生成一条通用提示，**绝不静默丢弃**。
        """
        known = PRESENTATION_TABLE.get(event.code)
        if known is not None:
            base = AlarmPresentation(**vars(known))
            # 严重度升级时把灯拉红，避免"表里写黄但实际很严重"的不一致
            if event.severity is Severity.CRITICAL and base.light != "red":
                base.light = "red"
                base.blink = True
            return base
        severity_light = {
            Severity.CRITICAL: ("red", True),
            Severity.WARNING: ("yellow", True),
            Severity.NOTICE: ("yellow", False),
        }.get(event.severity, ("yellow", False))
        return AlarmPresentation(
            speak=event.message or "请注意",
            beep_times=2 if event.severity >= Severity.WARNING else 0,
            light=severity_light[0],
            blink=severity_light[1],
            lcd_lines=("ALARM", event.code.value[:16]),
        )

    # ------------------------------------------------------------------
    # 持续提醒（2026-09-26 新增：报警"响两声就完"变成"按周期重发"）
    # ------------------------------------------------------------------

    @staticmethod
    def plan_for(code: Any, severity: Severity = Severity.WARNING) -> AlarmPresentation:
        """按报警码取呈现方案（纯查表 + 兜底）。

        与 :meth:`plan` 的区别：这里**只有码**（用于"报警还在、要再提醒一次"的场景，
        此时手上没有新的事件对象）。
        """
        known = PRESENTATION_TABLE.get(code)
        if known is not None:
            return AlarmPresentation(**vars(known))
        severity_light = {
            Severity.CRITICAL: ("red", True),
            Severity.WARNING: ("yellow", True),
            Severity.NOTICE: ("yellow", False),
        }.get(severity, ("yellow", False))
        code_text = getattr(code, "value", str(code))
        return AlarmPresentation(
            speak="请注意", beep_times=2 if severity >= Severity.WARNING else 0,
            light=severity_light[0], blink=severity_light[1],
            lcd_lines=("ALARM", str(code_text)[:16]),
        )

    def alert_light(self, code: Any, now: float, blink: bool = True, severity: Severity = Severity.WARNING) -> None:
        """把报警灯切到"该报警码对应的颜色"，并按需**持续闪**（``blink=True``）或常亮。

        用途：① 报警持续期间保持闪烁；② 用户**消音**后把灯切成常亮（"灯仍亮"是本项目的
        明确设计：消音只停声音，不停状态提示）。
        """
        plan = self.plan_for(code, severity)
        self._send_kind(DeviceKind.LIGHT, LightCommand(color=plan.light, blink=blink, ts=now))

    def re_alert(self, code: Any, now: float, severity: Severity = Severity.WARNING) -> AlarmPresentation:
        """**重发一次**某个仍在生效的报警（灯 + 屏 + 声），用于"报警持续提醒"。

        与 :meth:`dispatch` 的区别（刻意设计）：
        * **不追加** ``dispatched`` 流水（那是"事件流水"，重发会让手机端列表被刷屏）；
          只累加 ``realerts`` 计数，便于测试与诊断；
        * 尊重静音与 ``enabled``：静音期间只更新灯/屏、**不出声**；
        * 播报仍受 ``speak_repeat_s`` 节流（同一句话不反复念）。
        """
        plan = self.plan_for(code, severity)
        self.realerts += 1
        self._send_kind(DeviceKind.LIGHT, LightCommand(color=plan.light, blink=plan.blink, ts=now))
        self._send_kind(DeviceKind.DISPLAY, DisplayCommand(lines=plan.lcd_lines, ts=now))
        if not self.enabled or self.is_silenced(now):
            return plan
        if plan.beep_times > 0:
            self._send_kind(
                DeviceKind.AUDIO,
                BeepCommand(times=plan.beep_times, on_ms=plan.beep_on_ms, off_ms=plan.beep_off_ms, ts=now),
                only_driver="buzzer",
            )
        if plan.speak and self._speak_allowed(plan.speak, now):
            self._send_kind(
                DeviceKind.AUDIO, SpeakCommand(text=plan.speak, priority=severity, ts=now),
                only_driver="bt_speaker",
            )
        return plan

    # ------------------------------------------------------------------
    # 静音
    # ------------------------------------------------------------------

    def silence(self, now: float) -> None:
        """消音（用户按了消音键）：一段时间内只亮灯与更新显示，不出声。"""
        self._silenced_until = now + self.silence_after_s

    def is_silenced(self, now: float) -> bool:
        return now < self._silenced_until

    def unsilence(self) -> None:
        self._silenced_until = 0.0

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _speak_allowed(self, text: str, now: float) -> bool:
        """同一句话在 ``speak_repeat_s`` 内不重复播报。"""
        last = self._last_spoken.get(text)
        if last is not None and (now - last) < self.speak_repeat_s:
            return False
        self._last_spoken[text] = now
        return True

    def show_page(self, lines: Any, page: int = 0, now: Optional[float] = None) -> None:
        """把"信息页"**只发给彩屏（TFT）**：不动 LCD、不进报警流水（2026-09-29，E57）。

        为什么必须"只给 TFT"：LCD 与彩屏同属 ``DeviceKind.DISPLAY``，而 **LCD 是报警显示**
        （`docs/07` H11、`docs/14` T1）—— 闲时轮播的信息页若广播出去，会把 LCD 上的报警文案
        冲掉。这里复用 `_send_kind` 已有的 ``only_driver`` 过滤（彩屏驱动名 = ``tft_spi``），
        **不必改协议、也不影响报警链路**（报警仍走 :meth:`dispatch` → 广播给所有显示器件）。
        """
        self._send_kind(
            DeviceKind.DISPLAY,
            DisplayCommand(lines=tuple(str(x) for x in (lines or ("", "")))[:2],
                           page=int(page), ts=now or now_ts()),
            only_driver="tft_spi",
        )

    def show_debug(self, lines: Any, now: Optional[float] = None,
                   page: int = LCD_DEBUG_PAGE) -> None:
        """把"调试面板"文案**只发给 LCD（``lcd1602``）**：不动彩屏（2026-09-30）。

        这是 :meth:`show_page` 的**镜像**，两边理由完全对称：

        * ``show_page`` 只给彩屏 —— 信息页不能冲掉 LCD 上的报警文案（E57）；
        * ``show_debug`` 只给 LCD —— 调试信息不能冲掉彩屏上的信息页（同一个坑的另一半）。

        ``page`` 默认 :data:`~health_monitor.hal.models.LCD_DEBUG_PAGE`（负数），
        这样读 ``status()["page"]`` 时能一眼区分"这是调试面板"还是"这是第 n 个信息页"。
        """
        self._send_kind(
            DeviceKind.DISPLAY,
            DisplayCommand(lines=tuple(str(x) for x in (lines or ("", "")))[:2],
                           page=int(page), ts=now or now_ts()),
            only_driver="lcd1602",
        )

    def _send_kind(self, kind: DeviceKind, command: Any, only_driver: Optional[str] = None) -> None:
        """把指令发给某一大类的所有输出器件。

        Args:
            only_driver: 只发给该驱动类型的器件（用于区分"蜂鸣器"与"音箱"，
                         因为二者同属 ``AUDIO`` 大类但指令类型不同）。

        退化保护：某个输出器件**连续多次**下发失败（例如它根本没打开成功），
        只记一次日志后进"冷却名单"，避免每帧刷屏把真问题淹掉；
        之后每次仍会尝试（器件可能热插拔恢复），一旦成功立刻恢复常态。
        """
        for name, device in self.outputs.items():
            if getattr(device, "KIND", None) is not kind:
                continue
            if only_driver is not None and getattr(device, "NAME", "") != only_driver:
                continue
            try:
                device.send(command)
                self._send_failures.pop(name, None)
            except Exception as exc:  # noqa: BLE001 - 输出器件失败不该中断整个报警链路
                count = self._send_failures.get(name, 0) + 1
                self._send_failures[name] = count
                if count == 1:
                    msg = f"下发到 {name}（{type(device).__name__}）失败：{type(exc).__name__}: {exc}"
                    self.errors.append(msg)
                    _LOG.warning("报警下发失败（继续尝试其它器件，同类失败后续不再刷屏）：%s", msg)
                elif count == self._quiet_after:
                    msg = f"器件 {name} 已连续 {count} 次下发失败，后续同类错误不再重复记录（仍会继续尝试）"
                    self.errors.append(msg)
                    _LOG.warning("%s", msg)

    # ------------------------------------------------------------------
    # 诊断
    # ------------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "outputs": sorted(self.outputs),
            "dispatched": len(self.dispatched),
            "errors": self.errors[-5:],
            "silenced_until": self._silenced_until,
        }

    def recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        """最近下发的报警（供 ``/api/v1/alarms`` 与安卓端展示）。"""
        return self.dispatched[-limit:]


__all__ = ["AlarmDispatcher", "AlarmPresentation", "PRESENTATION_TABLE"]
