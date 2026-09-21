#!/usr/bin/env python3
"""文档一致性自检：抓"悬空引用"和"索引少一条"。

为什么需要它（真实教训）
------------------------
本项目文档有十几份、互相引用。**人在写文档时最容易犯的两类错误机器才能抓**：
1. **悬空引用**：链接/反引号里的文件路径不存在（改过文件名、或计划写但忘了写）；
2. **索引与实体不相等**：`docs/README.md` 的索引表里少列了一份文档
   （人眼扫一遍是发现不了"少一条"的）。

用法（在 ``rpi/`` 目录下，或在仓库根目录都行）::

    python rpi/scripts/check_docs.py
    python rpi/scripts/check_docs.py --fix-hint   # 额外打印修复建议

退出码：0 = 通过；1 = 有悬空引用或索引不一致。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Dict, List, Set, Tuple

# 仓库根目录（本文件在 <root>/rpi/scripts/ 下）
ROOT = Path(__file__).resolve().parents[2]

#: 允许在文档里"提到但不要求存在"的路径（示例/将来的文件名/外部工具）
ALLOW_MISSING = {
    "DEVELOPMENT.md",     # 只在开发副本，canonical 里没有
    "ERROR.md",           # 同上
    "config/devices.json",  # 本机配置，仓库里只有 .example.json
    "devices.json",
    "data/history.db",
    "app-debug.apk",
    "report.json",
    "rpi/data/",
    # 计划中/模板里出现、但仓库里刻意没有的文件
    "rpi/health_monitor/net/mqtt.py",   # docs/07 里的"将来要加"的文件
    "sensors/a.py",                     # docs/CHANGELOG 模板示例（不存在的虚构文件）
    "outputs/b.py",                     # 同上
}

#: markdown 链接 [文本](路径)
MD_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
#: 反引号里的像是路径的内容（含 / 且不像是命令参数）
BACKTICK_PATH = re.compile(r"`([A-Za-z0-9_\-./]+\.(?:md|py|json|kt|kts|sh|ps1|xml|toml|service|txt))`")


def iter_docs() -> List[Path]:
    docs: List[Path] = []
    for pattern in ("*.md", "docs/*.md", "hardware/*.md", "contrib/*.md", "android/*.md"):
        docs.extend(sorted(ROOT.glob(pattern)))
    return [d for d in docs if d.is_file()]


def build_suffix_index() -> Dict[str, List[Path]]:
    """建立"路径后缀 → 文件"索引，用于解析文档里的部分路径。

    文档里写 ``core/rules.py``、``sensors/dht11.py`` 这种**相对包根的部分路径**是正常做法
    （读者一眼就知道是哪个模块）。所以判据不是"从本文件出发能不能解析"，
    而是"**在仓库里能不能唯一找到一个以此后缀结尾的真实文件**"：
    - 唯一命中 ⇒ 视为有效引用；
    - 零命中 ⇒ 悬空引用（真问题）；
    - 多个命中 ⇒ 引用含糊（提示性警告，不算错）。
    """
    index: Dict[str, List[Path]] = {}
    skip_dirs = {".git", "build", ".gradle", "__pycache__", "node_modules", ".pytest_cache"}
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in skip_dirs for part in path.parts):
            continue
        rel = path.relative_to(ROOT).as_posix()
        # 为每一级后缀都建索引：a/b/c.py -> "c.py", "b/c.py", "a/b/c.py"
        parts = rel.split("/")
        for i in range(len(parts)):
            index.setdefault("/".join(parts[i:]), []).append(path)
    return index


SUFFIX_INDEX: Dict[str, List[Path]] = {}
AMBIGUOUS: Set[str] = set()


def check_links(doc: Path) -> List[Tuple[str, str]]:
    """返回 ``[(缺失的目标, 原文片段), ...]``。"""
    text = doc.read_text(encoding="utf-8")
    missing: List[Tuple[str, str]] = []

    def probe(raw: str) -> None:
        target = raw.split("#", 1)[0].strip()
        if not target or target.startswith(("http://", "https://", "mailto:")):
            return
        if target in ALLOW_MISSING or target.rstrip("/") in ALLOW_MISSING:
            return
        # ① 相对本文件或仓库根能直接解析
        if (doc.parent / target).exists() or (ROOT / target).exists():
            return
        # ② 在仓库里按后缀唯一命中（文档里的"部分路径"写法）
        hits = SUFFIX_INDEX.get(target)
        if hits:
            if len(hits) > 1 and target not in AMBIGUOUS:
                AMBIGUOUS.add(target)
            return
        missing.append((target, raw))

    for match in MD_LINK.finditer(text):
        probe(match.group(1))
    for match in BACKTICK_PATH.finditer(text):
        probe(match.group(1))
    return missing


def check_docs_index() -> List[str]:
    """`docs/README.md` 的索引表必须覆盖 docs/ 下的每一份 .md（除它自己）。"""
    index = ROOT / "docs" / "README.md"
    if not index.exists():
        return ["docs/README.md 不存在（文档索引缺失）"]
    text = index.read_text(encoding="utf-8")
    actual = {p.name for p in (ROOT / "docs").glob("*.md")} - {"README.md"}
    problems: List[str] = []
    for name in sorted(actual):
        # 索引里以链接形式出现即可
        if f"({name})" not in text and f"]({name})" not in text:
            problems.append(f"docs/README.md 索引里缺少 {name}")
    # 反向：索引里列了但文件不存在（悬空条目）
    for match in MD_LINK.finditer(text):
        target = match.group(1).split("#", 1)[0].strip()
        if not target.endswith(".md") or target.startswith(("http", "..")):
            continue
        if not (ROOT / "docs" / target).exists():
            problems.append(f"docs/README.md 索引里的 {target} 不存在")
    return problems


def _extract_enum_members(text: str, enum_name: str) -> Set[str]:
    """从 ``models.py`` 里抽出**指定枚举类**的字符串值。

    ⚠️ 必须限定在这个 class 的代码块内：文件里有 6 个枚举，都用同样的
    ``    XXX = "yyy"`` 形式缩进，不限定类就会把 ``MotionState``/``CommandType``
    的成员也捞进来，然后报出一堆"缺少报警码"的**假问题**（实测踩到）。
    """
    pattern = re.compile(
        rf"class\s+{enum_name}\s*\([^)]*\)\s*:(.*?)(?=\nclass\s|\Z)", re.S
    )
    block = pattern.search(text)
    if block is None:
        return set()
    return set(re.findall(r'^\s{4}[A-Z_]+\s*=\s*"([a-z_]+)"', block.group(1), re.M))


def check_report_rules_consistency() -> List[str]:
    """报警码三方一致性：`AlarmCode` 枚举 ↔ 报警规则表 ↔ 安卓端文案表。"""
    problems: List[str] = []
    models = ROOT / "rpi" / "health_monitor" / "hal" / "models.py"
    rules_doc = ROOT / "docs" / "08-报警规则表.md"
    android_catalog = ROOT / "android" / "app" / "src" / "main" / "java" / "com" / "dboycht" / "healthmonitor" / "domain" / "AlarmCatalog.kt"

    if not models.exists():
        return ["找不到 models.py（路径变了？）"]
    code_values = _extract_enum_members(models.read_text(encoding="utf-8"), "AlarmCode")
    if not code_values:
        return ["没能从 models.py 里抽出 AlarmCode 成员（枚举写法变了？检查 _extract_enum_members）"]

    if rules_doc.exists():
        doc_text = rules_doc.read_text(encoding="utf-8")
        missing = sorted(v for v in code_values if v not in doc_text)
        if missing:
            problems.append(f"报警规则表缺少这些报警码：{missing}")
    else:
        problems.append("docs/08-报警规则表.md 不存在（接口规格里引用了它）")

    if android_catalog.exists():
        kt = android_catalog.read_text(encoding="utf-8")
        missing_kt = sorted(v for v in code_values if v not in kt)
        if missing_kt:
            problems.append(f"安卓端 AlarmCatalog.kt 缺少这些报警码文案：{missing_kt}")
    else:
        problems.append("安卓端 AlarmCatalog.kt 不存在（路径变了？）")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description="文档一致性自检")
    parser.add_argument("--fix-hint", action="store_true", help="打印修复建议")
    args = parser.parse_args()

    docs = iter_docs()
    global SUFFIX_INDEX
    SUFFIX_INDEX = build_suffix_index()
    print("=" * 78)
    print(f"文档一致性自检（扫描 {len(docs)} 份 .md，仓库文件索引 {len(SUFFIX_INDEX)} 条后缀）")
    print("=" * 78)

    problems: List[str] = []
    for doc in docs:
        rel = doc.relative_to(ROOT)
        for target, raw in check_links(doc):
            problems.append(f"{rel}：引用了不存在的路径 {target}（原文 `{raw}`）")

    problems.extend(check_docs_index())
    problems.extend(check_report_rules_consistency())

    if not problems:
        print(f"✅ 全部通过：{len(docs)} 份文档的链接都可解析；docs 索引与实体一一对应；报警码三方一致")
        if AMBIGUOUS:
            print(f"ℹ️ 引用含糊（多个文件同名，建议写全路径，不算错）：{len(AMBIGUOUS)} 处")
            for name in sorted(AMBIGUOUS)[:8]:
                print(f"   - {name}")
        return 0

    print(f"❌ 发现 {len(problems)} 处问题：")
    for p in problems:
        print(f"   - {p}")
    if args.fix_hint:
        print()
        print("修复建议：")
        print("  1) 悬空引用 → 改链接到真实文件，或把该文件写出来（二选一，别留着）")
        print("  2) 只存在于开发副本的文件（DEVELOPMENT.md / ERROR.md）应在 ALLOW_MISSING 里")
        print("  3) docs 索引缺条目 → 在 docs/README.md 的索引表里补一行")
        print("  4) 报警码不一致 → 三方（models.py / 报警规则表 / AlarmCatalog.kt）必须同时改")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
