#!/usr/bin/env python3
"""TFT 彩屏上机自检：确定控制器型号 + 肉眼确认颜色/方向是否正确。

为什么需要它
------------
SPI TFT 屏外观相似但控制器不同（ST7735S / ST7789 / ILI9341），
**初始化序列、分辨率、甚至颜色位序都不一样**。点亮它靠的是"试 + 看"：
本脚本会把每种控制器依次初始化，并在屏上画三色条 + 文字，让你看一眼就知道对不对。

用法（**必须在树莓派上跑**）::

    cd ~/raspberry-health-monitor/rpi
    python3 scripts/tft_check.py                 # 自动依次尝试三种控制器
    python3 scripts/tft_check.py --controller st7735
    python3 scripts/tft_check.py --device 0      # 用 CE0（脚 24）而不是默认 CE1（脚 26）
    python3 scripts/tft_check.py --bgr           # 红蓝互换（颜色不对时试）
    python3 scripts/tft_check.py --invert        # 反色（像底片时试）
    python3 scripts/tft_check.py --rotate 0      # 换方向（0/90/180/270）
    python3 scripts/tft_check.py --steps         # 逐屏展示，每步等你按回车

**看什么算对**：
1. 出现**纯正的红、绿、蓝**三条横条（顺序：上红中绿下蓝）；
2. 文字 `TFT OK` 是白字红底，`R G B TEST` 是黑字绿底，`CHECK COLORS` 是白字蓝底；
3. 文字方向正确（不是镜像/倒置）。

**典型症状对照**：
- 红蓝互换（"红"看着像蓝）⇒ 加 `--bgr`（或反过来去掉它）
- 画面像底片/负片 ⇒ 加 `--invert`
- 花屏、条纹、只亮不画 ⇒ 控制器选错了，换另一个试
- 全白/全黑、什么都没动 ⇒ 接线问题（先查 DC/RST/CS/VCC），见 `docs/05-排错手册.md`
- 画面上下或左右错位一条边 ⇒ 该型号需要 col/row 偏移（在本项目驱动里加 offset）
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

from health_monitor.hal.exceptions import DeviceInitError  # noqa: E402
from health_monitor.outputs.tft_spi import AUTO_ORDER, CONTROLLERS, TftSpi  # noqa: E402

TIPS = {
    "st7735": "1.8\" 128x160 常见（Waveshare 1.8inch LCD Module 等）",
    "st7735_128x160": "同上，但竖屏 128x160（偏移与横屏版不同）",
    "st7789": "1.3\"/1.54\"/2.0\" IPS 240x240 常见",
    "st7789_240x320": "2.0\"/2.4\" 240x320",
    "ili9341": "2.4\"/2.8\"/3.2\" 240x320 常见",
}


def preflight(device: int) -> int:
    """接线与环境的前置检查（不做这些就白试）。"""
    print("=" * 78)
    print("TFT 上机自检 · 前置检查")
    print("=" * 78)
    problems = 0

    for path, hint in (
        (f"/dev/spidev0.{device}", "开 SPI：sudo raspi-config → Interface Options → SPI → Enable，然后重启"),
        ("/dev/gpiochip0", "树莓派 5 需要 lgpio：sudo apt install -y python3-lgpio"),
    ):
        ok = Path(path).exists()
        print(f"[{'OK ' if ok else 'FAIL'}] {path}")
        if not ok:
            print(f"        → {hint}")
            problems += 1

    for mod, hint in (("spidev", "sudo apt install -y python3-spidev"),
                      ("lgpio", "sudo apt install -y python3-lgpio")):
        try:
            __import__(mod)
            print(f"[OK ] python 模块 {mod}")
        except ImportError:
            print(f"[FAIL] python 模块 {mod} 未安装 → {hint}")
            problems += 1

    print()
    print("接线复核（务必逐条看）：")
    print("  TFT 引脚        树莓派物理脚")
    print("  VCC          →  脚 1（3.3V；部分模块需 5V 且自带稳压）")
    print("  GND          →  脚 6（务必共地）")
    print("  SCL/CLK      →  脚 23（GPIO11）")
    print("  SDA/MOSI     →  脚 19（GPIO10）")
    print(f"  CS/CE        →  脚 {'24（GPIO8, CE0）' if device == 0 else '26（GPIO7, CE1）'}")
    print("  DC/RS        →  脚 18（GPIO24）")
    print("  RES/RST      →  脚 22（GPIO25）")
    print("  BLK/BL/LED   →  脚 1 或 33（背光常亮）")
    print()
    if problems:
        safe_print(f"⚠️ 有 {problems} 项前置条件不满足 —— 先解决它们，否则试控制器没有意义。")
    else:
        safe_print("✅ 前置条件齐备，开始试控制器。\n")
    return problems


def try_one(name: str, args: argparse.Namespace, wait: bool) -> bool:
    """初始化一种控制器并画测试画面；返回是否成功（不代表颜色对）。"""
    print("-" * 78)
    print(f"尝试控制器：{name}　（{TIPS.get(name, '')}）")
    print("-" * 78)
    tft = TftSpi(
        controller=name,
        spi_bus=args.bus,
        spi_device=args.device,
        dc_pin=args.dc,
        reset_pin=args.reset,
        backlight_pin=args.backlight,
        baudrate=args.baudrate,
        rotate=args.rotate,
        bgr=True if args.bgr else None,
        invert=True if args.invert else None,
        mock=False,
    )
    try:
        tft.open()
    except DeviceInitError as exc:
        safe_print(f"❌ 初始化失败：{exc}\n")
        return False
    except Exception as exc:  # noqa: BLE001
        safe_print(f"❌ 初始化异常：{type(exc).__name__}: {exc}\n")
        return False

    spec = CONTROLLERS[name]
    safe_print(f"✅ 初始化完成：{spec.name}　{screen_text(tft)}　后端 {tft._backend}")
    print(f"   已写入 SPI {tft.writes} 次")
    print()
    print("👉 请看屏幕，确认：")
    print("   1) 三条横条是【上红、中绿、下蓝】且颜色纯正；")
    print("   2) 文字 'TFT OK'（白字红底）、'R G B TEST'（黑字绿底）、'CHECK COLORS'（白字蓝底）清晰；")
    print("   3) 文字方向正确、不镜像、不倒置。")
    print()
    if wait:
        answer = input("   这三条都对吗？[y=对 / n=不对 / q=退出] ").strip().lower()
        if answer.startswith("q"):
            tft.close()
            raise SystemExit(0)
        if answer.startswith("y"):
            tft.close()
            print()
            print("=" * 78)
            print(f"🎉 确定控制器：{name}")
            print("=" * 78)
            print("把这一段填进 rpi/config/devices.json 的 tft 段：")
            print(f'  "controller": "{name}",')
            print(f'  "spi_device": {args.device}, "dc_pin": {args.dc}, "reset_pin": {args.reset},')
            print(f'  "rotate": {args.rotate}, "bgr": {str(bool(args.bgr)).lower()}, "invert": {str(bool(args.invert)).lower()}')
            print("然后把 tft 段的 \"enabled\" 改成 true，重启服务即可。")
            return True
    else:
        for remaining in range(args.hold, 0, -1):
            print(f"\r   {remaining} 秒后试下一个…", end="", flush=True)
            time.sleep(1)
        print("\r" + " " * 40 + "\r", end="")
    tft.close()
    return False


def screen_text(tft: TftSpi) -> str:
    return f"{tft.width}x{tft.height}"


def main() -> int:
    parser = argparse.ArgumentParser(description="TFT 彩屏自检（确定控制器 + 确认颜色）")
    parser.add_argument("--controller", default="auto",
                        help="控制器名，或 auto（依次尝试）；可用：" + ", ".join(sorted(CONTROLLERS)))
    parser.add_argument("--bus", type=int, default=0, help="SPI 总线号（默认 0）")
    parser.add_argument("--device", type=int, default=1, help="片选号：0=CE0(脚24)，1=CE1(脚26)（默认 1）")
    parser.add_argument("--dc", type=int, default=24, help="DC 引脚 BCM（默认 24 = 物理脚 18）")
    parser.add_argument("--reset", type=int, default=25, help="RST 引脚 BCM（默认 25 = 物理脚 22）")
    parser.add_argument("--backlight", type=int, default=-1, help="背光引脚 BCM（默认 -1 = 不控制）")
    parser.add_argument("--baudrate", type=int, default=24_000_000, help="SPI 时钟 Hz（花屏就调小，如 8000000）")
    parser.add_argument("--rotate", type=int, default=90, choices=[0, 90, 180, 270])
    parser.add_argument("--bgr", action="store_true", help="强制 BGR（红蓝互换时用）")
    parser.add_argument("--invert", action="store_true", help="强制反色（像底片时用）")
    parser.add_argument("--hold", type=int, default=6, help="auto 模式下每种显示几秒（默认 6）")
    parser.add_argument("--steps", action="store_true", help="逐屏等待回车确认（最省事，推荐）")
    parser.add_argument("--skip-preflight", action="store_true", help="跳过前置检查")
    args = parser.parse_args()

    name_list = list(AUTO_ORDER) if args.controller == "auto" else [args.controller]
    for name in name_list:
        if name not in CONTROLLERS:
            safe_print(f"❌ 未知控制器 {name!r}；可用：{', '.join(sorted(CONTROLLERS))}")
            return 2

    if not args.skip_preflight:
        problems = preflight(args.device)
        if problems:
            print("（如果你确认这些都不是问题，可加 --skip-preflight 继续试）")
            if not input("仍要继续吗？[y/N] ").strip().lower().startswith("y"):
                return 1

    if args.controller == "auto" and not args.steps:
        print("提示：加 --steps 可以每试一种就停下来问你，比定时切换更好判断。\n")

    for name in name_list:
        try:
            if try_one(name, args, wait=args.steps):
                return 0
        except SystemExit:
            return 0

    print("=" * 78)
    safe_print("❌ 所有候选控制器都没有被确认。下一步：")
    print("   1) 先查接线：DC/RST/CS 三根线最常见接错（CS 别与 MCP3002 抢同一个片选）")
    print("   2) VCC 换到 5V 试（若你的模块自带稳压）")
    print("   3) SPI 时钟调小：--baudrate 8000000")
    print("   4) 色偏/底片：分别加 --bgr / --invert 再试一轮")
    print("   5) 完全没反应：用万用表确认 VCC 有电压、背光脚是否要单独接高电平")
    print("   详见 docs/05-排错手册.md 的『B. 显示器件』一节")
    print("=" * 78)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
