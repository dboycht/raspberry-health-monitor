"""`scripts/sense_cues.py`（LCD + 蜂鸣提示器）的测试 —— 用户明确要求的那条约定要能拦住退化。

来历（2026-09-29，用户原话）：
> "**记着每一次这种测试使用蜂鸣器提示我，还有 LCD 指示我**"

所以这里不仅测"提示器本身能用"，还**扫描源码**钉住：
"要人看/听"的测试脚本（`tft_check.py` / `vitals_check.py`）必须走共享提示器，
不许各自手搓一套（暗号一旦分裂，用户听到的"1 声/2 声/5 声"含义就会漂移）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

_RPI_DIR = Path(__file__).resolve().parents[2]
if str(_RPI_DIR) not in sys.path:
    sys.path.insert(0, str(_RPI_DIR))
_SCRIPTS_DIR = _RPI_DIR / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import sense_cues as sc  # noqa: E402


class _FakeLcd:
    def __init__(self, fail: bool = False) -> None:
        self.lines = []
        self.fail = fail
        self.closed = False

    def send(self, command) -> None:
        if self.fail:
            raise OSError("假的 LCD 写失败")
        self.lines.append(command.lines)

    def close(self) -> None:
        self.closed = True


class _FakeBuzzer:
    def __init__(self, fail: bool = False) -> None:
        self.beeps = []
        self.fail = fail
        self.closed = False

    def send(self, command) -> None:
        if self.fail:
            raise OSError("假的蜂鸣失败")
        self.beeps.append((command.times, command.on_ms))

    def close(self) -> None:
        self.closed = True


class TestBeepCodes(unittest.TestCase):
    """蜂鸣暗号是"人在远处唯一能分辨的信号" —— 改动必须是有意识的。"""

    def test_暗号与跟用户约定的含义一致(self) -> None:
        self.assertEqual(sc.BEEP_ATTENTION, (1, 150), "1 声 = 注意，请看屏")
        self.assertEqual(sc.BEEP_DONE, (2, 150), "2 声 = 成功结束")
        self.assertEqual(sc.BEEP_FAIL, (5, 120), "5 声 = 失败/重试")
        for plan in (sc.BEEP_ATTENTION, sc.BEEP_DONE, sc.BEEP_FAIL):
            self.assertGreater(plan[0], 0)
            self.assertGreater(plan[1], 0)


class TestLcdLine(unittest.TestCase):
    def test_每行收进16字符(self) -> None:
        for text in ("", "A", "X" * 16, "Y" * 40):
            self.assertEqual(len(sc.lcd_line(text)), sc.LCD_WIDTH)

    def test_超长截断而不是换行(self) -> None:
        self.assertEqual(sc.lcd_line("0123456789ABCDEFGHIJ"), "0123456789ABCDEF")


class TestCues(unittest.TestCase):
    def test_正常路径写出两行并鸣叫(self) -> None:
        lcd, buzzer = _FakeLcd(), _FakeBuzzer()
        cues = sc.Cues(lcd, buzzer)
        cues.show("TFT TEST", "SEE SCREEN")
        cues.beep(sc.BEEP_ATTENTION)
        self.assertEqual(lcd.lines[0], ("TFT TEST" + " " * 8, "SEE SCREEN" + " " * 6))
        self.assertEqual(buzzer.beeps, [(1, 150)])
        cues.close()
        self.assertTrue(lcd.closed)
        self.assertTrue(buzzer.closed)

    def test_没有器件时静默不抛(self) -> None:
        cues = sc.Cues(None, None)
        cues.show("A", "B")          # 不应抛
        cues.beep(sc.BEEP_DONE)      # 不应抛
        cues.close()
        self.assertEqual(cues.notes, [])

    def test_LCD坏掉只记一笔且不抛(self) -> None:
        cues = sc.Cues(_FakeLcd(fail=True), _FakeBuzzer())
        cues.show("A", "B")
        self.assertEqual(len(cues.notes), 1)
        self.assertIn("LCD", cues.notes[0])
        cues.show("C", "D")          # 第二次直接跳过（不再重复报）
        self.assertEqual(len(cues.notes), 1)

    def test_蜂鸣坏掉只记一笔且不抛(self) -> None:
        cues = sc.Cues(_FakeLcd(), _FakeBuzzer(fail=True))
        cues.beep(sc.BEEP_FAIL)
        self.assertEqual(len(cues.notes), 1)
        self.assertIn("蜂鸣", cues.notes[0])

    def test_report把提示器故障打出来(self) -> None:
        cues = sc.Cues(_FakeLcd(fail=True), None)
        cues.show("A", "B")
        cues.report()                # 只要求不抛（内容走 safe_print）


class TestOpenCues(unittest.TestCase):
    def _config(self, mapping):
        return SimpleNamespace(device=lambda name: mapping.get(name))

    def test_两个器件都缺时返回空提示器而不是抛(self) -> None:
        cues = sc.open_cues(self._config({}))
        self.assertIsNone(cues.lcd)
        self.assertIsNone(cues.buzzer)


class TestConventionIsEnforced(unittest.TestCase):
    """★ 用户要求的那条约定：**要人看/听的测试都必须走共享提示器**。"""

    def test_要人看的脚本都引用共享提示器(self) -> None:
        for name in ("tft_check.py", "vitals_check.py"):
            source = (_SCRIPTS_DIR / name).read_text(encoding="utf-8")
            self.assertIn("sense_cues", source, f"{name} 必须用共享提示器（LCD+蜂鸣）")

    def test_tft_check默认会提示且可以关掉(self) -> None:
        source = (_SCRIPTS_DIR / "tft_check.py").read_text(encoding="utf-8")
        self.assertIn("--no-cue", source, "默认要提示，同时留一个显式关掉的开关")
        self.assertIn("open_cues(", source)
        self.assertIn("BEEP_ATTENTION", source, "开始前必须叫人（1 声）")

    def test_暗号只有一处定义(self) -> None:
        """别的脚本只能引用、不许再写一份 (1,150)/(2,150)/(5,120)。"""
        for name in ("tft_check.py", "vitals_check.py"):
            source = (_SCRIPTS_DIR / name).read_text(encoding="utf-8")
            self.assertNotIn("(1, 150)", source, f"{name} 里不该再复制一份暗号")
            self.assertNotIn("(2, 150)", source)
            self.assertNotIn("(5, 120)", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
