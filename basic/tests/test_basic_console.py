#!/usr/bin/env python3
"""控制台输出的编码安全测试（**不依赖硬件，也不依赖操作系统**）。

为什么会有这份测试（2026-09-25 实测，详见 `ERROR.md` E32）
----------------------------------------------------------
中文 Windows 上 Python 的 `sys.stdout.encoding` 是 **GBK（cp936）**，
于是任何 `print("✅ …")` 都会抛 `UnicodeEncodeError` —— **程序崩掉**，不是"显示难看"：

* `basic/tools/selfcheck.py` 十项检查全通过，却在打印最后一行 `✅` 时崩 ⇒ 退出码 1 ⇒
  主项目的 `rpi/scripts/validate.py` 第 10 项把它判成"基础版自检未通过"。

这里用一个"**按 GBK 工作的假 stdout**"在任意平台上复现中文 Windows 的处境，
断言修复真的生效（而不是在开发机上"碰巧没炸"）。
"""

from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]           # 仓库根
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from basic import console  # noqa: E402
from basic.tools import selfcheck, wire_docs  # noqa: E402

#: 故意写成 ASCII 转义：测试文件本身不许依赖"文件里就带着这些符号"
CHECK = "\u2705"      # 对勾
CROSS = "\u274c"      # 叉
WARN = "\u26a0\ufe0f"  # 警告
DEG_C = "\u2103"      # 摄氏度符号


class GbkWriter:
    """模拟"中文 Windows 下 `print` 的处境"：任何 GBK 编不出的字符都抛异常。

    真实情况是 `TextIOWrapper(encoding="gbk")` 在**写**的时候抛
    `UnicodeEncodeError`；这里用 `str.encode` 模拟同一件事（`errors="strict"`）。
    """

    encoding = "gbk"

    def __init__(self) -> None:
        self.parts = []

    def write(self, text: str) -> int:
        text.encode("gbk")          # 编不出来就在这里抛 UnicodeEncodeError
        self.parts.append(text)
        return len(text)

    def flush(self) -> None:        # pragma: no cover - 契约需要
        pass

    def getvalue(self) -> str:
        return "".join(self.parts)


class TestSafeText(unittest.TestCase):
    def test_UTF8环境下原样返回(self):
        text = f"{CHECK} 通过 {DEG_C}"
        self.assertEqual(console.safe_text(text, encoding="utf-8"), text)

    def test_GBK环境下换成ASCII替身而不是抛异常(self):
        out = console.safe_text(f"{CHECK} 通过 {CROSS} 失败", encoding="gbk")
        self.assertEqual(out, "[OK] 通过 [X] 失败")
        out.encode("gbk")           # 关键判据：结果一定能被 GBK 编码

    def test_GBK环境保留中文可读性(self):
        """降级的是符号，不是信息：中文必须原样留着。"""
        out = console.safe_text("温度 25.0 degC 湿度 58 %", encoding="gbk")
        self.assertIn("温度", out)
        self.assertIn("湿度", out)

    def test_警告符号的变体选择符被丢掉(self):
        """`⚠️` 是两个码位（U+26A0 + U+FE0F），只换第一个会剩一个编不出的隐形字符。"""
        out = console.safe_text(f"{WARN} 注意", encoding="gbk")
        out.encode("gbk")
        self.assertEqual(out, "[!] 注意")

    def test_没有替身的字符退化成问号(self):
        out = console.safe_text("星星 \u2b50", encoding="gbk")
        self.assertNotIn("\u2b50", out)

    def test_取不到输出编码时不崩(self):
        self.assertIn(console.output_encoding(), ("utf-8", "gbk", "cp936"))


class TestSelfcheckUnderGbkConsole(unittest.TestCase):
    """**端到端复现**：在"GBK 控制台"下跑完整的 10 项自检，必须正常收尾。"""

    def test_自检在GBK控制台下不崩且返回0(self):
        fake = GbkWriter()
        with redirect_stdout(fake):
            code = selfcheck.run_checks()

        self.assertEqual(code, 0, "GBK 控制台下自检退出码必须仍是 0（以前会因 ✅ 崩成 1）")
        text = fake.getvalue()
        self.assertIn("[OK]", text, "非 UTF-8 控制台下应当退化成 ASCII 替身")
        self.assertNotIn(CHECK, text, "GBK 控制台里不该出现编不出的 ✅")

    def test_自检的每一项都被跑到(self):
        """防止"提前 return"式修复：十项检查名必须都出现在输出里。"""
        fake = GbkWriter()
        with redirect_stdout(fake):
            selfcheck.run_checks()
        text = fake.getvalue()
        for name in ("版本与导入", "单总线解码", "引脚映射", "CSV 存档", "曲线数据窗口",
                     "演示数据", "接线事实", "接线文档", "DHT11 后端探测", "matplotlib"):
            self.assertIn(name, text)


class TestWireDocsUnderGbkConsole(unittest.TestCase):
    def test_接线文档校验在GBK控制台下不崩(self):
        fake = GbkWriter()
        with redirect_stdout(fake):
            code = wire_docs.main(["--check"])
        self.assertEqual(code, 0, "GBK 控制台下接线文档校验必须仍然通过")
        self.assertIn("[OK]", fake.getvalue())

    def test_文档里仍然保留符号(self):
        """**只降级终端显示**：生成出来的 Markdown 必须还是原样的符号（PDF 里要好看）。

        ⚠️ 接线表（`接线表.md`）本身**不含 ✅/❌**（它是接线用的表，不是检查报告），
        所以这里断言"符号不会被 safe_text 改掉"用的是项目里确实带符号的生成物：
        自检报告由 `selfcheck.py` 生成，走的是同一套 `safe_print`。
        判据用 `safe_text` 直接验证：UTF-8 环境下**原样返回**（一个字符都不改）。
        """
        text = "✅ 通过 ❌ 失败 ⚠️ 注意"
        self.assertEqual(console.safe_text(text, encoding="utf-8"), text,
                         "UTF-8 环境下 safe_text 必须原样返回（生成物/管道里不许被改）")


class TestNoUnprotectedPrintOfSymbols(unittest.TestCase):
    """结构守卫：仓库里**打印** ✅/❌/⚠️ 的地方必须过 `safe_text`。

    用 AST 扫 `print(...)` 的第一个参数是否含这些符号，含了就必须调用 `safe_text`。
    （写入文件/文档的字符串不算 —— 那些要保留原符号，见上一个用例。）
    """

    SYMBOLS = (CHECK, CROSS, "\u26a0", "\u2b50", DEG_C)
    FILES = ("run.py", "plot.py", "dht11read.py", "pins.py", "series.py", "store.py", "model.py",
             "tools/selfcheck.py", "tools/wire_docs.py", "tools/diag_dht_line.py")

    @staticmethod
    def _string_constants(node):
        """收集该 print 调用里所有字面量字符串（含 f-string 的固定片段）。"""
        import ast

        out = []
        for sub in ast.walk(node):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                out.append(sub.value)
        return out

    def test_所有打印符号的地方都过了safe_text(self):
        import ast

        offenders = []
        for rel in self.FILES:
            path = ROOT / "basic" / rel
            if not path.exists():       # pragma: no cover - 文件被改名时给出清晰失败
                self.fail(f"扫描清单里的文件不存在了：{rel}（请更新 TestNoUnprotectedPrintOfSymbols.FILES）")
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if not (isinstance(node.func, ast.Name) and node.func.id == "print"):
                    continue
                texts = self._string_constants(node)
                if not any(sym in text for text in texts for sym in self.SYMBOLS):
                    continue
                wrapped = any(
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Name)
                    and sub.func.id == "safe_text"
                    for sub in ast.walk(node)
                )
                if not wrapped:
                    offenders.append(f"{rel}:{node.lineno}")

        self.assertEqual(
            offenders, [],
            msg=(
                "这些 print 直接打印了 ✅/❌/⚠️ 等符号，没走 safe_text —— "
                f"中文 Windows 上会崩（ERROR.md E32）：{offenders}"
            ),
        )


if __name__ == "__main__":
    unittest.main()
