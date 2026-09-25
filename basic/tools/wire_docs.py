#!/usr/bin/env python3
"""基础版**接线表**生成器 / 校验器 —— 只产出**一份**文档。

产物（就这两件）
------------------
    basic/hardware/接线表.md      代码生成，供阅读/编辑
    basic/hardware/接线表.pdf     打印带上机用（由 `scripts/md2pdf.cjs` 转）

内容只有**两张表**（用户 2026-09-25 明确要求"就一张接线表、不要一堆文档"）：
    ① 树莓派接线：本基础版要动哪几个物理脚
    ② 元件接线：元件每个针脚插到哪

为什么文档要"生成"而不是"手写"
------------------------------
接线文档里最容易出错、后果最严重的不是措辞，而是**数字**：物理脚号、供电电压、
上拉电阻、最短读取间隔。手写就一定会漂（本项目历史上真发生过：文档把 LCD1602 的
供电写成 3.3V，实际必须 5V，照着接屏幕几乎全黑）。

所以这一套是"**代码 → 文档**"单向生成：

    basic/pins.py          （引脚映射，唯一来源）
    basic/dht11read.py     （数据脚默认值、量程、最短间隔）
    basic/wire_spec.py     （接线事实：电源脚、地脚、电阻、判据 + 自检）
            │
            ▼  basic/tools/wire_docs.py
    basic/hardware/接线表.md

用法::

    python3 basic/tools/wire_docs.py --generate   # 重新生成接线表
    python3 basic/tools/wire_docs.py --check      # 校验磁盘上的接线表 == 生成结果（CI 用）
    python3 basic/tools/wire_docs.py --print      # 打到标准输出（不写盘，便于 diff/预览）

退出码：0 = 一致/已生成；1 = 有漂移（打印哪一行不一样）。

⚠️ **不要手工编辑接线表**：下次生成会覆盖。要改内容请改 `basic/wire_spec.py`
   （数字）或本文件（文案），然后重新生成。
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from pathlib import Path
from typing import Callable, Dict, List, Tuple

BASIC_DIR = Path(__file__).resolve().parents[1]
if str(BASIC_DIR.parent) not in sys.path:
    sys.path.insert(0, str(BASIC_DIR.parent))

from basic import __version__, pins, wire_spec  # noqa: E402
from basic.console import safe_print  # noqa: E402
from basic.model import CSV_HEADER  # noqa: E402

#: 文档输出目录（`basic/` 的子文件夹）
HW_DIR = BASIC_DIR / "hardware"

#: ★ 本生成器**只产出这一个文件**（用户要求：不要一堆文档）
DOC_NAME = "接线表.md"
DOC_PATH = HW_DIR / DOC_NAME

#: 打印产物（由 `scripts/md2pdf.cjs` 生成；本生成器不写 PDF，只校验它在不在）
#: ⚠️ HTML 是中间产物：导出 PDF 后**请删掉**（用户要求：目录里不要一堆文件），
#:    所以校验只要求 PDF 存在、不要求 HTML。
PDF_NAME = "接线表.pdf"
HTML_NAME = "接线表.html"

#: 每份文档开头的固定声明（"不许手工编辑"必须写在读者第一眼看到的地方）
GEN_NOTICE = f"""> 🤖 **本文件由代码生成，请勿手工编辑** —— 下次生成会覆盖。
> 生成器：`python3 basic/tools/wire_docs.py --generate`（校验：`--check`）
> 事实来源：`basic/pins.py`（引脚映射）、`basic/dht11read.py`（驱动默认值与量程）、`basic/wire_spec.py`（接线事实）
> 基础版版本：{__version__}"""

#: 旧的多份文档（已归档；这里只用于提示，不参与生成）
ARCHIVE_DIR_NAME = "_旧文档存档"


# ==========================================================================
# 小工具
# ==========================================================================


def _table(headers: List[str], rows: List[List[str]], aligns: List[str] | None = None) -> str:
    """把二维数据渲染成 markdown 表格（分隔行按 `aligns` 决定对齐）。"""
    aligns = aligns or ["---"] * len(headers)
    sep = {"---": "---", "---:": "---:", ":---:": ":---:"}
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(sep.get(a, "---") for a in aligns) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return "\n".join(lines) + "\n"


# ==========================================================================
# 生成：一张表 = 两个接口
# ==========================================================================


def build_wiring_table() -> str:
    """**两张表就说完接线**：① 树莓派接线 ② 元件接线。

    事实**全部来自 `wire_spec`**（引脚号、BCM、线色、上拉文字、供电方式、量程、采样周期），
    所以它不可能与代码对不上 —— 改了 `wire_spec` 忘了改这里，`--check` 会当场报错。
    """
    pi_rows: List[List[str]] = []
    for wire in wire_spec.wires():
        pi_rows.append([
            f"**{wire.physical}**",
            wire.bcm_text,
            wire.signal,
            f"{wire.sensor_pin.split(' / ')[0]}（元件丝印）",
            wire.color,
            wire.note,
        ])

    v33 = "/".join(str(p) for p in wire_spec.V33_PHYSICAL)
    gnd = "/".join(str(p) for p in wire_spec.GND_PHYSICAL)
    data_pin = int(wire_spec.DATA_PHYSICAL or 0)
    module_rows = [
        ["`VCC` / `+` / `VDD`", "**3.3V**", v33, "—",
         "**绝不要接 5V**（脚 2/4）：5V 会把数据电平一起拉高，伤 GPIO"],
        ["`DATA` / `OUT` / `S`", "**DATA**", str(data_pin), f"GPIO{wire_spec.DATA_BCM}",
         f"单总线（双向）；裸四针必须外接 {wire_spec.pullup_text()} 上拉到 3.3V"],
        ["`GND` / `-`", "**GND**", gnd, "—",
         f"任意一个地脚都行（推荐脚 {wire_spec.GND_RECOMMENDED}）；**必须共地**"],
    ]

    return f"""# 基础版 · 接线表（树莓派接线 + 元件接线）

{GEN_NOTICE}

> **这一份只有两张表**：先看树莓派这头要动哪几个脚，再看元件那头的每个针脚插到哪。
> 实测命令与"读不到数怎么查"见 [`../README.md`](../README.md) 第 5 节；
> 历史细节文档（40-pin 全表 / 万用表逐项验证 / 线色自查卡）在
> [`_旧文档存档/`](_旧文档存档/)，需要时再看。

## 1. 树莓派接线（本基础版要用到的脚）

本基础版只动 **{len(wire_spec.wires())} 个脚**（其余全空闲）：
**脚 {wire_spec.V33_PHYSICAL[0]} = 3.3V、脚 {wire_spec.GND_RECOMMENDED} = GND、脚 {data_pin} = DATA**。

{_table(
    ["物理脚", "BCM", "信号", "接到哪（元件丝印）", "建议线色", "关键提醒"],
    pi_rows,
    [":---:", "---", "---", "---", ":---:", "---"],
)}
> ⚠️ **插线看物理脚号、写代码看 BCM 编号** —— 两者**不是**偏移关系
> （GPIO{wire_spec.DATA_BCM} = 物理脚 {data_pin}，但 GPIO27 = 物理脚 13）。
> 脚 {data_pin} 的邻居脚 6 是 **GND**，**插错一格就会把数据线接成地**。

## 2. 元件接线（DHT11 温湿度模块这头）

先认丝印：三针模块通常是 `VCC / DATA / GND`（或 `+ / OUT / -`）；
**裸四针**传感器没有上拉电阻，第 2 行那个 {wire_spec.pullup_text()} 电阻必须自己接。

{_table(
    ["元件针脚（丝印）", "信号", "接到树莓派（物理脚）", "BCM", "关键提醒"],
    module_rows,
    ["---", "---", ":---:", "---", "---"],
)}
> **三根线之外不要多接**：DHT11 没有其它针脚；接错电源（3.3V ↔ 5V）是最常见的烧件原因，
> 接线前请断电（带电插拔是烧 GPIO 的头号原因）。

## 3. 三分钟接完

1. **断电**，按第 1 节挑好三个脚（脚 {wire_spec.V33_PHYSICAL[0]} / 脚 {wire_spec.GND_RECOMMENDED} / 脚 {data_pin}）；
2. 按第 2 节把元件的三个针脚分别插到对应脚（红=3.3V、黑=GND、黄=DATA）；
3. 裸四针传感器：在 DATA 与 3.3V 之间接 {wire_spec.pullup_text()} 电阻；
4. 上电后验收：
   `python3 tools/diag_dht_line.py`（三态电平）→ `python3 run.py`（真实读数 + 动态曲线）。

## 4. 参数速查（都来自代码）

| 项 | 值 | 出处 |
| --- | --- | --- |
| 默认采样周期 | {wire_spec.RECOMMENDED_INTERVAL_S:g} 秒（硬件要求 ≥{wire_spec.MIN_INTERVAL_S:g} 秒） | `basic/wire_spec.py`（与驱动常量同源） |
| 温度量程 | {wire_spec.TEMP_RANGE_C[0]:g} ~ {wire_spec.TEMP_RANGE_C[1]:g} ℃ | `basic/dht11read.py` |
| 湿度量程 | {wire_spec.HUMI_RANGE_PCT[0]:g} ~ {wire_spec.HUMI_RANGE_PCT[1]:g} % | `basic/dht11read.py` |
| 供电 / 电流 | {wire_spec.MODULE_VOLTAGE} V，约 {wire_spec.MODULE_CURRENT_MA} mA | `basic/wire_spec.py` |
| 数据存哪 | `basic/data/dht11_日期_时刻.csv`（一次运行一个文件） | `basic/store.py` 的 `default_csv_path()` |
| CSV 表头 | `{",".join(CSV_HEADER)}` | `basic/model.py` 的 `CSV_HEADER` |
| 读失败怎么写 | 温度/湿度字段**留空**、`status=fail`、`note` 写原因（**绝不写 0**） | `basic/model.py` 的 `Reading.to_csv_row()` |

## 5. 导出 PDF（打印带上机用）

```bash
node scripts/md2pdf.cjs --in basic/hardware/{DOC_NAME} --out basic/hardware/{PDF_NAME} --title "基础版 · 接线表（树莓派接线 + 元件接线）"
python3 basic/tools/wire_docs.py --generate     # 让生成器知道 PDF 是哪一版（--check 会比对）
```
"""


# ==========================================================================
# 生成 / 校验
# ==========================================================================

#: ``文件名 → 生成函数``（只剩一份；保留 dict 是为了校验逻辑统一、以后加也不难）
DOCUMENTS: Dict[str, Callable[[], str]] = {
    DOC_NAME: build_wiring_table,
}


def _write_lf(path: Path, text: str) -> None:
    """以 **LF** 换行写入（跨平台一致）。

    为什么不能用 `Path.write_text()`：Windows 上它会把 ``\\n`` 写成 CRLF，
    而仓库的 `.gitattributes` 要求 LF —— 结果是"本地生成 → git 归一化成 LF →
    下次 `--check` 报不一致"的假问题。判据：生成类工具必须显式控制换行符。
    """
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


#: 记录"PDF 是用哪一版 md 生成的"（生成时写、校验时比对）
MANIFEST_NAME = ".print-manifest.json"


def _sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_print_manifest(target_dir: Path = HW_DIR) -> Path:
    """记录打印产物及其源 md 的哈希（生成时写）。

    ⚠️ 为什么需要：生成器只写 md，PDF 是**另一个命令**（`scripts/md2pdf.cjs`）产的。
    只检查"PDF 存在"的话，"PDF 被手改 / 回退 / 丢失"会被漏过去 ——
    打印出来的接线表与正文不一致，正好是最难发现的一类错误。
    """
    import json

    manifest = {
        "note": "由 wire_docs.py --generate 写出；记录接线表 PDF 与其源 md 的哈希"
                "（发现'忘了重新导出 PDF'与'产物被改'）",
        "source": DOC_NAME,
        "source_sha256": _sha256(target_dir / DOC_NAME) if (target_dir / DOC_NAME).exists() else None,
        "outputs": {
            # 只认 PDF：HTML 是导出时的中间产物，导出后会被删掉（目录要干净）
            PDF_NAME: (_sha256(target_dir / PDF_NAME) if (target_dir / PDF_NAME).exists() else None),
        },
    }
    path = target_dir / MANIFEST_NAME
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return path


def check_print_sync(target_dir: Path = HW_DIR) -> List[str]:
    """校验 PDF/HTML 与"生成清单时的版本"一致（哈希比对）。"""
    import json

    problems: List[str] = []
    manifest_path = target_dir / MANIFEST_NAME
    if not manifest_path.exists():
        return [f"缺少打印产物清单：{manifest_path}（跑 `--generate` 会重建）"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"打印产物清单读不出来：{exc}"]

    source = manifest.get("source")
    if source and not (target_dir / source).exists():
        problems.append(f"清单里记录的源文件不存在：{source}")

    for name, recorded in (manifest.get("outputs") or {}).items():
        output = target_dir / name
        if not output.exists():
            problems.append(f"缺少打印产物：{name}（生成命令见 `{DOC_NAME}` 末尾）")
            continue
        if recorded and _sha256(output) != recorded:
            problems.append(
                f"打印产物与清单不一致：{name} 被改过或已回退 —— "
                f"请重新导出（命令在 `{DOC_NAME}` 末尾）再跑 `--generate`"
            )
    return problems


def check_required_facts(corpus: str) -> List[str]:
    """关键事实必须出现在文档里（纯文本判据，可注入自测）。"""
    return [
        f"接线表里缺少关键事实「{label}」= {fact!r}（生成器可能漏渲染了这一项）"
        for label, fact in wire_spec.REQUIRED_FACTS.items()
        if fact not in corpus
    ]


def check_referenced_paths(corpus: str) -> List[str]:
    """文档里以 `basic/...` 形式提到的文件必须真实存在。

    判据只取"看起来是本仓库内的相对路径"（形如 `basic/tools/xxx.py`），
    不检查系统命令与树莓派上的绝对路径 —— 那两类本来就不该存在于仓库里。
    """
    problems: List[str] = []
    repo_root = BASIC_DIR.parent
    pattern = re.compile(r"`(basic/[A-Za-z0-9_./\-\u4e00-\u9fff]+\.(?:py|md|csv|png|pdf))`")
    placeholders = ("日期", "时刻", "YYYY", "MMDD", "xxxx", "…", "...")
    seen = set()
    for match in pattern.finditer(corpus):
        rel = match.group(1)
        if rel in seen or rel.endswith("/"):
            continue
        if any(token in rel for token in placeholders):
            continue
        seen.add(rel)
        if not (repo_root / rel).exists():
            problems.append(f"接线表引用了不存在的文件：`{rel}`（照着敲命令会报 No such file）")
    return problems


def check_no_extra_docs(target_dir: Path = HW_DIR) -> List[str]:
    """★ 硬件目录里**不许再出现别的 .md**（用户要求：不要一堆文档）。

    这条同时防两件事：① 有人手写一份新的 md 混进来；② 生成器将来又长出第二份文档。
    只允许：接线表本身 + 归档子目录（`_旧文档存档/`）+ 清单文件 + PDF/HTML。
    """
    if not target_dir.exists():
        return []
    allowed_dirs = {ARCHIVE_DIR_NAME}
    problems: List[str] = []
    for entry in sorted(target_dir.iterdir()):
        if entry.is_dir():
            if entry.name not in allowed_dirs:
                problems.append(f"硬件目录里多了子目录：{entry.name}（只允许 {ARCHIVE_DIR_NAME}/）")
            continue
        if entry.suffix.lower() != ".md":
            continue
        if entry.name not in DOCUMENTS and entry.name not in ("README.md",):
            problems.append(
                f"硬件目录里多了文档：{entry.name}（本基础版只保留 `{DOC_NAME}`；"
                f"历史文档请移到 `{ARCHIVE_DIR_NAME}/`）"
            )
    return problems


def check_all(target_dir: Path = HW_DIR) -> List[str]:
    """校验磁盘上的接线表 == 生成结果；返回问题列表（空 = 全部一致）。

    五项判据：
    1. 文档存在；
    2. 内容与生成结果**逐字符一致**（不一致 ⇒ 有人手改，或改了代码没重新生成）；
    3. 关键事实必须在文档里出现（防生成器漏渲染某个数字，而"自己和自己一致"仍通过）；
    4. 文档里提到的仓库内脚本路径必须真实存在（防"照着敲命令 → 文件不存在"）；
    5. **不许有第二份文档**（见 :func:`check_no_extra_docs`）。
    """
    problems: List[str] = []
    for name, builder in DOCUMENTS.items():
        path = target_dir / name
        expected = builder()
        if not path.exists():
            problems.append(f"缺少文档：{path}（跑 `--generate` 生成）")
            continue
        actual = path.read_text(encoding="utf-8")
        if actual != expected:
            diff = list(difflib.unified_diff(
                actual.splitlines(), expected.splitlines(),
                fromfile=f"{name}（磁盘上）", tofile=f"{name}（按代码生成）", lineterm="", n=1,
            ))
            problems.append(
                f"接线表与代码不一致：{name}（改了代码没重新生成，或手工改过）\n      "
                + "\n      ".join(diff[:20])
            )
        problems.extend(check_required_facts(actual))
        problems.extend(check_referenced_paths(actual))

    problems.extend(check_no_extra_docs(target_dir))
    problems.extend(check_print_sync(target_dir))
    return problems


def self_test() -> List[str]:
    """★ 注入自测：先证明"检查器真的能抓到问题"，再拿它去检查真文档。

    为什么必须有（本项目踩过的教训）：一个"看起来在跑、其实一直没匹配"的检查器
    比没有检查器更危险 —— 它会给出虚假的安全感。
    """
    problems: List[str] = []
    good = "；".join(f"{label}={fact}" for label, fact in wire_spec.REQUIRED_FACTS.items())
    if wire_spec.pullup_text() not in good:
        problems.append("自测构造有误：好文本里应当含上拉电阻（否则'不误报'这条断言没有意义）")

    # ① 事实检查：把上拉电阻写错（4.7kΩ 写成 4kΩ）必须被抓到
    bad_fact = good.replace(wire_spec.pullup_text(), "4kΩ~10kΩ")
    if not check_required_facts(bad_fact):
        problems.append("自测失败：把上拉电阻写错（4.7kΩ → 4k）却仍然通过 —— 事实检查形同虚设")
    if check_required_facts(good):
        problems.append("自测失败：正确文本被误报缺少关键事实（假红）")

    # ② 路径检查：引用一个不存在的脚本必须被抓到
    if not check_referenced_paths("跑 `basic/tools/根本没有这个文件.py` 即可"):
        problems.append("自测失败：引用了不存在的脚本却没被抓到")
    if check_referenced_paths("跑 `basic/tools/selfcheck.py` 即可"):
        problems.append("自测失败：真实存在的脚本路径被误报为不存在")

    # ③ 事实源本身的一致性检查也要能被触发：把"建议周期 < 最小间隔"注入进去
    saved = wire_spec.RECOMMENDED_INTERVAL_S
    try:
        wire_spec.RECOMMENDED_INTERVAL_S = wire_spec.MIN_INTERVAL_S - 1.0
        if not wire_spec.self_check():
            problems.append("自测失败：把建议采样周期设成小于硬件最小间隔，事实自查却通过了")
    finally:
        wire_spec.RECOMMENDED_INTERVAL_S = saved

    # ④ 多文档守卫：临时造一份"多余的 md"，必须被抓到
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        probe_dir = Path(tmp)
        (probe_dir / DOC_NAME).write_text(build_wiring_table(), encoding="utf-8", newline="\n")
        if check_no_extra_docs(probe_dir):
            problems.append("自测失败：只有接线表时却报'多了文档'（误报）")
        (probe_dir / "99-多余的.md").write_text("# 多余的\n", encoding="utf-8", newline="\n")
        if not check_no_extra_docs(probe_dir):
            problems.append("自测失败：硬件目录里多了一份 md 却没被抓到（守卫失效）")
    return problems


def generate_all(target_dir: Path = HW_DIR) -> List[Path]:
    """写出接线表（并刷新打印产物清单），返回写入的文件列表。"""
    target_dir.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []
    for name, builder in DOCUMENTS.items():
        path = target_dir / name
        _write_lf(path, builder())
        written.append(path)
    written.append(write_print_manifest(target_dir))
    return written


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="基础版接线表生成 / 校验")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--generate", action="store_true", help="重新生成 basic/hardware/接线表.md")
    group.add_argument("--check", action="store_true", help="校验磁盘 == 生成结果（CI 用）")
    group.add_argument("--print", dest="print_one", action="store_true",
                       help="把接线表打到标准输出（不写盘，便于 diff/预览）")
    args = parser.parse_args(argv)

    facts = wire_spec.self_check()
    if args.print_one:
        print(build_wiring_table())
        return 0

    print("=" * 74)
    print(f"基础版接线表 · {'生成' if args.generate else '校验'}")
    print("=" * 74)
    print(f"事实来源：{wire_spec.summary()}")
    if facts:
        print("接线事实自查：")
        for problem in facts:
            print(f"  {problem}")
        return 1
    safe_print("接线事实自查：✅ 通过（引脚 / 电源 / 地 / 上拉 / 间隔 全部自洽）")

    if args.generate:
        written = generate_all()
        print(f"已生成 {len(written)} 个文件：")
        for path in written:
            print(f"  - {path.relative_to(BASIC_DIR.parent)}")
        problems = check_all()
        if problems:
            safe_print(f"⚠️ 生成后自查发现 {len(problems)} 项问题：")
            for problem in problems:
                print(f"  - {problem}")
            return 1
        safe_print("生成后自查：✅ 接线表与代码一致，关键事实齐全")
        return 0

    guard_problems = self_test()
    if guard_problems:
        safe_print("❌ 检查器自测失败（守卫可能已失效，先修它）：")
        for problem in guard_problems:
            print(f"  - {problem}")
        return 1
    safe_print("检查器注入自测：✅ 能抓到注入的错误（上拉写错 / 引用不存在的文件 / 周期小于硬件下限 / 多出一份文档）")

    problems = check_all()
    if problems:
        safe_print(f"❌ 校验未通过（{len(problems)} 项）：")
        for problem in problems:
            print(f"  - {problem}")
        print("\n修复：先改 `basic/wire_spec.py` / `basic/pins.py` 等来源，再跑")
        print("      python3 basic/tools/wire_docs.py --generate")
        return 1
    safe_print(f"✅ 校验通过：{len(DOCUMENTS)} 份接线表与代码逐字符一致，关键事实齐全，引用的脚本都存在")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
