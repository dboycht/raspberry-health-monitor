"""蓝牙音箱（语音播报）驱动测试。

三条重点：
1. **假执行器断言命令**：注入 ``runner``，断言"到底执行了哪两条命令、参数对不对"，
   **绝不会真的发出声音**（也不能依赖树莓派上装没装 espeak-ng）；
2. **文本去重窗口**：同一句话 10 秒内只播一次（防吵人），窗口过后可以重播；
3. **self_check**：缺 espeak-ng / aplay 时必须给出可直接复制的安装命令。
"""

from __future__ import annotations

import unittest

from health_monitor.hal import (
    AlarmDispatchError,
    BeepCommand,
    DeviceInitError,
    DeviceKind,
    DeviceNotReady,
    Sample,
    Severity,
    SpeakCommand,
    UnsupportedError,
)
from health_monitor.outputs.bt_speaker import (
    DEFAULT_DEDUP_WINDOW_S,
    BtSpeaker,
)


class FakeClock:
    """假时钟：去重窗口测试用，不真的等 10 秒。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> float:
        self.t += seconds
        return self.t


class FakeRunner:
    """假执行器：记录被调用过的命令，**不执行任何外部程序**。"""

    def __init__(self, fail_on: int = -1) -> None:
        self.calls: list = []
        self.kwargs: list = []
        self._fail_on = fail_on      # 第 N 次调用（从 0 数）抛异常，-1 表示不抛

    def __call__(self, args, **kwargs):
        index = len(self.calls)
        self.calls.append(list(args))
        self.kwargs.append(dict(kwargs))
        if index == self._fail_on:
            raise OSError("模拟外部命令执行失败")
        return None

    @property
    def command_lines(self) -> list:
        """把命令还原成字符串列表，便于断言。"""
        return [" ".join(cmd) for cmd in self.calls]


def make_speaker(**kwargs) -> BtSpeaker:
    """造一个已 open 的假执行器音箱。"""
    kwargs.setdefault("mock", False)
    kwargs.setdefault("which", lambda prog: f"/usr/bin/{prog}")   # 假装工具都在
    kwargs.setdefault("runner", FakeRunner())
    kwargs.setdefault("clock", FakeClock())
    speaker = BtSpeaker(**kwargs)
    speaker.open()
    return speaker


# ==========================================================================
# 一、mock 模式：绝不执行外部命令
# ==========================================================================


class TestBtSpeakerMock(unittest.TestCase):
    def test_未open就send必须报错(self) -> None:
        spk = BtSpeaker(mock=True)
        with self.assertRaises(DeviceNotReady):
            spk.send(SpeakCommand(text="心率偏高"))

    def test_未open就read必须报错(self) -> None:
        with self.assertRaises(DeviceNotReady):
            BtSpeaker(mock=True).read()

    def test_mock模式可用且不执行外部命令(self) -> None:
        runner = FakeRunner()
        which = lambda prog: None  # noqa: E731 - 故意让 which 返回 None（缺工具）
        spk = BtSpeaker(mock=True, runner=runner, which=which)
        spk.open()                 # mock 模式**不检查**外部程序，不该报错
        spk.send(SpeakCommand(text="检测到心率偏高", priority=Severity.WARNING))
        self.assertEqual(runner.calls, [], "mock 模式绝不能执行任何外部命令")
        self.assertEqual(spk.spoken_history, ["检测到心率偏高"])
        self.assertEqual(spk.KIND, DeviceKind.AUDIO)
        spk.close()

    def test_mock模式记录最近N条(self) -> None:
        spk = BtSpeaker(mock=True, history_size=3)
        spk.open()
        for i in range(5):
            spk.send(SpeakCommand(text=f"第 {i} 条"))
        self.assertEqual(spk.spoken_history, ["第 2 条", "第 3 条", "第 4 条"])
        self.assertEqual(spk.last_spoken, "第 4 条")
        spk.close()

    def test_mock模式self_check为ok(self) -> None:
        spk = BtSpeaker(mock=True)
        check = spk.self_check()
        self.assertTrue(check["ok"])
        self.assertIn("mock", check["detail"])

    def test_mock模式read返回Sample(self) -> None:
        spk = BtSpeaker(mock=True)
        spk.open()
        sample = spk.read()
        self.assertIsInstance(sample, Sample)
        self.assertEqual(sample.device, "bt_speaker")
        spk.close()


# ==========================================================================
# 二、命令拼装：用假执行器断言"到底跑了什么"
# ==========================================================================


class TestBtSpeakerCommands(unittest.TestCase):
    def test_合成与播放两条命令参数正确(self) -> None:
        runner = FakeRunner()
        spk = make_speaker(runner=runner, voice="zh", rate=150, player="aplay")
        spk.send(SpeakCommand(text="心率 128，偏高"))
        self.assertEqual(len(runner.calls), 2, "应恰好执行两条命令：合成 + 播放")
        synth, play = runner.calls
        self.assertEqual(synth[0], "espeak-ng")
        self.assertIn("-v", synth)
        self.assertEqual(synth[synth.index("-v") + 1], "zh")
        self.assertEqual(synth[synth.index("-s") + 1], "150")
        self.assertTrue(synth[synth.index("-w") + 1].endswith(".wav"))
        self.assertEqual(synth[-1], "心率 128，偏高", "文本必须是最后一个参数")
        self.assertEqual(play[0], "aplay")
        self.assertEqual(play[-1], synth[synth.index("-w") + 1], "播放的必须是刚合成的那个 wav")
        spk.close()

    def test_指定sink时播放命令带D参数(self) -> None:
        runner = FakeRunner()
        spk = make_speaker(runner=runner, sink="bluealsa:DEV=AA:BB:CC:DD:EE:FF,PROFILE=a2dp")
        spk.send(SpeakCommand(text="测试"))
        play = runner.calls[1]
        self.assertEqual(play[0], "aplay")
        self.assertIn("-D", play)
        self.assertEqual(play[play.index("-D") + 1], "bluealsa:DEV=AA:BB:CC:DD:EE:FF,PROFILE=a2dp")
        spk.close()

    def test_不指定sink时走系统默认输出(self) -> None:
        runner = FakeRunner()
        spk = make_speaker(runner=runner, sink="")
        spk.send(SpeakCommand(text="测试"))
        self.assertNotIn("-D", runner.calls[1], "sink 为空时不应传 -D（用系统默认 sink）")
        spk.close()

    def test_可换播放器paplay(self) -> None:
        runner = FakeRunner()
        spk = make_speaker(runner=runner, player="paplay")
        spk.send(SpeakCommand(text="测试"))
        self.assertEqual(runner.calls[1][0], "paplay")
        spk.close()

    def test_紧急播报语速更快(self) -> None:
        runner = FakeRunner()
        spk = make_speaker(runner=runner, rate=150)
        spk.send(SpeakCommand(text="紧急求助", priority=Severity.CRITICAL))
        synth = runner.calls[0]
        self.assertGreater(int(synth[synth.index("-s") + 1]), 150)
        spk.close()

    def test_普通播报语速不变(self) -> None:
        runner = FakeRunner()
        spk = make_speaker(runner=runner, rate=150)
        spk.send(SpeakCommand(text="一切正常", priority=Severity.NOTICE))
        synth = runner.calls[0]
        self.assertEqual(synth[synth.index("-s") + 1], "150")
        spk.close()

    def test_每次播报用不同的临时文件(self) -> None:
        runner = FakeRunner()
        clock = FakeClock()
        spk = make_speaker(runner=runner, clock=clock)
        spk.send(SpeakCommand(text="第一句"))
        clock.advance(20.0)          # 跨过去重窗口
        spk.send(SpeakCommand(text="第二句"))
        wav1 = runner.calls[0][runner.calls[0].index("-w") + 1]
        wav2 = runner.calls[2][runner.calls[2].index("-w") + 1]
        self.assertNotEqual(wav1, wav2, "两次播报不能共用一个临时文件")
        spk.close()

    def test_临时wav用完被清理(self) -> None:
        """树莓派 SD 卡写满会直接起不来，临时文件必须删掉。"""
        import os

        runner = FakeRunner()
        spk = make_speaker(runner=runner)
        spk.send(SpeakCommand(text="测试"))
        wav = runner.calls[0][runner.calls[0].index("-w") + 1]
        self.assertFalse(os.path.exists(wav), "播报结束后临时 wav 必须被删除")
        spk.close()

    def test_runner抛异常时抛AlarmDispatchError(self) -> None:
        runner = FakeRunner(fail_on=0)
        spk = make_speaker(runner=runner)
        with self.assertRaises(AlarmDispatchError) as ctx:
            spk.send(SpeakCommand(text="测试"))
        self.assertIn("espeak-ng", str(ctx.exception), "报错要给出可复制的排查命令")
        self.assertEqual(spk.status()["fault_count"], 1)
        self.assertEqual(spk.spoken_history, [], "失败的播报不该记进历史")
        spk.close()

    def test_播放失败时抛AlarmDispatchError(self) -> None:
        runner = FakeRunner(fail_on=1)      # 合成成功、播放失败
        spk = make_speaker(runner=runner)
        with self.assertRaises(AlarmDispatchError):
            spk.send(SpeakCommand(text="测试"))
        self.assertEqual(len(runner.calls), 2)
        spk.close()

    def test_不认识的指令抛UnsupportedError(self) -> None:
        runner = FakeRunner()
        spk = make_speaker(runner=runner)
        with self.assertRaises(UnsupportedError):
            spk.send(BeepCommand(times=1))
        self.assertEqual(runner.calls, [], "指令不认识就不该执行任何外部命令")
        spk.close()

    def test_空文本被忽略且不发声(self) -> None:
        runner = FakeRunner()
        spk = make_speaker(runner=runner)
        spk.send(SpeakCommand(text="   "))
        self.assertEqual(runner.calls, [])
        self.assertEqual(spk.spoken_history, [])
        self.assertEqual(spk.status()["spoken_count"], 0)
        spk.close()

    def test_命令失败时也会清理临时文件(self) -> None:
        import os

        runner = FakeRunner(fail_on=1)
        spk = make_speaker(runner=runner)
        with self.assertRaises(AlarmDispatchError):
            spk.send(SpeakCommand(text="测试"))
        wav = runner.calls[0][runner.calls[0].index("-w") + 1]
        self.assertFalse(os.path.exists(wav), "即使播放失败，临时文件也必须清掉")
        spk.close()


# ==========================================================================
# 三、防吵人：文本去重窗口
# ==========================================================================


class TestBtSpeakerDedup(unittest.TestCase):
    def test_默认去重窗口为10秒(self) -> None:
        self.assertEqual(DEFAULT_DEDUP_WINDOW_S, 10.0)
        spk = BtSpeaker(mock=True)
        self.assertEqual(spk.dedup_window_s, 10.0)
        self.assertEqual(spk.status()["dedup_window_s"], 10.0)

    def test_同一句话窗口内只播一次(self) -> None:
        """报警引擎每一轮都会下发同一句话，不去重就会"心率高、心率高…"念个不停。"""
        runner = FakeRunner()
        clock = FakeClock()
        spk = make_speaker(runner=runner, clock=clock)
        spk.send(SpeakCommand(text="心率偏高，请休息"))
        spk.send(SpeakCommand(text="心率偏高，请休息"))
        spk.send(SpeakCommand(text="心率偏高，请休息"))
        self.assertEqual(len(runner.calls), 2, "10 秒内同一句话只应合成/播放一次")
        self.assertEqual(spk.status()["spoken_count"], 1)
        self.assertEqual(spk.status()["skipped_dup"], 2)
        self.assertEqual(spk.spoken_history, ["心率偏高，请休息"])
        spk.close()

    def test_窗口过后可以重播(self) -> None:
        """去重不是"永不再播"：报警还在，窗口一过就必须继续提醒。"""
        runner = FakeRunner()
        clock = FakeClock()
        spk = make_speaker(runner=runner, clock=clock, dedup_window_s=10.0)
        spk.send(SpeakCommand(text="心率偏高"))
        clock.advance(9.9)
        spk.send(SpeakCommand(text="心率偏高"))
        self.assertEqual(len(runner.calls), 2, "9.9 秒仍在窗口内，不应重播")
        clock.advance(0.2)            # 累计 10.1 秒 > 10 秒
        spk.send(SpeakCommand(text="心率偏高"))
        self.assertEqual(len(runner.calls), 4, "窗口过后必须能重播")
        self.assertEqual(spk.status()["spoken_count"], 2)
        spk.close()

    def test_不同文本不受去重影响(self) -> None:
        runner = FakeRunner()
        spk = make_speaker(runner=runner)
        spk.send(SpeakCommand(text="心率偏高"))
        spk.send(SpeakCommand(text="血氧偏低"))
        self.assertEqual(len(runner.calls), 4, "不同的话必须都能播出来")
        spk.close()

    def test_首尾空白差异被视作同一句话(self) -> None:
        """规范化会去掉首尾空白、折叠内部连续空白：规范后相同即算同一句。"""
        runner = FakeRunner()
        clock = FakeClock()
        spk = make_speaker(runner=runner, clock=clock)
        spk.send(SpeakCommand(text="心率偏高"))
        clock.advance(1.0)
        spk.send(SpeakCommand(text="  心率偏高  "))   # 首尾空白 → 规范后完全一样
        self.assertEqual(len(runner.calls), 2, "只有首尾空白差异时不应重复播报")
        self.assertEqual(spk.status()["skipped_dup"], 1)

        # "心率  偏高" 规范后是 "心率 偏高"（含一个空格），与 "心率偏高" 不是同一句
        clock.advance(1.0)
        spk.send(SpeakCommand(text="心率  偏高"))
        self.assertEqual(len(runner.calls), 4, "规范后不同的文本应当能播")
        spk.close()

    def test_可以关闭去重(self) -> None:
        runner = FakeRunner()
        spk = make_speaker(runner=runner, dedup_window_s=0)
        spk.send(SpeakCommand(text="测试"))
        spk.send(SpeakCommand(text="测试"))
        self.assertEqual(len(runner.calls), 4)
        spk.close()

    def test_mock模式也走去重(self) -> None:
        """mock 演示时同样不该"复读机"式刷屏。"""
        spk = BtSpeaker(mock=True, clock=FakeClock())
        spk.open()
        spk.send(SpeakCommand(text="测试"))
        spk.send(SpeakCommand(text="测试"))
        self.assertEqual(spk.spoken_history, ["测试"])
        self.assertEqual(spk.status()["skipped_dup"], 1)
        spk.close()


# ==========================================================================
# 四、自检与生命周期
# ==========================================================================


class TestBtSpeakerSelfCheck(unittest.TestCase):
    def test_缺espeak_ng时给出安装命令(self) -> None:
        spk = BtSpeaker(mock=False, which=lambda prog: None if prog == "espeak-ng" else "/usr/bin/x")
        check = spk.self_check()
        self.assertFalse(check["ok"])
        self.assertIn("espeak-ng", check["detail"])
        self.assertIn("sudo apt install -y espeak-ng", check["detail"])
        self.assertIsNone(check["programs"]["espeak-ng"])

    def test_缺aplay时给出安装命令(self) -> None:
        spk = BtSpeaker(mock=False, which=lambda prog: None if prog == "aplay" else "/usr/bin/x")
        check = spk.self_check()
        self.assertFalse(check["ok"])
        self.assertIn("alsa-utils", check["detail"])

    def test_两个都缺时都列出来(self) -> None:
        spk = BtSpeaker(mock=False, which=lambda prog: None)
        check = spk.self_check()
        self.assertFalse(check["ok"])
        self.assertIn("espeak-ng", check["detail"])
        self.assertIn("aplay", check["detail"])

    def test_工具齐全时自检通过(self) -> None:
        spk = BtSpeaker(mock=False, which=lambda prog: f"/usr/bin/{prog}", device_name="客厅音箱")
        check = spk.self_check()
        self.assertTrue(check["ok"])
        self.assertIn("客厅音箱", check["detail"])

    def test_真实模式缺工具时open抛DeviceInitError(self) -> None:
        spk = BtSpeaker(mock=False, which=lambda prog: None)
        with self.assertRaises(DeviceInitError) as ctx:
            spk.open()
        text = str(ctx.exception)
        self.assertIn("espeak-ng", text)
        self.assertIn("bluetoothctl", text, "必须提示蓝牙配对这一步")
        self.assertFalse(spk._opened)

    def test_真实模式工具齐全时open成功(self) -> None:
        spk = BtSpeaker(mock=False, which=lambda prog: f"/usr/bin/{prog}")
        spk.open()
        self.assertTrue(spk._opened)
        spk.close()
        self.assertFalse(spk._opened)

    def test_close可重复调用且幂等(self) -> None:
        spk = BtSpeaker(mock=True)
        spk.open()
        spk.close()
        spk.close()
        self.assertFalse(spk._opened)


class TestBtSpeakerStatus(unittest.TestCase):
    def test_status计数正确(self) -> None:
        runner = FakeRunner()
        clock = FakeClock()
        spk = make_speaker(runner=runner, clock=clock)
        spk.send(SpeakCommand(text="第一句"))
        spk.send(SpeakCommand(text="第一句"))     # 去重跳过
        clock.advance(30.0)
        spk.send(SpeakCommand(text="第二句"))
        st = spk.status()
        self.assertEqual(st["driver"], "BtSpeaker")
        self.assertEqual(st["spoken_count"], 2)
        self.assertEqual(st["skipped_dup"], 1)
        self.assertEqual(st["commands_run"], 4, "两次播报各 2 条命令")
        self.assertEqual(st["fault_count"], 0)
        self.assertEqual(st["engine"], "espeak-ng")
        self.assertEqual(st["player"], "aplay")
        spk.close()

    def test_read计数与status一致(self) -> None:
        spk = BtSpeaker(mock=True)
        spk.open()
        spk.read()
        spk.read()
        self.assertEqual(spk.status()["read_count"], 2)
        spk.close()

    def test_describe写清配对与去重(self) -> None:
        spk = make_speaker(sink="bluealsa:DEV=AA,PROFILE=a2dp", device_name="客厅音箱")
        desc = spk.describe()
        self.assertEqual(desc["name"], "bt_speaker")
        self.assertIn("espeak-ng", desc["commands"]["synth"])
        self.assertIn("aplay", desc["commands"]["play"])
        self.assertIn("bluealsa", desc["pins"]["audio"])
        self.assertIn("10 秒内只播一次", desc["notes"])
        self.assertIn("离线", desc["notes"])
        spk.close()

    def test_命令拼装方法本身可单独调用(self) -> None:
        """synth_command / play_command 是纯函数式拼装，便于测试与文档生成。"""
        spk = BtSpeaker(mock=True, rate=120, voice="zh", sink="my_sink")
        self.assertEqual(
            spk.synth_command("你好", "/tmp/a.wav"),
            ["espeak-ng", "-v", "zh", "-s", "120", "-w", "/tmp/a.wav", "你好"],
        )
        self.assertEqual(spk.play_command("/tmp/a.wav"), ["aplay", "-D", "my_sink", "/tmp/a.wav"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
