"""`output_probe.py` 的纯逻辑测试（LED 颜色解析与"点哪些颜色"）。

为什么测这两件事（2026-09-26）：输出探针本身要真硬件才能验收，但**"点哪些颜色"**这个判断
是最容易出错、又最该被机器钉住的一环 —— 默认配置没有红灯（GPIO24 让给了 TFT 的 DC），
一旦退回"硬写绿黄红"，点到不存在的颜色会让驱动抛 `UnsupportedError`，
**看起来像"灯坏了"**（假红）。所以把它抽成纯函数并在这里锁死。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_RPI_DIR = Path(__file__).resolve().parents[2]      # .../rpi
_SCRIPTS_DIR = _RPI_DIR / "scripts"
for _path in (_RPI_DIR, _SCRIPTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import output_probe  # noqa: E402  必须在 sys.path 处理之后导入


class _LedMethod:
    """`available_colors()` 是方法。"""

    def __init__(self, colors=None, boom=False) -> None:
        self._colors = colors if colors is not None else []
        self._boom = boom

    def available_colors(self):
        if self._boom:
            raise RuntimeError("器件自报失败")
        return list(self._colors)


class _LedProperty:
    """`available_colors` 是**属性** —— 本项目的真实形态（`@property`）。"""

    def __init__(self, colors=None) -> None:
        self._colors = list(colors or [])

    @property
    def available_colors(self):
        return list(self._colors)


class TestParseColors(unittest.TestCase):
    def test_逗号与空白(self) -> None:
        self.assertEqual(output_probe.parse_colors("green, yellow"), ["green", "yellow"])
        self.assertEqual(output_probe.parse_colors("green,,yellow,"), ["green", "yellow"])

    def test_空与None都返回None(self) -> None:
        self.assertIsNone(output_probe.parse_colors(None))
        self.assertIsNone(output_probe.parse_colors(""))
        self.assertIsNone(output_probe.parse_colors(" , "))


class TestResolveColors(unittest.TestCase):
    def test_显式优先(self) -> None:
        self.assertEqual(
            output_probe.resolve_colors(_LedProperty(["green"]), ["yellow", "green"]),
            ["yellow", "green"],
        )

    def test_属性形态_默认用器件自报并去掉off(self) -> None:
        """★ 真机踩过的那条：属性形态（`@property`）必须被认出来。"""
        self.assertEqual(
            output_probe.resolve_colors(_LedProperty(["green", "off", "yellow"]), None),
            ["green", "yellow"],
        )

    def test_方法形态_同样认(self) -> None:
        self.assertEqual(
            output_probe.resolve_colors(_LedMethod(["green", "off", "yellow"]), None),
            ["green", "yellow"],
        )

    def test_本项目现状不会点到红灯(self) -> None:
        self.assertNotIn("red", output_probe.resolve_colors(_LedProperty(["green", "yellow"]), None))

    def test_没有该接口时退回绿黄(self) -> None:
        self.assertEqual(output_probe.resolve_colors(object(), None), ["green", "yellow"])

    def test_自报失败时退回绿黄(self) -> None:
        self.assertEqual(output_probe.resolve_colors(_LedMethod(boom=True), None), ["green", "yellow"])

    def test_自报为空时退回绿黄(self) -> None:
        self.assertEqual(output_probe.resolve_colors(_LedProperty([]), None), ["green", "yellow"])
        self.assertEqual(output_probe.resolve_colors(_LedProperty(["off"]), None), ["green", "yellow"])


class Test预告与重复(unittest.TestCase):
    """★ 2026-09-26 用户真机反馈："我没注意，你再试一下【下一次这种你提前说一声】"。

    结论：**凡是要人看/听的验收，必须先"预告"再动作**，并给足观察窗口。
    所以探针加了 `--delay`（先倒数）与 `--repeat`（整套动作重复几轮），这里把两条钉死。
    """

    def _run(self, argv):
        from unittest import mock

        sent: list = []

        class FakeLed:
            NAME = "led"

            @property
            def available_colors(self):
                return ["green", "yellow", "red", "off"]

            def send(self, command):
                sent.append(str(command.color))

            def close(self):
                pass

        class FakeBuzzer:
            NAME = "buzzer"

            def send(self, command):
                sent.append(f"beep{command.times}")

            def close(self):
                pass

        def fake_open(config, name):
            return FakeLed() if name == "status_led" else FakeBuzzer()

        slept: list = []
        with mock.patch.object(output_probe, "_open", side_effect=fake_open), \
                mock.patch.object(output_probe, "load_config", return_value=object()), \
                mock.patch.object(output_probe.time, "sleep", side_effect=lambda s: slept.append(s)), \
                mock.patch.object(output_probe, "safe_print", lambda *a, **k: None):
            code = output_probe.main(argv)
        return code, sent, slept

    def test_默认只做一轮(self) -> None:
        code, sent, _ = self._run(["--led-colors", "green", "--hold", "0", "--buzzer-only"])
        self.assertEqual(code, 0)
        self.assertEqual(sent, ["beep2"], "只鸣叫一次")

    def test_repeat_2会做两轮(self) -> None:
        code, sent, _ = self._run(
            ["--led-colors", "green,red", "--hold", "0", "--repeat", "2", "--buzzer-only"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(sent), 2, f"两轮就该有两次动作：{sent}")

    def test_led_only时点两轮且结尾熄灭(self) -> None:
        code, sent, _ = self._run(["--led-colors", "green", "--hold", "0", "--repeat", "2", "--led-only"])
        self.assertEqual(code, 0)
        self.assertEqual(sent, ["green", "off", "green", "off"], "每轮结尾都要熄灭")

    def test_delay先倒数再动作(self) -> None:
        code, sent, slept = self._run(
            ["--led-colors", "green", "--hold", "0", "--delay", "3", "--led-only"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(slept[:3], [1.0, 1.0, 1.0], "倒数 3 秒 = 三次 1 秒")
        self.assertEqual(sent[0], "green", "倒数完才动灯")


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
