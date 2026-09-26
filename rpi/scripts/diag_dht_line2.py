#!/usr/bin/env python3
"""精确诊断 DHT11 数据线：用**芯片级**读数区分"模块问题"与"板上/接线短路"。

背景（2026-09-24 真机）
----------------------
GPIO4（物理脚 7）在内部上拉、内部下拉、浮空三种配置下**读回都是高电平**。
"下拉拉不动"只可能是三种原因：

  A. **外部有东西把它强拉到 3.3V**（短路到 3.3V / 模块把线驱动高）；
  B. 内部下拉**没真的生效**（引脚被别的驱动占用、或 lgpio 没设成功）；
  C. 引脚烧了（罕见）。

本脚本做两件事把它们分开：
  1. 打印**芯片寄存器实际状态**（`pinctrl get 4` / `pigs` 一类工具），
     确认 pull 配置到底有没有落到寄存器；
  2. 给出**拔线对照实验**的明确步骤：拔掉 DHT11 的 DATA 线后重测 ——
     · 变成"上拉=1、下拉=0" ⇒ 盘子/板子没问题，**是 DHT11 模块或它的接线**把它拉高的；
     · 仍然是"上拉=1、下拉=1" ⇒ 说明**树莓派这一侧**（面包板/杜邦线/插座）短路到 3.3V。

用法：python3 scripts/diag_dht_line2.py
"""

from __future__ import annotations

import shutil
import subprocess
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

PIN = 4


def run(cmd: list[str]) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=15,
                           encoding="utf-8", errors="replace")
        return ((p.stdout or "") + (p.stderr or "")).strip()
    except Exception as exc:  # noqa: BLE001
        return f"<失败：{type(exc).__name__}: {exc}>"


def chip_state() -> None:
    print("【1】芯片寄存器实际状态（看 pull 到底有没有生效）")
    for tool, args in (("pinctrl", ["get", str(PIN)]),
                       ("raspi-gpio", ["get", str(PIN)]),
                       ("pigs", ["r", str(PIN)])):
        if shutil.which(tool):
            print(f"  $ {tool} {' '.join(args)}")
            print("    " + run([tool] + args).replace("\n", "\n    "))
            return
    safe_print("  ⚠️ 没有 pinctrl / raspi-gpio / pigs 可用，改用 lgpio 读模式")
    try:
        import lgpio

        h = lgpio.gpiochip_open(0)
        mode = lgpio.gpio_get_mode(h, PIN)
        print(f"  lgpio.gpio_get_mode({PIN}) = {mode}")
        lgpio.gpiochip_close(h)
    except Exception as exc:  # noqa: BLE001
        print(f"  读取失败：{exc}")


def level_with(label: str, flags: int) -> int:
    import lgpio

    h = lgpio.gpiochip_open(0)
    try:
        try:
            lgpio.gpio_free(h, PIN)
        except Exception:  # noqa: BLE001
            pass
        lgpio.gpio_claim_input(h, PIN, flags)
        time.sleep(0.1)
        samples = [lgpio.gpio_read(h, PIN) for _ in range(60)]
        return sum(samples)
    finally:
        try:
            lgpio.gpio_free(h, PIN)
        except Exception:  # noqa: BLE001
            pass
        lgpio.gpiochip_close(h)


def measure(tag: str) -> None:
    import lgpio

    up = level_with("up", lgpio.SET_PULL_UP)
    down = level_with("down", lgpio.SET_PULL_DOWN)
    none = level_with("none", 0)
    print(f"  {tag}：上拉={up}/60　下拉={down}/60　浮空={none}/60")
    return


def verdict(up: int, down: int) -> str:
    if down <= 3:
        return "✅ 正常：下拉能把它拉低（线上没有强上拉）→ 这一侧没问题"
    if up >= 57 and down >= 57:
        return "❌ 线被**强拉到 3.3V**（下拉拉不动）→ 短路或模块把它驱动高"
    return "⚠️ 读数不稳定/介于中间 → 接触不良或线太长"


def main() -> int:
    print("=" * 74)
    print(f"DHT11 数据线精确诊断（GPIO{PIN} = 物理脚 7）")
    print("=" * 74)

    chip_state()

    print("\n【2】改配置测电平（能读到寄存器状态与拉电阻是否生效）")
    measure("当前状态")

    print("\n【3】拔线对照实验（**需要你动手，一步即可**）")
    safe_print("  👉 请把 **DHT11 模块上的 DATA 线拔掉**（只拔 DATA，VCC/GND 可以留着）")
    print("     拔好后按回车继续（这样能区分是模块的问题还是树莓派这一侧的问题）…")
    try:
        input()
    except EOFError:
        pass
    import lgpio

    up = level_with("up", lgpio.SET_PULL_UP)
    down = level_with("down", lgpio.SET_PULL_DOWN)
    print(f"  拔掉 DATA 后：上拉={up}/60　下拉={down}/60")

    print("\n【4】结论")
    print("  " + verdict(up, down))
    print()
    if down <= 3:
        safe_print("  ⇒ 树莓派这一侧没问题。**问题在 DHT11 模块或它的其余接线**：")
        print("     · 模块丝印顺序（有的模块是 DATA/VCC/GND，有的是 VCC/DATA/GND）——对调一下 VCC 与 DATA 试试")
        print("     · 模块可能损坏（内部把 DATA 短路到 VCC）：换一个模块验证")
        print("     · 杜邦线本身短路（用万用表量两端是否与相邻线导通）")
    else:
        safe_print("  ⇒ 拔了线还是被拉高 → **树莓派这一侧**有问题：")
        print("     · 面包板该列与 3.3V 电源列短路（换一列插）")
        print("     · 杜邦线插到了物理脚 1/17（3.3V）而不是 7   ← 最常见")
        print("     · 排针/面包板内部短路（换一个孔位）")

    print("\n  改完后重新插上 DATA，再跑：python3 scripts/diag_pin_levels.py --dht")
    print("  正常应看到：内部上拉=1、内部下拉=**0**")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
