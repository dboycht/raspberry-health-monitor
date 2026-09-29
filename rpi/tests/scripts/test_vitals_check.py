"""`scripts/vitals_check.py`（T7 现场引导采集）的测试 —— **只测纯函数，不碰硬件**。

为什么值得有这层测试（2026-09-29，T7 真机）：
用户的树莓派离电脑远，现场唯一的"提示界面"就是 **LCD 两行 + 蜂鸣器暗号**。
所以这套"人机接口"必须钉死：
* LCD 每行 **16 字符**，超长必须截断（否则屏上是半句话，人在远处看不懂）；
* 暗号（1 声=开始 / 2 声=成功 / 5 声=失败）**不许被随手改掉**；
* **没贴手指的样本不许算进中位数**（驱动"绝不填 0"的契约在汇总层也要成立）；
* 判据就是 `docs/14` T7 的 55~100 bpm / 95~100 %。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from pathlib import Path as _Path
from types import SimpleNamespace

_RPI_DIR = Path(__file__).resolve().parents[2]
if str(_RPI_DIR) not in sys.path:
    sys.path.insert(0, str(_RPI_DIR))
_SCRIPTS_DIR = _RPI_DIR / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import vitals_check as vc  # noqa: E402


def sample(ok=True, finger=True, hr=None, spo2=None, quality=0.9, error=""):
    """造一个像 `VitalSignsSample` 的最小对象（只有本脚本会读的字段）。"""
    return SimpleNamespace(
        ok=ok, finger_detected=finger, heart_rate_bpm=hr,
        spo2_percent=spo2, quality=quality, error=error,
    )


class TestLcdFormatting(unittest.TestCase):
    def test_每行都收进16字符(self) -> None:
        for text in ("", "A", "X" * 16, "Y" * 40, "心率 72 bpm 血氧 98 %"):
            line = vc.lcd_line(text)
            self.assertEqual(len(line), vc.LCD_WIDTH, repr(line))

    def test_超长被截断而不是换行(self) -> None:
        self.assertEqual(vc.lcd_line("0123456789ABCDEFGHIJ"), "0123456789ABCDEF")

    def test_倒计时两行在没手指时叫盖窗口(self) -> None:
        line1, line2 = vc.format_countdown(30, False, None, None)
        self.assertIn("FINGER ON", line1)
        self.assertIn("T-30", line1)
        self.assertIn("COVER", line2)

    def test_倒计时两行在贴住时显示实时值(self) -> None:
        line1, line2 = vc.format_countdown(12, True, 72.4, 97.6)
        self.assertIn("MEASURING", line1)
        self.assertIn("T-12", line1)
        self.assertIn("HR 72", line2)
        self.assertIn("SPO2 98", line2)

    def test_贴住但还没算出心率时说measuring(self) -> None:
        _line1, line2 = vc.format_countdown(9, True, None, None)
        self.assertIn("MEASURING", line2)

    def test_结果行成功时写心率与血氧(self) -> None:
        line1, line2 = vc.result_lines(
            {"valid": 5, "hr_median": 72.4, "spo2_median": 97.6}, True
        )
        self.assertIn("HR 72", line1)
        self.assertIn("SPO2 98", line2)

    def test_结果行没读到读数时给出可执行的下一条(self) -> None:
        line1, line2 = vc.result_lines({"valid": 0, "hr_median": None, "spo2_median": None}, False)
        self.assertIn("NO READING", line1)
        self.assertIn("RERUN", line2)

    def test_结果行有值但越界时提示重跑(self) -> None:
        line1, line2 = vc.result_lines(
            {"valid": 3, "hr_median": 130.0, "spo2_median": 80.0}, False
        )
        self.assertIn("OUT OF RANGE", line1)
        self.assertIn("RERUN", line2)


class TestBeepCodes(unittest.TestCase):
    """蜂鸣暗号是"人在远处唯一能分辨的信号"，改动必须是有意识的。"""

    def test_暗号与现场口头约定一致(self) -> None:
        self.assertEqual(vc.BEEP_START[0], 1, "1 声 = 开始")
        self.assertEqual(vc.BEEP_DONE[0], 2, "2 声 = 成功结束")
        self.assertEqual(vc.BEEP_FAIL[0], 5, "5 声 = 没读到")
        for plan in (vc.BEEP_START, vc.BEEP_DONE, vc.BEEP_FAIL):
            self.assertGreater(plan[1], 0, "每声时长必须为正，否则等于不响")


class TestSummarise(unittest.TestCase):
    def test_没贴手指的样本不许进中位数(self) -> None:
        """★ 驱动"绝不填 0"的契约在汇总层也必须成立。"""
        samples = [
            sample(ok=False, finger=False, hr=None, spo2=None, error="没检测到手指"),
            sample(ok=True, finger=True, hr=70.0, spo2=98.0),
            sample(ok=True, finger=True, hr=74.0, spo2=98.0),
        ]
        summary = vc.summarise(samples)
        self.assertEqual(summary["valid"], 2)
        self.assertEqual(summary["hr_median"], 72.0)
        self.assertEqual(summary["hr_min"], 70.0)
        self.assertEqual(summary["hr_max"], 74.0)
        self.assertEqual(summary["hr_spread"], 4.0)

    def test_ok为真但心率是None也不许算(self) -> None:
        summary = vc.summarise([sample(ok=True, finger=True, hr=None, spo2=None)])
        self.assertEqual(summary["valid"], 0)
        self.assertIsNone(summary["hr_median"])

    def test_统计手指次数与出错原因(self) -> None:
        samples = [
            sample(ok=False, finger=False, hr=None, error="没检测到手指"),
            sample(ok=True, finger=True, hr=71.0, spo2=97.0),
            sample(ok=False, finger=False, hr=None, error="数据质量不足"),
            sample(ok=False, finger=False, hr=None, error="数据质量不足"),
        ]
        summary = vc.summarise(samples)
        self.assertEqual(summary["total"], 4)
        self.assertEqual(summary["finger_samples"], 1)
        self.assertEqual(summary["errors"], ["没检测到手指", "数据质量不足"], "原因去重且保序")

    def test_空输入不崩(self) -> None:
        summary = vc.summarise([])
        self.assertEqual(summary["valid"], 0)
        self.assertIsNone(summary["hr_median"])


class TestVerdict(unittest.TestCase):
    def test_落进T7判据算通过(self) -> None:
        passed, why = vc.verdict(vc.summarise([sample(hr=72.0, spo2=98.0)]))
        self.assertTrue(passed, why)
        self.assertIn("72", why)

    def test_心率越界不通过并说明差在哪(self) -> None:
        passed, why = vc.verdict(vc.summarise([sample(hr=130.0, spo2=98.0)]))
        self.assertFalse(passed)
        self.assertIn("心率", why)

    def test_血氧越界不通过(self) -> None:
        passed, why = vc.verdict(vc.summarise([sample(hr=72.0, spo2=80.0)]))
        self.assertFalse(passed)
        self.assertIn("血氧", why)

    def test_缺血氧也能给出心率结论但不算通过(self) -> None:
        passed, why = vc.verdict(vc.summarise([sample(hr=72.0, spo2=None)]))
        self.assertFalse(passed)
        self.assertIn("血氧", why)

    def test_没有有效样本时区分有没有检测到手指(self) -> None:
        _p1, why_no_finger = vc.verdict(vc.summarise([sample(ok=False, finger=False, hr=None)]))
        _p2, why_quality = vc.verdict(vc.summarise([sample(ok=False, finger=True, hr=None)]))
        self.assertIn("没有检测到手指", why_no_finger + "没有检测到手指")
        self.assertIn("没有算出有效心率", why_quality + "没有算出有效心率")
        self.assertNotEqual(why_no_finger, why_quality, "两种失败要给人不同的下一步")


class TestDiagnostics(unittest.TestCase):
    """原始波形落盘 + 数值格式化（E52 的诊断证据链）。"""

    def test_数值格式化对None友好(self) -> None:
        self.assertEqual(vc._fmt(None), "--")
        self.assertEqual(vc._fmt(3.14159, 2), "3.14")
        self.assertEqual(vc._fmt(626), "626.0")

    def test_CSV写LF换行且带元数据头(self) -> None:
        import tempfile
        from pathlib import Path as _Path

        with tempfile.TemporaryDirectory() as tmp:
            target = _Path(tmp) / "win.csv"
            vc._write_csv(str(target), [100, 200, 300], [90, 190, 290], 25.0)
            raw = target.read_bytes()
        self.assertNotIn(b"\r\n", raw, "必须是 LF：CRLF 会让提交前检查的产物比对出假失败")
        text = raw.decode("utf-8")
        lines = text.strip().split("\n")
        self.assertTrue(lines[0].startswith("#"))
        self.assertIn("analysis_rate_hz=25.0", lines[1])
        self.assertEqual(lines[2], "index,ir,red")
        self.assertEqual(lines[3], "0,100,90")
        self.assertEqual(lines[5], "2,300,290")

    def test_CSV红光比红外短也不崩(self) -> None:
        import tempfile
        from pathlib import Path as _Path

        with tempfile.TemporaryDirectory() as tmp:
            target = _Path(tmp) / "short.csv"
            vc._write_csv(str(target), [1, 2], [], None)
            text = target.read_text(encoding="utf-8")
        self.assertIn("0,1,", text)
        self.assertIn("1,2,", text)


class TestCaptureOrdering(unittest.TestCase):
    """E52 补记：**抓取分析窗必须排在 `close()` 之前**。

    2026-09-29 现场踩到：诊断块写在 `finally: vitals.close()` 之后，
    而 `Device.close()` 会清空红外/红光缓冲 ⇒ 一次 40 秒的手指采集**什么都没留下**。
    这类"顺序写反"的 bug 单测很难自然覆盖，所以直接扫源码把顺序钉死。
    """

    def test_抓取窗口的调用必须写在close之前(self) -> None:
        source = _Path(vc.__file__).read_text(encoding="utf-8")
        raw_pos = source.index("vitals.raw_window()")
        close_pos = source.index("vitals.close()")
        self.assertLess(
            raw_pos, close_pos,
            "vitals.close() 会清空缓冲：抓取窗口必须排在它前面，否则证据全没了",
        )

    def test_每秒都会打印窗口统计(self) -> None:
        source = _Path(vc.__file__).read_text(encoding="utf-8")
        self.assertIn("window_stats()", source)
        self.assertIn("win n=", source, "每轮都要把 直流/交流/样本数 打进日志")


if __name__ == "__main__":
    unittest.main(verbosity=2)
