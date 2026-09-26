"""`AlarmDispatcher` 的单元测试（含 2026-09-26 新增的"报警持续提醒"）。

为什么单独建这个文件：下发器原来只在端到端测试里被间接覆盖，
而"重发提示 / 消音后转常亮 / 重发不污染事件流水"这三条都是**它自己的契约**，
值得有明确的、不依赖采集与规则引擎的判据。
"""

from __future__ import annotations

import unittest

from health_monitor.core.dispatcher import PRESENTATION_TABLE, AlarmDispatcher
from health_monitor.hal.models import AlarmCode, AlarmEvent, Severity
from health_monitor.playback import ConsoleBuzzer, ConsoleLed, ConsoleSpeaker


def make_dispatcher() -> AlarmDispatcher:
    """造一个"输出器件已 open"的下发器。

    ⚠️ **必须 open()**：`ConsoleOutput._record()` 里会 `_require_open()`，
    没 open 的话每次下发都会被记成"器件未就绪"（现象是 `total_beeps == 0` 而颜色却变了 —— 
    本轮就踩了一次，测试白跑了）。
    """
    outputs = {
        "status_led": ConsoleLed(name="status_led"),
        "alarm_buzzer": ConsoleBuzzer(name="alarm_buzzer"),
        "speaker": ConsoleSpeaker(name="speaker"),
    }
    for device in outputs.values():
        device.open()
    return AlarmDispatcher(outputs=outputs)


class TestPlanFor(unittest.TestCase):
    def test_按报警码取呈现方案(self) -> None:
        plan = AlarmDispatcher.plan_for(AlarmCode.HR_TOO_HIGH)
        self.assertEqual(plan.light, "yellow")
        self.assertTrue(plan.blink)
        self.assertEqual(plan.beep_times, PRESENTATION_TABLE[AlarmCode.HR_TOO_HIGH].beep_times)

    def test_未知码有兜底不丢(self) -> None:
        plan = AlarmDispatcher.plan_for("not_a_code", Severity.CRITICAL)
        self.assertEqual(plan.light, "red")
        self.assertGreater(plan.beep_times, 0)


class TestAlertLight(unittest.TestCase):
    def test_按需常亮或持续闪(self) -> None:
        disp = make_dispatcher()
        led = disp.outputs["status_led"]
        disp.alert_light(AlarmCode.HR_TOO_HIGH, now=1000.0, blink=True)
        self.assertEqual(led.current_color, "yellow")
        self.assertTrue(led.blink_requested, "报警期间应当是持续闪")
        disp.alert_light(AlarmCode.HR_TOO_HIGH, now=1001.0, blink=False)
        self.assertFalse(led.blink_requested, "消音后应转常亮")


class TestReAlert(unittest.TestCase):
    """重发提示：会响、会刷屏，但**不追加事件流水**、且尊重静音。"""

    def setUp(self) -> None:
        self.disp = make_dispatcher()
        self.led = self.disp.outputs["status_led"]
        self.buzzer = self.disp.outputs["alarm_buzzer"]

    def test_重发会再响一次但不进流水(self) -> None:
        self.disp.re_alert(AlarmCode.SENSOR_FAULT, now=1000.0)
        first = self.buzzer.total_beeps
        self.assertEqual(len(self.disp.dispatched), 0, "重发不该塞进事件流水（那是手机端的列表）")
        self.disp.re_alert(AlarmCode.SENSOR_FAULT, now=1005.0)
        self.assertGreater(self.buzzer.total_beeps, first, "重发必须真的再响")
        self.assertEqual(self.disp.realerts, 2)

    def test_静音期间只更新灯屏不出声(self) -> None:
        self.disp.silence(now=1000.0)
        self.disp.re_alert(AlarmCode.SENSOR_FAULT, now=1001.0)
        self.assertEqual(self.buzzer.total_beeps, 0, "静音期间重发不许响")
        self.assertEqual(self.led.current_color, "yellow", "静音期间灯仍要亮")

    def test_enabled_false_时不出声(self) -> None:
        buzzer = ConsoleBuzzer(name="alarm_buzzer")
        buzzer.open()
        disp = AlarmDispatcher(outputs={"alarm_buzzer": buzzer}, enabled=False)
        disp.re_alert(AlarmCode.SENSOR_FAULT, now=1000.0)
        self.assertEqual(buzzer.total_beeps, 0)

    def test_播报仍受节流(self) -> None:
        self.disp.re_alert(AlarmCode.HR_TOO_HIGH, now=1000.0)
        spoken_first = len(self.disp.outputs["speaker"].spoken)
        self.disp.re_alert(AlarmCode.HR_TOO_HIGH, now=1001.0)   # 1 秒后：同一句话不该重复念
        self.assertEqual(len(self.disp.outputs["speaker"].spoken), spoken_first)

    def test_求救是红灯且解除静音后能响(self) -> None:
        self.disp.silence(now=1000.0)
        self.disp.unsilence()
        self.disp.re_alert(AlarmCode.SOS_PRESSED, now=1001.0, severity=Severity.CRITICAL)
        self.assertEqual(self.led.current_color, "red")
        self.assertGreater(self.buzzer.total_beeps, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
