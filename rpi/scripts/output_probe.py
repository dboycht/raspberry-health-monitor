#!/usr/bin/env python3
"""输出器件定点探针：LED 颜色序列 + 蜂鸣器（+ 可选语音）——**只做动作，不问问题**。

为什么单独有一个（2026-09-26 起 T2 开始用）
-------------------------------------------
`hardware_test.py` 的输出检查是**交互式**的（它点灯/响铃，然后 `input()` 问人"看到了吗"），
适合"人坐在板子前、边跑边看"。但"**代理远程跑、跑完再让人确认**"的流程需要一条
**不问问题、只做动作、并把"我做了什么"打成 ASCII 摘要**的命令 —— 本脚本就是它。
配合 `docs/14` 的阶梯：每加一个输出器件（T2 蜂鸣/LED、T8 语音、T9 TFT）都用它先做一次动作。

判据（人眼/人耳，脚本替不了）
-----------------------------
* `--led`：按**配置里真有的颜色**依次点亮（默认绿 → 黄 → 灭，各 1.5 秒）；
  ⚠️ 刻意"按配置里真有的颜色"，而不是硬写绿黄红 —— 红灯的 GPIO24 已让给 TFT 的 DC。
* `--buzzer`：鸣叫 N 声（默认 2 声，200ms 响 / 200ms 停）。
* 跑完打印 ASCII 摘要，便于留档（填 `docs/07` 的 H12/H13 时照着抄）。

用法::

    cd ~/raspberry-health-monitor/rpi
    python3 scripts/output_probe.py                    # 灯 + 蜂鸣（默认）
    python3 scripts/output_probe.py --led-only         # 只点灯
    python3 scripts/output_probe.py --buzzer-only      # 只鸣叫
    python3 scripts/output_probe.py --led-colors green,yellow --hold 2.0
    python3 scripts/output_probe.py --beeps 3 --config ../config/devices.json

退出码：0 = 动作全部下发成功（**不代表人看到了**）；2 = 器件打开/下发失败（带排查线索）。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# 让 `basic.console.safe_print` 可导入（打印 ✅/❌/⚠ 时在窄编码控制台上自动降级）
# ⚠️ 为什么（2026-09-25 真机实测，ERROR.md E32/E35/E41）：中文 Windows / GBK 控制台上
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

from health_monitor.core.config import load_config  # noqa: E402
from health_monitor.hal import create_device  # noqa: E402
from health_monitor.hal.models import BeepCommand, LightCommand  # noqa: E402


def parse_colors(text: Optional[str]) -> Optional[List[str]]:
    """把 ``--led-colors green,yellow`` 解析成 ``["green","yellow"]``（空/None → None）。"""
    if not text:
        return None
    colors = [piece.strip() for piece in str(text).split(",")]
    return [c for c in colors if c] or None


def device_colors(device: Any) -> List[str]:
    """问器件"你支持哪些颜色" —— **属性与方法两种形态都要认**。

    ⚠️ 2026-09-26 真机踩到：本项目的 `Led.available_colors` 是 **`@property`**，
    最初只写 `callable()` 分支 ⇒ 属性形态被静默跳过、回退成默认三色（含没有的红灯）。
    （同一个坑也在 `hardware_test.py` 里修过，两边判据保持一致。）
    """
    attr = getattr(device, "available_colors", None)
    raw: Any = None
    if callable(attr):
        try:
            raw = attr()
        except Exception:  # noqa: BLE001 - 自报失败就退回默认
            raw = None
    elif attr is not None:
        raw = attr
    if raw is None:
        return []
    try:
        return [str(c) for c in raw if str(c)]
    except TypeError:  # pragma: no cover
        return []


def resolve_colors(device: Any, explicit: Optional[List[str]]) -> List[str]:
    """定这次要点哪些颜色：**显式优先**，否则用器件自报的（去掉 ``off``），再退回绿/黄。

    ⚠️ 为什么不硬写"绿黄红"（2026-09-26）：默认配置在 09-24 去掉了红灯
    （GPIO24 让给 TFT 的 DC），点不存在的颜色会让驱动抛 ``UnsupportedError``，
    看起来像"灯坏了"（假红）。
    """
    if explicit:
        return list(explicit)
    colors = [c for c in device_colors(device) if c != "off"]
    return colors or ["green", "yellow"]


def _open(config: Any, name: str) -> Any:
    """按配置里的设备名装配并打开一个真实器件（失败直接抛，由调用方给人话）。"""
    cfg = config.device(name)
    if cfg is None:
        raise RuntimeError(f"配置里没有设备 {name}（检查 rpi/config/devices.json）")
    device = create_device(cfg.driver, params=cfg.params, mock=False, name=cfg.name)
    device.open()
    return device


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="输出器件定点探针（LED / 蜂鸣器）")
    parser.add_argument("--config", default=None, help="配置文件路径（默认 config/devices.json）")
    parser.add_argument("--led-only", action="store_true", help="只点灯")
    parser.add_argument("--buzzer-only", action="store_true", help="只鸣叫")
    parser.add_argument("--led-colors", default=None, help="点哪些颜色，逗号分隔（默认取配置里真有的）")
    parser.add_argument("--hold", type=float, default=1.5, help="每个颜色亮多久（秒，默认 1.5）")
    parser.add_argument("--beeps", type=int, default=2, help="蜂鸣几声（默认 2）")
    args = parser.parse_args(argv)

    if args.led_only and args.buzzer_only:
        safe_print("[X] --led-only 与 --buzzer-only 不能同时给（要么都别给=两个都做）")
        return 2
    do_led = not args.buzzer_only
    do_buzzer = not args.led_only

    safe_print("=" * 72)
    safe_print("输出器件探针（只做动作、不问问题；看不到就是你那边的事了）")
    safe_print("=" * 72)

    report: Dict[str, str] = {}
    problems: List[str] = []

    try:
        config = load_config(args.config)
    except Exception as exc:  # noqa: BLE001
        safe_print(f"[X] 加载配置失败：{type(exc).__name__}: {exc}")
        return 2

    # ---- LED ----
    if do_led:
        try:
            led = _open(config, "status_led")
            colors = resolve_colors(led, parse_colors(args.led_colors))
            safe_print(f"[..] LED：将依次点亮 {' → '.join(colors)}（各 {args.hold:g} 秒）")
            for color in colors:
                led.send(LightCommand(color=color))
                safe_print(f"     -> {color}")
                time.sleep(max(0.0, args.hold))
            led.send(LightCommand(color="off"))
            safe_print("     -> off")
            led.close()
            report["LED"] = f"已按 {'、'.join(colors)} 依次点亮（各 {args.hold:g}s）后熄灭"
        except Exception as exc:  # noqa: BLE001
            problems.append(f"LED：{type(exc).__name__}: {exc}")
            safe_print(f"[X] LED 失败：{type(exc).__name__}: {exc}")

    # ---- 蜂鸣器 ----
    if do_buzzer:
        try:
            buzzer = _open(config, "alarm_buzzer")
            safe_print(f"[..] 蜂鸣器：鸣叫 {args.beeps} 声（200ms 响 / 200ms 停）")
            buzzer.send(BeepCommand(times=max(1, int(args.beeps)), on_ms=200, off_ms=200))
            buzzer.close()
            report["蜂鸣器"] = f"已鸣叫 {max(1, int(args.beeps))} 声"
        except Exception as exc:  # noqa: BLE001
            problems.append(f"蜂鸣器：{type(exc).__name__}: {exc}")
            safe_print(f"[X] 蜂鸣器失败：{type(exc).__name__}: {exc}")

    safe_print("-" * 72)
    for name, detail in report.items():
        safe_print(f"[OK] {name}：{detail}")
    if problems:
        safe_print("[X] 失败项：")
        for line in problems:
            safe_print(f"    - {line}")
        safe_print("    排查：1) 引脚是否与配置一致 2) 是否串了限流电阻 3) 蜂鸣器是否为**有源**")
        return 2
    safe_print("请人眼/人耳确认；确认后在 docs/07 的 H12/H13 打勾。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
