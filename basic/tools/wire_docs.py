#!/usr/bin/env python3
"""基础版**接线文档生成器 / 校验器**（`basic/hardware/` 里的四份文档由它产出）。

为什么文档要"生成"而不是"手写"
------------------------------
接线文档里最容易出错、后果最严重的不是措辞，而是**数字**：物理脚号、供电电压、
上拉电阻、最短读取间隔。手写就一定会漂（本项目历史上真发生过：文档把 LCD1602 的
供电写成 3.3V，实际必须 5V，照着接屏幕几乎全黑）。

所以这一套是"**代码 → 文档**"单向生成：

```
basic/pins.py          （引脚映射，唯一来源）
basic/dht11read.py     （数据脚默认值、量程、最短间隔）
basic/wire_spec.py     （接线事实：电源脚、地脚、电阻、判据 + 自检）
        │
        ▼  basic/tools/wire_docs.py
basic/hardware/*.md    （四份文档）
```

用法::

    python3 basic/tools/wire_docs.py --generate   # 重新生成四份文档
    python3 basic/tools/wire_docs.py --check      # 校验磁盘上的文档 == 生成的文档（CI 用）

退出码：0 = 一致/已生成；1 = 有漂移（逐条打印哪份文档哪一行不一样）。

⚠️ **不要手工编辑 `basic/hardware/` 下的 .md**：下次生成会覆盖。
要改内容请改 `basic/wire_spec.py` 或本文件里的文案段落，然后重新生成。
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

BASIC_DIR = Path(__file__).resolve().parents[1]
if str(BASIC_DIR.parent) not in sys.path:
    sys.path.insert(0, str(BASIC_DIR.parent))

from basic import __version__, pins, wire_spec  # noqa: E402
from basic.console import safe_text  # noqa: E402
from basic.model import CSV_HEADER  # noqa: E402
from basic.store import default_csv_path  # noqa: E402
# ⚠️ 全部事实**通过 `wire_spec.X` 访问**（下面这些名字只为 IDE/静态检查可发现，运行时不用）：
#    这样单测可以注入（例如把 DATA_PHYSICAL 改成 40，验证"换引脚后图仍对齐"），
#    而 `from ... import DATA_PHYSICAL` 会把值绑死在导入那一刻，注入就失效了。
from basic.wire_spec import (  # noqa: E402,F401
    DATA_BCM,
    DATA_PHYSICAL,
    FIXED_FUNCTION_PHYSICAL,
    GND_PHYSICAL,
    GND_RECOMMENDED,
    HUMI_RANGE_PCT,
    IDLE_LEVEL,
    MIN_INTERVAL_S,
    MODULE_CURRENT_MA,
    MODULE_VOLTAGE,
    PHYSICAL_FUNCTION,
    PI_3V3_RAIL_A,
    PI_PSU,
    POWER_MODE,
    PULLUP_OHM_RANGE,
    RECOMMENDED_INTERVAL_S,
    REQUIRED_FACTS,
    RESERVED_PHYSICAL,
    TEMP_RANGE_C,
    V33_PHYSICAL,
    V5_PHYSICAL,
    wires,
)

#: 文档输出目录
HW_DIR = BASIC_DIR / "hardware"

#: 每份文档开头的固定声明（**不许手工编辑**这件事必须写在读者第一眼看到的地方）
GEN_NOTICE = f"""> 🤖 **本文件由代码生成，请勿手工编辑** —— 下次生成会覆盖。
> 生成器：`python3 basic/tools/wire_docs.py --generate`（校验：`--check`）
> 事实来源：`basic/pins.py`（引脚映射）、`basic/dht11read.py`（驱动默认值与量程）、`basic/wire_spec.py`（接线事实）
> 基础版版本：{__version__}　｜　生成后由 `basic/tools/selfcheck.py` 与 `rpi/scripts/validate.py` 一起校验"""


# ==========================================================================
# 小工具
# ==========================================================================


def _header(title: str) -> str:
    return f"# {title}\n\n{GEN_NOTICE}\n"


def _table(headers: List[str], rows: List[List[str]], aligns: Optional[List[str]] = None) -> str:
    """把二维数据渲染成 markdown 表格（分隔行按 `aligns` 决定对齐）。"""
    aligns = aligns or ["---"] * len(headers)
    sep = {"---": "---", "---:": "---:", ":---:": ":---:"}
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(sep.get(a, "---") for a in aligns) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return "\n".join(lines) + "\n"


def _gnd_list_text() -> str:
    return "、".join(str(p) for p in wire_spec.GND_PHYSICAL)


def _canvas(rows: int, cols: int, fill: str = " ") -> List[List[str]]:
    return [[fill] * cols for _ in range(rows)]


def _put(canvas: List[List[str]], row: int, col: int, text: str) -> None:
    """把 ``text`` 写在 ``(row, col)``，允许越界自动扩展（不抛异常，便于拼图）。"""
    width = len(canvas[0]) if canvas else max(col + len(text), 1)
    while len(canvas) <= row:
        canvas.append([" "] * width)
    line = canvas[row]
    need = col + len(text) - len(line)
    if need > 0:
        line.extend([" "] * need)
    for offset, char in enumerate(text):
        line[col + offset] = char


def _line(canvas: List[List[str]], row: int, col_from: int, col_to: int) -> None:
    """在第 ``row`` 行从 ``col_from`` 画到 ``col_to``（含两端）的横线。"""
    for col in range(col_from, col_to + 1):
        _put(canvas, row, col, "─")


def _render(canvas: List[List[str]]) -> str:
    return "\n".join("".join(row).rstrip() for row in canvas)


def pinout_diagram() -> str:
    """40-pin 布局图（两列，与实物排针一致）：标出本基础版用到的脚。

    ⚠️ **对齐由列号保证，不靠手写空格**：这就是本函数存在的理由 ——
    手写 ASCII 图时，只要插入或替换一个数字（例如数据脚从 4 变成 17），
    整张图就会错位。做法是先算好"内列/外列"的列号，再按列号填字符。
    """
    usage = wire_spec.pin_usage()
    left = list(range(1, 41, 2))       # 1,3,5,...,39（内侧一列）
    right = list(range(2, 41, 2))      # 2,4,6,...,40（外侧一列）

    left_num_col = 2
    left_func_col = 6
    right_func_col = left_func_col + 16
    right_num_col = right_func_col + 14
    sep_col = right_func_col - 2
    total_cols = right_num_col + 4

    canvas = _canvas(0, total_cols)
    _put(canvas, 0, 20, "树莓派 5 · 40-pin 排针（俯视，USB 口朝下）")
    _put(canvas, 2, left_num_col, "物理脚")
    _put(canvas, 2, left_func_col, "功能")
    _put(canvas, 2, right_func_col, "功能")
    _put(canvas, 2, right_num_col, "物理脚")

    for index, (left_pin, right_pin) in enumerate(zip(left, right)):
        row = 4 + index
        _put(canvas, row, left_num_col, f"{left_pin:>2}{'★' if left_pin in usage else ' '}")
        _put(canvas, row, left_func_col, f"{wire_spec.PHYSICAL_FUNCTION[left_pin]:<14}")
        _put(canvas, row, sep_col, "┃")
        _put(canvas, row, right_func_col, f"{wire_spec.PHYSICAL_FUNCTION[right_pin]:<14}")
        _put(canvas, row, right_num_col, f"{'★' if right_pin in usage else ' '}{right_pin:>2}")

    lines = _render(canvas).rstrip().splitlines()
    used_text = "、".join(f"{p}({wire_spec.pin_usage()[p]})" for p in sorted(usage))
    legend = [
        "",
        f"  ★ = 本基础版用到的脚：{used_text}",
        f"  3.3V 电源：物理脚 {'、'.join(map(str, wire_spec.V33_PHYSICAL))}　　5V（本基础版不要用）：物理脚 {'、'.join(map(str, wire_spec.V5_PHYSICAL))}",
        f"  GND：物理脚 {_gnd_list_text()}（任意一个都行，推荐物理脚 {wire_spec.GND_RECOMMENDED}）",
        f"  保留不用：物理脚 27、28（{wire_spec.RESERVED_PHYSICAL[27]}、{wire_spec.RESERVED_PHYSICAL[28]}）",
    ]
    return "```\n" + "\n".join(lines + legend) + "\n```"


def _wire_rows() -> List[List[str]]:
    return [
        [
            w.signal,
            f"`{w.sensor_pin}`",
            w.physical_text,
            w.bcm_text,
            w.color,
            w.note,
        ]
        for w in wires()
    ]


# ==========================================================================
# 一、硬件总览（索引）
# ==========================================================================


def build_index() -> str:
    used_rows = [
        [
            f"**{w.physical}**",
            w.bcm_text,
            w.sensor_pin.split(" / ")[0],
            w.signal,
            w.color,
            w.note,
        ]
        for w in wires()
    ]
    body = f"""{_header("基础版硬件接线总览（课程作业 H：温湿度测量）")}

> 这份目录是 `basic/` 的**硬件入口**：只讲一件事 —— 把 **DHT11 温湿度模块**接到树莓派 5 上，
> 让 `python3 run.py` 能读到真实温度与湿度。
> 软件怎么跑见 [`../README.md`](../README.md)；验收怎么打勾见 [`../验收说明.md`](../验收说明.md)。

## 0. 一句话接线（三根线）

{_table(
    ["DHT11 侧", "接到树莓派（物理脚）", "BCM", "说明"],
    _wire_rows_min(),
    ["---", ":---:", "---", "---"],
)}
> 记忆点：**供电 3.3V、数据 GPIO{wire_spec.DATA_BCM}（物理脚 {wire_spec.DATA_PHYSICAL}）、一定要共地**。

## 1. 本目录有什么

| 文件 | 什么时候看 | 内容 |
| --- | --- | --- |
| [`01-引脚分配表.md`](01-引脚分配表.md) | 插线前先看 | 40-pin 逐脚分配、本基础版用到哪几个脚、空闲脚一览 |
| [`02-接线图.md`](02-接线图.md) | 动手接线时 | ASCII 接线图、逐线接续表、上拉电阻怎么接、万用表验证步骤 |
| [`03-供电与安全.md`](03-供电与安全.md) | 接线前必读 | 3.3V/5V 不要接错、电流预算、断电插拔、防静电 |
| [`04-线色与自查卡.md`](04-线色与自查卡.md) | 贴在工位上 | 一页纸内容：线色约定 + 通电前自查 + 现象对照 |
| [`04-线色与自查卡.pdf`](04-线色与自查卡.pdf) | 打印带到工位 | 上面那份的 PDF（A4，由 `scripts/md2pdf.cjs` 生成） |
| [`05-接线文档合集.md`](05-接线文档合集.md) / [`05-接线文档合集.pdf`](05-接线文档合集.pdf) | **打印这一份就够** | 上面 1–4 全部合成的一份（含目录），PDF 由 `scripts/md2pdf.cjs` 生成 |

## 2. 接线速查（本基础版用到的 3 个脚）

{_table(
    ["物理脚", "BCM", "用途", "信号", "建议线色", "关键提醒"],
    used_rows,
    [":---:", "---", "---", "---", ":---:", "---"],
)}

**其余 37 个脚本基础版全部空闲**（完整分配见 [`01-引脚分配表.md`](01-引脚分配表.md)）。

## 3. 三分钟接完的顺序

1. **断电**（拔掉树莓派电源；带电插拔是烧 GPIO 的头号原因）；
2. 红线上 **3.3V**（物理脚 {'/'.join(map(str, wire_spec.V33_PHYSICAL))}），黑线接 **GND**（物理脚 {wire_spec.GND_RECOMMENDED} 或任意地脚）；
3. 黄线接**数据脚 {wire_spec.DATA_PHYSICAL}（GPIO{wire_spec.DATA_BCM}）**；
4. 如果是**裸四针**传感器：在 DATA 与 3.3V 之间接一个 {wire_spec.pullup_text()} 电阻（三针模块通常已自带，不用接）；
5. 上电，跑自检与采集：

```bash
cd ~/raspberry-health-monitor/basic
python3 tools/selfcheck.py            # 应看到"真硬件后端可用：lgpio"
python3 tools/diag_dht_line.py        # 三态电平判据（读不到数据时用它定位）
python3 run.py                        # 真实读数 + 动态曲线
```

## 4. 硬件判据（验收用）

| # | 判据 | 怎么验 | 期望 |
| --- | --- | --- | --- |
| 1 | 模块供电正确 | 万用表直流档量模块 VCC 与 GND 之间 | **约 3.3V**（不是 5V） |
| 2 | 共地 | 万用表蜂鸣档：树莓派 GND ↔ 模块 GND | **响**（导通） |
| 3 | 数据线通 | 万用表蜂鸣档：模块 DATA ↔ **物理脚 {wire_spec.DATA_PHYSICAL}** | **响** |
| 4 | 上拉存在（裸传感器） | 断电量 DATA ↔ 3.3V 电阻 | 约 {wire_spec.pullup_text()} |
| 5 | 空闲电平 | `python3 tools/diag_dht_line.py` | 上拉=1、下拉=0（线上没有器件强驱动） |
| 6 | 能读出数 | `python3 run.py --no-plot --duration 20` | 每行都是真实温湿度（不是"未知"） |

## 5. 器件现状（**诚实标注**）

| 项 | 状态 |
| --- | --- |
| 引脚号、供电、上拉电阻要求 | ✅ 来自代码与数据手册（`wire_docs.py --check` 每次提交前机器校验） |
| 文档里的命令是否真实存在 | ✅ 由检查器核对（`basic/tools/`、`basic/run.py` 的路径必须存在） |
| 本基础版的**真机接线与读数** | ⬜ **未实测**（树莓派当前不在线；接好后按上面第 4 节的判据逐条打勾） |

> ⚠️ 报告与答辩请照实区分"纸面推导"与"真机实测"：本文档的所有数字都能在代码里找到出处，
> 但**"文档自洽"不等于"真机已经读通"**。

## 6. 读到数据之后（软件侧速查）

| 项 | 值 | 出处 |
| --- | --- | --- |
| 默认采样周期 | {wire_spec.RECOMMENDED_INTERVAL_S:g} 秒（硬件要求 ≥{wire_spec.MIN_INTERVAL_S:g} 秒） | `basic/wire_spec.py`（与驱动常量同源） |
| 数据存哪 | `basic/data/dht11_日期_时刻.csv`（一次运行一个文件；由 `basic/store.py` 的 `default_csv_path()` 生成） | `basic/store.py` |
| CSV 表头 | `{",".join(CSV_HEADER)}` | `basic/model.py` 的 `CSV_HEADER` |
| 读失败怎么写 | 温度/湿度字段**留空**、`status=fail`、`note` 写原因（**绝不写 0**） | `basic/model.py` 的 `Reading.to_csv_row()` |
| 曲线窗口怎么开 | `python3 run.py`（无显示器：`--duration 60 --save curve.png`） | `basic/plot.py` |
"""
    return body


def _wire_rows_min() -> List[List[str]]:
    """索引页用的简版逐线表（3 行，去掉冗余列）。"""
    return [
        [w.sensor_pin, f"**{w.physical}**", w.bcm_text, w.note]
        for w in wires()
    ]


# ==========================================================================
# 二、引脚分配表
# ==========================================================================


def build_pin_table() -> str:
    usage = wire_spec.pin_usage()
    rows: List[List[str]] = []
    for physical in range(1, 41):
        function = wire_spec.PHYSICAL_FUNCTION[physical]
        bcm = pins.physical_to_bcm(physical)
        bcm_text = f"GPIO{bcm}" if bcm is not None else "—"
        if physical in usage:
            status, assign = "✅ 本基础版使用", usage[physical]
        elif physical in wire_spec.RESERVED_PHYSICAL:
            status, assign = "🟡 保留（不要用）", wire_spec.RESERVED_PHYSICAL[physical]
        elif function in ("3.3V", "5V"):
            status, assign = "🔌 电源", "可给传感器供电（本基础版只用 3.3V）"
        elif function == "GND":
            status, assign = "🔌 地", "公共地（推荐用脚 " + str(wire_spec.GND_RECOMMENDED) + "）"
        elif physical in FIXED_FUNCTION_PHYSICAL:
            status, assign = "⚪ 空闲（固定功能）", FIXED_FUNCTION_PHYSICAL[physical]
        else:
            status, assign = "⚪ 空闲", "可用于扩展（加蜂鸣器 / LED / 按键…）"
        rows.append([physical, bcm_text, function, assign, status])

    used = sorted(usage)
    free_gpio = [
        p for p in range(1, 41)
        if p not in usage
        and wire_spec.PHYSICAL_FUNCTION[p] not in ("3.3V", "5V", "GND")
        and p not in wire_spec.RESERVED_PHYSICAL
    ]
    return f"""{_header("01 · 引脚分配表（树莓派 5 · 40-pin）")}

> **看物理脚号插线，看 BCM 编号写代码** —— 两者不是偏移关系
> （GPIO4 = 物理脚 {wire_spec.DATA_PHYSICAL}，但 GPIO27 = 物理脚 13）。
> 本表把 40 个脚**全部列出**（含未用的），方便你确认"这个脚到底能不能用"。

## 1. 完整 40-pin 分配

「状态」列：**✅ 本基础版使用** / **🔌 电源或地** / **🟡 保留** / **⚪ 空闲**

{_table(
    ["物理脚", "BCM", "默认功能", "本基础版怎么用", "状态"],
    rows,
    [":---:", "---", "---", "---", ":---:"],
)}

## 2. 统计

| 类别 | 数量 | 物理脚 |
| --- | ---: | --- |
| 3.3V | {len(wire_spec.V33_PHYSICAL)} | {'、'.join(map(str, wire_spec.V33_PHYSICAL))} |
| 5V（**本基础版不要用**） | {len(wire_spec.V5_PHYSICAL)} | {'、'.join(map(str, wire_spec.V5_PHYSICAL))} |
| GND | {len(wire_spec.GND_PHYSICAL)} | {_gnd_list_text()} |
| 本基础版使用 | {len(used)} | {'、'.join(map(str, used))} |
| 空闲（可扩展） | {len(free_gpio)} | {'、'.join(map(str, free_gpio))} |
| 保留（不要用） | {len(wire_spec.RESERVED_PHYSICAL)} | {'、'.join(map(str, sorted(wire_spec.RESERVED_PHYSICAL)))} |

> ⚠️ **GND 只有 8 个脚**：本基础版只用 1 个（脚 {wire_spec.GND_RECOMMENDED}），
> 以后加器件时建议用**面包板地轨**汇流，不要把好几根线硬塞进同一个物理脚。

## 3. 常用扩展引脚（以后加功能时先看这里）

| 想加什么 | 推荐引脚（物理脚 / BCM） | 说明 |
| --- | --- | --- |
| 蜂鸣器（报警） | 12 / GPIO18 | 需串 100Ω~1kΩ 限流 |
| LED 状态灯 | 15 / GPIO22（绿）、16 / GPIO23（黄） | 每个都要串 220Ω~1kΩ |
| 按键（手动记录） | 13 / GPIO27 | 一端接脚 13，另一端接 GND，用内部上拉 |
| 第二个单总线器件 | 11 / GPIO17、29 / GPIO5 | 不要把两个器件并到同一条数据线 |

⚠️ **同一个 GPIO 只能有一个主人**：两个器件抢一个脚，会出现"一动这个、那个也跟着变"
的诡异现象（本项目在监护系统里真的踩过：人体红外与按键撞脚，人一走过就误触发求助）。
`basic/pins.py` 里的 `find_conflicts()` 就是查这个的。

## 4. 引脚号从哪来（**不许手写**）

| 内容 | 唯一来源 |
| --- | --- |
| BCM ↔ 物理脚映射 | `basic/pins.py` 的 `BCM_TO_PHYSICAL` |
| 数据脚默认值 | `basic/dht11read.py` 的 `Dht11Reader(pin=...)` 默认参数 |
| 本表 | `python3 basic/tools/wire_docs.py --generate` 从上面两处生成 |

**为什么强调这一点**：本项目早期有一个驱动用 `pin + 1` 猜物理脚号，
把 GPIO27 报成"物理脚 28"（28 其实是 HAT ID_SC）—— 同学照着插线就插错了。
判据：**任何形如 `pin + N` 的物理脚换算一律视为缺陷**。
"""


# ==========================================================================
# 三、接线图（逐线 + 上拉 + 万用表验证）
# ==========================================================================


def wiring_diagram() -> str:
    """ASCII 接线图（**按列号拼出来**，插值对齐不靠手写空格）。

    结构（自左向右）：树莓派 40-pin 的三个脚 → 三根线 → 传感器模块的三个端子。
    画的规则：先算好"引脚列 / 走线列 / 端子列"，再用 ``_put`` 往画布上填字符 ——
    所以以后数据脚从 4 改成 17，整张图**仍然对齐**（手写空格做不到这一点）。
    """
    #: 三个信号：(物理脚, 功能标签, 线色, 传感器端子名)
    rows_spec = [
        (wire_spec.V33_PHYSICAL[0], "3.3V", "红", "VCC"),
        (wire_spec.GND_RECOMMENDED, "GND", "黑", "GND"),
        (int(wire_spec.DATA_PHYSICAL or 0), f"GPIO{wire_spec.DATA_BCM}", "黄", "DATA"),
    ]
    pin_col, wire_col, dot_col, term_col, note_col = 1, 16, 26, 30, 44
    step = 2
    title_row = 0
    canvas = _canvas(0, 100)
    _put(canvas, title_row, pin_col, "树莓派 5 · 40-pin（俯视，只画要接的三个脚）")
    _put(canvas, title_row, term_col, "DHT11 模块端子")
    first_row = title_row + 2
    terminal_rows: List[int] = []
    for index, (physical, label, color, terminal) in enumerate(rows_spec):
        row = first_row + index * step
        terminal_rows.append(row)
        star = "★" if physical in wire_spec.pin_usage() else " "
        _put(canvas, row, pin_col, f"脚 {physical:>2}{star} {label:<6}")
        _line(canvas, row, wire_col, dot_col)
        _put(canvas, row, dot_col, "●")
        _put(canvas, row, term_col, f"{terminal:<5}")
        _put(canvas, row, note_col, f"（{color}线）")

    picture = _render(canvas).rstrip()
    used = wire_spec.pin_usage()
    legend = [
        "",
        f"  ① 三个物理脚：{'、'.join(f'{p}（{label}）' for p, label, _c, _t in rows_spec)}"
        f"　★ = 本基础版用到的脚（其余 37 个空闲）",
        f"  ② 裸四针传感器：在 DATA 与 3.3V 之间接一个 {wire_spec.pullup_text()} 上拉电阻（三针模块通常已自带）",
        "  ③ 颜色只是约定：红线=电源、黑线=地、黄线=数据（换色可以，但要在验收记录里写明）",
        "  ④ DHT11 模块丝印可能是 VCC/DATA/GND，也可能是 + / out / −（对应关系见下面的逐线表）",
    ]
    return "```\n" + picture + "\n" + "\n".join(legend) + "\n```"


def build_wiring_diagram() -> str:
    pull_text = wire_spec.pullup_text()
    return f"""{_header("02 · 接线图与逐线接续表（DHT11 → 树莓派 5）")}

## 1. 接线图（示意图，只画本基础版要接的三根线）

{wiring_diagram()}

> 三针**模块**（带小板的成品）通常已经焊好上拉电阻，**不用自己再接**；
> 四针**裸传感器**（光板一个元件）必须外接 {pull_text} 上拉，否则永远读不到（一直 NaN / 无应答）。

## 2. 逐线接续表（照这个插，一根都别猜）

{_table(
    ["信号", "DHT11 侧丝印", "树莓派物理脚", "BCM", "建议线色", "关键提醒"],
    _wire_rows(),
    ["---", "---", ":---:", "---", ":---:", "---"],
)}

**接线顺序**：先电源 → 再地 → 最后信号。每插一根就检查一次有没有插错列。

## 3. 三种封装的差别（买到的可能是哪一种）

| 封装 | 长相 | 需要上拉吗 | 供电 |
| --- | --- | --- | --- |
| **三针模块** | 小板 + 3 个排针，丝印 `VCC/DATA/GND` 或 `+ / out / -` | **不需要**（板上已有） | 3.3V（**见第 4 节**） |
| **四针裸传感器** | 光秃秃一个蓝色/白色元件，4 个长脚 | **需要** {pull_text} | 3.3V（脚 1 或 17） |
| **AM2302 / DHT22** | 白色塑料壳，3 或 4 脚 | 模块版已自带 | 3.3V |

> 本基础版的代码对 DHT11 / DHT22 都适用（都是"单总线、40 bit、校验和"），
> 只是量程判据按 DHT11 写死（{wire_spec.TEMP_RANGE_C[0]:.0f}~{wire_spec.TEMP_RANGE_C[1]:.0f}℃ / {wire_spec.HUMI_RANGE_PCT[0]:.0f}~{wire_spec.HUMI_RANGE_PCT[1]:.0f}%RH）。

## 4. 供电为什么必须 3.3V（**本基础版最容易被坑的一点**）

| 事实 | 说明 |
| --- | --- |
| 数据线电平 = 模块供电电压 | DHT11 的 DATA 是**开漏/上拉**结构。模块接 5V 时，上拉把 DATA 拉到 **5V**；树莓派 GPIO 只耐受 **3.3V** |
| 后果 | 轻则读数不稳、重则**永久损坏 GPIO**（甚至整块板） |
| 有些模块丝印写 5V | 指的是 **AM2302 那种带稳压的模块**（板上有 3.3V LDO）。**DHT11 不要接 5V** |
| 本基础上的做法 | 供电一律走 **3.3V（物理脚 {'/'.join(map(str, wire_spec.V33_PHYSICAL))}）**，5V（脚 {'/'.join(map(str, wire_spec.V5_PHYSICAL))}）本基础版**不接任何东西** |

**判据（可执行）**：上电后万用表量模块 `VCC` 对 `GND`，必须是 **约 3.3V**；
若量到 5V，立刻断电改线（这是"接线错误"里唯一会烧硬件的错）。

## 5. 接完怎么验（万用表 + 脚本，两套判据都要过）

### 5.1 断电量电阻（防短路，30 秒）

| 量哪里 | 档位 | 期望 | 不对说明什么 |
| --- | --- | --- | --- |
| 3.3V ↔ GND（树莓派这侧，**模块先拔掉**） | 电阻档 | 不导通（或读数很大） | 导通 = 电源短路，**绝对不能上电** |
| 模块 DATA ↔ 树莓派物理脚 {wire_spec.DATA_PHYSICAL} | 蜂鸣档 | 响 | 不响 = 线插错列/断了 |
| 模块 GND ↔ 树莓派 GND 脚 | 蜂鸣档 | 响 | 不响 = 没共地（读数必然乱跳） |
| 模块 DATA ↔ 3.3V（裸传感器） | 电阻档 | 约 {pull_text} | 无穷大 = 上拉没接 |

### 5.2 上电量电压（判断器件有没有工作）

| 量哪里 | 期望 | 不对时的含义 |
| --- | --- | --- |
| 模块 VCC ↔ GND | **约 3.3V** | 0V = 没供上电；5V = 接错到 5V 脚（危险） |
| DATA ↔ GND（空闲时） | **约 3.3V**（{wire_spec.IDLE_LEVEL}） | 0V = 线被拉死/接错；乱跳 = 没接上/接触不良 |

### 5.3 脚本判据（`basic/tools/diag_dht_line.py`）

```bash
cd ~/raspberry-health-monitor/basic
python3 tools/diag_dht_line.py            # 三态电平（上拉/下拉/浮空 各读 50 次）
python3 tools/diag_dht_line.py --read 5   # 顺便连读 5 次，看真实应答
```

它会把"上拉=1、下拉=0"这类组合翻译成结论（**哪一种组合对应哪种故障**），
不要求你看懂寄存器。

### 5.4 最终判据（读得到数才算通）

```bash
python3 run.py --no-plot --duration 20    # 20 秒，只采集与存档
python3 run.py                            # 真实读数 + 动态曲线窗口
```

| 期望 | 说明 |
| --- | --- |
| 终端每行都是真实数值（如 `温度: 25.0 ℃，湿度: 58 %`） | 这才是"通了" |
| 温度落在 {wire_spec.TEMP_RANGE_C[0]:.0f}~{wire_spec.TEMP_RANGE_C[1]:.0f}℃、湿度落在 {wire_spec.HUMI_RANGE_PCT[0]:.0f}~{wire_spec.HUMI_RANGE_PCT[1]:.0f}%RH | 超出量程说明器件/接线有问题 |
| 采样间隔 ≥{wire_spec.RECOMMENDED_INTERVAL_S:g} 秒（默认） | DHT11 两次读取必须间隔 ≥{wire_spec.MIN_INTERVAL_S:g} 秒，否则返回陈旧数据 |

## 6. 现象 → 原因对照（按现象查）

| 现象 | 最可能的原因 | 下一步 |
| --- | --- | --- |
| `只捕获到 0 个边沿：传感器没有应答` | 没供电 / 数据线插错列 / 没共地 / 裸传感器缺上拉 | 先量 VCC 是否 3.3V，再跑 `diag_dht_line.py` |
| `校验和不符` | 线太长、接触不良、电源噪声、读取太快 | 缩短杜邦线、换线、确认间隔 ≥{wire_spec.MIN_INTERVAL_S:g} 秒 |
| 每次都是"未知" | DATA 接到别的脚（不是物理脚 {wire_spec.DATA_PHYSICAL}） | 蜂鸣档量 DATA ↔ 脚 {wire_spec.DATA_PHYSICAL} |
| 读数偶尔对、偶尔乱 | 面包板接触不良 / 电源轨与信号插在同一列 | 换孔重插；面包板**每一列纵向导通**，别把电源和信号插同列 |
| 中文变方框（曲线窗口） | 缺中文字体 | `sudo apt install -y fonts-noto-cjk` |
| `没有可用的 DHT11 读取后端` | 没装 lgpio，或是在电脑上跑 | `sudo apt install -y python3-lgpio`；电脑上用 `--mock` |

> 完整"现象 → 原因"还包括软件侧的（中文方框、matplotlib 缺失等），见 [`../README.md`](../README.md) 第 7 节。
"""


# ==========================================================================
# 四、供电与安全
# ==========================================================================


def build_power_safety() -> str:
    low_ma, high_ma = wire_spec.MODULE_CURRENT_MA
    rail_ma = wire_spec.PI_3V3_RAIL_A * 1000
    return f"""{_header("03 · 供电与安全（接线前必读）")}

> 这一页只有 6 条规矩，但**每一条都对应一次真实的硬件损坏或一周的排查**。
> 本基础版只接一个 DHT11，风险很低 —— 但"以后加器件"时请回来重读第 5 节。

## 1. 六条红线（照做）

| # | 规矩 | 为什么 |
| --- | --- | --- |
| 1 | **插拔任何杜邦线之前先断电**（拔电源，或 `sudo poweroff` 后等绿灯灭） | 带电插拔最容易把 5V 蹭到 GPIO 上；热插拔还可能损坏 SD 卡上的文件系统 |
| 2 | **DHT11 供 3.3V，绝不接 5V** | 数据电平会跟着供电变成 5V，超出 GPIO 耐压（见 [`02-接线图.md`](02-接线图.md) 第 4 节） |
| 3 | **所有器件必须与树莓派共地** | 不共地的典型症状：读数乱跳、时通时不通 |
| 4 | **不要用 40-pin 的 5V 脚给 3.3V 器件供电**（脚 {'、'.join(map(str, wire_spec.V5_PHYSICAL))}） | 本基础版**一个 5V 器件都没有**，那两脚直接空着 |
| 5 | **上电前先量电源对地有没有短路** | 30 秒的检查能避免烧板（方法见 [`02-接线图.md`](02-接线图.md) 第 5.1 节） |
| 6 | **手摸板子前先摸一下接地的金属**（水管/机箱） | 静电击穿是"什么都没做就坏了"的常见原因 |

## 2. 电源从哪来

| 项 | 值 | 来源 |
| --- | --- | --- |
| 树莓派 5 官方电源要求 | **{wire_spec.PI_PSU}** | 官方规格（5A 是为了给 USB 外设留余量） |
| 本基础版的供电方式 | {wire_spec.POWER_MODE} | 只接一个 DHT11，不需要外部电源 |
| 3.3V 轨电流上限 | 约 **{wire_spec.PI_3V3_RAIL_A:g} A（{rail_ma:.0f} mA）** | 40-pin 的 3.3V 由板载稳压器提供 |

**电源不足的典型症状**（不是"跑不起来"，而是"跑得诡异"）：
彩虹屏 / 反复重启 / I2C 器件时有时无 / 传感器读数乱跳。
看到这些先换电源，别急着改代码。

## 3. 电流预算（本基础版）

| 器件 | 典型电流 | 说明 |
| --- | ---: | --- |
| DHT11 模块 | {low_ma:g}~{high_ma:g} mA | 只在测量瞬间取电（本基础版唯一的负载） |
| **合计** | **≤ {high_ma:g} mA** | 相对 {rail_ma:.0f} mA 的 3.3V 上限，**可忽略** |

> 以后加器件（LCD、TFT、多个 LED）时，把它们的电流加进来重算；
> 接近 {rail_ma:.0f} mA 就该考虑给器件单独供电（**但地仍要共**）。

## 4. 电平匹配：什么时候需要电平转换

| 器件输出 | 能否直连树莓派 GPIO | 说明 |
| --- | --- | --- |
| **3.3V 输出**（DHT11 @3.3V 供电、常见的 HC-SR501 模块） | ✅ 可以直连 | 本项目基础版就属于这一类 |
| **5V 输出**（DHT11 误接 5V 时、HC-SR04 的 ECHO） | ❌ **绝对不行** | 必须用 TXS0102 双向电平转换，或用 1kΩ+2kΩ 电阻分压 |
| 开漏输出 + 上拉到 3.3V | ✅ 可以 | 单总线（DHT11）就是这种形态：**上拉接到 3.3V，而不是 5V** |

**记忆点**：决定"能不能直连"的不是器件名字，而是**它的输出电平是多少伏**。
本基础版把 DHT11 接在 3.3V 上，所以数据线是 3.3V —— 这正是必须 3.3V 供电的根本原因。

## 5. 安全接线检查单（上电前逐条打勾）

- [ ] 电源已**断开**
- [ ] 3.3V ↔ GND 不导通（没有短路）
- [ ] 模块 VCC 接的是**物理脚 {'/'.join(map(str, wire_spec.V33_PHYSICAL))}**（3.3V），不是脚 {'、'.join(map(str, wire_spec.V5_PHYSICAL))}（5V）
- [ ] 模块 GND 接的是 **GND 脚**（推荐脚 {wire_spec.GND_RECOMMENDED}）
- [ ] DATA 接的是**物理脚 {wire_spec.DATA_PHYSICAL}（GPIO{wire_spec.DATA_BCM}）**
- [ ] 裸四针传感器已接 {wire_spec.pullup_text()} 上拉到 **3.3V**（不是 5V）
- [ ] 杜邦线插紧、没有跨列（面包板每一列纵向导通）
- [ ] 上电后模块 VCC 对 GND 约 3.3V

## 6. 出了问题怎么安全收尾

| 情况 | 正确做法 |
| --- | --- |
| 闻到焦味 / 板子发烫 | **立刻拔电源**，不要先看代码 |
| 读数一直不对 | 断电 → 按 [`02-接线图.md`](02-接线图.md) 第 5 节重新量一遍 → 再上电 |
| 树莓派起不来 | 断电，拔掉所有杜邦线，只留电源试开机（区分"接线问题"与"系统问题"） |
| 要收工放起来 | `sudo poweroff` → 等绿灯熄灭 → 再拔电源；器件装进塑料盒防潮防静电 |
"""


# ==========================================================================
# 五、线色与自查卡（一页纸，可打印）
# ==========================================================================


def build_quick_card() -> str:
    pull_text = wire_spec.pullup_text()
    usage = wire_spec.pin_usage()
    return f"""{_header("04 · 线色与自查卡（一页纸，建议打印）")}

> 贴在工位上：接线时不用翻长文档，这一页就够了。
> PDF 版：[`04-线色与自查卡.pdf`](04-线色与自查卡.pdf)（A4，生成命令在文件末尾）
> —— 同目录的 `04-线色与自查卡.html` 是打印用的中间文件，不用管它。

## 1. 线色约定（先定规矩，排错时省一半时间）

| 线色 | 用途 | 接到哪 |
| --- | --- | --- |
| 🔴 红 | 电源 | 3.3V（物理脚 {'/'.join(map(str, wire_spec.V33_PHYSICAL))}） |
| ⚫ 黑 | 地 | GND（物理脚 {wire_spec.GND_RECOMMENDED} 或任意地脚） |
| 🟡 黄 | 数据 | **物理脚 {wire_spec.DATA_PHYSICAL}（GPIO{wire_spec.DATA_BCM}）** |

> 手上的线颜色不全也没关系 —— **约定好一套并写下来**比"颜色好看"重要得多。

## 2. 三根线速查

{_table(
    ["DHT11 侧", "树莓派物理脚", "BCM", "线色"],
    [[w.sensor_pin, f"**{w.physical}**", w.bcm_text, w.color] for w in wires()],
    ["---", ":---:", "---", ":---:"],
)}

## 3. 上电前 30 秒自查

- [ ] 断电量过：3.3V ↔ GND **不导通**（没短路）
- [ ] 红 = 3.3V（脚 {'/'.join(map(str, wire_spec.V33_PHYSICAL))}）　⚫ 黑 = GND（脚 {wire_spec.GND_RECOMMENDED}）　🟡 黄 = 数据（脚 {wire_spec.DATA_PHYSICAL}）
- [ ] 裸四针传感器：DATA ↔ 3.3V 之间接了 {pull_text} 电阻
- [ ] 杜邦线插紧、没插在同一列（面包板每列纵向导通）

## 4. 上电后 30 秒自查

```bash
cd ~/raspberry-health-monitor/basic
python3 tools/selfcheck.py            # 看后端是不是 lgpio
python3 tools/diag_dht_line.py        # 三态电平结论
python3 run.py --no-plot --duration 20
```

| 看到什么 | 说明 |
| --- | --- |
| `温度: 25.0 ℃，湿度: 58 %` | ✅ 通了 |
| `只捕获到 0 个边沿` | 器件没应答：查供电 / 数据线 / 共地 / 上拉 |
| `校验和不符` | 时序抖动：换短线、确认间隔 ≥{wire_spec.MIN_INTERVAL_S:g} 秒 |
| `没有可用的 DHT11 读取后端` | 没装 lgpio 或不在树莓派上跑 |

## 5. 引脚速查（本基础版只用这 3 个）

{_table(
    ["物理脚", "功能", "本基础版用途"],
    [[p, wire_spec.PHYSICAL_FUNCTION[p], usage[p]] for p in sorted(usage)],
    [":---:", "---", "---"],
)}

其余 37 个脚空闲（完整表见 [`01-引脚分配表.md`](01-引脚分配表.md)）。

## 6. 红线三条（背下来）

1. **插拔线先断电**；
2. **DHT11 接 3.3V，不接 5V**；
3. **必须共地**。

## 7. 这份 PDF 怎么来的

```bash
cd ~/raspberry-health-monitor
node scripts/md2pdf.cjs --in basic/hardware/04-线色与自查卡.md --out basic/hardware/04-线色与自查卡.pdf --title "基础版 · DHT11 线色与自查卡"
```

> 改了 md 之后请重新生成 PDF（两份必须同步）；
> 没有 Node/Edge 也不影响接线 —— md 里内容是全的，PDF 只是给打印用的。
"""


# ==========================================================================
# 六、打印合集（把 5 份文档合成一份可打印的 Markdown → PDF）
# ==========================================================================

#: 合成文档的文件名（先出 Markdown，再用 `scripts/md2pdf.cjs` 转 PDF）
COLLECTION_MD = "05-接线文档合集.md"

#: 合集里的 PDF 文件名
COLLECTION_PDF = "05-接线文档合集.pdf"

#: 生成 PDF 时留下的中间 HTML（由 md2pdf.cjs 产出）
COLLECTION_HTML = "05-接线文档合集.html"


def print_sources() -> Dict[str, Tuple[str, ...]]:
    """``源 md → 它产生的打印产物``（供清单与校验使用）。

    两类：
    1. 单份文档的 PDF（`04-线色与自查卡.md` → 一页纸自查卡）；
    2. **合集 PDF**（由全部 5 份文档合成，源就是合集自身的 md）。
    """
    return {
        "04-线色与自查卡.md": ("04-线色与自查卡.pdf", "04-线色与自查卡.html"),
        COLLECTION_MD: (COLLECTION_PDF, COLLECTION_HTML),
    }

#: 合集的目录（与 DOCUMENTS 的顺序一致；标题里的"·"前是编号）
COLLECTION_TOC: Tuple[str, ...] = (
    "接线总览（三根线速查 + 硬件判据）",
    "40-pin 引脚分配表",
    "接线图与逐线接续表（含万用表验证）",
    "供电与安全",
    "线色与自查卡",
)

#: 指向"另一份 basic 文档"的链接：打印件里没有别的文件，转成纯文本
_LINK_RE = re.compile(r"\[([^\]]+)\]\((?!https?://)([^)#][^)]*)\)")
#: 指向本文档内部章节的链接（`#xxx`）：打印件里跳转没意义，保留文字
_ANCHOR_RE = re.compile(r"\[([^\]]+)\]\(#[^)]*\)")
#: 有序列表项（`1. xxx`）：`md2pdf.cjs` 只在"**上一行是空行或另一个列表项**"时才当列表处理，
#: 所以合成时要把连着写的列表项拆开，否则它们会被渲染成一行（实测踩到）
_ORDERED_ITEM_RE = re.compile(r"^(?P<num>\d+)\.\s+(?P<text>.+)$")


def _plain_links(text: str) -> str:
    """把"指向仓库内文件"的 Markdown 链接降级成纯文本（打印件里那些文件不存在）。

    判据：http(s) 链接保留（打印出来也能照着敲）；指向仓库文件的链接保留**文字**并去掉路径，
    否则 PDF 里会看到一堆 `../README.md` 这种"点了也没用"的路径。
    """
    text = _ANCHOR_RE.sub(r"\1", text)
    return _LINK_RE.sub(r"\1", text)


def _space_lists(text: str) -> str:
    """把连着的有序列表项拆成"每项之间空一行"。

    为什么（实测）：`scripts/md2pdf.cjs` 判定"列表开始"的条件是 **上一行是空行**、
    "列表继续"的条件是 **上一行也是列表项**。而 `1.` 和 `2.` 这种**有序**列表项
    它的续行判断只认 `-`/`*` 开头，于是连着的 `1. 2. 3.` 会被当普通段落渲染成一行。
    合集里我们用有序列表写目录，所以合成前统一加空行 —— 这是"生成给谁看"的适配：
    面向 PDF 渲染器，而不是面向 GitHub。
    """
    out: List[str] = []
    for line in text.splitlines():
        is_item = bool(_ORDERED_ITEM_RE.match(line))
        if is_item and out and out[-1].strip():
            out.append("")
        out.append(line)
    return "\n".join(out)


def _strip_generated_notice(text: str) -> str:
    """去掉单份文档开头的"本文件由代码生成"声明（合集开头统一写一次即可）。"""
    lines = text.splitlines()
    kept: List[str] = []
    for line in lines:
        if line.startswith("> 🤖 **本文件由代码生成"):
            continue
        if line.startswith("> 生成器：`python3 basic/tools/wire_docs.py"):
            continue
        if line.startswith("> 事实来源："):
            continue
        if line.startswith("> 基础版版本："):
            continue
        kept.append(line)
    return "\n".join(kept).strip("\n")


def _demote_headings(text: str, demote: int = 1) -> str:
    """把 `#` 标题整体降级（合集里各份文档是"章"，所以降到 `##` 起）。"""
    out: List[str] = []
    for line in text.splitlines():
        if line.startswith("#"):
            hashes = len(line) - len(line.lstrip("#"))
            out.append("#" * (hashes + demote) + line[hashes:])
        else:
            out.append(line)
    return "\n".join(out)


def build_collection() -> str:
    """把 5 份文档合成一份适合打印的 Markdown（含封面 + 用法 + 各章）。

    做法与理由：
    - 每份文档降两级标题，合集用 `##` 当章标题；
    - 每份文档的"本文件由代码生成"声明只保留一次（合集抬头）；
    - 指向仓库内文件的链接降级成纯文本（打印件里没有那些文件）；
    - 有序列表项之间补空行（`md2pdf.cjs` 的有序列表续行判定只认 `-`/`*`，见 `_space_lists`）。

    ⚠️ **刻意不写"页数"**：页数由浏览器排版决定，写进 Markdown 会引入"改了页数 →
    Markdown 哈希变 → PDF 又要重导出"的循环（生成与校验互相打架）。
    页数在导出时由 `md2pdf.cjs` 打印出来，看终端即可。
    """
    parts: List[str] = []
    parts.append(f"""# 基础版 · DHT11 温湿度接线文档（合集）

> 用途：**打印这一份就够** —— 接线图、逐线接续表、引脚分配、供电安全、自查卡全在里面。
> 单份 Markdown（可编辑版）与其它资料见仓库 `basic/hardware/` 目录。
>
> 事实来源：`basic/pins.py`（引脚映射）、`basic/dht11read.py`（驱动默认值与量程）、`basic/wire_spec.py`（接线事实）
> 生成方式：`python3 basic/tools/wire_docs.py --generate`（Markdown）→
> `node scripts/md2pdf.cjs --in basic/hardware/{COLLECTION_MD} --out basic/hardware/{COLLECTION_PDF}`
> 基础版版本：{__version__}

## 怎么用这一份（打印前先看三行）

1. **要接线**：看"接线图与逐线接续表"那一章 —— 三根线：脚 1=3.3V（红）、脚 6=GND（黑）、脚 7=DATA（黄）；
2. **接完验收**：看那章的"接完怎么验"（万用表三项）与最后一章"线色与自查卡"；
3. **读不到数**：先跑 `python3 tools/diag_dht_line.py`，再对照那章的"现象 → 原因"表。

---

""")
    for name, title in zip(DOCUMENTS, COLLECTION_TOC):
        body = _strip_generated_notice(DOCUMENTS[name]())
        # 去掉单份文档自己的 H1（合集里统一用小节标题），其余标题整体降一级
        lines = [line for line in body.splitlines() if not line.startswith("# ")]
        body = _demote_headings("\n".join(lines).strip("\n"), demote=2)
        body = _space_lists(_plain_links(body))
        parts.append(f"## {title}\n\n{body}\n\n---\n")
    parts[0] = _space_lists(parts[0])      # 封面里的"怎么用"也是有序列表，同样要拆开
    return "\n".join(parts)


def generate_collection(target_dir: Path = HW_DIR) -> Path:
    """写出合集 Markdown（**只写 .md**；PDF 由 `scripts/md2pdf.cjs` 转）。"""
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / COLLECTION_MD
    _write_lf(path, build_collection())
    return path


# ==========================================================================
# 生成 / 校验
# ==========================================================================

#: ``文件名 → 生成函数``（顺序即索引页里的顺序）
DOCUMENTS: Dict[str, Callable[[], str]] = {
    "README.md": build_index,
    "01-引脚分配表.md": build_pin_table,
    "02-接线图.md": build_wiring_diagram,
    "03-供电与安全.md": build_power_safety,
    "04-线色与自查卡.md": build_quick_card,
}

#: 生成/校验时必须存在的其它文件（两份 PDF 与它们的中间 HTML，由 `scripts/md2pdf.cjs` 产出）
REQUIRED_FILES: Tuple[str, ...] = (
    "04-线色与自查卡.pdf", "04-线色与自查卡.html",
    COLLECTION_PDF, COLLECTION_HTML,
)

#: 记录"打印产物是用哪一版 md 生成的"的清单文件
MANIFEST_NAME = ".print-manifest.json"


def _sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_print_manifest(target_dir: Path = HW_DIR) -> Path:
    """记录每个打印产物（及其源 md）的哈希（生成时写）。

    ⚠️ 为什么需要（真问题）：生成器只写 md，PDF 是**另一个命令**（`scripts/md2pdf.cjs`）产的。
    如果只检查"PDF 存在"，那么"PDF 被手改 / 回退 / 丢失"就会被漏过去 ——
    打印出来的接线图与正文不一致，正好是最难发现的一类错误。
    所以把"当时源文件与产物的哈希"记进清单，`--check` 时逐项比对。
    """
    import json

    sources = {}
    for source, outputs in print_sources().items():
        if not (target_dir / source).exists():
            continue
        sources[source] = {
            "sha256": _sha256(target_dir / source),
            "outputs": {
                name: (_sha256(target_dir / name) if (target_dir / name).exists() else None)
                for name in outputs
            },
        }
    manifest = {
        "note": "由 wire_docs.py --generate 写出；记录打印产物及其源 md 的哈希（发现'忘了重新导出 PDF'与'产物被改'）",
        "sources": sources,
    }
    path = target_dir / MANIFEST_NAME
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return path


def check_print_sync(target_dir: Path = HW_DIR) -> List[str]:
    """校验打印产物与"生成清单时的版本"一致（哈希比对，见 :func:`write_print_manifest`）。

    ⚠️ 这里**只管"产物有没有被换过/丢失"**：改了 md 之后正确做法是
    ① 重新导出 PDF/HTML ② 再跑 `--generate`（它会把新哈希写进清单）。
    所以本检查能发现"PDF 被手改/丢失/回退"，而流程性疏漏由文档里那句
    "改了 md 之后请重新生成 PDF" 与人工验收兜住（机器无法判断"人有没有记得"）。
    """
    import json

    problems: List[str] = []
    manifest_path = target_dir / MANIFEST_NAME
    if not manifest_path.exists():
        return [f"缺少打印产物清单：{manifest_path}（跑 `--generate` 会重建）"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"打印产物清单读不出来：{exc}"]

    for source, info in (manifest.get("sources") or {}).items():
        source_path = target_dir / source
        if not source_path.exists():
            problems.append(f"清单里记录的源文件不存在：{source}")
            continue
        for output, recorded in (info.get("outputs") or {}).items():
            output_path = target_dir / output
            if not output_path.exists():
                problems.append(f"缺少打印产物：{output}（由 {source} 生成，命令在该文档末尾）")
                continue
            if recorded and _sha256(output_path) != recorded:
                problems.append(
                    f"打印产物与清单不一致：{output} 被改过或已回退 —— "
                    f"请重新导出（命令在 `{source}` 末尾）再跑 `--generate`"
                )
    return problems


def _write_lf(path: Path, text: str) -> None:
    """以 **LF** 换行写入（跨平台一致）。

    为什么不能用 `Path.write_text()`：Windows 上它会把 ``\\n`` 写成 CRLF，
    而仓库的 `.gitattributes`（以及 `.sh` 的 LF 纪律）要求 LF ——
    结果是"本地生成 → git 归一化成 LF → 下次 `--check` 报不一致"的假问题。
    判据：生成类工具**必须显式控制换行符**，不要交给平台默认行为。
    """
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def generate_all(target_dir: Path = HW_DIR) -> List[Path]:
    """把全部文档写到磁盘（含打印合集 md），返回写入的文件列表（并刷新打印产物清单）。"""
    target_dir.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []
    for name, builder in DOCUMENTS.items():
        path = target_dir / name
        _write_lf(path, builder())
        written.append(path)
    written.append(generate_collection(target_dir))
    written.append(write_print_manifest(target_dir))
    return written


def check_all(target_dir: Path = HW_DIR) -> List[str]:
    """校验磁盘上的文档 == 生成的文档；返回问题列表（空 = 全部一致）。

    四项判据：
    1. 文档存在；
    2. 内容与生成结果**逐字符一致**（不一致 ⇒ 有人手改了，或改了代码没重新生成）；
    3. 关键事实必须在**全部文档的并集**里出现（物理脚号、供电电压、上拉、间隔、CSV 表头…）
       —— 防止生成器将来漏渲染某个数字，而"自己和自己一致"却仍然通过；
    4. 文档里提到的仓库内脚本路径必须真实存在（防止"照着文档敲命令 → 文件不存在"）。
    """
    problems: List[str] = []
    combined: List[str] = []
    for name, builder in DOCUMENTS.items():
        path = target_dir / name
        expected = builder()
        if not path.exists():
            problems.append(f"缺少文档：{path}（跑 `--generate` 生成）")
            continue
        actual = path.read_text(encoding="utf-8")
        combined.append(actual)
        if actual != expected:
            diff = list(
                difflib.unified_diff(
                    actual.splitlines(), expected.splitlines(),
                    fromfile=f"{name}（磁盘上）", tofile=f"{name}（按代码生成）", lineterm="", n=1,
                )
            )
            head = "\n      ".join(diff[:20])
            problems.append(f"文档与代码不一致：{name}（改了代码没重新生成，或手工改过文档）\n      {head}")

    corpus = "\n".join(combined)
    if corpus:
        problems.extend(check_required_facts(corpus))
        problems.extend(check_referenced_paths(corpus, target_dir))

    # 打印合集：内容与生成结果一致 + 5 个章节齐全（少了章节说明合成逻辑坏了）
    collection_path = target_dir / COLLECTION_MD
    if not collection_path.exists():
        problems.append(f"缺少打印合集：{collection_path}（跑 `--generate` 生成）")
    else:
        expected_collection = build_collection()
        if collection_path.read_text(encoding="utf-8") != expected_collection:
            problems.append(
                f"打印合集与代码不一致：{COLLECTION_MD}（改了生成器/事实源没重新生成，或手工改过）"
            )
        missing_chapters = [title for title in COLLECTION_TOC if title not in expected_collection]
        if missing_chapters:
            problems.append(f"打印合集缺少章节：{'；'.join(missing_chapters)}")

    for extra in REQUIRED_FILES:
        if not (target_dir / extra).exists():
            problems.append(
                f"缺少必需文件：{target_dir / extra}（PDF/HTML，生成命令见 `{COLLECTION_MD}` 或 "
                "`04-线色与自查卡.md` 的末尾）"
            )
    problems.extend(check_print_sync(target_dir))
    return problems


def check_required_facts(corpus: str) -> List[str]:
    """关键事实必须出现在文档里（纯文本判据，可注入自测）。"""
    return [
        f"文档里缺少关键事实「{label}」= {fact!r}（生成器可能漏渲染了这一项）"
        for label, fact in wire_spec.REQUIRED_FACTS.items()
        if fact not in corpus
    ]


def check_referenced_paths(corpus: str, target_dir: Path) -> List[str]:
    """文档里以 `basic/...` 形式提到的脚本文件必须真实存在。

    判据只取"看起来是本仓库内的相对路径"（形如 `basic/tools/xxx.py`），
    不检查系统命令与树莓派上的绝对路径 —— 那两类本来就不该存在于仓库里。
    """
    import re

    problems: List[str] = []
    repo_root = BASIC_DIR.parent
    pattern = re.compile(r"`(basic/[A-Za-z0-9_./\-\u4e00-\u9fff]+\.(?:py|md|csv|png|pdf))`")
    #: 占位符（运行时才产生真实文件名的路径）不是"引用文件"，不该被判悬空
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
            problems.append(f"文档引用了不存在的文件：`{rel}`（照着敲命令会报 No such file）")
    return problems


def self_test() -> List[str]:
    """★ 注入自测：先证明"检查器真的能抓到问题"，再拿它去检查真文档。

    为什么必须有（本项目踩过的教训）：一个"看起来在跑、其实一直没匹配"的检查器
    比没有检查器更危险 —— 它会给出虚假的安全感。
    做法：在内存里造几段"有问题的文本"，断言探针**必须**报警；
    再造一段正常文本，断言它**不**误报。
    """
    problems: List[str] = []
    # "好文本" = 把所有要求的关键事实都放进去（**从 wire_spec.REQUIRED_FACTS 自己拼**，
    # 这样以后新增一条关键事实时，自测不会因为"没跟上"而假红）
    good = "；".join(f"{label}={fact}" for label, fact in wire_spec.REQUIRED_FACTS.items())
    if wire_spec.pullup_text() not in good:
        problems.append("自测构造有误：好文本里应当含上拉电阻（否则'不误报'这条断言没有意义）")

    # ① 事实检查：把上拉电阻写错（4.7k 写成 4k）必须被抓到
    bad_fact = good.replace(wire_spec.pullup_text(), "4kΩ~10kΩ")
    if not check_required_facts(bad_fact):
        problems.append("自测失败：把上拉电阻写错（4.7kΩ → 4k）却仍然通过 —— 事实检查形同虚设")
    if check_required_facts(good):
        problems.append("自测失败：正确文本被误报缺少关键事实（假红）")

    # ② 路径检查：引用一个不存在的脚本必须被抓到
    bad_path = "跑 `basic/tools/根本没有这个文件.py` 即可"
    if not check_referenced_paths(bad_path, HW_DIR):
        problems.append("自测失败：引用了不存在的脚本却没被抓到")
    real_path = "跑 `basic/tools/selfcheck.py` 即可"
    if check_referenced_paths(real_path, HW_DIR):
        problems.append("自测失败：真实存在的脚本路径被误报为不存在")

    # ③ 事实源本身的一致性检查也要能被触发：把"建议周期 < 最小间隔"注入进去
    saved = wire_spec.RECOMMENDED_INTERVAL_S
    try:
        wire_spec.RECOMMENDED_INTERVAL_S = wire_spec.MIN_INTERVAL_S - 1.0
        if not wire_spec.self_check():
            problems.append("自测失败：把建议采样周期设成小于硬件最小间隔，事实自查却通过了")
    finally:
        wire_spec.RECOMMENDED_INTERVAL_S = saved
    return problems


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="基础版接线文档生成 / 校验")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--generate", action="store_true", help="重新生成 basic/hardware/ 下的文档")
    group.add_argument("--check", action="store_true", help="校验磁盘文档 == 生成结果（CI 用）")
    group.add_argument("--print", dest="print_one", metavar="文件名",
                       help="把某份文档打到标准输出（不写盘，便于 diff/预览）")
    args = parser.parse_args(argv)

    # 先自查"代码里的接线事实"（不读文档），它是最根本的一层
    facts = wire_spec.self_check()
    if args.print_one:
        builder = DOCUMENTS.get(args.print_one)
        if builder is None:
            print(safe_text(f"❌ 没有这份文档：{args.print_one}（可选：{'、'.join(DOCUMENTS)}）"))
            return 2
        print(builder())
        return 0

    print("=" * 74)
    print(f"基础版接线文档 · {'生成' if args.generate else '校验'}")
    print("=" * 74)
    print(f"事实来源：{wire_spec.summary()}")
    if facts:
        print("接线事实自查：")
        for problem in facts:
            print(f"  {problem}")
        return 1
    print(safe_text("接线事实自查：✅ 通过（引脚 / 电源 / 地 / 上拉 / 间隔 全部自洽）"))

    if args.generate:
        written = generate_all()
        print(f"已生成 {len(written)} 份文档：")
        for path in written:
            print(f"  - {path.relative_to(BASIC_DIR.parent)}")
        # 生成完立刻**自查一遍**：证明刚写出去的东西确实能通过检查（而不是"写完就算完"）
        problems = check_all()
        if problems:
            print(safe_text(f"⚠️ 生成后自查发现 {len(problems)} 项问题："))
            for problem in problems:
                print(f"  - {problem}")
            return 1
        print(safe_text("生成后自查：✅ 文档与代码一致，关键事实齐全"))
        return 0

    # 校验前先跑**注入自测**：证明"检查器真的能抓到问题"，否则后面那句"通过"毫无意义
    guard_problems = self_test()
    if guard_problems:
        print(safe_text("❌ 检查器自测失败（守卫可能已失效，先修它）："))
        for problem in guard_problems:
            print(f"  - {problem}")
        return 1
    print(safe_text("检查器注入自测：✅ 能抓到注入的错误（上拉写错 / 引用不存在的文件 / 周期小于硬件下限）"))

    problems = check_all()
    if problems:
        print(safe_text(f"❌ 校验未通过（{len(problems)} 项）："))
        for problem in problems:
            print(f"  - {problem}")
        print("\n修复：先改 `basic/wire_spec.py` / `basic/pins.py` 等来源，再跑")
        print("      python3 basic/tools/wire_docs.py --generate")
        return 1
    print(safe_text(f"✅ 校验通过：{len(DOCUMENTS)} 份文档与代码逐字符一致，关键事实齐全，引用的脚本都存在"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
