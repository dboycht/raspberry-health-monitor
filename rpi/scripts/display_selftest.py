#!/usr/bin/env python3
"""显示器件实测：把内容写到**已连接**的显示器件上，验证"真的显示出来了"。

用法（在树莓派上）::

    cd ~/raspberry-health-monitor/rpi
    python3 scripts/display_selftest.py                     # 自动测所有已启用的显示器件
    python3 scripts/display_selftest.py --device lcd1602     # 只测 LCD
    python3 scripts/display_selftest.py --device tft --controller st7735_128x160
    python3 scripts/display_selftest.py --text "HR 72" --text "SpO2 98"

判据（脚本逐条打印，最后一条只能靠人眼）：
  ① 器件能 open（I2C/SPI 通信真的成功）；
  ② 写入不报错（驱动记录了写入次数 / 回读的内容）；
  ③ **人工确认**：屏上出现的文字与预期一致 —— 这一步脚本替不了你。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import List, Optional

RPI_DIR = Path(__file__).resolve().parents[1]
if str(RPI_DIR) not in sys.path:
    sys.path.insert(0, str(RPI_DIR))

from health_monitor.hal import DisplayCommand, create_device  # noqa: E402
from health_monitor.hal.exceptions import DeviceError, DeviceInitError  # noqa: E402


def _report(step: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'OK ' if ok else 'FAIL'}] {step}" + (f"　{detail}" if detail else ""))


def hold_lcd(rows: List[str], seconds: float) -> bool:
    """在 LCD 上**持续显示**若干秒（不关闭器件）。

    ⚠️ 为什么要这个模式：`test_lcd()` 发完命令就 `close()`，
    而 LCD 驱动在 close 时可能清屏/关背光 —— 用户根本来不及看，
    于是"测试成功"却"看不到内容"（第一版就是这样）。
    这里保持连接不关，反复刷新，让内容一直留在屏上。
    """
    print("\n" + "=" * 74)
    print(f"LCD1602 持续显示 {seconds:g} 秒（期间请直接看屏；Ctrl+C 可提前结束）")
    print("=" * 74)
    dev = create_device("lcd1602", params={"bus": 1, "address": 0, "cols": 16, "rows": 2},
                        mock=False, name="display")
    try:
        dev.open()
        first, second = rows[0][:16], (rows[1][:16] if len(rows) > 1 else "")
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            dev.send(DisplayCommand(lines=(first, second)))
            print(f"  显示中：第1行='{first}'　第2行='{second}'", end="\r", flush=True)
            time.sleep(1.0)
        print(f"\n  ✅ 保持显示结束（内容应仍在屏上，直到下一次写入或断电）")
        return True
    except (DeviceInitError, DeviceError) as exc:
        print(f"\n  ❌ {type(exc).__name__}: {exc}")
        return False
    finally:
        try:
            dev.close()
        except Exception:  # noqa: BLE001
            pass


def test_lcd(rows: List[str]) -> bool:
    print("\n" + "=" * 74)
    print("LCD1602（I2C，地址 0x27 / 0x3F 自动探测；每行 16 字符）")
    print("=" * 74)
    dev = create_device("lcd1602", params={"bus": 1, "address": 0, "cols": 16, "rows": 2},
                        mock=False, name="display")
    ok = False
    try:
        dev.open()
        _report("open（含 I2C 地址探测）", True, dev.describe()["bus"])
        first, second = rows[0][:16], (rows[1][:16] if len(rows) > 1 else "")
        dev.send(DisplayCommand(lines=(first, second)))
        _report("写入两行", True, f"第1行='{first}'　第2行='{second}'")
        back = dev.read()
        # ⚠️ 驱动会把每行**补空格到面板宽度**（16 字符），所以比较前要 strip，
        #    否则会把"正常补齐"误判成失败（第一版就踩了这个）。
        got = tuple(line.strip() for line in back.lines)
        want = (first.strip(), second.strip())
        _report("回读驱动记录的内容", got == want, f"{got}")
        ok = True
    except (DeviceInitError, DeviceError) as exc:
        _report("失败", False, f"{type(exc).__name__}: {exc}")
    finally:
        try:
            dev.close()
        except Exception:  # noqa: BLE001
            pass
    if ok:
        print("\n  👀 请看 LCD：")
        print("     · 第 1 行应显示 " + repr(rows[0][:16]))
        if len(rows) > 1:
            print("     · 第 2 行应显示 " + repr(rows[1][:16]))
        print("     · 只有背光没有字 ⇒ 调背面的**对比度电位器**（蓝色小螺丝）")
        print("     · 显示方块/乱码 ⇒ I2C 地址对了但初始化时序问题，可改用 address=0x3F 试")
    return ok


def test_tft(args: argparse.Namespace, rows: List[str]) -> bool:
    print("\n" + "=" * 74)
    print(f"TFT 彩屏（SPI；控制器={args.controller}）")
    print("=" * 74)
    params = {
        "controller": args.controller,
        "spi_bus": 0,
        "spi_device": args.spi_device,
        "dc_pin": args.dc,
        "reset_pin": args.reset,
        "rotate": args.rotate,
        "baudrate": args.baud,
    }
    if args.bgr:
        params["bgr"] = True
    if args.invert:
        params["invert"] = True
    dev = create_device("tft_spi", params=params, mock=False, name="tft")
    ok = False
    try:
        dev.open()
        desc = dev.describe()
        _report("open", True, f"{desc['notes'].split('；')[0]}")
        _report("接线", True, f"{desc['bus']}；DC={desc['pins']['dc']}；RST={desc['pins']['rst']}")
        first, second = rows[0][:16], (rows[1][:16] if len(rows) > 1 else "")
        dev.send(DisplayCommand(lines=(first, second), page=1))
        _report("写入两行", True, f"第1行='{first}'　第2行='{second}'")
        ok = True
    except DeviceInitError as exc:
        _report("初始化失败", False, str(exc))
    finally:
        try:
            dev.close()
        except Exception:  # noqa: BLE001
            pass
    if ok:
        print("\n  👀 请看 TFT：")
        print("     · **上电瞬间**应先出现【红/绿/蓝三色条】（这是驱动的自检画面）")
        print(f"     · 随后是两行大字：{first!r} / {second!r}")
        print("     · 颜色红蓝互换 ⇒ 加 --bgr；画面像底片 ⇒ 加 --invert")
        print("     · 花屏/条纹 ⇒ 换控制器试，或降速 --baud 8000000")
        print("     · 全白/全黑 ⇒ 查 DC(脚18)/RST(脚22)/CS(脚26)/VCC 接线")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="显示器件实测（LCD / TFT）")
    parser.add_argument("--device", default="all", choices=["all", "lcd1602", "tft_spi", "tft"])
    parser.add_argument("--hold", type=float, default=0.0,
                        help="持续显示 N 秒不关闭器件（LCD 用；方便肉眼确认，默认 0）")
    parser.add_argument("--text", action="append", default=None,
                        help="要显示的文字（可给两次，分别对应两行）；默认显示演示文案")
    # TFT 参数
    parser.add_argument("--controller", default="auto", help="TFT 控制器（auto 会依次尝试）")
    parser.add_argument("--spi-device", type=int, default=1, help="TFT 片选：0=CE0(脚24) 1=CE1(脚26)")
    parser.add_argument("--dc", type=int, default=24, help="TFT DC/A0 引脚 BCM（默认 24）")
    parser.add_argument("--reset", type=int, default=25, help="TFT RESET 引脚 BCM（默认 25）")
    parser.add_argument("--rotate", type=int, default=0, choices=[0, 90, 180, 270],
                        help="旋转（你说的 1.8 寸 128*160 竖屏用 0）")
    parser.add_argument("--baud", type=int, default=24_000_000, help="SPI 时钟 Hz")
    parser.add_argument("--bgr", action="store_true", help="颜色红蓝互换时加")
    parser.add_argument("--invert", action="store_true", help="画面像底片时加")
    args = parser.parse_args()

    rows = args.text if args.text else ["HEALTH MONITOR", "HR-- SpO2-- 36.5C"]
    rows = list(rows)[:2]

    results: List[tuple[str, bool]] = []
    if args.hold and args.hold > 0:
        # 持续显示模式：不关器件，内容留在屏上
        if args.device in ("all", "lcd1602"):
            results.append(("LCD1602", hold_lcd(rows, args.hold)))
        if args.device in ("all", "tft", "tft_spi"):
            results.append(("TFT", test_tft(args, rows)))
    else:
        if args.device in ("all", "lcd1602"):
            results.append(("LCD1602", test_lcd(rows)))
        if args.device in ("all", "tft", "tft_spi"):
            results.append(("TFT", test_tft(args, rows)))

    print("\n" + "=" * 74)
    print("  小结（第 ③ 步「人工确认」只能你自己看屏，脚本替不了）")
    print("=" * 74)
    for name, ok in results:
        print(f"  {name:<12} {'通信与写入成功，请确认屏幕内容' if ok else '失败（见上面的报错）'}")
    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
