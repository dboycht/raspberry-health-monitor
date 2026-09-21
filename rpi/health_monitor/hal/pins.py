"""树莓派 40-pin 引脚映射与冲突检测 —— **引脚信息的单一来源**。

为什么需要这个模块（真实踩坑）
------------------------------
某驱动曾用 ``self.pin + 1`` 猜"物理脚号"，结果把 GPIO27 报成"物理脚 28"
（28 脚其实是 HAT ID_SC）——**BCM 编号与物理脚号根本不是偏移关系**。
这类"文案错误"看起来只是注释问题，但同学就是照着 ``describe()`` 输出的
脚号去插线的，插错了要排查半天。

因此规矩是：
1. **不许自己写偏移公式**，一律用 :func:`bcm_to_physical` / :func:`describe_pin`；
2. 配置里的引脚要能被 :func:`find_conflicts` 检查**是否撞脚**
   （两个驱动抢同一个 GPIO 是真实发生过的高危问题：PIR 一动会顺带把按钮读成"按下"，
   凭空触发紧急求助）。
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple

#: BCM 编号 → 物理脚号（树莓派 40-pin 标准布局）
BCM_TO_PHYSICAL: Dict[int, int] = {
    2: 3, 3: 5, 4: 7, 17: 11, 27: 13, 22: 15, 10: 19, 9: 21, 11: 23, 8: 24,
    25: 22, 24: 18, 23: 16, 18: 12, 15: 10, 14: 8, 7: 26, 5: 29, 6: 31,
    12: 32, 13: 33, 19: 35, 16: 36, 26: 37, 20: 38, 21: 40,
}

#: BCM 编号 → 引脚默认用途说明（写进 describe() 方便同学核对）
BCM_ROLE: Dict[int, str] = {
    0: "ID_SD（HAT EEPROM 数据，勿占用）",
    1: "ID_SC（HAT EEPROM 时钟，勿占用）",
    2: "I2C1 SDA（固定功能，接传感器 SDA）",
    3: "I2C1 SCL（固定功能，接传感器 SCL）",
    4: "GPIO（常用作 1-Wire / DHT 数据脚）",
    5: "GPIO（也叫 GPCLK0）",
    6: "GPIO（也叫 GPCLK1）",
    7: "SPI0 CE1（片选 1）",
    8: "SPI0 CE0（片选 0，MCP3002 默认用它）",
    9: "SPI0 MISO（D_OUT）",
    10: "SPI0 MOSI（D_IN）",
    11: "SPI0 SCLK（时钟）",
    12: "GPIO / PWM0",
    13: "GPIO / PWM1",
    14: "UART TXD（默认串口控制台，谨慎占用）",
    15: "UART RXD（默认串口控制台，谨慎占用）",
    16: "GPIO（常见于 SPI1 CS）",
    17: "GPIO（常用作中断/数字输入）",
    18: "GPIO / PWM0（常用蜂鸣器、舵机）",
    19: "SPI0 MOSI 的备用定义（PCM FS）",
    20: "GPIO（PCM DIN）",
    21: "GPIO（PCM DOUT）",
    22: "GPIO（常用状态灯）",
    23: "GPIO（常用状态灯）",
    24: "GPIO（常用状态灯）",
    25: "GPIO（低电平有效，有内部上下拉）",
    26: "GPIO（SPI1 CE1 常见）",
    27: "GPIO（常用数字输入/输出）",
}


def bcm_to_physical(bcm: int) -> Optional[int]:
    """把 BCM 编号换成 40-pin 物理脚号；未知引脚返回 ``None``。

    判据示例::

        bcm_to_physical(27) == 13      # GPIO27 在物理脚 13
        bcm_to_physical(17) == 11      # GPIO17 在物理脚 11
        bcm_to_physical(99) is None    # 不是树莓派引脚
    """
    return BCM_TO_PHYSICAL.get(int(bcm))


def physical_to_bcm(physical: int) -> Optional[int]:
    """物理脚号 → BCM 编号（反向查表）。"""
    for bcm, phys in BCM_TO_PHYSICAL.items():
        if phys == int(physical):
            return bcm
    return None


def describe_pin(bcm: int) -> str:
    """生成给人看的引脚描述，例如 ``"GPIO27（物理脚 13，GPIO（常用数字输入/输出））"``。

    **驱动写 ``describe()`` 时必须用这个函数**，不要自己拼字符串。
    """
    phys = bcm_to_physical(bcm)
    role = BCM_ROLE.get(int(bcm), "")
    if phys is None:
        return f"GPIO{bcm}（⚠️ 不是树莓派 40-pin 上的合法 BCM 引脚，请核对接线）"
    return f"GPIO{bcm}（物理脚 {phys}{('，' + role) if role else ''}）"


def is_reserved(bcm: int) -> bool:
    """是否属于系统保留/固定用途（I2C、SPI、UART、HAT ID）。"""
    return int(bcm) in BCM_ROLE and any(
        marker in BCM_ROLE[int(bcm)]
        for marker in ("固定功能", "SPI0", "UART", "ID_SD", "ID_SC", "SPI1")
    )


def find_conflicts(pin_claims: Iterable[Tuple[str, int]]) -> List[str]:
    """检查多个设备是否抢同一个 GPIO。

    Args:
        pin_claims: ``[(设备名, BCM 编号), ...]``（I2C/SPI 共享总线**不算冲突**，
            所以调用方只应传入"独占型"引脚，如按钮、PIR、蜂鸣器、LED、DHT 数据脚）。

    Returns:
        人类可读的冲突说明列表（空列表 = 无冲突）。

    典型用法（``tests/`` 与 ``selfcheck`` 都该跑一遍）::

        find_conflicts([("motion", 17), ("sos_button", 27)])
        # -> []                     无冲突
        find_conflicts([("motion", 17), ("sos_button", 17)])
        # -> ["GPIO17（物理脚 11）被 2 个设备同时占用：motion, sos_button ..."]
    """
    grouped: Dict[int, List[str]] = {}
    for name, pin in pin_claims:
        grouped.setdefault(int(pin), []).append(name)

    conflicts: List[str] = []
    for pin, names in sorted(grouped.items()):
        if len(names) <= 1:
            continue
        conflicts.append(
            f"{describe_pin(pin)} 被 {len(names)} 个设备同时占用：{', '.join(names)}"
            " —— 独占型引脚不能共用，会导致互相当成对方的信号"
            "（例：PIR 检测到人时会把按钮读成'按下'，凭空触发求助）"
        )
    return conflicts


def reserved_pin_warnings(pin_claims: Iterable[Tuple[str, int]]) -> List[str]:
    """检查是否有设备占用了系统保留/固定用途引脚（警告，不一定是错误）。"""
    seen: set = set()
    warnings: List[str] = []
    for name, pin in pin_claims:
        if is_reserved(pin) and int(pin) not in seen:
            seen.add(int(pin))
            warnings.append(f"设备 {name} 使用了 {describe_pin(pin)}（系统保留/固定用途，请确认无冲突）")
    return warnings


def full_table() -> List[Tuple[int, int, str]]:
    """返回完整物理脚表 ``[(物理脚, BCM, 用途), ...]``（硬件文档生成脚本用）。"""
    return sorted(
        ((phys, bcm, BCM_ROLE.get(bcm, "")) for bcm, phys in BCM_TO_PHYSICAL.items()),
        key=lambda row: row[0],
    )


__all__ = [
    "BCM_TO_PHYSICAL",
    "BCM_ROLE",
    "bcm_to_physical",
    "physical_to_bcm",
    "describe_pin",
    "is_reserved",
    "find_conflicts",
    "reserved_pin_warnings",
    "full_table",
]
