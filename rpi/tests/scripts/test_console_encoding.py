"""回归测试：主项目在**中文 Windows（GBK 控制台）**上也不许崩，且降级要"看得懂"。

缘起（2026-09-26，详见 `ERROR.md` E41）
----------------------------------------
`health_monitor/demo.py` 与 `health_monitor/main.py` 里原本是**直接** `print("⚠️ …")`。
中文 Windows 上 `sys.stdout.encoding` 是 **cp936**，打不出 `⚠️`(U+26A0/U+FE0F)、`❌`(U+274C)
⇒ `UnicodeEncodeError` 把整个进程崩掉：`python -m health_monitor demo` 演到"报警"那一幕就退出，
`serve` 在"有设备打开失败"时也会退出。树莓派（UTF-8）永远看不见，所以这是"只在开发机炸"的暗雷。

判据（可执行的一句话）：
> **在 `sys.stdout` 的编码被钉成 cp936 时，`run_demo()` 仍必须返回 0，且输出里看得懂降级后的符号
> （`⚠️` → `[!]`、`❌` → `[X]`）。**

为什么不是"只测 safe_text"：只测工具函数的话，**漏掉 `print(` 没换成 `safe_print(` 的那一处**
照样绿 —— 本轮 39 处违规里就有不少是这个原因（不是函数坏了，是没接线）。
"""

from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path

_RPI_DIR = Path(__file__).resolve().parents[2]
if str(_RPI_DIR) not in sys.path:
    sys.path.insert(0, str(_RPI_DIR))

from health_monitor import demo as demo_module  # noqa: E402
from health_monitor.console import safe_print, safe_text  # noqa: E402


class FakeGbKStdout(io.TextIOBase):
    """假装自己是中文 Windows 的控制台：编码 cp936，写不出的字符就抛异常（与真机一致）。"""

    encoding = "gbk"

    def __init__(self) -> None:
        super().__init__()
        self._buffer: list[str] = []

    def write(self, text: str) -> int:            # noqa: D102 - 故意照搬真 stdout 的行为
        text.encode(self.encoding)                # 打不出来就抛 UnicodeEncodeError
        self._buffer.append(text)
        return len(text)

    def flush(self) -> None:                      # noqa: D102
        return None

    @property
    def text(self) -> str:
        return "".join(self._buffer)


class TestSafeText(unittest.TestCase):
    """安全打印工具本身：UTF-8 一个字符都不改，窄编码降级成 ASCII 替身。"""

    def test_UTF8环境原样返回(self) -> None:
        for text in ("⚠️ 报警", "✅ 无报警", "❌ 失败", "温度 25.0 ℃"):
            self.assertEqual(safe_text(text, encoding="utf-8"), text)

    def test_GBK环境降级成替身(self) -> None:
        cases = {
            "⚠️ 报警": "[!] 报警",
            "✅ 无报警": "[OK] 无报警",
            "❌ 失败": "[X] 失败",
            "A ↔ B": "A <-> B",
            "x ⇒ y": "x => y",
            "µs": "us",
        }
        for source, expected in cases.items():
            self.assertEqual(safe_text(source, encoding="gbk"), expected)

    def test_没有替身的字符降级成问号而不是抛异常(self) -> None:
        self.assertEqual(safe_text("\U0001f389 完成", encoding="gbk"), "? 完成")

    def test_中文在GBK上原样保留(self) -> None:
        """中文本来就能在 cp936 上显示 —— 降级只该动"真打不出来"的字符。"""
        self.assertEqual(safe_text("温度 25.0 度，湿度 58 %", encoding="gbk"), "温度 25.0 度，湿度 58 %")

    def test_safe_print不炸GBK控制台(self) -> None:
        fake = FakeGbKStdout()
        real = sys.stdout
        try:
            sys.stdout = fake                       # type: ignore[assignment]
            safe_print("⚠️ 报警：心率过高")
        finally:
            sys.stdout = real
        self.assertIn("[!] 报警：心率过高", fake.text)


class TestDemoSurvivesGbKConsole(unittest.TestCase):
    """端到端：把控制台换成 GBK，把 11 幕演示跑完，必须**不崩、不失真**。"""

    def _run_with_gbk_console(self) -> "FakeGbKStdout":
        fake = FakeGbKStdout()
        real = sys.stdout
        try:
            sys.stdout = fake                       # type: ignore[assignment]
            code = demo_module.run_demo()
        finally:
            sys.stdout = real
        self.assertEqual(code, 0, "GBK 控制台下演示必须正常结束（不能因为打印崩掉）")
        return fake

    def test_演示全程不因打印崩掉且11幕都在(self) -> None:
        fake = self._run_with_gbk_console()
        self.assertIn("全链路演示", fake.text)
        self.assertEqual(fake.text.count("幕："), 11, "11 幕一幕都不能少（崩在中途会少）")

    def test_报警行降级后仍然看得懂(self) -> None:
        fake = self._run_gbk_with_warning()
        self.assertIn("[!]", fake.text, "⚠️ 必须降级成 [!]，否则用户看到的是异常而不是报警")
        self.assertNotIn("\u26a0", fake.text, "降级后不该再有打不出来的字符")

    def test_无报警行降级后仍然看得懂(self) -> None:
        fake = self._run_gbk_with_warning()
        self.assertIn("[OK]", fake.text, "✅ 必须降级成 [OK]")

    def _run_gbk_with_warning(self) -> "FakeGbKStdout":
        return self._run_with_gbk_console()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
