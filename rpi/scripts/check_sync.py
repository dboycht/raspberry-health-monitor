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


def main() -> int:
    parser = argparse.ArgumentParser(description="开发副本 ↔ canonical 同步验收")
    parser.add_argument("--target", default=str(DEFAULT_TARGET), help="canonical 仓库路径")
    parser.add_argument("--hash", action="store_true", help="同时比对内容哈希（更严格）")
    args = parser.parse_args()

    target = Path(args.target)
    dev = collect(DEV_ROOT)

    print("=" * 78)
    print("同步验收：开发副本 ↔ canonical")
    print(f"  开发副本：{DEV_ROOT}")
    print(f"  canonical：{target}")
    print("=" * 78)

    if not target.exists():
        print(f"❌ canonical 路径不存在：{target}")
        print("   用 --target 指定正确路径，或先把仓库 clone 下来。")
        return 1

    dst = collect(target)
    print(f"开发副本文件数：{len(dev)}；canonical 文件数：{len(dst)}")
    only_dev, only_dst, differing = compare(dev, dst, args.hash)

    problems = 0
    if only_dev:
        problems += len(only_dev)
        print(f"\n❌ 只在开发副本里（canonical **缺失**，必须同步过去）：{len(only_dev)} 个")
        for rel in only_dev:
            print(f"   - {rel}")
    if only_dst:
        problems += len(only_dst)
        print(f"\n⚠️ 只在 canonical 里（开发副本没有；若是发布用的 LICENSE/.gitattributes 之类属正常）：{len(only_dst)} 个")
        for rel in only_dst:
            print(f"   - {rel}")
    if args.hash and differing:
        problems += len(differing)
        print(f"\n❌ 内容不同：{len(differing)} 个")
        for rel in differing:
            print(f"   - {rel}")

    print("-" * 78)
    if problems == 0:
        print(f"✅ 两侧一致（{'含内容哈希' if args.hash else '仅按文件清单'}比对）")
        print("   提醒：canonical 里 `git status --porcelain` 应当为空；有变更就 commit + push。")
        return 0
    print(f"⚠️ 共 {problems} 处差异（其中「只在开发副本里」的那一类是必须修的）")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
