#!/usr/bin/env python3
"""同步验收：逐文件比对**开发副本**与 **canonical 仓库**，把差异列出来。

为什么需要它（真实教训）
------------------------
`robocopy /E` 之类的同步命令**会自己报告成功**，但实际可能漏文件——
2026-09-21 本项目的 canonical 就少了两个安卓测试文件，
本地一切正常、直到 GitHub Actions 在干净 checkout 里跑文档一致性检查才暴露
（那份 README 引用了这两个文件，而它们在仓库里不存在）。

所以规矩是：**同步之后必须"逐文件比对"，不能只看同步命令的退出码**。
（这与工作区 `memory/13-文档索引与台账自检纪律.md` 的"索引↔实体双向相等"是同一类判据。）

用法（在 ``rpi/`` 目录下，或在仓库任意位置都行）::

    python rpi/scripts/check_sync.py                       # 默认比对 D:\\code\\github_repository\\raspberry-health-monitor
    python rpi/scripts/check_sync.py --target <路径>
    python rpi/scripts/check_sync.py --hash                # 同时比对内容哈希（更严格，稍慢）

退出码：0 = 两侧一致；1 = 有差异（逐条列出：缺失 / 内容不同）。
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path
from typing import Dict, List, Set, Tuple

# 让 `basic.console.safe_print` 可导入（打印 ✅/❌/⚠ 时在窄编码控制台上自动降级）
# ⚠️ 为什么（2026-09-25 真机实测，ERROR.md E32/E35）：中文 Windows / GBK 控制台上
#    `print` 直接打印这些符号时会抛 UnicodeEncodeError 把**整个脚本**崩掉；这些工具主要跑在
#    树莓派（UTF-8）上，导入失败就退回内置 print（行为与过去一致）。
try:
    from pathlib import Path  # noqa: E402
except ImportError:  # pragma: no cover - Path 是标准库，理论上不会失败
    Path = None
ROOT = Path(__file__).resolve().parents[2] if Path is not None else None
if ROOT is not None and str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    from basic.console import safe_print  # noqa: E402
except ImportError:  # pragma: no cover - 只在 basic 不可用时
    safe_print = print

#: 开发副本根目录（本文件在 <dev>/rpi/scripts/ 下）
DEV_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TARGET = Path(r"D:\code\github_repository\raspberry-health-monitor")

#: 不入库的目录名（两侧都不该出现，或只在一侧出现是正常的）
SKIP_DIRS = {
    ".git", "__pycache__", ".gradle", "build", ".pytest_cache",
    ".idea", ".vscode", "data", ".venv",
}
#: 只在开发副本保留、刻意不同步的文件
DEV_ONLY = {"DEVELOPMENT.md", "ERROR.md", "HANDOVER.md"}
#: 本地/生成物：不参与比对
IGNORED_SUFFIX = {".pyc", ".db", ".log", ".apk"}
IGNORED_NAMES = {"local.properties", ".DS_Store", "Thumbs.db"}


def collect(root: Path) -> Dict[str, Path]:
    """收集参与比对的文件：``{相对路径(POSIX): 绝对路径}``。"""
    files: Dict[str, Path] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        parts = set(path.relative_to(root).parts)
        if parts & SKIP_DIRS:
            continue
        name = path.name
        if name in DEV_ONLY or name in IGNORED_NAMES:
            continue
        if path.suffix in IGNORED_SUFFIX:
            continue
        rel = path.relative_to(root).as_posix()
        files[rel] = path
    return files


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compare(dev: Dict[str, Path], dst: Dict[str, Path], check_hash: bool) -> Tuple[List[str], List[str], List[str]]:
    dev_set, dst_set = set(dev), set(dst)
    only_dev = sorted(dev_set - dst_set)
    only_dst = sorted(dst_set - dev_set)
    differing: List[str] = []
    if check_hash:
        for rel in sorted(dev_set & dst_set):
            # 换行符差异不算问题（.gitattributes 会规范化），先比大小再比哈希
            if dev[rel].stat().st_size != dst[rel].stat().st_size:
                differing.append(rel)
            elif sha256(dev[rel]) != sha256(dst[rel]):
                differing.append(rel)
    return only_dev, only_dst, differing


def check_git_ignored(target: Path) -> List[str]:
    """★ 入库验收：找出"存在、本该入库、却被 .gitignore 忽略"的文件。

    为什么必须有这条检查（2026-09-21 真实事故）
    ------------------------------------------
    根 `.gitignore` 里写了没加前导斜杠的 `data/`，于是它匹配了**任意层级**的 data 目录，
    把 `android/app/src/**/healthmonitor/data/*.kt`（安卓数据层 8 个源码 + 2 个测试）
    整包忽略掉了。表现是：
      - `git status` 干净、本地单测全绿、同步脚本也"一致"；
      - **干净 checkout 却缺文件** —— GitHub Actions 的文档一致性检查才把它抓出来
        （`android/README.md` 引用的 `data/UrlNormalizerTest.kt` 在仓库里不存在）。
    根因是"忽略规则的匹配范围"，而**唯一可靠的判据就是问 git 本人**：
    `git status --porcelain --ignored` 会把被忽略的路径列出来，再按白名单过滤掉
    构建产物/运行数据，剩下的就是"被误忽略的源码"。
    """
    problems: List[str] = []
    if not (target / ".git").exists():
        return [f"目标不是 git 仓库（缺少 .git）：{target}"]

    import subprocess

    proc = subprocess.run(
        ["git", "status", "--porcelain", "--ignored"],
        cwd=str(target), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        return [f"无法读取 git 状态：{proc.stderr.strip()[:200]}"]

    #: 被忽略但**正常**的东西（构建产物 / 运行数据 / 本地配置 / 开发文档）
    allowed_dir_parts = {
        "build", ".gradle", "__pycache__", ".pytest_cache", "data", ".venv", "venv",
        ".idea", ".vscode", "captures", ".cxx",
    }
    allowed_suffix = {".pyc", ".db", ".log", ".apk", ".aab", ".keystore", ".jks", ".iml", ".db-journal"}
    allowed_names = {"local.properties", "DEVELOPMENT.md", "ERROR.md", "HANDOVER.md", "devices.local.json"}

    for line in proc.stdout.splitlines():
        if not line.startswith("!! "):
            continue
        rel = line[3:].strip().strip('"')
        name = rel.rsplit("/", 1)[-1]
        parts = set(rel.split("/"))
        if name in allowed_names or any(rel.endswith(s) for s in allowed_suffix):
            continue
        if parts & allowed_dir_parts:
            continue
        problems.append(
            f"被 .gitignore 忽略但看起来是源码/文档：{rel}"
            "（多半是忽略模式太宽，比如 `data/` 会匹配任意层级；改成 `/data/` 锚定到仓库根）"
        )
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description="开发副本 ↔ canonical 同步验收")
    parser.add_argument("--target", default=str(DEFAULT_TARGET), help="canonical 仓库路径")
    parser.add_argument("--hash", action="store_true", help="同时比对内容哈希（更严格）")
    parser.add_argument("--no-git", action="store_true", help="跳过 git 忽略项检查")
    args = parser.parse_args()

    target = Path(args.target)
    dev = collect(DEV_ROOT)

    print("=" * 78)
    print("同步验收：开发副本 ↔ canonical")
    print(f"  开发副本：{DEV_ROOT}")
    print(f"  canonical：{target}")
    print("=" * 78)

    if not target.exists():
        safe_print(f"❌ canonical 路径不存在：{target}")
        print("   用 --target 指定正确路径，或先把仓库 clone 下来。")
        return 1

    dst = collect(target)
    print(f"开发副本文件数：{len(dev)}；canonical 文件数：{len(dst)}")
    only_dev, only_dst, differing = compare(dev, dst, args.hash)

    problems = 0
    if only_dev:
        problems += len(only_dev)
        safe_print(f"\n❌ 只在开发副本里（canonical **缺失**，必须同步过去）：{len(only_dev)} 个")
        for rel in only_dev:
            print(f"   - {rel}")
    if only_dst:
        problems += len(only_dst)
        safe_print(f"\n⚠️ 只在 canonical 里（开发副本没有；若是发布用的 LICENSE/.gitattributes 之类属正常）：{len(only_dst)} 个")
        for rel in only_dst:
            print(f"   - {rel}")
    if args.hash and differing:
        problems += len(differing)
        safe_print(f"\n❌ 内容不同：{len(differing)} 个")
        for rel in differing:
            print(f"   - {rel}")

    # 入库验收：存在但被忽略的文件（只有在目标是 git 仓库时才做）
    if not args.no_git and (target / ".git").exists():
        ignored = check_git_ignored(target)
        if ignored:
            problems += len(ignored)
            safe_print(f"\n❌ 存在但**被 .gitignore 忽略**（本地有、干净 checkout 没有 ⇒ CI 会红）：{len(ignored)} 个")
            for item in ignored:
                print(f"   - {item}")
        else:
            safe_print("\n✅ 忽略项检查通过：没有被误忽略的源码/文档")

    print("-" * 78)
    if problems == 0:
        safe_print(f"✅ 两侧一致（{'含内容哈希' if args.hash else '仅按文件清单'}比对）")
        print("   提醒：canonical 里 `git status --porcelain` 应当为空；有变更就 commit + push。")
        return 0
    safe_print(f"⚠️ 共 {problems} 处差异（其中「只在开发副本里」与「被误忽略」这两类是必须修的）")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
