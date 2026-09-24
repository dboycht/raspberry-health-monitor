#!/usr/bin/env python3
"""基础版的**接线事实来源**（Single Source of Truth）：所有接线文档都由这个模块生成。

为什么要有这个模块
------------------
接线文档最容易出的两类错，都不是"写得不好"，而是**抄错数字**：
1. **物理脚号**：BCM 与物理脚号不是偏移关系（GPIO4 = 物理脚 7，但 GPIO27 = 物理脚 13）。
   手抄一次就可能把 7 写成 4 —— 同学照着插线，然后花一晚上排一个"根本不存在的 bug"；
2. **器件要求**：供电到底是 3.3V 还是 5V、上拉电阻要多大、两次读取最短间隔多少 ——
   这些数字散落在驱动代码里，文档手写一定会漂。

所以规矩是：**文档里的每个数字都从代码取**。
- 引脚映射 → :mod:`basic.pins`（`BCM_TO_PHYSICAL`）
- 数据脚默认值 → :mod:`basic.dht11read`（`Dht11Reader` 的 `pin` 默认参数）
- 量程/间隔/电阻要求 → 本模块（与驱动同源，改一处即可）
- 文档正文 → :mod:`basic.tools.wire_docs` 生成；`--check` 会验证"磁盘上的文档 == 生成的文档"

⚠️ 本模块**不 import matplotlib**（基础版只有画图那一步需要它），
所以文档生成/检查在纯文本环境（CI、树莓派最小系统）也能跑。
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from . import dht11read, pins
from .model import CSV_HEADER
from .store import default_csv_path

# ==========================================================================
# 一、从代码里读出来的事实（**不要手写数字**）
# ==========================================================================


def _default_pin_from_driver() -> int:
    """数据脚默认值：直接从 :class:`basic.dht11read.Dht11Reader` 的签名里读。

    为什么用签名而不是抄一个 4：改了驱动的默认引脚却忘了改文档，
    是最容易发生、后果最严重（插错脚）的一种漂移。签名是唯一事实。
    """
    parameter = inspect.signature(dht11read.Dht11Reader.__init__).parameters["pin"]
    return int(parameter.default)


#: DHT11 数据脚（BCM 编号）—— **从驱动签名读取**
DATA_BCM: int = _default_pin_from_driver()

#: DHT11 数据脚（40-pin 物理脚号）—— **从引脚映射表查得**
DATA_PHYSICAL: Optional[int] = pins.bcm_to_physical(DATA_BCM)

#: 供电脚（物理脚号）：3.3V。DHT11 模块**不要接 5V**（数据电平会被拉到 5V，伤 GPIO）
VCC_PHYSICAL: Tuple[int, ...] = (1, 17)

#: GND 脚（物理脚号）：树莓派 40-pin 上全部 8 个地脚
GND_PHYSICAL: Tuple[int, ...] = (6, 9, 14, 20, 25, 30, 34, 39)

#: 本基础版**推荐**的 GND 脚（离数据脚 7 最近，接线最短、最不容易插错）
GND_RECOMMENDED: int = 6

#: 3.3V 与 5V 的物理脚（写进文档供对照；避免有人把 5V 当成"通用的正极"）
V33_PHYSICAL: Tuple[int, ...] = (1, 17)
V5_PHYSICAL: Tuple[int, ...] = (2, 4)

# --------------------------------------------------------------------------
# 器件要求（与驱动同源；改驱动就要改这里，检查脚本会核对间隔与量程）
# --------------------------------------------------------------------------

#: 两次读取的最小间隔（秒）—— 与驱动常量同源
MIN_INTERVAL_S: float = dht11read.MIN_INTERVAL_S

#: 建议的采样周期（秒）：留出余量，避免"刚好 2.0 秒"被判成太早
RECOMMENDED_INTERVAL_S: float = 3.0

#: 温度 / 湿度量程（与驱动同源，用于验收判据）
TEMP_RANGE_C: Tuple[float, float] = (dht11read.TEMP_MIN_C, dht11read.TEMP_MAX_C)
HUMI_RANGE_PCT: Tuple[float, float] = (dht11read.HUMI_MIN_PCT, dht11read.HUMI_MAX_PCT)

#: 裸四针传感器需要的上拉电阻（欧姆）
PULLUP_OHM_RANGE: Tuple[int, int] = (4700, 10000)


def ohm_text(ohm: int) -> str:
    """欧姆 → 人话（``4700`` → ``4.7kΩ``，``10000`` → ``10kΩ``）。

    ⚠️ 别用 `// 1000`：那会把 4700 写成 "4kΩ"（**文档写错的经典方式**：
    数字看起来对，实际把数据手册里的 4.7k 说成了 4k）。
    """
    if ohm % 1000 == 0:
        return f"{ohm // 1000}kΩ"
    return f"{ohm / 1000:g}kΩ"


def pullup_text() -> str:
    """上拉电阻范围的文字形式（文档统一用它，避免各处自己写格式化）。"""
    low, high = PULLUP_OHM_RANGE
    return f"{ohm_text(low)}~{ohm_text(high)}"

#: 模块工作电压（3.3V 供电；模块板上写 5V 也按 3.3V 接，理由见文档）
MODULE_VOLTAGE: str = "3.3V"

#: 模块工作电流（毫安，典型值）—— 数据手册口径
MODULE_CURRENT_MA: Tuple[float, float] = (0.5, 2.5)

#: 单总线空闲电平：上拉后应为高（约 3.3V）
IDLE_LEVEL: str = "高（约 3.3V）"

#: 树莓派 5 的 3.3V 轨电流上限（安培，官方规格；用于供电预算）
PI_3V3_RAIL_A: float = 0.5

#: 树莓派 5 官方电源要求（用于"供电不足会怎样"一节）
PI_PSU: str = "5V/5A USB-C（官方 27W）"

#: 40-pin 上的保留脚（HAT ID EEPROM，绝对不要接）
RESERVED_PHYSICAL: Dict[int, str] = {
    27: "HAT ID_SD（EEPROM 数据）",
    28: "HAT ID_SC（EEPROM 时钟）",
}

#: 不建议新手占用的固定功能脚（用了会丢串口/别的总线）
FIXED_FUNCTION_PHYSICAL: Dict[int, str] = {
    3: "I2C1 SDA（GPIO2）",
    5: "I2C1 SCL（GPIO3）",
    8: "UART TXD（GPIO14）",
    10: "UART RXD（GPIO15）",
    19: "SPI0 MOSI（GPIO10）",
    21: "SPI0 MISO（GPIO9）",
    23: "SPI0 SCLK（GPIO11）",
    24: "SPI0 CE0（GPIO8）",
    26: "SPI0 CE1（GPIO7）",
}

#: 40-pin 上每个物理脚的功能名（用于画接线总图；电源脚无 BCM）
PHYSICAL_FUNCTION: Dict[int, str] = {
    1: "3.3V", 2: "5V", 3: "GPIO2 / SDA1", 4: "5V", 5: "GPIO3 / SCL1", 6: "GND",
    7: "GPIO4", 8: "GPIO14 / TXD", 9: "GND", 10: "GPIO15 / RXD", 11: "GPIO17",
    12: "GPIO18", 13: "GPIO27", 14: "GND", 15: "GPIO22", 16: "GPIO23", 17: "3.3V",
    18: "GPIO24", 19: "GPIO10 / MOSI", 20: "GND", 21: "GPIO9 / MISO", 22: "GPIO25",
    23: "GPIO11 / SCLK", 24: "GPIO8 / CE0", 25: "GND", 26: "GPIO7 / CE1",
    27: "GPIO0 / ID_SD", 28: "GPIO1 / ID_SC", 29: "GPIO5", 30: "GND", 31: "GPIO6",
    32: "GPIO12", 33: "GPIO13", 34: "GND", 35: "GPIO19", 36: "GPIO16", 37: "GPIO26",
    38: "GPIO20", 39: "GND", 40: "GPIO21",
}

#: 供电方式（本项目默认"树莓派 3.3V 脚直接给模块供电"）
POWER_MODE: str = "树莓派 3.3V（物理脚 1 或 17）"


# ==========================================================================
# 二、逐线接续表（DHT11 → 树莓派）
# ==========================================================================


@dataclass(frozen=True)
class Wire:
    """一根线：``DHT11 侧 → 树莓派侧``。"""

    signal: str          # 信号名（中文）
    sensor_pin: str      # 模块上的丝印 / 脚名
    physical: int        # 树莓派物理脚号
    bcm: Optional[int]   # BCM 编号（电源/地为 None）
    color: str           # 建议线色（便于排错时一眼认出）
    note: str            # 关键提醒

    @property
    def bcm_text(self) -> str:
        return f"GPIO{self.bcm}" if self.bcm is not None else "—"

    @property
    def physical_text(self) -> str:
        return f"**{self.physical}**"


def wires() -> List[Wire]:
    """按"接线顺序"返回逐线表（先电源、再地、最后信号）。"""
    return [
        Wire(
            signal="供电",
            sensor_pin="VCC / + / VDD",
            physical=VCC_PHYSICAL[0],
            bcm=None,
            color="红",
            note="**必须 3.3V**；不要接 5V（物理脚 2/4）——数据电平会被拉到 5V，伤 GPIO",
        ),
        Wire(
            signal="地",
            sensor_pin="GND / −",
            physical=GND_RECOMMENDED,
            bcm=None,
            color="黑",
            note="任意一个 GND 脚都行（脚 6/9/14/20/25/30/34/39）；**必须共地**",
        ),
        Wire(
            signal="数据",
            sensor_pin="DATA / OUT / S",
            physical=int(DATA_PHYSICAL or 0),
            bcm=DATA_BCM,
            color="黄",
            note="单总线数据线（双向）；裸四针传感器必须外接 4.7k~10kΩ 上拉到 3.3V",
        ),
    ]


def pin_usage() -> Dict[int, str]:
    """``物理脚号 → 本基础版用它做什么``（用于 40-pin 总表）。"""
    usage: Dict[int, str] = {}
    for wire in wires():
        usage[wire.physical] = f"DHT11 {wire.sensor_pin.split(' / ')[0]}"
    return usage


# ==========================================================================
# 三、各级校验（供文档生成与检查共用）
# ==========================================================================


@dataclass(frozen=True)
class Problem:
    """一条不一致/不安全的发现。"""

    where: str
    detail: str
    severity: str = "error"     # error | warn

    def __str__(self) -> str:
        mark = "❌" if self.severity == "error" else "⚠️"
        return f"{mark} [{self.where}] {self.detail}"


def self_check() -> List[Problem]:
    """对**代码里的接线事实本身**做一致性检查（不读文档）。

    这些判据都是"能一句话说清、且失败一定是真问题"的：

    1. 数据脚必须能在 40-pin 映射表里查到（否则驱动默认值写错了）；
    2. 数据脚**不能**落在电源脚、GND 脚、HAT 保留脚或固定功能脚上
       （历史上真发生过"PIR 与按钮抢同一个 GPIO"的高危撞脚）；
    3. 3.3V / 5V / GND 名单必须与引脚映射表吻合（不许出现"不存在的物理脚"）；
    4. 上拉电阻范围必须落在合理工程区间（1k~100k），否则多半是笔误；
    5. 采样周期必须大于等于硬件最小间隔（否则会读到陈旧数据）。
    """
    problems: List[Problem] = []
    # 合法的 40-pin 物理脚 = 引脚映射表里的脚 ∪ 电源/地脚 ∪ HAT 保留脚（27/28）
    valid_physical = (
        set(pins.BCM_TO_PHYSICAL.values())
        | set(V33_PHYSICAL)
        | set(V5_PHYSICAL)
        | set(GND_PHYSICAL)
        | set(RESERVED_PHYSICAL)
    )
    if len(valid_physical) != 40:
        problems.append(
            Problem(
                "wire_spec 事实表",
                f"把功能表拼起来只覆盖了 {len(valid_physical)} 个脚（应为 40），"
                "说明引脚映射/电源地名单里有遗漏或重复",
            )
        )

    # ① 数据脚必须存在
    if DATA_PHYSICAL is None:
        problems.append(Problem("wire_spec.DATA_BCM", f"GPIO{DATA_BCM} 不在 40-pin 映射表里（驱动默认引脚写错了？）"))
    else:
        if DATA_PHYSICAL not in valid_physical:
            problems.append(Problem("wire_spec.DATA_PHYSICAL", f"物理脚 {DATA_PHYSICAL} 不是合法的 40-pin 脚"))
        # ② 不许撞电源/地/保留脚/固定功能脚
        if DATA_PHYSICAL in V33_PHYSICAL or DATA_PHYSICAL in V5_PHYSICAL:
            problems.append(Problem("数据脚", f"物理脚 {DATA_PHYSICAL} 是电源脚，不能当数据线"))
        if DATA_PHYSICAL in GND_PHYSICAL:
            problems.append(Problem("数据脚", f"物理脚 {DATA_PHYSICAL} 是 GND，不能当数据线"))
        if DATA_PHYSICAL in RESERVED_PHYSICAL:
            problems.append(Problem("数据脚", f"物理脚 {DATA_PHYSICAL} 是 {RESERVED_PHYSICAL[DATA_PHYSICAL]}，绝对不要占用"))
        if DATA_PHYSICAL in FIXED_FUNCTION_PHYSICAL:
            problems.append(
                Problem("数据脚", f"物理脚 {DATA_PHYSICAL} 是固定功能脚（{FIXED_FUNCTION_PHYSICAL[DATA_PHYSICAL]}），新手不建议占用", "warn")
            )

    # ③ 电源/地名单必须与映射表吻合
    for label, group in (("3.3V", V33_PHYSICAL), ("5V", V5_PHYSICAL), ("GND", GND_PHYSICAL)):
        for physical in group:
            if PHYSICAL_FUNCTION.get(physical) != label:
                problems.append(
                    Problem(
                        f"wire_spec.{label} 名单",
                        f"物理脚 {physical} 在功能表里是 {PHYSICAL_FUNCTION.get(physical)!r}，与声明的 {label} 不符",
                    )
                )
    # 反向：功能表里的电源/地都要被声明到
    for physical, function in sorted(PHYSICAL_FUNCTION.items()):
        if function in ("3.3V", "5V", "GND"):
            in_group = physical in V33_PHYSICAL or physical in V5_PHYSICAL or physical in GND_PHYSICAL
            if not in_group:
                problems.append(Problem("wire_spec 名单", f"物理脚 {physical}（{function}）没有被列进任何电源/地名单"))

    # ④ 上拉电阻范围
    low, high = PULLUP_OHM_RANGE
    if not (1000 <= low < high <= 100_000):
        problems.append(Problem("wire_spec.PULLUP_OHM_RANGE", f"上拉范围 {low}~{high}Ω 不像合理的工程取值（1k~100k）"))

    # ⑤ 采样周期 vs 硬件最小间隔
    if RECOMMENDED_INTERVAL_S < MIN_INTERVAL_S:
        problems.append(
            Problem(
                "wire_spec.RECOMMENDED_INTERVAL_S",
                f"建议采样周期 {RECOMMENDED_INTERVAL_S}s 小于硬件最小间隔 {MIN_INTERVAL_S}s，会读到陈旧数据",
            )
        )

    # ⑥ 供电电压声明
    if MODULE_VOLTAGE != "3.3V":
        problems.append(Problem("wire_spec.MODULE_VOLTAGE", f"DHT11 应声明为 3.3V 供电，当前是 {MODULE_VOLTAGE!r}", "warn"))

    return problems


#: 生成/检查时要保证"文档里必须出现这些数字"（缺一个就说明文档漏了关键信息）
REQUIRED_FACTS: Dict[str, str] = {
    "数据脚物理脚号": str(DATA_PHYSICAL),
    "数据脚 BCM": f"GPIO{DATA_BCM}",
    "供电电压": MODULE_VOLTAGE,
    "最小读取间隔": f"{MIN_INTERVAL_S:g} 秒",
    "建议采样周期": f"{RECOMMENDED_INTERVAL_S:g} 秒",
    "上拉电阻": f"{ohm_text(PULLUP_OHM_RANGE[0])}~{ohm_text(PULLUP_OHM_RANGE[1])}",
    "CSV 表头": ",".join(CSV_HEADER[:4]),
    "CSV 文件命名": "dht11_日期_时刻.csv",
}


def summary() -> str:
    """一行摘要（自检脚本打印用）。"""
    return (
        f"DHT11 数据脚 GPIO{DATA_BCM}（物理脚 {DATA_PHYSICAL}）、"
        f"供电 {MODULE_VOLTAGE}（脚 {'/'.join(map(str, VCC_PHYSICAL))}）、"
        f"地脚 {'/'.join(map(str, GND_PHYSICAL))}、"
        f"上拉 {pullup_text()}、"
        f"间隔 ≥{MIN_INTERVAL_S:g}s（默认 {RECOMMENDED_INTERVAL_S:g}s）"
    )


__all__ = [
    "DATA_BCM",
    "DATA_PHYSICAL",
    "VCC_PHYSICAL",
    "GND_PHYSICAL",
    "GND_RECOMMENDED",
    "V33_PHYSICAL",
    "V5_PHYSICAL",
    "MIN_INTERVAL_S",
    "RECOMMENDED_INTERVAL_S",
    "TEMP_RANGE_C",
    "HUMI_RANGE_PCT",
    "PULLUP_OHM_RANGE",
    "MODULE_VOLTAGE",
    "MODULE_CURRENT_MA",
    "IDLE_LEVEL",
    "PI_3V3_RAIL_A",
    "PI_PSU",
    "RESERVED_PHYSICAL",
    "FIXED_FUNCTION_PHYSICAL",
    "PHYSICAL_FUNCTION",
    "POWER_MODE",
    "REQUIRED_FACTS",
    "Wire",
    "Problem",
    "wires",
    "pin_usage",
    "ohm_text",
    "pullup_text",
    "self_check",
    "summary",
]
