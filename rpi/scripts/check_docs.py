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
import os
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Set, Tuple

# 仓库根目录（本文件在 <root>/rpi/scripts/ 下）
ROOT = Path(__file__).resolve().parents[2]

#: 允许在文档里"提到但不要求存在于本仓库"的路径（示例/将来的文件名/外部工具/工作区文档）
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
    # 系统路径（不是仓库内文件）：排错手册里会引用树莓派的配置文件
    "boot/firmware/config.txt",
    "/boot/firmware/config.txt",
    "~/Desktop/lab1b_bmp_iot_clean.py",   # 树莓派桌面上的课程示例（docs/10 引用）
    # ⚠️ 本机覆盖配置：**按设计不入库**（含 OneNET 密钥等凭据，.gitignore 已忽略），
    #    所以仓库里永远找不到它 —— 文档里必须能讨论它，检查器不该报悬空。
    "config/devices.local.json",
    "rpi/config/devices.local.json",
    "devices.local.json",
}

#: 允许的**路径前缀**：这些是"仓库之外、但在同一个工作区里"的文档，
#: 本项目文档会引用它们（例如项目 ERROR.md 指向工作区的 rules/ 与 memory/）。
#: 这类引用是**有意的跨文档连接**，检查器不该判为悬空。
ALLOW_PREFIXES = (
    "rules/",
    "memory/",
)

#: 已重命名/已移动的文件：旧路径 → 新路径（检查器据此报"引用已过时"而不是"不存在"）
#: 2026-09-22 重组：参考资料统一移入 ``docs/手册/``，操作手册留在 ``docs/`` 根下。
RENAMED = {
    "docs/01-项目管理.md": "docs/手册/01-项目管理.md",
    "docs/02-接口规格说明书.md": "docs/手册/02-接口规格说明书.md",
    "docs/03-器件任务书.md": "docs/手册/03-器件任务书.md",
    "docs/04-驱动开发规范与贡献指南.md": "docs/手册/04-驱动开发规范与贡献指南.md",
    "docs/05-安卓通信协议.md": "docs/手册/05-安卓通信协议.md",
    "docs/06-安卓开发指南.md": "docs/手册/06-安卓开发指南.md",
    "docs/07-拓展与上云.md": "docs/手册/07-拓展与上云.md",
    "docs/08-报警规则表.md": "docs/手册/08-报警规则表.md",
    "docs/03-报警规则表.md": "docs/手册/08-报警规则表.md",   # 更早的一次改名
}

#: markdown 链接 [文本](路径)
MD_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
#: 行内反引号里"看起来像文件路径"的内容。
#: ⚠️ 字符类必须允许**非 ASCII**（本项目文档文件名全是中文，例如
#: `docs/08-报警规则表.md`）；早前只写 [A-Za-z0-9_\-./] 会**漏掉全部中文文件名**，
#: 等于检查器对最需要检查的那批文件视而不见（用注入自测才发现，见 ERROR.md E18）。
BACKTICK_PATH = re.compile(
    r"`([^\s`<>|]*[^\s`<>|]*\.(?:md|py|json|kt|kts|sh|ps1|xml|toml|service|txt|cfg|ini|yml|yaml))`"
)
#: fenced code block：```...```
FENCED_CODE = re.compile(r"```.*?```", re.S)
#: inline code span：`...`
INLINE_CODE = re.compile(r"`[^`\n]*`")


def _strip_code_blocks(text: str) -> str:
    """把**围栏代码块**替换成空格。

    为什么要这么做：`.gitignore` 片段（```/data/```、```*.py[cod]```）与真实文件引用
    在字面上无法区分，但**上下文能区分**——放在围栏代码块里的是"配置片段"，
    夹在正文里的才是"文件引用"。判据按上下文而不是按字面，才不会既误报又漏报。
    """
    return FENCED_CODE.sub(lambda m: " " * len(m.group(0)), text)


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


def _probe_targets(text: str, base: Path | None = None) -> List[str]:
    """纯逻辑核心：从一段 markdown 文本里挑出"看起来像文件引用但解析不到"的目标。

    抽成独立函数是为了能被 :func:`self_test` 直接用**内存里的字符串**检验
    （注入自测不需要碰真实文件）。
    """
    missing: List[str] = []
    base = base or ROOT

    def probe(raw: str) -> None:
        target = raw.split("#", 1)[0].strip()
        if not target or target.startswith(("http://", "https://", "mailto:")):
            return
        # 通配/占位写法不是具体文件引用（例如 `sensors/*.py`、`*.local`、`<path>`）：
        # 判据 = 含通配符就跳过（它在描述"一类文件"，而不是指向某一个文件）
        if any(ch in target for ch in ("*", "?", "[", "]", "<", ">")):
            return
        # 裸扩展名（`.sh`、`.ps1`）是在说"这类文件"，不是路径
        if target.startswith(".") and "/" not in target:
            return
        # 仓库之外、但同属一个工作区的文档（rules/、memory/）：有意的跨文档引用
        if target.startswith(ALLOW_PREFIXES):
            return
        # 被重命名/移动的文件：报"引用已过时"（比"不存在"更有指导性），
        # 返回带说明的字符串（只用于展示，不影响 self_test 的判定）
        if target in RENAMED:
            missing.append(f"{target}（已移动/改名为 {RENAMED[target]}，请更新引用）")
            return
        # `/xxx` 是"以仓库根为锚"的写法（.gitignore 片段与文档里都常见）：
        # 去掉前导斜杠后按仓库根解析，而不是当成绝对路径丢掉。
        if target.startswith("/"):
            target = target.lstrip("/")
            if not target:
                return
        if target in ALLOW_MISSING or target.rstrip("/") in ALLOW_MISSING:
            return
        # ① 相对本文件或仓库根能直接解析
        if (base / target).exists() or (ROOT / target).exists():
            return
        # ② 在仓库里按后缀唯一命中（文档里的"部分路径"写法，如 `core/rules.py`）
        hits = SUFFIX_INDEX.get(target)
        if hits:
            if len(hits) > 1 and target not in AMBIGUOUS:
                AMBIGUOUS.add(target)
            return
        missing.append(target)

    # 链接语法：总是检查（代码块里的链接也算文档的一部分）
    for match in MD_LINK.finditer(text):
        probe(match.group(1))
    # 行内反引号：**先剥掉围栏代码块**，避免把 .gitignore 片段当成文件引用
    prose = _strip_code_blocks(text)
    for match in BACKTICK_PATH.finditer(prose):
        probe(match.group(1))
    return missing


def check_links(doc: Path) -> List[str]:
    """返回该文档里"解析不到的文件引用"（带出处说明，供报告用）。"""
    text = doc.read_text(encoding="utf-8")
    return _probe_targets(text, base=doc.parent)


def check_docs_index() -> List[str]:
    """双向检查：操作手册文档必须被 `docs/README.md` 索引；索引条目必须真实存在。

    ⚠️ 2026-09-22 文档改组后：参考资料在 ``docs/手册/``（在索引里以 ``手册/xx.md`` 形式出现），
    操作手册在 ``docs/`` 根下（以 ``xx.md`` 形式出现）。所以这里要**扫描两处**，
    并且按"索引里怎么写的"去核对，避免重构后索引与实体悄悄脱节。
    """
    index = ROOT / "docs" / "README.md"
    if not index.exists():
        return ["docs/README.md 不存在（文档索引缺失）"]
    text = index.read_text(encoding="utf-8")
    problems: List[str] = []

    entries = [(p.name, p) for p in (ROOT / "docs").glob("*.md")]
    entries += [(f"手册/{p.name}", p) for p in (ROOT / "docs" / "手册").glob("*.md")]
    for label, path in sorted(entries):
        if path.name == "README.md":
            continue
        if f"({label})" not in text:
            problems.append(f"docs/README.md 索引里缺少 {label}")

    # 反向：索引里列了但文件不存在
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
    # 报警规则表在 2026-09-22 移到了 docs/手册/ 下；两个位置都认（避免下次搬家又断）
    rules_doc_candidates = [ROOT / "docs" / "手册" / "08-报警规则表.md", ROOT / "docs" / "08-报警规则表.md"]
    rules_doc = next((p for p in rules_doc_candidates if p.exists()), rules_doc_candidates[0])
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


def self_test() -> List[str]:
    """★ 检查器的**注入自测**：先证明"它真能抓到悬空引用"，再证明"不误报"。

    为什么必须有（来自项目的两条教训）：
    1. `memory/26` 记过"**注入型自测必须断言注入真的生效**"——否则会把
       "没注入"误报成"守卫失效"；
    2. 本项目 2026-09-21 真的出现过一次"守卫看起来在跑、其实一直没匹配"：
       行内反引号的正则只允许 ASCII，而本项目文档文件名**全是中文**，
       于是它对最该检查的那批引用视而不见（用外部脚本注入才暴露）。
    做法：在内存里造一份文档，断言探针能抓到悬空引用、
    且对围栏代码块里的 `.gitignore` 片段不误报。

    ⚠️ 2026-09-22 在真树莓派上暴露的第二个坑：**自测不能假设仓库的形状**。
    早前版本用固定字符串 ``docs/README.md`` 当"真实存在的文件"，
    而这只在"仓库根 == 检查器的 ROOT"时才成立——树莓派上代码放在
    ``~/raspberry-health-monitor/rpi/``（不是仓库根），那句就真的解析不到，
    于是自测报"真实存在的文件被误报为悬空引用"（**假红**，把守卫自身搞成了失败项）。
    现在的做法：**临时造一个真实文件**来验"不误报"，不再依赖任何既有文件。
    """
    problems: List[str] = []
    probe_dir = ROOT / "docs"
    probe_dir.mkdir(parents=True, exist_ok=True)
    # 名字带 pid+时间戳：确保不可能与既有文件重名
    # （否则"该抓到的没抓到"这条断言就失去意义）
    unique = f"_selftest_probe_{os.getpid()}_{int(time.time())}.md"
    probe_file = probe_dir / unique
    real_target = f"docs/{unique}"
    try:
        probe_file.write_text("自测用临时文件，检查完立即删除\n", encoding="utf-8")
        global SUFFIX_INDEX
        if not SUFFIX_INDEX:
            SUFFIX_INDEX = build_suffix_index()
        payload = (
            f"正常引用 `{real_target}`\n\n"
            "悬空引用 `docs/99-不存在文档.md`\n\n"
            "```gitignore\n/data/\n/logs/\n```\n"
        )
        targets = _probe_targets(payload, base=probe_dir)
        if "docs/99-不存在文档.md" not in targets:
            problems.append(
                "自测失败：悬空引用没被抓到（探针的正则或过滤条件失效了）"
                "——注意本项目文档名是中文，正则必须允许非 ASCII"
            )
        if any(t.startswith("/data") or t.startswith("/logs") for t in targets):
            problems.append("自测失败：围栏代码块里的 .gitignore 片段被误判成文件引用")
        if real_target in targets:
            problems.append("自测失败：真实存在的文件被误报为悬空引用")
    finally:
        try:
            probe_file.unlink()
        except OSError:
            pass
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description="文档一致性自检")
    parser.add_argument("--fix-hint", action="store_true", help="打印修复建议")
    parser.add_argument("--self-test", action="store_true", help="只跑注入自测（验证检查器本身有效）")
    args = parser.parse_args()

    docs = iter_docs()
    global SUFFIX_INDEX
    SUFFIX_INDEX = build_suffix_index()

    if args.self_test:
        print("=" * 78)
        print("文档一致性检查器 · 注入自测")
        print("=" * 78)
        problems = self_test()
        if problems:
            for p in problems:
                print(f"❌ {p}")
            return 1
        print("✅ 自测通过：悬空引用能被抓到；代码块里的模式不被误报；真实文件不被误报")
        return 0

    print("=" * 78)
    print(f"文档一致性自检（扫描 {len(docs)} 份 .md，仓库文件索引 {len(SUFFIX_INDEX)} 条后缀）")
    print("=" * 78)

    problems: List[str] = self_test()      # 先自证有效，再拿它去检查
    for doc in docs:
        rel = doc.relative_to(ROOT)
        for target in check_links(doc):
            if "（已移动" in target:
                problems.append(f"{rel}：{target}")
            else:
                problems.append(f"{rel}：引用了不存在的路径 {target}")

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
