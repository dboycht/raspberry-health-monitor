"""守卫：**声明的 Python 下限（3.9）必须与代码实际用到的特性一致**。

## 为什么需要它（2026-10-01，第 9 轮）

`scripts/validate.py` 第 1 项写着「Python {ver}（**要求 >= 3.9**）」，
而实际跑过的只有 **3.12（开发机 / CI）** 与 **3.13（板子）** ——
**3.9 / 3.10 / 3.11 从没验过**。若代码里用了更高版本才有的东西，
那句"要求 >= 3.9"就是**假话**，而按它装 3.9 的同学会**直接跑不起来**（连报错都看不懂）。

本文件做**静态扫描**（不需要装 3.9）：AST 看 import 与语法，配上"最低引入版本"表。

## ⚠️ 这个检测器自己踩过的坑（所以它内置自测）

第一版是**纯文本匹配**，报了 8 处"高于 3.9"——**全是假红**：
`UTC` 出现在 docstring 里（"Unix 时间戳，UTC"）、`override`/`walk` 是别的词的一部分。
第二版改用 AST，但**版本比较写成了字符串比较**：`"3.11" > "3.9"` 是 **False**
（逐字符比，"3.1" < "3.9"）⇒ 3.10/3.11/3.12 的特性**全被漏掉**、检测器**永远是绿的**。
—— 两处都是**自测当场抓出来的**，也正是本文件 `TestDetectorWorks` 存在的理由：
**永远绿的守卫比没有守卫更糟**（`memory/30`）。
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

#: 声明下限（与 `scripts/validate.py` 里那句"要求 >= 3.9"一致）
FLOOR = (3, 9)

#: `import X` 的顶层模块 → 最低版本
MODULE_MIN = {"tomllib": (3, 11)}

#: `from X import Y` 的全名 → 最低版本
NAME_MIN = {
    "enum.StrEnum": (3, 11),
    "enum.ReprEnum": (3, 11),
    "typing.Self": (3, 11),
    "typing.override": (3, 12),
    "typing.TypeAliasType": (3, 12),
    "typing.assert_type": (3, 11),
    "typing.assert_never": (3, 11),
    "datetime.UTC": (3, 11),
    "itertools.pairwise": (3, 10),
    "itertools.batched": (3, 12),
    "contextlib.chdir": (3, 11),
    "asyncio.timeout": (3, 11),
    "asyncio.TaskGroup": (3, 11),
    "graphlib.TopologicalSorter": (3, 9),   # 恰好在 3.9，留着当"不该报"的对照
    "zoneinfo.ZoneInfo": (3, 9),            # 同上
}

#: 语法节点类型 → (最低版本, 说明)
SYNTAX_MIN = {
    "Match": ((3, 10), "match/case 语句"),
    "TryStar": ((3, 11), "except* 异常组"),
}

SCAN_ROOTS = ("rpi/health_monitor", "rpi/scripts", "rpi/tests", "basic", "scripts")


def scan_source(text: str, label: str = "src") -> list[str]:
    """返回"高于声明下限"的问题清单（空 = 合规）。**纯函数**，便于注入自测。"""
    out: list[str] = []
    tree = ast.parse(text)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                ver = MODULE_MIN.get(top)
                if ver and ver > FLOOR:
                    out.append(f"{label}: import {alias.name}（需 Python {ver[0]}.{ver[1]}）")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for alias in node.names:
                full = f"{mod}.{alias.name}" if mod else alias.name
                ver = NAME_MIN.get(full)
                if ver and ver > FLOOR:
                    out.append(f"{label}: from {mod} import {alias.name}（需 {ver[0]}.{ver[1]}）")

    for node in ast.walk(tree):
        kind = type(node).__name__
        if kind in SYNTAX_MIN:
            ver, why = SYNTAX_MIN[kind]
            out.append(f"{label}:{node.lineno}: {why}（需 {ver[0]}.{ver[1]}）")
        if getattr(node, "type_params", None):
            out.append(f"{label}:{node.lineno}: PEP 695 泛型语法（需 3.12）")
        # 运行时 `isinstance(x, int | str)` 是 3.10+（写在注解里则不受影响）
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) in ("isinstance", "issubclass"):
            for arg in node.args[1:]:
                if isinstance(arg, ast.BinOp) and isinstance(arg.op, ast.BitOr):
                    out.append(f"{label}:{node.lineno}: isinstance/issubclass 用 `X | Y`（需 3.10）")
    return out


class TestDetectorWorks(unittest.TestCase):
    """★ 先证明"检测器真的能看到东西"，再拿它去判代码 —— 否则"没发现"只说明它瞎。"""

    def test_高于下限的样本全部检出(self) -> None:
        samples = {
            "match": ("match x:\n    case 1:\n        pass\n", (3, 10)),
            "tomllib": ("import tomllib\n", (3, 11)),
            "StrEnum": ("from enum import StrEnum\n", (3, 11)),
            "Self": ("from typing import Self\n", (3, 11)),
            "datetime.UTC": ("from datetime import UTC\n", (3, 11)),
            "PEP695": ("class Box[T]:\n    pass\n", (3, 12)),
            "isinstance 联合": ("isinstance(1, int | str)\n", (3, 10)),
        }
        for name, (src, ver) in samples.items():
            hits = scan_source(src)
            self.assertTrue(hits, f"自测失败：{name} 没被检出")
            self.assertTrue(any(f"{ver[0]}.{ver[1]}" in h for h in hits),
                            f"自测失败：{name} 的版本号不对：{hits}")

    def test_版本比较不是字符串比较(self) -> None:
        """钉死那个"`\"3.11\" > \"3.9\"` 为假"的坑：3.11 必须被判为**高于** 3.9。"""
        self.assertGreater((3, 11), FLOOR)
        self.assertGreater((3, 10), FLOOR)
        self.assertGreater((3, 12), FLOOR)
        self.assertLess((3, 9), (3, 10))
        # 字符串比较会得出相反的结论（这就是当初的 bug）
        self.assertFalse("3.11" > "3.9")

    def test_恰好等于下限的不报(self) -> None:
        """3.9 引入的库**不该**被报（否则这条守卫会变成噪音，最后被人关掉）。"""
        self.assertEqual(scan_source("from zoneinfo import ZoneInfo\n"), [])
        self.assertEqual(scan_source("from graphlib import TopologicalSorter\n"), [])

    def test_3_9安全的写法不误报(self) -> None:
        safe = (
            "from __future__ import annotations\n"
            "import os\n"
            "from typing import Dict, Optional\n"
            "def f(x: 'Optional[int]') -> 'Dict[str, int]':\n"
            "    return {}\n"
        )
        self.assertEqual(scan_source(safe), [], "3.9 安全的写法被误报了")


class TestRepoMeetsDeclaredFloor(unittest.TestCase):
    def test_全仓没有高于_3_9_的特性(self) -> None:
        targets: list[Path] = []
        for sub in SCAN_ROOTS:
            base = ROOT / sub
            if base.exists():
                targets.extend(p for p in sorted(base.rglob("*.py"))
                               if "__pycache__" not in str(p))
        self.assertGreater(len(targets), 50, "扫描目标太少，路径写错了？")

        problems: list[str] = []
        for path in targets:
            problems.extend(scan_source(path.read_text(encoding="utf-8", errors="replace"),
                                        str(path.relative_to(ROOT))))
        self.assertEqual(
            problems, [],
            "`scripts/validate.py` 声明「要求 >= 3.9」，但这些地方用了更高的版本：\n  "
            + "\n  ".join(problems[:20])
            + "\n要么改代码，要么**把声明下限改对**（别让文档说谎）。",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
