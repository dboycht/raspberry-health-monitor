"""回归测试：`scripts/validate.py` 在**中文 Windows** 上必须真判据、不许假失败。

缘起（2026-09-25 实测，详见 `ERROR.md` E32）
--------------------------------------------
`validate.py` 原来给每个子进程都写 `encoding="utf-8"` 抓输出，而**子进程按什么编码打印
是由子进程自己决定的**：Python 在中文 Windows 上 `sys.stdout.encoding == "gbk"`
（cp936；本机实测即使控制台已经是 `chcp 65001` 也一样）。结果子进程吐 GBK 字节、
父进程按 UTF-8 解 ⇒ `subprocess.run` 内部抛 `UnicodeDecodeError`、`stdout` 变成 `None` ⇒

* 「文档一致性」「端到端演示」「基础版（basic/）」**三项被假报为失败**，
  而它们单独跑（或树莓派的 UTF-8 locale 下）全是通过的。

这类"假失败"比"假通过"更坏：它会让人开始**不信任**提交前检查，进而真的漏掉问题。
所以这里既测"修好没"（子进程强制 UTF-8 + 容错解码），也测"修法有没有被绕过"
（AST 扫描：`validate.py` 里不许再出现裸 `subprocess.run`）。
"""

from __future__ import annotations

import ast
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

# ⚠️ 测试必须自包含：不能只依赖 conftest.py 改 sys.path（那是 pytest 专有钩子，
# `python -m unittest discover -s tests` 不会加载它）。
_RPI_DIR = Path(__file__).resolve().parents[2]      # .../rpi
_SCRIPTS_DIR = _RPI_DIR / "scripts"
for _path in (_RPI_DIR, _SCRIPTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import validate  # noqa: E402  必须在 sys.path 处理之后导入


#: 子进程脚本：只说明两件事 —— ①环境变量到底被设成了什么 ②非 ASCII 能不能打印出来。
#: **必须写成 ASCII 源码 + 转义序列**（"✅" 与 "温度" 都不写进本文件），
#: 这样脚本本身在任何编码假设下都不会成为变量。
_CHILD_PROBE = (
    "import os, sys\n"
    "print('IOENC=' + os.environ.get('PYTHONIOENCODING', ''))\n"
    "print('UTF8FLAG=' + os.environ.get('PYTHONUTF8', ''))\n"
    "print('STDOUT=' + str(sys.stdout.encoding))\n"
    "print('MARK=' + '\\u2705' + '\\u6e29\\u5ea6')\n"
)


class TestChildEnv(unittest.TestCase):
    def test_子进程环境被强制为UTF8(self) -> None:
        env = validate.child_env()
        self.assertEqual(env["PYTHONIOENCODING"], "utf-8")
        self.assertEqual(env["PYTHONUTF8"], "1")

    def test_不污染当前进程的环境(self) -> None:
        """必须改副本：`validate.py` 跑完不能把调用者的环境改掉。"""
        before = os.environ.get("PYTHONIOENCODING")
        validate.child_env()
        self.assertEqual(os.environ.get("PYTHONIOENCODING"), before)


class TestRunPythonUnicode(unittest.TestCase):
    def test_父进程环境是GBK时子进程仍按UTF8打印(self) -> None:
        """**核心回归**：模拟中文 Windows 的 GBK 父环境，验证修法真的赢了它。"""
        with mock.patch.dict(os.environ, {"PYTHONIOENCODING": "gbk"}, clear=False):
            proc = validate.run_python(["-c", _CHILD_PROBE], cwd=_RPI_DIR)

        self.assertEqual(proc.returncode, 0, msg=f"stderr={proc.stderr!r}")
        out = proc.stdout or ""
        self.assertIn("IOENC=utf-8", out, "父进程给的 PYTHONIOENCODING=gbk 必须被子进程看到 utf-8")
        self.assertIn("UTF8FLAG=1", out)
        self.assertIn("MARK=\u2705\u6e29\u5ea6", out, "非 ASCII 必须被完整读到（不是 U+FFFD）")

    def test_非ASCII输出能被解开且不含替换字符(self) -> None:
        proc = validate.run_python(["-c", _CHILD_PROBE], cwd=_RPI_DIR)
        out = proc.stdout or ""
        self.assertNotIn("\ufffd", out, "出现 U+FFFD 说明解码兜底在替我们掩盖编码问题")
        self.assertTrue(out.startswith("IOENC="), "输出必须原样可读，不能是空串")

    def test_标准错误与退出码原样传递(self) -> None:
        proc = validate.run_python(
            ["-c", "import sys; sys.stderr.write('E\\u9519\\n'); raise SystemExit(3)"],
            cwd=_RPI_DIR,
        )
        self.assertEqual(proc.returncode, 3)
        self.assertIn("E\u9519", proc.stderr or "")


class TestDecodeFallback(unittest.TestCase):
    """容错解码：即使子进程**不听**环境变量（例如非 Python 程序），也不许抛异常。"""

    def test_UTF8字节正常(self) -> None:
        self.assertEqual(validate._decode("温度".encode("utf-8")), "温度")

    def test_GBK字节也能读出来(self) -> None:
        self.assertEqual(validate._decode("温度".encode("gbk")), "温度")

    def test_混合编码退化为替换字符而不是抛异常(self) -> None:
        raw = "温度".encode("gbk") + "ok".encode("utf-8")
        out = validate._decode(raw)
        self.assertIsInstance(out, str)
        self.assertIn("ok", out)

    def test_None与str原样返回(self) -> None:
        self.assertEqual(validate._decode(None), "")
        self.assertEqual(validate._decode("已经是 str"), "已经是 str")


class TestFailureHint(unittest.TestCase):
    """"没有输出"与"输出是坏的"必须能被区分出来（否则又回到"假失败"看不清）。"""

    def test_演示失败且无输出时给出编码提示(self) -> None:
        fake = validate.subprocess.CompletedProcess(args=["demo"], returncode=1, stdout="", stderr="")
        with mock.patch.object(validate, "run_python", return_value=fake):
            ok, detail = validate.check_demo()
        self.assertFalse(ok)
        self.assertIn("E32", detail)

    def test_测试通过时摘要取自子进程输出(self) -> None:
        fake = validate.subprocess.CompletedProcess(
            args=["pytest"], returncode=0,
            stdout="566 passed, 1 skipped in 17.75s\n", stderr="",
        )
        with mock.patch.object(validate, "run_python", return_value=fake):
            ok, detail = validate.check_tests()
        self.assertTrue(ok)
        self.assertIn("566 passed", detail)


class TestNoBareSubprocessCalls(unittest.TestCase):
    """**结构守卫**：新增子进程调用时不许绕过 `run_python()`。

    为什么用 AST 而不是 grep：注释与文档里也会出现 `subprocess.run` 字样
    （本文件的说明、`validate.py` 的模块 docstring 都提到它），grep 会误报。
    """

    @staticmethod
    def _bare_subprocess_calls(tree: ast.AST):
        """返回 (行号, 属性名, 所在函数名)——凡是 `subprocess.xxx(...)` 形式的调用。"""
        found = []
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(func):
                if not isinstance(node, ast.Call):
                    continue
                callee = node.func
                if (
                    isinstance(callee, ast.Attribute)
                    and isinstance(callee.value, ast.Name)
                    and callee.value.id == "subprocess"
                ):
                    found.append((node.lineno, callee.attr, func.name))
        return found

    def test_唯一的裸subprocess调用在run_python里(self) -> None:
        tree = ast.parse((_SCRIPTS_DIR / "validate.py").read_text(encoding="utf-8"))
        found = self._bare_subprocess_calls(tree)

        self.assertTrue(found, "没扫到任何 subprocess 调用，说明扫描逻辑本身失效了（守卫不能静默失效）")
        outside = [item for item in found if item[2] != "run_python"]
        self.assertEqual(
            outside,
            [],
            msg=(
                "validate.py 里出现了 run_python() 之外的裸子进程调用 "
                f"(行号, 调用, 所在函数)={outside}；请改用 run_python()，"
                "否则中文 Windows 上会假失败（ERROR.md E32）"
            ),
        )
        self.assertTrue(
            any(attr == "run" for _, attr, _ in found),
            "run_python() 内部应当用 subprocess.run 起进程",
        )


if __name__ == "__main__":
    unittest.main()
