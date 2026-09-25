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


class TestNodeCertFallback(unittest.TestCase):
    """`run_node()`：文档写的 `node scripts/check_ci.cjs` 必须能用（E33）。

    这台机器装了 TLS 解密代理（Steam++），Node 自带 CA 列表不认 Windows 证书存储里的
    自签根证书 ⇒ 直接跑会 `UNABLE_TO_VERIFY_LEAF_SIGNATURE`。
    修法是"**只在真的报证书错时**带 `--use-system-ca` 重跑一次"，
    所以这里要证明两件事：证书错会重试，**非证书错绝不重试**（否则会把真问题掩盖掉）。
    """

    @staticmethod
    def _completed(returncode, stdout="", stderr=""):
        """假的 `subprocess.CompletedProcess`——刻意给**原始 bytes**，
        因为 `run_node()` 的职责之一就是把这些字节解成 str（与真实调用一致）。"""
        return validate.subprocess.CompletedProcess(
            args=["node"],
            returncode=returncode,
            stdout=validate._decode(stdout.encode()),
            stderr=validate._decode(stderr.encode()),
        )

    def test_证书错会带着use_system_ca重试一次(self) -> None:
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            if "--use-system-ca" in cmd:
                return self._completed(0, "RESULT: latest run SUCCESS")
            return self._completed(1, stderr="Error: unable to verify the first certificate")

        with mock.patch.object(validate.shutil, "which", return_value="C:/node/node.exe"), \
                mock.patch.object(validate, "_node_supports_system_ca", return_value=True), \
                mock.patch.object(validate.subprocess, "run", side_effect=fake_run):
            proc = validate.run_node(["scripts/check_ci.cjs"], cwd=validate.REPO_ROOT)

        self.assertEqual(proc.returncode, 0)
        self.assertEqual(len(calls), 2, f"应当只重试一次，实际调用：{calls}")
        self.assertEqual(calls[0], ["C:/node/node.exe", "scripts/check_ci.cjs"])
        self.assertEqual(calls[1], ["C:/node/node.exe", "--use-system-ca", "scripts/check_ci.cjs"])

    def test_非证书错不重试(self) -> None:
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            return self._completed(1, stderr="HTTP 404 Not Found")

        with mock.patch.object(validate.shutil, "which", return_value="C:/node/node.exe"), \
                mock.patch.object(validate, "_node_supports_system_ca", return_value=True), \
                mock.patch.object(validate.subprocess, "run", side_effect=fake_run):
            proc = validate.run_node(["scripts/check_ci.cjs"], cwd=validate.REPO_ROOT)

        self.assertEqual(proc.returncode, 1)
        self.assertEqual(len(calls), 1, "非证书错不许重试（会把真问题掩盖成'重试后仍失败'）")
        self.assertFalse(any("--use-system-ca" in cmd for cmd in calls))

    def test_旧版node不支持该标志时不重试(self) -> None:
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            return self._completed(1, stderr="unable to verify the first certificate")

        with mock.patch.object(validate.shutil, "which", return_value="C:/node/node.exe"), \
                mock.patch.object(validate, "_node_supports_system_ca", return_value=False), \
                mock.patch.object(validate.subprocess, "run", side_effect=fake_run):
            proc = validate.run_node(["scripts/check_ci.cjs"], cwd=validate.REPO_ROOT)

        self.assertEqual(proc.returncode, 1)
        self.assertEqual(len(calls), 1, "Node < 22.15 不认识 --use-system-ca，不该白跑一趟")

    def test_没有node时给出可读的失败而不是崩(self) -> None:
        with mock.patch.object(validate.shutil, "which", return_value=None):
            proc = validate.run_node(["scripts/check_ci.cjs"], cwd=validate.REPO_ROOT)
        self.assertEqual(proc.returncode, 127)
        self.assertIn("node not found", proc.stderr)

    def test_check_node_scripts把自检结果翻译成人话(self) -> None:
        ok = self._completed(0, "selftest OK: 25 assertions passed (node v24.14.0)\n")
        with mock.patch.object(validate.shutil, "which", return_value="C:/node/node.exe"), \
                mock.patch.object(validate, "run_node", return_value=ok):
            passed, detail = validate.check_node_scripts()
        self.assertTrue(passed)
        self.assertIn("25 assertions", detail)

    def test_自检失败时把失败行带出来(self) -> None:
        bad = self._completed(
            1,
            "  FAIL every script is pure ASCII\n\nFAILED: 1 of 25 assertions\n  - x: y\n",
        )
        with mock.patch.object(validate.shutil, "which", return_value="C:/node/node.exe"), \
                mock.patch.object(validate, "run_node", return_value=bad):
            passed, detail = validate.check_node_scripts()
        self.assertFalse(passed)
        self.assertIn("ASCII", detail)

    def test_没抓到FAIL行时也要给出尾部输出(self) -> None:
        """只显示一句"失败"、什么都没有，等于没告诉我们任何信息。"""
        bad = self._completed(1, "some unrelated node output\n", "and an error on stderr\n")
        with mock.patch.object(validate.shutil, "which", return_value="C:/node/node.exe"), \
                mock.patch.object(validate, "run_node", return_value=bad):
            passed, detail = validate.check_node_scripts()
        self.assertFalse(passed)
        self.assertIn("unrelated node output", detail)


class TestBasicVersionRunnerFallback(unittest.TestCase):
    """`check_basic_version()` 在没有 pytest 的机器上也必须能跑（E35，真机实测）。

    树莓派上没装 pytest（README 明说核心零第三方依赖），原来写死 `-m pytest`
    ⇒ 真机第 10 项恒红，而那 124 项单测用标准库 `unittest` 跑是**全绿**的。
    `unittest` 还把 "Ran N tests / OK" 摘要写在 **stderr**，所以判据要连 stderr 一起读。
    """

    @staticmethod
    def _proc(returncode: int, stdout: str = "", stderr: str = ""):
        return validate.subprocess.CompletedProcess(
            args=["python"],
            returncode=returncode,
            stdout=validate._decode(stdout.encode()),
            stderr=validate._decode(stderr.encode()),
        )

    def _run_with(self, has_pytest: bool, tests_result):
        """喂给 `run_python` 确定的假结果；返回 (ok, detail) 与实际调用列表。"""
        calls: list = []

        def fake(argv, cwd=None, env_extra=None):
            calls.append(list(argv))
            if list(argv[:2]) == ["-c", "import pytest"]:
                return self._proc(0 if has_pytest else 1)
            if argv and "selfcheck.py" in str(argv[0]):
                return self._proc(0, "结果：全部 10 项通过\n")
            return tests_result

        with mock.patch.object(validate, "run_python", side_effect=fake):
            return validate.check_basic_version(), calls

    def test_有pytest时走pytest(self) -> None:
        result, calls = self._run_with(True, self._proc(0, "124 passed, 4 subtests passed in 1.5s\n"))
        passed, detail = result
        self.assertTrue(passed, detail)
        self.assertIn("pytest", detail)
        self.assertIn(["-m", "pytest", "basic/tests", "-q"], calls)

    def test_没pytest时退回unittest(self) -> None:
        """★ 真机路径：树莓派上就是这个分支。"""
        # unittest 的摘要写在 stderr —— 这正是真机上"只显示单测 OK"的那个坑
        result, calls = self._run_with(
            False, self._proc(0, "", "Ran 124 tests in 0.9s\n\nOK\n"),
        )
        passed, detail = result
        self.assertTrue(passed, detail)
        self.assertIn("unittest", detail, "失败时也要能看出是哪个 runner 跑的")
        self.assertIn(["-m", "unittest", "discover", "-s", "basic/tests"], calls)
        self.assertFalse(any(list(c[:2]) == ["-m", "pytest"] for c in calls), "没有 pytest 就不该调它")

    def test_读得到写在stderr里的测试摘要(self) -> None:
        """判据与顺序无关：**只看 stderr 里的摘要有没有被带进报告**。

        ⚠️ 别断言"取到的是 `Ran N tests` 而不是 `OK`"：`run_python` 把 stdout/stderr 拼成
        `"…\\nRan N tests…\\n\\nOK\\n"`，循环从后往前取，先命中的就是 `OK`。
        本轮就因为这个**依赖实现细节的脆弱断言**白排查了一轮 —— 判据只该锁"读没读到"。

        做法：让假结果的 **stderr 非空、stdout 为空**，断言 stderr 里的摘要出现在报告里 ——
        只看 stdout 的实现必然取不到它（已反向验证：去掉 `+ tests.stderr` 这条就红）。
        """
        result, _ = self._run_with(False, self._proc(0, "", "Ran 9 tests in 0.1s\n"))
        passed, detail = result
        self.assertTrue(passed, detail)
        self.assertIn("Ran 9 tests", detail, "unittest 的摘要在 stderr 里，必须连 stderr 一起读")

    def test_基础版单测失败时给出runner与输出(self) -> None:
        result, _ = self._run_with(False, self._proc(1, "", "FAILED (failures=1)\n"))
        passed, detail = result
        self.assertFalse(passed)
        self.assertIn("unittest", detail, "失败时要写清是哪个 runner 跑的")
        self.assertIn("failures=1", detail, "要把失败原因带出来")

    def test_basic测试目录必须是可导入的包(self) -> None:
        """没有 `basic/tests/__init__.py` 时 `unittest discover` 会报
        `Start directory is not importable` —— 这正是真机上踩到的第二个坑。"""
        init = validate.REPO_ROOT / "basic" / "tests" / "__init__.py"
        self.assertTrue(init.exists(), "缺少 basic/tests/__init__.py（unittest 发现会失败）")


class TestScriptsPrintSymbolsSafely(unittest.TestCase):
    """**AST 守卫**：`rpi/scripts/*.py` 里打印 ✅/❌/⚠ 的地方必须过 `safe_text`。

    为什么（2026-09-25 真机实测，E32/E35）：`check_docs.py` 当时漏了这道加固，
    在中文 Windows（GBK 控制台）上打印"✅ 全部通过"直接抛 `UnicodeEncodeError` ——
    整个检查器崩掉，`validate.py` 把它报成"文档自检失败"，而**一个悬空引用都没有**。
    假失败会让人不再信任提交前检查，所以这里用机器把它钉住。
    """

    SYMBOLS = ("\u2705", "\u274c", "\u26a0", "\u2103", "\u2b50")

    @staticmethod
    def _printed_symbols(tree) -> list:
        """返回行号列表——**裸** `print(...)` 里带符号、且不在 `safe_print(...)` 里面的调用。

        注意别把 `safe_print(safe_text("✅ …"))` 那种**已经加固过**的当违规：
        `ast.walk` 会把嵌套的 `print`/`safe_text` 一起走一遍，所以这里要记父节点。
        多行拼接的 print 也跳过（本轮的重构脚本刻意不碰它们，避免改错）。
        """
        found = []

        def walk(node, parent_chain):
            if isinstance(node, ast.Call):
                is_print = isinstance(node.func, ast.Name) and node.func.id == "print"
                if is_print:
                    inside_safe = any(
                        isinstance(anc, ast.Call)
                        and isinstance(anc.func, ast.Name)
                        and anc.func.id == "safe_print"
                        for anc in parent_chain
                    )
                    # `print(safe_text("✅ …"))` 也算加固过（safe_text 逐个参数降级）
                    wraps_safe_text = any(
                        isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Name)
                        and sub.func.id == "safe_text"
                        for sub in ast.walk(node)
                    )
                    single_line = node.lineno == (node.end_lineno or node.lineno)
                    literals = [
                        sub.value for sub in ast.walk(node)
                        if isinstance(sub, ast.Constant) and isinstance(sub.value, str)
                    ]
                    has_symbol = any(
                        sym in text for text in literals
                        for sym in TestScriptsPrintSymbolsSafely.SYMBOLS
                    )
                    if has_symbol and not inside_safe and not wraps_safe_text and single_line:
                        found.append(node.lineno)
            for child in ast.iter_child_nodes(node):
                walk(child, parent_chain + [node])

        walk(tree, [])
        return found

    def test_rpi脚本打印符号时都过了safe_text(self) -> None:
        scripts_dir = validate.REPO_ROOT / "rpi" / "scripts"
        offenders = []
        scanned = 0
        for path in sorted(scripts_dir.glob("*.py")):
            scanned += 1
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for line in self._printed_symbols(tree):
                offenders.append(f"{path.name}:{line}")
        self.assertGreater(scanned, 5, "没扫到脚本，守卫可能失效了")
        self.assertEqual(
            offenders, [],
            msg=(
                "这些 print 直接打印了 ✅/❌/⚠ 却没走 safe_text —— "
                f"中文 Windows（GBK）上会把整个脚本崩掉（E32）：{offenders}"
            ),
        )


class TestNoBareSubprocessCalls(unittest.TestCase):
    """**结构守卫**：新增子进程调用时不许绕过 `run_python()` / `run_node()`。

    为什么用 AST 而不是 grep：注释与文档里也会出现 `subprocess.run` 字样
    （本文件的说明、`validate.py` 的模块 docstring 都提到它），grep 会误报。

    允许的落点（其它任何函数里出现裸 `subprocess` 调用都算违规）：
    `run_python` / `run_node` 是给调用方的入口，`_node_supports_system_ca` 只在
    "判断当前 Node 能不能用 `--use-system-ca`"时问一次版本号。
    """

    #: 允许直接调用 subprocess 的函数名 + 理由
    ALLOWED = {
        "run_python": "Python 子进程统一入口（强制 UTF-8 + 容错解码）",
        "run_node": "Node 子进程统一入口（额外处理本机 TLS 代理证书链，E33）",
        "_node_supports_system_ca": "只问一次 node --version",
    }

    @staticmethod
    def _bare_subprocess_calls(tree: ast.AST):
        """返回 (行号, 属性名, **顶层**函数名)——凡是 `subprocess.xxx(...)` 形式的调用。

        只看**顶层**函数名，一路 `ast.walk` 到底：`run_node()` 里有个嵌套的 `_run()`
        辅助函数，它属于 `run_node` 这个被允许的入口，不该被算成"绕过入口"。
        """
        found = []
        for func in tree.body:
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

    def test_唯一的裸subprocess调用在允许的入口里(self) -> None:
        tree = ast.parse((_SCRIPTS_DIR / "validate.py").read_text(encoding="utf-8"))
        found = self._bare_subprocess_calls(tree)

        self.assertTrue(found, "没扫到任何 subprocess 调用，说明扫描逻辑本身失效了（守卫不能静默失效）")
        outside = [item for item in found if item[2] not in self.ALLOWED]
        self.assertEqual(
            outside,
            [],
            msg=(
                "validate.py 里出现了统一入口之外的裸子进程调用 "
                f"(行号, 调用, 所在函数)={outside}；请改用 run_python()/run_node()，"
                "否则中文 Windows 上会假失败（ERROR.md E32）"
            ),
        )
        self.assertTrue(
            any(name == "run_python" for _, _, name in found),
            "run_python() 内部应当用 subprocess.run 起进程",
        )
        self.assertTrue(
            any(name == "run_node" for _, _, name in found),
            "run_node() 内部应当用 subprocess.run 起进程",
        )


if __name__ == "__main__":
    unittest.main()
