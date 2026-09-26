#!/usr/bin/env python3
"""TFT 控制器"试出来"：逐个候选配置在屏上画测试图，由你肉眼确认哪个对。

为什么需要它
------------
SPI TFT 屏外观一样但**控制器/偏移/颜色顺序**可能不同（1.8" 128×160 这批模组尤其杂）：
同一块屏，用错控制器会花屏；偏移差 1~2 像素会在边缘留一条杂色边；
BGR 设反会让红蓝互换。硬靠"猜一个"经常白折腾，所以做成**轮流显示 + 你选**。

流程：对每个候选配置 → 初始化 → 显示 **2 秒三色条 + 4 秒文字** → 关闭 → 下一个。
你看哪一轮"颜色纯正、方向正确、边缘无杂色"，就记下它的名字。

用法（在树莓派上）::

    python3 scripts/tft_pick_controller.py                    # 自动遍历常用候选（每轮 6 秒）
    python3 scripts/tft_pick_controller.py --hold 8           # 每轮看 8 秒
    python3 scripts/tft_pick_controller.py --only st7735_128x160   # 只试一个
    python3 scripts/tft_pick_controller.py --bus 0 --spi-device 1 --dc 24 --reset 25

结束后它会打印"该填进 config/devices.json 的参数"，你确认后再写进配置。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import List, Tuple

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

from health_monitor.hal import create_device  # noqa: E402
from health_monitor.hal.exceptions import DeviceInitError  # noqa: E402
from health_monitor.outputs.tft_spi import CONTROLLERS  # noqa: E402

#: 优先试的候选（1.8" 128×160 最常见 → 其它）
CANDIDATES: Tuple[Tuple[str, str], ...] = (
    ("st7735_1.8_128x160", "1.8 寸 128×160 竖屏（偏移 2/1）——**你这块最可能是它**"),
    ("st7735_128x160", "1.8 寸 128×160 竖屏（无偏移）"),
    ("st7735_128x160_c1", "1.8 寸 128×160 竖屏（列偏移 1）"),
    ("st7735_128x160_c0r2", "1.8 寸 128×160 竖屏（行偏移 2）"),
    ("st7735", "1.8 寸 160×128 横屏（无偏移）"),
    ("st7735r", "ST7735R 红版（偏移 2/1）"),
    ("st7789", "ST7789 240×240（1.3/1.54 寸 IPS）"),
    ("ili9341", "ILI9341 240×320（2.4/2.8 寸）"),
)


def show(dev, name: str) -> None:
    """在屏上画一段测试图：先三色条（由 open() 自带），再两行文字。"""
    from health_monitor.hal import DisplayCommand

    # open() 已经画过三色条 + "TFT OK"，这里再写两行大字便于确认方向
    dev.send(DisplayCommand(lines=(f"{name[:16]}", "ABC 123 xyz"), page=7))


def try_one(name: str, note: str, args: argparse.Namespace) -> bool:
    print("\n" + "-" * 74)
    safe_print(f"▶ 候选：{name}")
    print(f"  {note}")
    print("-" * 74)
    dev = create_device(
        "tft_spi",
        params={
            "controller": name,
            "spi_bus": args.bus,
            "spi_device": args.spi_device,
            "dc_pin": args.dc,
            "reset_pin": args.reset,
            "rotate": args.rotate,
            "baudrate": args.baud,
        },
        mock=False, name="tft",
    )
    try:
        dev.open()
    except DeviceInitError as exc:
        safe_print(f"  ❌ 初始化失败：{str(exc)[:120]}")
        return False
    except Exception as exc:  # noqa: BLE001
        safe_print(f"  ❌ 异常：{type(exc).__name__}: {exc}")
        return False

    try:
        show(dev, name)
        safe_print(f"  ✅ 已显示，请观察 {args.hold:g} 秒……")
        # 前 2 秒是三色条（open 时画的），后几秒是文字
        for remaining in range(int(args.hold), 0, -1):
            print(f"     {remaining}…", end="\r", flush=True)
            time.sleep(1.0)
        print(" " * 20, end="\r")
    finally:
        dev.close()
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="逐个候选配置试 TFT 控制器（由你肉眼选）")
    parser.add_argument("--bus", type=int, default=0, help="SPI 总线（默认 0）")
    parser.add_argument("--spi-device", type=int, default=1, help="片选：0=CE0(脚24) 1=CE1(脚26)")
    parser.add_argument("--dc", type=int, default=24, help="DC/A0 引脚 BCM（默认 24 = 物理脚 18）")
    parser.add_argument("--reset", type=int, default=25, help="RESET 引脚 BCM（默认 25 = 物理脚 22）")
    parser.add_argument("--rotate", type=int, default=0, choices=[0, 90, 180, 270],
                        help="旋转；128*160 竖屏用 0（默认）")
    parser.add_argument("--baud", type=int, default=16_000_000,
                        help="SPI 时钟 Hz（默认 16MHz；花屏就调小到 8000000）")
    parser.add_argument("--hold", type=float, default=6.0, help="每个候选显示几秒（默认 6）")
    parser.add_argument("--only", default="", help="只试指定控制器名")
    args = parser.parse_args()

    print("=" * 74)
    print("TFT 控制器试选（每轮：先三色条 → 再文字）")
    print("=" * 74)
    print("请盯着屏幕，逐轮判断：")
    print("  · 三条横条应是【上红、中绿、下蓝】且颜色纯正 → 否则换 --bgr 再试一轮")
    print("  · 文字应清晰、方向正确（不镜像、不倒置）")
    print("  · 边缘不应出现一条杂色/错位带 → 有的话说明该换偏移候选")
    print("  · 花屏/条纹/全白 → 这个候选不对，看下一个")

    if args.only:
        if args.only not in CONTROLLERS:
            safe_print(f"❌ 未知控制器 {args.only!r}；可用：{', '.join(sorted(CONTROLLERS))}")
            return 2
        candidates = ((args.only, "（你指定的）"),)
    else:
        candidates = CANDIDATES

    shown: List[str] = []
    for name, note in candidates:
        if name not in CONTROLLERS:
            continue
        if try_one(name, note, args):
            shown.append(name)

    print("\n" + "=" * 74)
    print("试完了。请回忆哪一轮**颜色/方向/边缘**都正常：")
    print("=" * 74)
    for name in shown:
        print(f"  · {name}")
    print("\n确认后，把下面这段填进 rpi/config/devices.json 的 tft 段：")
    print('  "controller": "<你选中的名字>",')
    print(f'  "spi_bus": {args.bus}, "spi_device": {args.spi_device}, '
          f'"dc_pin": {args.dc}, "reset_pin": {args.reset},')
    print(f'  "rotate": {args.rotate}, "baudrate": {args.baud}')
    print('然后把 tft 段的 "enabled" 改成 true。')
    print("\n若颜色红蓝互换：在配置里加 \"bgr\": true；若像底片：加 \"invert\": true。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
