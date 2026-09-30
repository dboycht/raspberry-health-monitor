#!/usr/bin/env python3
"""针对性诊断：DHT11 的"数据线电平"。

具体疑问（2026-09-24 真机）
-------------------------------
DHT11 完全没有边沿：到底是"器件没工作"还是"数据线被拉死"？
→ 测 **空闲电平**：接上内部上拉后，数据线应当是高（约 3.3V）。
  若一直是低，说明线被器件拉死/接错/短路；
  若悬空乱跳，说明 DATA 没接到 GPIO4（或线断了）。

用法（在树莓派上）::

    python3 scripts/diag_pin_levels.py                # 测 DHT11 数据线电平
    python3 scripts/diag_pin_levels.py --pin 4        # 指定 BCM 引脚
"""

from __future__ import annotations

import argparse
import statistics
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


def section(t: str) -> None:
    print("\n" + "=" * 74)
    print(t)
    print("=" * 74)


def diag_dht_line(pin: int = 4) -> None:
    """测 DHT11 数据线的空闲电平（判断"器件有没有把线拉死"）。"""
    section(f"DHT11 数据线电平诊断（GPIO{pin} = 物理脚 7）")
    try:
        import lgpio
    except ImportError as exc:
        safe_print(f"  ❌ 未安装 lgpio：{exc}")
        return

    h = lgpio.gpiochip_open(0)
    try:
        for label, flags in (("内部上拉", lgpio.SET_PULL_UP),
                             ("内部下拉", lgpio.SET_PULL_DOWN),
                             ("浮空", 0)):
            try:
                lgpio.gpio_free(h, pin)
            except Exception:  # noqa: BLE001
                pass
            lgpio.gpio_claim_input(h, pin, flags)
            time.sleep(0.05)
            samples = [lgpio.gpio_read(h, pin) for _ in range(50)]
            high = sum(samples)
            avg = statistics.mean(samples)
            print(f"  {label:<8}：高电平 {high}/50 次　平均 {avg:.2f}")
        print("\n  怎么读这三行（关键）：")
        print("    · 上拉=1、下拉=0 → 线上**没有**器件在驱动（DHT11 没供电/没接上）")
        print("    · 上拉=0、下拉=0 → 线被**拉死到地**（DATA 接到了 GND / 短路）")
        print("    · 上拉=1、下拉=1 → 线被**拉死到 3.3V**（DATA 接到了 VCC/3.3V 那一列，")
        print("                       或与物理脚 1/17 短路）← 2026-09-24 真机遇到的就是这种")
        print("    · 三组读数都乱跳 → 线太长为天线效应，或面包板接触不良")
        print("\n  提示：面包板**每一列纵向是导通的**，把 DATA 插到 VCC 同一列就会变成这种症状。")
    finally:
        try:
            lgpio.gpio_free(h, pin)
        except Exception:  # noqa: BLE001
            pass
        lgpio.gpiochip_close(h)


def main() -> int:
    parser = argparse.ArgumentParser(description="DHT11 数据线电平 针对性诊断")
    parser.add_argument("--pin", type=int, default=4, help="DHT11 的 BCM 引脚（默认 4）")
    args = parser.parse_args()

    diag_dht_line(args.pin)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
