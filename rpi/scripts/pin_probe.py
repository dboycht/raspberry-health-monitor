#!/usr/bin/env python3
"""引脚脉冲/读取助手：用来**用万用表验证接线**（无需把树莓派从盒子里掏出来猜）。

用途
----
"线到底通不通"用眼睛看杜邦线插没插紧很难判断，但用万用表一量就清楚。
本脚本让某个物理脚按固定频率翻转（例如 1Hz），你在**器件那一端**量它的电平：
  · 跟着一起高低跳变 → 这根线是通的 ✅
  · 一直是 0V 或 3.3V 不动 → 线没通/插错脚 ❌

用法（在树莓派上）::

    # 让物理脚 24（MCP3002 的 CS）每秒翻转一次，持续 60 秒
    python3 scripts/pin_probe.py --pulse 24 --seconds 60

    # 让物理脚 7（DHT11 的 DATA）翻转
    python3 scripts/pin_probe.py --pulse 7 --seconds 30

    # 只"读"某个脚（看外部器件把它拉高还是拉低）
    python3 scripts/pin_probe.py --read 7 --seconds 10

    # 一次验证整组接线（按顺序逐个翻转，脚本会提示该量哪个脚）
    python3 scripts/pin_probe.py --wiring

⚠️ 本脚本只操作**它自己声明的那些 BCM 引脚**，不碰 I2C/SPI 上的其它器件。
   脉冲模式会把该脚临时设为**输出**，所以**不要**在 DHT11/传感器正在工作时跑它。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

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

RPI_DIR = Path(__file__).resolve().parents[1]
if str(RPI_DIR) not in sys.path:
    sys.path.insert(0, str(RPI_DIR))

from health_monitor.hal.pins import bcm_to_physical, describe_pin  # noqa: E402
from health_monitor.hal.pins import BCM_TO_PHYSICAL  # noqa: E402

#: 物理脚号 → BCM（本项目用到的关键脚，用于 --wiring 演示）
KEY_PHYSICAL = (7, 11, 12, 13, 15, 16, 18, 19, 21, 22, 23, 24, 26)


def physical_to_bcm(physical: int) -> int:
    for bcm, phys in BCM_TO_PHYSICAL.items():
        if phys == physical:
            return bcm
    raise SystemExit(f"物理脚 {physical} 不是 GPIO（可能是电源/地脚）")


def pulse(bcm: int, seconds: float, period: float = 1.0) -> None:
    import lgpio

    h = lgpio.gpiochip_open(0)
    physical = bcm_to_physical(bcm)
    print(f"  正在让 GPIO{bcm}（物理脚 {physical}）以 {1/period:.1f}Hz 翻转 {seconds:g} 秒…")
    print("  用万用表（直流电压档）红表笔量器件的对应脚、黑表笔接 GND：")
    safe_print("    · 电压在 0V ↔ 3.3V 之间跳变 → 这根线通了 ✅")
    safe_print("    · 一直不动 → 线没通/插错脚 ❌")
    try:
        lgpio.gpio_claim_output(h, bcm, 0)
        deadline = time.monotonic() + seconds
        level = 0
        while time.monotonic() < deadline:
            level ^= 1
            lgpio.gpio_write(h, bcm, level)
            print(f"    当前电平：{'3.3V' if level else '0V  '}", end="\r", flush=True)
            time.sleep(period / 2)
        print("\n  脉冲结束，引脚已释放（回到输入状态）")
    finally:
        try:
            lgpio.gpio_free(h, bcm)
        except Exception:  # noqa: BLE001
            pass
        lgpio.gpiochip_close(h)


def read_level(bcm: int, seconds: float) -> None:
    import lgpio

    h = lgpio.gpiochip_open(0)
    physical = bcm_to_physical(bcm)
    print(f"  读取 GPIO{bcm}（物理脚 {physical}）{seconds:g} 秒（每 0.2 秒报一次）：")
    try:
        lgpio.gpio_claim_input(h, bcm, lgpio.SET_PULL_UP)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            level = lgpio.gpio_read(h, bcm)
            print(f"    {'3.3V (高)' if level else '0V (低)  '}", end="\r", flush=True)
            time.sleep(0.2)
        print()
    finally:
        try:
            lgpio.gpio_free(h, bcm)
        except Exception:  # noqa: BLE001
            pass
        lgpio.gpiochip_close(h)


def wiring_walkthrough(seconds_each: float) -> None:
    """逐个翻转"本项目用到的关键引脚"，并在每一步告诉你该量哪个器件的哪个脚。"""
    hints = {
        7: "DHT11 的 DATA 脚",
        11: "HC-SR501 的 OUT 脚",
        12: "蜂鸣器的 + 脚",
        13: "按键的一端",
        15: "LED 绿 的阳极（经电阻）",
        16: "LED 黄 的阳极（经电阻）",
        18: "TFT 的 A0/DC 脚（红灯没接的话）",
        19: "MCP3002 的 DIN ＋ TFT 的 SDA",
        21: "MCP3002 的 DOUT",
        22: "TFT 的 RESET 脚",
        23: "MCP3002 的 CLK ＋ TFT 的 SCK",
        24: "MCP3002 的 CS 脚 ← 你今天要查的就是它",
        26: "TFT 的 CS 脚",
    }
    print("=" * 74)
    print("接线逐脚验证（每步：脚本翻转 → 你用万用表量 → 回车继续）")
    print("=" * 74)
    for physical in KEY_PHYSICAL:
        bcm = physical_to_bcm(physical)
        hint = hints.get(physical, "")
        safe_print(f"\n▶ 现在翻转 物理脚 {physical}（GPIO{bcm}）：应量 **{hint}**")
        try:
            pulse(bcm, seconds_each, period=1.0)
        except Exception as exc:  # noqa: BLE001
            safe_print(f"  ❌ 失败：{type(exc).__name__}: {exc}")
        try:
            answer = input("  这一根通吗？[回车=通 / n=不通 / q=退出] ").strip().lower()
        except EOFError:
            break
        if answer == "q":
            break
        if answer == "n":
            safe_print(f"  ⚠️ 记录：物理脚 {physical} 未通 → 检查杜邦线、面包板同一列、器件端插到底")


def main() -> int:
    parser = argparse.ArgumentParser(description="引脚脉冲/读取（配合万用表验证接线）")
    parser.add_argument("--pulse", type=int, default=0, help="要翻转的**物理脚**号（如 24）")
    parser.add_argument("--read", type=int, default=0, help="要读取的**物理脚**号（如 7）")
    parser.add_argument("--seconds", type=float, default=30.0, help="持续秒数（默认 30）")
    parser.add_argument("--period", type=float, default=1.0, help="翻转周期秒（默认 1）")
    parser.add_argument("--wiring", action="store_true", help="逐脚走一遍关键接线")
    args = parser.parse_args()

    if args.wiring:
        wiring_walkthrough(args.seconds if args.seconds != 30.0 else 6.0)
        return 0
    if args.pulse:
        pulse(physical_to_bcm(args.pulse), args.seconds, args.period)
        return 0
    if args.read:
        read_level(physical_to_bcm(args.read), args.seconds)
        return 0

    print("用法示例：")
    print("  python3 scripts/pin_probe.py --pulse 24 --seconds 60     # 验证 MCP3002 的 CS")
    print("  python3 scripts/pin_probe.py --pulse 7 --seconds 30      # 验证 DHT11 的 DATA")
    print("  python3 scripts/pin_probe.py --wiring                    # 逐脚走一遍")
    safe_print("\n物理脚 ↔ BCM 对照（本项目用到的）：")
    for physical in KEY_PHYSICAL:
        bcm = physical_to_bcm(physical)
        # describe_pin() 自己会写 `GPIO{n}（…）`，这里只需要括号里的说明（E35）
        role = describe_pin(bcm).split("（", 1)[-1].rstrip("）")
        print(f"  物理脚 {physical:>2}  BCM{bcm:<3} {role}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
