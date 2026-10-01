"""`AlarmDispatcher` 的单元测试（含 2026-09-26 新增的"报警持续提醒"）。

为什么单独建这个文件：下发器原来只在端到端测试里被间接覆盖，
而"重发提示 / 消音后转常亮 / 重发不污染事件流水"这三条都是**它自己的契约**，
值得有明确的、不依赖采集与规则引擎的判据。

⚠️ **音频自 2026-10-01 起跑在工作线程里**（`ERROR.md` E63，因为语音播报曾让主循环
停摆 25 秒）。因此本文件里：

* 凡是要断言"响了 / 念了"的，**必须先** :meth:`AlarmDispatcher.flush_audio`；
* 要观察"音频时序本身"的用 ``make_dispatcher()``（它自带 flush）；
* 要验证"音频不阻塞"的用 :class:`TestAudioNeverBlocksLoop`（那里用**故意卡住的**假器件，
  并直接量 `dispatch()` 的耗时 —— 这才是 E63 的判据本体）。
"""

from __future__ import annotations

import threading
import time
import unittest
from typing import Any

from health_monitor.core.dispatcher import PRESENTATION_TABLE, AlarmDispatcher
from health_monitor.core.output_worker import DEFAULT_FAIL_BACKOFF_AFTER
from health_monitor.hal.device import OutputDevice
from health_monitor.hal.models import AlarmCode, AlarmEvent, DeviceKind, Severity
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
    disp = AlarmDispatcher(outputs=outputs)
    disp.start_audio()
    return disp


class _SlowSpeaker(OutputDevice):
    """一个 `send()` 会**故意卡住**的假音箱（模拟真机上 15 秒超时的 aplay）。"""

    KIND = DeviceKind.AUDIO
    NAME = "bt_speaker"

    def __init__(self, block_s: float = 10.0) -> None:
        super().__init__(mock=True)
        self.block_s = float(block_s)
        self.started = threading.Event()
        self.finished = threading.Event()
        self.received: list = []

    def open(self) -> None:
        self._opened = True

    def close(self) -> None:
        self._opened = False

    def send(self, command: Any) -> None:
        self.received.append(command)
        self.started.set()
        time.sleep(self.block_s)          # ← 真机上这里是"等外部命令超时"
        self.finished.set()


class TestAudioNeverBlocksLoop(unittest.TestCase):
    """★ E63 的判据：**输出器件再慢，也不许拖住监护循环**。

    真机事故：`bt_speaker.speak()` 串行两条各 15 秒超时的命令、`dispatch()` 又在主循环
    线程里同步跑 ⇒ 一次播报让监护盲掉 20~30 秒，还顺手造出一串假 `sensor_fault`。
    """

    def test_慢音箱不许拖住dispatch(self) -> None:
        slow = _SlowSpeaker(block_s=3.0)
        slow.open()
        disp = AlarmDispatcher(outputs={"speaker": slow}, audio_in_background=True)
        disp.start_audio()
        try:
            event = AlarmEvent(
                ts=1000.0, code=AlarmCode.SOS_PRESSED, severity=Severity.CRITICAL,
                message="已收到紧急求助", source="sos",
            )
            t0 = time.monotonic()
            disp.dispatch(event, 1000.0)
            elapsed = time.monotonic() - t0
            self.assertLess(elapsed, 0.5,
                            f"dispatch() 花了 {elapsed:.2f}s —— 音频又回到主循环里了（E63）")
            self.assertTrue(slow.started.wait(1.0), "音频应当在工作线程里**真的**被执行")
        finally:
            disp.close()

    def test_墙上的时钟能证明后台确实在跑(self) -> None:
        """反向钉子：不是"什么都不做"——卡住的 `send()` 确实被执行了（只是不在主线程）。"""
        slow = _SlowSpeaker(block_s=0.3)
        slow.open()
        disp = AlarmDispatcher(outputs={"speaker": slow})
        disp.start_audio()
        try:
            disp.dispatch(
                AlarmEvent(ts=1000.0, code=AlarmCode.SOS_PRESSED, severity=Severity.CRITICAL,
                           message="求助", source="sos"),
                1000.0,
            )
            self.assertTrue(slow.finished.wait(3.0), "工作线程最终必须把这条指令跑完")
            self.assertTrue(disp.flush_audio(3.0))
        finally:
            disp.close()

    def test_音频器件连续失败会熔断(self) -> None:
        """★ 真机上 `speaker` 是**永久坏的**（Pi 5 无 3.5 mm 孔、T8 已取消）。

        以前的表现是"每一次报警都白等 15 秒"（两条命令串起来 30 秒）；
        现在连续失败 ``DEFAULT_FAIL_BACKOFF_AFTER`` 次后熔断，
        冷却期内**连入队都不入**（不再占用工作线程），且监护循环全程不受影响。

        ⚠️ 这条走**蜂鸣器**那条路：语音有 10 秒去重（同一句话不重复念），
        用语音测会先被去重挡掉、测不到熔断。
        """
        class _BrokenBuzzer(OutputDevice):
            KIND = DeviceKind.AUDIO
            NAME = "buzzer"

            def __init__(self) -> None:
                super().__init__(mock=True)
                self.calls = 0

            def open(self) -> None:
                self._opened = True

            def close(self) -> None:
                self._opened = False

            def send(self, command: Any) -> None:
                self.calls += 1
                raise RuntimeError("GPIO busy")

        broken = _BrokenBuzzer()
        broken.open()
        disp = AlarmDispatcher(outputs={"alarm_buzzer": broken})
        disp.start_audio()
        try:
            for i in range(6):
                disp.dispatch(
                    AlarmEvent(ts=1000.0 + i, code=AlarmCode.SOS_PRESSED,
                               severity=Severity.CRITICAL, message=f"求助 {i}", source="sos"),
                    1000.0 + i,
                )
                disp.flush_audio(3.0)
            self.assertEqual(broken.calls, DEFAULT_FAIL_BACKOFF_AFTER,
                             f"熔断阈值是 {DEFAULT_FAIL_BACKOFF_AFTER} 次，之后不该再下发")
            self.assertTrue(disp.worker.in_backoff("alarm_buzzer"))
            self.assertGreaterEqual(disp.worker.dropped, 1)
            self.assertTrue(disp.status()["audio_worker"]["running"])
        finally:
            disp.close()

    def test_关服务会把工作线程停掉(self) -> None:
        slow = _SlowSpeaker(block_s=0.1)
        slow.open()
        disp = AlarmDispatcher(outputs={"speaker": slow})
        disp.start_audio()
        self.assertTrue(disp.worker.running)
        disp.close()
        self.assertFalse(disp.worker.running, "关服务必须把输出工作线程停掉")
        disp.close()          # 幂等


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
        try:
            led = disp.outputs["status_led"]
            disp.alert_light(AlarmCode.HR_TOO_HIGH, now=1000.0, blink=True)
            self.assertEqual(led.current_color, "yellow")
            self.assertTrue(led.blink_requested, "报警期间应当是持续闪")
            disp.alert_light(AlarmCode.HR_TOO_HIGH, now=1001.0, blink=False)
            self.assertFalse(led.blink_requested, "消音后应转常亮")
        finally:
            disp.close()


class TestReAlert(unittest.TestCase):
    """重发提示：会响、会刷屏，但**不追加事件流水**、且尊重静音。"""

    def setUp(self) -> None:
        self.disp = make_dispatcher()
        self.led = self.disp.outputs["status_led"]
        self.buzzer = self.disp.outputs["alarm_buzzer"]
        self.speaker = self.disp.outputs["speaker"]

    def tearDown(self) -> None:
        self.disp.close()

    def _flush(self) -> None:
        """音频在工作线程里 ⇒ 断言"响了几声/念没念"之前必须先把队列排空。"""
        self.assertTrue(self.disp.flush_audio(5.0), "音频队列没有在 5 秒内排空")

    def test_重发会再响一次但不进流水(self) -> None:
        self.disp.re_alert(AlarmCode.SENSOR_FAULT, now=1000.0)
        self._flush()
        first = self.buzzer.total_beeps
        self.assertGreater(first, 0, "第 1 次重发就该响")
        self.assertEqual(len(self.disp.dispatched), 0, "重发不该塞进事件流水（那是手机端的列表）")
        self.disp.re_alert(AlarmCode.SENSOR_FAULT, now=1005.0)
        self._flush()
        self.assertGreater(self.buzzer.total_beeps, first, "重发必须真的再响")
        self.assertEqual(self.disp.realerts, 2)

    def test_静音期间只更新灯屏不出声(self) -> None:
        self.disp.silence(now=1000.0)
        self.disp.re_alert(AlarmCode.SENSOR_FAULT, now=1001.0)
        self._flush()
        self.assertEqual(self.buzzer.total_beeps, 0, "静音期间重发不许响")
        self.assertEqual(self.led.current_color, "yellow", "静音期间灯仍要亮")

    def test_enabled_false_时不出声(self) -> None:
        buzzer = ConsoleBuzzer(name="alarm_buzzer")
        speaker = ConsoleSpeaker(name="speaker")
        for device in (buzzer, speaker):
            device.open()
        disp = AlarmDispatcher(outputs={"alarm_buzzer": buzzer, "speaker": speaker}, enabled=False)
        try:
            event = AlarmEvent(ts=1000.0, code=AlarmCode.SOS_PRESSED, severity=Severity.CRITICAL,
                               message="求助", source="sos")
            disp.dispatch(event, 1000.0)
            disp.flush_audio(5.0)
            self.assertEqual(buzzer.total_beeps, 0)
            self.assertEqual(speaker.spoken, [], "enabled=False 时连入队都不该有声音")
        finally:
            disp.close()

    def test_播报仍受节流(self) -> None:
        self.disp.re_alert(AlarmCode.HR_TOO_HIGH, now=1000.0)
        self._flush()
        spoken_first = len(self.speaker.spoken)
        self.assertGreater(spoken_first, 0, "第 1 次应当真的念出来")
        self.disp.re_alert(AlarmCode.HR_TOO_HIGH, now=1001.0)   # 1 秒后：同一句话不该重复念
        self._flush()
        self.assertEqual(len(self.speaker.spoken), spoken_first)

    def test_求救是红灯且解除静音后能响(self) -> None:
        self.disp.silence(now=1000.0)
        self.disp.unsilence()
        self.disp.re_alert(AlarmCode.SOS_PRESSED, now=1001.0, severity=Severity.CRITICAL)
        self._flush()
        self.assertEqual(self.led.current_color, "red")
        self.assertGreater(self.buzzer.total_beeps, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
