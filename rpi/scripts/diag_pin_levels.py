#!/usr/bin/env python3
"""针对性诊断：DHT11 的"数据线电平" 与 MCP3002 的"通道是否真的独立"。

两个具体疑问（2026-09-24 真机）
-------------------------------
① DHT11 完全没有边沿：到底是"器件没工作"还是"数据线被拉死"？
   → 测 **空闲电平**：接上内部上拉后，数据线应当是高（约 3.3V）。
     若一直是低，说明线被器件拉死/接错/短路；
     若悬空乱跳，说明 DATA 没接到 GPIO4（或线断了）。

② MCP3002 的 CH0 与 CH1 读数几乎相同：
   → 用 **CH1 做对照实验**：
     · 把 CH1 用杜邦线接到 3.3V → 若 CH1 变 1023 而 CH0 不变，说明两通道独立、正常；
     · 把 CH1 接到 GND → 若 CH1 变 0，同样说明独立；
     · 若 CH1 怎么接都不变 → MCP3002 的通道选择没生效（或 CH1 根本没接出来）。
   本脚本会把每一步要做什么写清楚，你按提示改线即可。

用法（在树莓派上）::

    python3 scripts/diag_pin_levels.py --dht            # 只测 DHT11 数据线电平
    python3 scripts/diag_pin_levels.py --adc            # 只测 MCP3002（含对照步骤）
    python3 scripts/diag_pin_levels.py                  # 两个都测
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


def read_adc(spi, channel: int, times: int = 5) -> list[int]:
    """读 MCP3002 某通道（单端模式）若干次。"""
    cmd = [0x60 | (channel << 5), 0x00]
    out = []
    for _ in range(times):
        resp = spi.xfer2(cmd)
        out.append(((resp[0] & 0x03) << 8) | resp[1])
        time.sleep(0.02)
    return out


def diag_adc() -> None:
    """MCP3002 通道独立性对照实验。"""
    section("MCP3002 通道独立性对照（判断 CH0/CH1 是否真的独立）")
    try:
        import spidev
    except ImportError as exc:
        safe_print(f"  ❌ 未安装 spidev：{exc}")
        return

    spi = spidev.SpiDev()
    try:
        spi.open(0, 0)
        spi.max_speed_hz = 1_000_000
        spi.mode = 0
    except Exception as exc:  # noqa: BLE001
        safe_print(f"  ❌ 打开 SPI0.0 失败：{exc}")
        return

    try:
        print("  【第 1 步】先看当前状态（CH0 接 TMP36、CH1 悬空）：")
        ch0 = read_adc(spi, 0)
        ch1 = read_adc(spi, 1)
        print(f"    CH0 = {ch0}　平均 {statistics.mean(ch0):.1f}（{(statistics.mean(ch0)/1023*3.3):.3f} V）")
        print(f"    CH1 = {ch1}　平均 {statistics.mean(ch1):.1f}（{(statistics.mean(ch1)/1023*3.3):.3f} V）")

        print("\n  【第 2 步 · 需要你动手】把 **CH1 用杜邦线接到 3.3V（物理脚 1）**，")
        print("  然后按回车继续（这一步是判断'通道选择是否真的生效'）…")
        try:
            input()
        except EOFError:
            pass
        ch1_high = read_adc(spi, 1)
        ch0_after = read_adc(spi, 0)
        print(f"    CH1（接 3.3V 后）= {ch1_high}　平均 {statistics.mean(ch1_high):.1f}")
        print(f"    CH0（应不受影响）= {ch0_after}　平均 {statistics.mean(ch0_after):.1f}")
        if statistics.mean(ch1_high) > 1000:
            safe_print("    ✅ CH1 接 3.3V 后接近满量程 → **通道选择正常、CH1 也能读**")
        else:
            safe_print("    ⚠️ CH1 接 3.3V 后没变化 → MCP3002 的通道选择没生效（或 CH1 没接出来）")

        print("\n  【第 3 步 · 需要你动手】把 CH1 改接到 **GND（物理脚 6）**，按回车继续…")
        try:
            input()
        except EOFError:
            pass
        ch1_low = read_adc(spi, 1)
        print(f"    CH1（接 GND 后）= {ch1_low}　平均 {statistics.mean(ch1_low):.1f}")
        if statistics.mean(ch1_low) < 50:
            safe_print("    ✅ CH1 接 GND 后接近 0 → 通道工作正常，接回悬空即可")
        else:
            safe_print("    ⚠️ CH1 接 GND 后仍不为 0 → 接线或芯片有问题")

        print("\n  【第 4 步】诊断 CH0 上的 TMP36：")
        avg0 = statistics.mean(read_adc(spi, 0, 10))
        volts0 = avg0 / 1023 * 3.3
        print(f"    CH0 平均 raw={avg0:.1f} → {volts0:.3f} V")
        safe_print(f"    按 TMP36 标准公式：(V − 0.5) / 0.01 = {(volts0 - 0.5) / 0.01:.1f} ℃")
        safe_print("    正常室温应 20~30 ℃。对不上时按顺序查：")
        print("      ① TMP36 三脚方向（平面朝自己：左=+Vs, 中=VOUT, 右=GND）")
        print("      ② TMP36 的 VOUT 是否真的接到 MCP3002 的 **CH0**（不是 CH1）")
        print("      ③ TMP36 的 +Vs 是否 3.3V、GND 是否与 MCP3002 共地")
        print("      ④ 若电压 ≈1.95V 这种「不像室温」的值：可能是接线错到了分压点或悬空")
        print("      ⑤ 用 `pin_probe.py --pulse` 配万用表确认 CH0 那根线通不通")
    finally:
        spi.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="DHT11 电平 / MCP3002 通道 针对性诊断")
    parser.add_argument("--dht", action="store_true", help="只测 DHT11 数据线电平")
    parser.add_argument("--adc", action="store_true", help="只测 MCP3002（含需要你动手的对照步骤）")
    parser.add_argument("--pin", type=int, default=4, help="DHT11 的 BCM 引脚（默认 4）")
    args = parser.parse_args()

    run_dht = args.dht or not (args.dht or args.adc)
    run_adc = args.adc or not (args.dht or args.adc)
    if run_dht:
        diag_dht_line(args.pin)
    if run_adc:
        diag_adc()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
