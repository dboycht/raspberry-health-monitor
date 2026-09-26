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


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
