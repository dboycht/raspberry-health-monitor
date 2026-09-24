#!/usr/bin/env python3
"""基础版自带的引脚映射表（树莓派 40-pin）。

为什么基础版也要有这张表
------------------------
因为"报错信息里的物理脚号"是**接线时唯一看得懂的东西**。曾经有驱动用
``pin + 1`` 去猜物理脚号，把 GPIO27 报成"物理脚 28"（28 脚其实是 HAT ID_SC），
同学照着插线就插错了 —— **BCM 编号与物理脚号不是偏移关系**。
所以基础版的规矩和主项目一致：**只查表，不写偏移公式**。

:data:`BCM_TO_PHYSICAL` 与主项目 ``rpi/health_monitor/hal/pins.py`` 完全一致
（同一块板子的同一张表，两处都是树莓派 40-pin 标准布局）。
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple

#: BCM 编号 → 物理脚号（树莓派 40-pin 标准布局）
BCM_TO_PHYSICAL: Dict[int, int] = {
    2: 3, 3: 5, 4: 7, 17: 11, 27: 13, 22: 15, 10: 19, 9: 21, 11: 23, 8: 24,
    25: 22, 24: 18, 23: 16, 18: 12, 15: 10, 14: 8, 7: 26, 5: 29, 6: 31,
    12: 32, 13: 33, 19: 35, 16: 36, 26: 37, 20: 38, 21: 40,
}

#: 几个常用脚的说明（只挑基础版会用到的）
BCM_ROLE: Dict[int, str] = {
    4: "常用作 1-Wire / DHT 数据脚（DHT11 默认就接这里）",
    17: "通用 GPIO，常用作数字输入",
    27: "通用 GPIO，常用作数字输入",
    2: "I2C1 SDA（固定功能）",
    3: "I2C1 SCL（固定功能）",
}


def bcm_to_physical(bcm: int) -> Optional[int]:
    """BCM 编号 → 物理脚号；不是树莓派引脚则返回 ``None``。

    判据::

        bcm_to_physical(4) == 7     # GPIO4 在物理脚 7
        bcm_to_physical(27) == 13   # 不是 28（28 是 HAT ID_SC）
    """
    return BCM_TO_PHYSICAL.get(int(bcm))


def physical_to_bcm(physical: int) -> Optional[int]:
    """物理脚号 → BCM 编号（反查）。"""
    for bcm, phys in BCM_TO_PHYSICAL.items():
        if phys == int(physical):
            return bcm
    return None


def describe_pin(bcm: int) -> str:
    """给人看的引脚描述，例如 ``GPIO4（物理脚 7）``。"""
    phys = bcm_to_physical(bcm)
    if phys is None:
        return f"GPIO{bcm}（⚠️ 不是树莓派 40-pin 上的合法 BCM 引脚，请核对接线）"
    role = BCM_ROLE.get(int(bcm), "")
    return f"GPIO{bcm}（物理脚 {phys}{('，' + role) if role else ''}）"


def find_conflicts(claims: Iterable[Tuple[str, int]]) -> List[str]:
    """检查"多个器件抢同一个 GPIO"（基础版只有一个传感器，留作拓展时用）。

    Args:
        claims: ``[(器件名, BCM 编号), ...]``。

    Returns:
        冲突描述列表（空列表 = 无冲突）。
    """
    used: Dict[int, List[str]] = {}
    for name, pin in claims:
        used.setdefault(int(pin), []).append(str(name))
    return [
        f"GPIO{pin} 被多个器件同时占用：{'、'.join(names)}（同一引脚只能有一个主人）"
        for pin, names in sorted(used.items())
        if len(names) > 1
    ]


def full_table() -> List[Tuple[int, int]]:
    """``[(物理脚号, BCM 编号), ...]``，按物理脚号排序（文档/自检用）。"""
    return sorted(((phys, bcm) for bcm, phys in BCM_TO_PHYSICAL.items()))


__all__ = [
    "BCM_TO_PHYSICAL",
    "BCM_ROLE",
    "bcm_to_physical",
    "physical_to_bcm",
    "describe_pin",
    "find_conflicts",
    "full_table",
]
