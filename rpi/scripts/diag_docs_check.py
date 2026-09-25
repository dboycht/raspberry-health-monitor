#!/usr/bin/env python3
"""在树莓派上诊断"文档一致性"这一步为什么在 validate.py 里失败、单独跑却通过。

这个差异本身就是线索：两者只差"先跑了自测"。而自测会**在 docs/ 下创建/删除一个临时文件**
——如果它留下的痕迹（或时序）干扰了随后的索引检查，就会出现这种"单独跑绿、合并跑红"。

用法（在树莓派上）::

    cd ~/raspberry-health-monitor/rpi
    python3 scripts/diag_docs_check.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import check_docs  # noqa: E402

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


def main() -> int:
    print("=" * 78)
    print("文档一致性：分步诊断")
    print("=" * 78)
    docs_dir = check_docs.ROOT / "docs"
    print(f"ROOT = {check_docs.ROOT}")
    print(f"docs 目录存在：{docs_dir.exists()}")

    probes = sorted(p.name for p in docs_dir.glob("_selftest_probe_*"))
    print(f"残留的自测临时文件：{probes or '（无）'}")
    print(f"docs 下的 .md 数量：{len(list(docs_dir.glob('*.md')))}")
    print(f"docs/手册 下的 .md 数量：{len(list((docs_dir / '手册').glob('*.md')))}")

    print("\n--- 第 1 步：只跑自测 ---")
    problems = check_docs.self_test()
    safe_print("自测结果：", problems or "✅ 通过")
    print("自测后残留：", sorted(p.name for p in docs_dir.glob("_selftest_probe_*")) or "（无）")

    print("\n--- 第 2 步：只跑索引检查 ---")
    index_problems = check_docs.check_docs_index()
    safe_print("索引检查：", index_problems or "✅ 通过")

    print("\n--- 第 3 步：只跑三方一致检查 ---")
    rules_problems = check_docs.check_report_rules_consistency()
    safe_print("三方一致：", rules_problems or "✅ 通过")

    print("\n--- 第 4 步：全量文档链接检查（前 10 条）---")
    check_docs.SUFFIX_INDEX = check_docs.build_suffix_index()
    all_problems = []
    for doc in check_docs.iter_docs():
        for target in check_docs.check_links(doc):
            all_problems.append(f"{doc.relative_to(check_docs.ROOT)}：{target}")
    print(f"共 {len(all_problems)} 条：")
    for item in all_problems[:10]:
        print("   -", item)

    print("\n" + "=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
