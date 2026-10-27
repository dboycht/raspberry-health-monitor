#!/usr/bin/env python3
"""Generate the hardware wiring diagrams (SVG) from the running config.

Why this script exists
----------------------
Every driver in this project already carries its own wiring truth in
``describe()`` -- and those docstrings literally say "will be read by the docs
generation script".  That script did not exist, so the hand-written hardware
pages slowly drifted away from the code (they still showed a red LED on
GPIO12 and had no "measure SpO2" button).

This script closes that loop: it asks the *drivers themselves* for the pin
numbers and renders two vector diagrams.

Outputs (both into ``hardware/`` by default)::

    hardware/接线图-引脚.svg        40-pin header, per-pin fan-out to devices
    hardware/接线图-原理.svg        system schematic (power rails + buses)

Both are plain UTF-8 SVG (no external assets, printable, embeddable in slides).

Usage (from the repo root or from ``rpi/``)::

    python hardware/tools/gen_wiring_svg.py                # write both SVGs
    python hardware/tools/gen_wiring_svg.py --check        # fail if SVG is stale
    python hardware/tools/gen_wiring_svg.py --outdir DIR   # render elsewhere

``--check`` is the anti-rot gate: it re-renders in memory and compares against
the committed files, so "someone changed devices.json but forgot to re-render"
turns into a red build instead of a wrong diagram on the lab bench.

ASCII-only source (project rule): this file must stay printable on a GBK
console.  All Chinese text lives inside the generated SVG.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
RPI_DIR = REPO_ROOT / "rpi"
if str(RPI_DIR) not in sys.path:
    sys.path.insert(0, str(RPI_DIR))


# ---------------------------------------------------------------------------
# palette -- one colour per bus class, reused by both diagrams
# ---------------------------------------------------------------------------
COL = {
    "bg": "#ffffff",
    "ink": "#1a1a1a",
    "dim": "#6b7780",
    "board": "#2f6f4e",
    "board_edge": "#1f4d36",
    "pin_idle": "#9aa5ad",
    "pwr33": "#c0392b",
    "pwr5": "#e67e22",
    "gnd": "#4a4a4a",
    "i2c": "#8e44ad",
    "spi": "#2980b9",
    "onewire": "#16a085",
    "gpio": "#7f8c8d",
    "warn": "#c0392b",
    "card": "#f7f9fb",
    "card_edge": "#c7d3dc",
}


def esc(text: str) -> str:
    """XML-escape a string for SVG text content."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def tidy(text: str) -> str:
    """Shrink a ``describe()`` sentence into something that fits a diagram."""
    out = str(text)
    for junk in ("（固定功能，接传感器 SDA）", "（固定功能，接传感器 SCL）"):
        out = out.replace(junk, "")
    # keep only the first clause -- the rest belongs in the prose docs
    for sep in (";", "；"):
        if sep in out:
            out = out.split(sep)[0]
    for sep in (" -- ", " —— ", "\u2014\u2014"):
        if sep in out:
            out = out.split(sep)[0]
    out = out.replace("（必须）", "").replace("（务必）", "")
    return out.strip()


# ---------------------------------------------------------------------------
# data collection -- everything below reads the code, nothing is hand-typed
# ---------------------------------------------------------------------------


def load_devices() -> Dict[str, Any]:
    """Load ``rpi/config/devices.json`` (+ local override, same as runtime)."""
    from health_monitor.core.config import load_config

    cfg = load_config(None)
    raw = json.loads((RPI_DIR / "config" / "devices.json").read_text(encoding="utf-8"))
    return {"devices": raw["devices"], "config": cfg}


def describe_all() -> Dict[str, Dict[str, Any]]:
    """Ask every configured device for its own wiring description."""
    from health_monitor.hal.registry import create_device

    cfg = json.loads((RPI_DIR / "config" / "devices.json").read_text(encoding="utf-8"))
    out: Dict[str, Dict[str, Any]] = {}
    for name, spec in cfg["devices"].items():
        dev = create_device(spec["driver"], spec.get("params", {}), mock=True, name=name)
        info = dev.describe()
        out[name] = {
            "driver": spec["driver"],
            "enabled": bool(spec.get("enabled")),
            "bus": info.get("bus", ""),
            "pins": info.get("pins", {}),
            "params": spec.get("params", {}),
        }
    return out


def bcm_of(info: Dict[str, Dict[str, Any]], device: str, key: str) -> Optional[int]:
    """Pull the BCM number a device actually uses **out of its config params**.

    This used to be a hand-written table -- which meant the diagram still looked
    "generated" while the pin numbers were copied by hand and never noticed when
    ``devices.json`` changed.  Now the only hand-written part is *which config
    key means what* (``"pin"`` -> the signal pin), and the number itself is read.

    Fixed-function bus pins (I2C SDA/SCL, SPI SCLK/MOSI/CS) are **not** in the
    config because the kernel owns them; those come from the driver's own
    ``describe()``/the bus definition and are passed in by the caller.
    """
    spec = info.get(device, {})
    params = spec.get("params", {}) or {}
    try:
        if key == "pin":
            return int(params["pin"])
        if key == "dc_pin":
            return int(params["dc_pin"])
        if key == "reset_pin":
            return int(params["reset_pin"])
        if key == "led":  # any colour -- caller picks the colour separately
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return None


def build_pin_rows(info: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One row per physical pin, with who uses it and on which bus."""
    from health_monitor.hal.pins import bcm_to_physical, full_table

    # (physical pin) -> list of (device label, bus class, note)
    uses: Dict[int, List[Tuple[str, str, str]]] = {}

    def add(bcm: Optional[int], label: str, bus: str, note: str = "") -> None:
        if bcm is None:
            return
        phys = bcm_to_physical(bcm)
        if phys is None:
            raise SystemExit(f"BUG: GPIO{bcm} is not on the 40-pin header")
        uses.setdefault(phys, []).append((label, bus, note))

    # --- I2C-1: two devices share one bus (distinct addresses, so no conflict).
    # SDA/SCL are fixed ALT-function pins (GPIO2 / GPIO3) -- the kernel owns them,
    # they never appear in devices.json, so they are stated once here.
    if "vitals" in info:
        add(2, "MAX30102 SDA", "i2c", "0x57")
        add(3, "MAX30102 SCL", "i2c", "0x57")
    if "display" in info:
        add(2, "LCD1602 SDA", "i2c", "0x27/0x3F")
        add(3, "LCD1602 SCL", "i2c", "0x27/0x3F")

    # --- everything below reads the real pin out of devices.json
    add(bcm_of(info, "ambient", "pin"), "DHT11 DATA", "onewire",
        "needs 4k7~10k pull-up if bare")
    add(bcm_of(info, "motion", "pin"), "HC-SR501 OUT", "gpio", "3.3V level, direct")
    add(bcm_of(info, "alarm_buzzer", "pin"), "Buzzer +", "gpio", "series 220R~1k")
    add(bcm_of(info, "sos_button", "pin"), "SOS button", "gpio", "internal pull-up")
    add(bcm_of(info, "spo2_button", "pin"), "SpO2 button", "gpio", "internal pull-up")

    led_pins = ((info.get("status_led", {}).get("params") or {}).get("pins") or {})
    for colour, note in (("green", "series 220R~1k"), ("yellow", "series 220R~1k")):
        add(led_pins.get(colour), f"LED {colour}", "gpio", note)

    # --- SPI0: SCLK/MOSI/CS are bus pins; DC/RESET are plain GPIO from the config
    if "tft" in info:
        add(11, "TFT SCK", "spi", "SPI0 SCLK")
        add(10, "TFT SDA (MOSI)", "spi", "SPI0 MOSI")
        add(7, "TFT CS", "spi", "SPI0 CE1")
        add(bcm_of(info, "tft", "dc_pin"), "TFT DC/A0", "spi", "plain GPIO")
        add(bcm_of(info, "tft", "reset_pin"), "TFT RESET", "spi", "plain GPIO")

    power = {1: "3.3V", 17: "3.3V", 2: "5V", 4: "5V",
             6: "GND", 9: "GND", 14: "GND", 20: "GND",
             30: "GND", 34: "GND", 39: "GND", 25: "GND"}

    rows: List[Dict[str, Any]] = []
    for phys, bcm, role in full_table():
        entry: Dict[str, Any] = {"phys": phys, "bcm": bcm, "role": role, "uses": uses.get(phys, [])}
        entry["power"] = power.get(phys, "")
        if phys in (27, 28):
            entry["status"] = "reserved"
        elif entry["power"] == "GND":
            entry["status"] = "gnd"
        elif entry["power"]:
            entry["status"] = "power"
        elif uses.get(phys):
            entry["status"] = "used"
        else:
            entry["status"] = "free"
        rows.append(entry)

    # pins that exist on the header but have no BCM number (power / ground)
    extra = []
    for phys, kind in sorted(power.items()):
        if not any(r["phys"] == phys for r in rows):
            extra.append({"phys": phys, "bcm": None, "role": "", "uses": [],
                          "power": kind,
                          "status": "gnd" if kind == "GND" else "power"})
    rows.extend(extra)
    rows.sort(key=lambda r: r["phys"])
    return rows


# ---------------------------------------------------------------------------
# SVG helpers
# ---------------------------------------------------------------------------


def svg_open(width: int, height: int, title: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="Microsoft YaHei, Noto Sans CJK SC, sans-serif">\n'
        f"<title>{esc(title)}</title>\n"
        f'<rect width="{width}" height="{height}" fill="{COL["bg"]}"/>\n'
    )


def rect(x: float, y: float, w: float, h: float, fill: str, stroke: str = "none",
         rx: float = 4, sw: float = 1) -> str:
    return (f'<rect x="{x:.0f}" y="{y:.0f}" width="{w:.0f}" height="{h:.0f}" rx="{rx}" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>\n')


def text(x: float, y: float, s: str, size: float = 12, fill: str = COL["ink"],
         anchor: str = "start", weight: str = "normal") -> str:
    return (f'<text x="{x:.0f}" y="{y:.0f}" font-size="{size}" fill="{fill}" '
            f'text-anchor="{anchor}" font-weight="{weight}">{esc(s)}</text>\n')


def path(d: str, stroke: str, sw: float = 1.6, dash: str = "") -> str:
    da = f' stroke-dasharray="{dash}"' if dash else ""
    return (f'<path d="{d}" fill="none" stroke="{stroke}" stroke-width="{sw}" '
            f'stroke-linecap="round"{da}/>\n')


def dot(x: float, y: float, r: float, fill: str) -> str:
    return f'<circle cx="{x:.0f}" cy="{y:.0f}" r="{r}" fill="{fill}"/>\n'


# ---------------------------------------------------------------------------
# diagram 1 -- 40-pin header fan-out
# ---------------------------------------------------------------------------

BUS_COLOR = {"i2c": COL["i2c"], "spi": COL["spi"], "onewire": COL["onewire"], "gpio": COL["gpio"]}
BUS_NAME = {"i2c": "I2C-1", "spi": "SPI0", "onewire": "1-Wire", "gpio": "GPIO"}


def render_pins(rows: List[Dict[str, Any]], version: str, date: str) -> str:
    """Diagram 1: the 40-pin header, laid out like the real connector.

    Two columns of 20 pads; each pad's text grows *outward* (odd pins to the
    left, even pins to the right), so nothing overlaps.  Device cards sit to
    the right of the header and are linked back to the pins they consume.
    """
    W = 1900
    row_h = 24
    top = 168
    cards = _device_cards()
    header_h = 20 * row_h + 40
    cards_h = sum(36 + 18 * len(lines) + 14 for _, _, lines, _, _ in cards)
    H = int(top + max(header_h, cards_h) + 120)

    out = [svg_open(W, H, "Raspberry Pi 5 40-pin wiring diagram")]

    out.append(text(34, 48, "树莓派 5 · 40-pin 引脚接线图", 26, COL["ink"], weight="bold"))
    out.append(text(34, 76, "Raspberry Pi 5 Model B  |  居家老人健康监护系统", 13.5, COL["dim"]))
    out.append(text(34, 100, "由图生成脚本从 rpi/config/devices.json 与各驱动 describe() 读取，"
                             "非手写；改了配置请重跑 hardware/tools/gen_wiring_svg.py",
                    11, COL["dim"]))
    out.append(text(34, 118, f"版本 {version}  ·  生成日期 {date}", 11, COL["dim"]))

    # --- legend
    lx = 34
    for key, label in (("i2c", "I2C-1"), ("spi", "SPI0"), ("onewire", "单总线"),
                       ("gpio", "普通 GPIO")):
        out.append(rect(lx, 134, 15, 9, BUS_COLOR[key], rx=2))
        out.append(text(lx + 21, 143, label, 11, COL["dim"]))
        lx += 96
    for color, label in ((COL["pwr33"], "3.3V"), (COL["pwr5"], "5V"),
                         (COL["gnd"], "GND"), (COL["pin_idle"], "空闲/保留")):
        out.append(rect(lx, 134, 15, 9, color, rx=2))
        out.append(text(lx + 21, 143, label, 11, COL["dim"]))
        lx += 84

    # --- header board silhouette
    # The board must be wide enough to hold *both* columns' text inside it,
    # otherwise the outward-growing labels collide in the middle.
    board_left = 60
    board_right = 1240
    mid = (board_left + board_right) / 2
    left_cx = board_left + 330          # odd pins: text grows to the left
    right_cx = board_right - 330        # even pins: text grows to the right
    board_h = 20 * row_h + 40
    out.append(rect(board_left, top - 20, board_right - board_left, board_h,
                    COL["board"], COL["board_edge"], rx=14, sw=2))
    out.append(text(mid, top - 2, "J8  ·  40-pin 排针", 12, "#d8ede1",
                    anchor="middle", weight="bold"))

    pin_r = 7.5
    left_rows = rows[0::2]
    right_rows = rows[1::2]
    # remember each pin's y so the cards can be linked back to it
    pin_y: Dict[int, Tuple[float, float, str]] = {}

    for i, row in enumerate(left_rows):
        cy = top + 26 + i * row_h
        _pin_pad(out, row, left_cx, cy, pin_r, left=True)
        pin_y[row["phys"]] = (left_cx, cy, _pin_color(row))
    for i, row in enumerate(right_rows):
        cy = top + 26 + i * row_h
        _pin_pad(out, row, right_cx, cy, pin_r, left=False)
        pin_y[row["phys"]] = (right_cx, cy, _pin_color(row))

    # --- device cards + connector lines
    card_x = board_right + 90
    card_w = W - card_x - 44
    cy0 = top - 14
    for title, sub, lines, color, pins in cards:
        h = 36 + 18 * len(lines)
        out.append(rect(card_x, cy0, card_w, h, COL["card"], COL["card_edge"], rx=7))
        out.append(rect(card_x, cy0, 5, h, color, rx=2.5))
        out.append(text(card_x + 18, cy0 + 23, title, 13.5, COL["ink"], weight="bold"))
        if sub:
            out.append(text(card_x + card_w - 16, cy0 + 23, sub, 10.5, color, anchor="end"))
        for j, ln in enumerate(lines):
            out.append(text(card_x + 18, cy0 + 43 + j * 18, ln, 11, COL["dim"]))
        # link the first real pin of this card back to its pad.
        # The leader starts at the board's right edge so it never crosses the
        # even-column labels that live inside the board.
        anchor_y = cy0 + h / 2
        for phys in pins:
            if phys in pin_y:
                px, py, pcol = pin_y[phys]
                start_x = board_right
                out.append(path(
                    f"M {start_x} {py} L {card_x - 28} {py} "
                    f"L {card_x - 28} {anchor_y} L {card_x - 3} {anchor_y}",
                    pcol, 1.3, dash="4 3"))
                break
        cy0 += h + 14

    out.append("</svg>\n")
    return "".join(out)


def _pin_color(row: Dict[str, Any]) -> str:
    """Colour code for a pin, by whoever claims it."""
    if row["status"] == "power":
        return COL["pwr33"] if row["power"] == "3.3V" else COL["pwr5"]
    if row["status"] == "gnd":
        return COL["gnd"]
    if row["uses"]:
        return BUS_COLOR[row["uses"][0][1]]
    return COL["pin_idle"]


def _pin_pad(out: List[str], row: Dict[str, Any], cx: float, cy: float, r: float,
             left: bool) -> None:
    """One physical pin: pad + number + what it carries, text growing outward."""
    fill = _pin_color(row)
    out.append(dot(cx, cy, r, fill))

    # physical pin number, just outside the pad
    num_x = cx + (-15 if left else 15)
    out.append(text(num_x, cy + 4.5, str(row["phys"]), 12, COL["ink"],
                    anchor="end" if left else "start", weight="bold"))

    # what it carries: bcm + label, further out
    if row["power"]:
        what, detail = row["power"], ""
    elif row["uses"]:
        what = row["uses"][0][0]
        detail = f"GPIO{row['bcm']}" if row["bcm"] is not None else ""
        if len(row["uses"]) > 1:
            detail += ("  " if detail else "") + f"+{len(row['uses']) - 1} 共享"
    elif row["bcm"] is not None:
        what, detail = f"GPIO{row['bcm']}", "空闲"
    else:
        what, detail = "", ""

    text_x = num_x + (-26 if left else 26)
    anchor = "end" if left else "start"
    if what:
        out.append(text(text_x, cy + 4.5,
                        f"{what}  ·  {detail}" if detail else what,
                        11, COL["ink"] if row["uses"] or row["power"] else COL["dim"],
                        anchor=anchor))


def _device_cards() -> List[Tuple[str, str, List[str], str, List[int]]]:
    """Right-hand summary cards: (title, bus tag, lines, colour, anchor pins).

    The anchor pins are the physical pin numbers used to draw the dashed
    leader line back to the header.
    """
    return [
        ("MAX30102 心率血氧", "I2C-1 · 0x57", [
            "VIN → 脚 1（3.3V）⚠ 接 5V 会烧芯片",
            "GND → 脚 6",
            "SDA → 脚 3（GPIO2）    SCL → 脚 5（GPIO3）",
            "INT 悬空不接（本驱动轮询读 FIFO）",
            "配置 enabled=false：测量窗口内临时装配",
        ], COL["i2c"], [1, 3]),
        ("LCD1602 液晶（I2C 转接板）", "I2C-1 · 0x27/0x3F", [
            "VCC → 脚 2（5V）⚠ 3.3V 时对比度极低",
            "GND → 脚 9",
            "SDA → 脚 3    SCL → 脚 5（与 MAX30102 并挂）",
            "只认 ASCII：中文会花屏，文案一律用英文",
        ], COL["i2c"], [2, 5]),
        ("DHT11 温湿度", "单总线 · 3s", [
            "VCC → 脚 1（3.3V）⚠ 不要接 5V",
            "DATA → 脚 7（GPIO4）",
            "GND → 脚 14",
            "裸四针需 4k7~10k 上拉到 3.3V；间隔 ≥2s",
        ], COL["onewire"], [7]),
        ("TFT 彩屏 ST7735S 128×160", "SPI0.1 · CE1", [
            "VCC → 脚 1（3.3V）    GND → 脚 39",
            "SCK → 脚 23（GPIO11）  SDA/MOSI → 脚 19（GPIO10）",
            "CS → 脚 26（GPIO7 = SPI0 CE1）⚠ 独占片选",
            "DC/A0 → 脚 18（GPIO24）  RESET → 脚 22（GPIO25）",
            "背光 backlight_pin=-1 ⇒ 接 3.3V 常亮",
        ], COL["spi"], [26, 23]),
        ("HC-SR501 人体红外", "GPIO · 0.5s", [
            "VCC → 脚 2（5V）⚠ 必须 5V，3.3V 会误触发",
            "OUT → 脚 11（GPIO17）输出 3.3V，可直连",
            "GND → 脚 20",
            "上电热身约 1 分钟；部署建议 settle_s=60",
        ], COL["gpio"], [11]),
        ("蜂鸣器（有源）", "GPIO · 高=响", [
            "正极 ── 串 220Ω~1kΩ ── 脚 12（GPIO18）",
            "负极 → GND（脚 39）",
            "⚠ 无源型号本驱动不支持（需 PWM 方波）",
        ], COL["gpio"], [12]),
        ("LED 状态灯（绿 / 黄）", "GPIO · 高=亮", [
            "绿 ── 串 220Ω~1kΩ ── 脚 15（GPIO22）",
            "黄 ── 串 220Ω~1kΩ ── 脚 16（GPIO23）",
            "共阴（−）→ GND（脚 39）",
            "驱动保证任意时刻只亮一种颜色",
        ], COL["gpio"], [15]),
        ("按键 ×2（求救 / 测血氧）", "GPIO · 内部上拉", [
            "求救：脚 13（GPIO27）── 另一端 → GND（脚 25）",
            "测血氧：脚 33（GPIO13）── 另一端 → GND（脚 34）",
            "短按 <1.0s = 消音/翻页；长按 ≥1.0s = 触发",
        ], COL["gpio"], [13]),
    ]


# ---------------------------------------------------------------------------
# diagram 2 -- system schematic
# ---------------------------------------------------------------------------


def render_schematic(info: Dict[str, Dict[str, Any]], rows: List[Dict[str, Any]],
                     version: str, date: str) -> str:
    W = 1240
    out: List[str] = []

    # --- layout constants (kept together so the geometry stays checkable)
    bx, by, bw = 300, 150, 300
    dx = 720
    block_h = 46
    block_gap = 14
    blocks = [
        ("I2C-1", COL["i2c"], "GPIO2 / GPIO3（脚 3 / 5）"),
        ("SPI0", COL["spi"], "GPIO7/10/11 + DC24/RST25"),
        ("GPIO 输入", COL["gpio"], "GPIO4 / 17 / 27 / 13"),
        ("GPIO 输出", COL["gpio"], "GPIO18 / 22 / 23"),
        ("监护核心", "#34495e", "collector → rules → dispatcher"),
        ("网络 / 上云", "#2c6e9b", "web.py HTTP · MQTT → OneNET"),
    ]
    bh = 88 + len(blocks) * (block_h + block_gap) + 8
    block_y = {name: by + 88 + i * (block_h + block_gap) for i, (name, _, _) in enumerate(blocks)}
    block_mid = {name: y + block_h / 2 for name, y in block_y.items()}

    groups = [
        ("i2c", "I2C-1 总线（多从机并挂，靠地址区分）", COL["i2c"], [
            ("MAX30102 心率血氧", "0x57 · 3.3V 供电 ⚠", "默认 false，按需装配"),
            ("LCD1602 转接板 PCF8574", "0x27 / 0x3F · 5V 供电 ⚠", "本地英文文案显示"),
        ]),
        ("spi", "SPI0 总线（单从设备）", COL["spi"], [
            ("TFT 彩屏 ST7735S 128×160", "CS=CE1(脚26) 独占 · 3.3V", "环境页 / 报警页"),
        ]),
        ("onewire", "单总线 + 普通 GPIO", COL["onewire"], [
            ("DHT11 温湿度", "GPIO4 · 3.3V · 周期 3s", "环境温度 / 湿度"),
            ("HC-SR501 人体红外", "GPIO17 · 5V 供电 ⚠", "在床 / 活动检测"),
        ]),
        ("gpio_out", "输出器件（含限流元件）", COL["gpio"], [
            ("蜂鸣器（有源）", "GPIO18 · 串 220Ω~1kΩ ⚠", "报警鸣叫"),
            ("LED 绿 / 黄", "GPIO22 / GPIO23 · 各串 220Ω~1kΩ", "正常 / 注意"),
        ]),
        ("button", "输入按键", COL["gpio"], [
            ("求救键", "GPIO27 · 内部上拉 · 长按 ≥1s", "触发紧急求助"),
            ("测血氧键", "GPIO13 · 内部上拉", "按需启动测量窗口"),
        ]),
    ]

    # stack the right-hand cards, then size the canvas to the taller column
    card_top = by - 10
    gap_between_cards = 34
    group_y: Dict[str, float] = {}
    y = card_top
    for key, _title, _color, items in groups:
        group_y[key] = y
        y += 30 + 34 * len(items) + gap_between_cards
    cards_bottom = y - gap_between_cards + 16
    # notes sit right under whichever column is taller
    content_bottom = max(by + bh, cards_bottom)
    ny = int(content_bottom + 46)
    notes_h = 5 * 18 + 30
    H = int(ny + notes_h)

    out.append(svg_open(W, H, "Raspberry Pi health monitor schematic"))
    out.append(text(34, 48, "树莓派健康监护系统 · 原理框图", 25, COL["ink"], weight="bold"))
    out.append(text(34, 74, "供电轨 / 总线 / 限流元件一览（与 40-pin 接线图配套）", 13, COL["dim"]))
    out.append(text(34, 96, f"生成自 rpi/config/devices.json  ·  版本 {version}  ·  {date}",
                    11, COL["dim"]))

    # --- power rails down the left
    rail_x = 70
    rail_top = by
    rail_bottom = by + bh - 20
    out.append(rect(rail_x - 22, rail_top, 44, rail_bottom - rail_top,
                    "#fbfcfd", COL["card_edge"], rx=8))
    rails = [("3.3V", COL["pwr33"], "脚 1 / 17（合计 500 mA 上限）", 0.10),
             ("5V", COL["pwr5"], "脚 2 / 4（直通电源，无稳压）", 0.42),
             ("GND", COL["gnd"], "脚 6/9/14/20/25/30/34/39", 0.74)]
    for name, color, note, frac in rails:
        box_y = rail_top + (rail_bottom - rail_top - 26) * frac
        out.append(path(f"M {rail_x} {box_y} L {rail_x} {box_y + 70}", color, 5))
        out.append(rect(rail_x - 40, box_y, 80, 26, color, rx=4))
        out.append(text(rail_x, box_y + 18, name, 13, "#ffffff", anchor="middle", weight="bold"))
        out.append(text(rail_x + 50, box_y + 17, note, 10.5, COL["dim"]))

    # --- central board
    out.append(rect(bx, by, bw, bh, COL["board"], COL["board_edge"], rx=12, sw=2))
    out.append(text(bx + bw / 2, by + 34, "树莓派 5 Model B", 17, "#ffffff",
                    anchor="middle", weight="bold"))
    out.append(text(bx + bw / 2, by + 56, "BCM2712 · Debian 13 · Python 3", 11, "#bfe0cd",
                    anchor="middle"))
    out.append(path(f"M {bx+18} {by+70} L {bx+bw-18} {by+70}", "#5f9a7e", 1))

    for name, color, sub in blocks:
        dy = block_y[name] - by
        out.append(rect(bx + 18, by + dy, bw - 36, block_h, color, rx=6))
        out.append(text(bx + 32, by + dy + 20, name, 12.5, "#ffffff", weight="bold"))
        out.append(text(bx + 32, by + dy + 37, sub, 10, "#e6eef4"))

    # --- device cards on the right
    for key, title, color, items in groups:
        gy = group_y[key]
        h = 30 + 34 * len(items)
        out.append(rect(dx - 14, gy - 22, W - dx - 30, h + 10,
                        COL["card"], COL["card_edge"], rx=6))
        out.append(rect(dx - 14, gy - 22, 5, h + 10, color, rx=2))
        out.append(text(dx, gy - 4, title, 12.5, COL["ink"], weight="bold"))
        yy = gy + 22
        for nm, sub, role in items:
            out.append(dot(dx + 4, yy - 4, 4, color))
            out.append(text(dx + 16, yy, nm, 11.5, COL["ink"], weight="bold"))
            out.append(text(dx + 16, yy + 14, f"{sub}   ·   {role}", 10, COL["dim"]))
            yy += 34

    # --- bus links: from the matching block's centre-line to its card group
    def link(block: str, group_key: str, color: str) -> None:
        y0 = block_mid[block]
        y1 = group_y[group_key] + 10
        out.append(path(f"M {bx+bw} {y0:.0f} L {dx-34} {y0:.0f} "
                        f"L {dx-34} {y1:.0f} L {dx-19} {y1:.0f}", color, 2.4))

    link("I2C-1", "i2c", COL["i2c"])
    link("SPI0", "spi", COL["spi"])
    link("GPIO 输入", "onewire", COL["onewire"])
    link("GPIO 输出", "gpio_out", COL["gpio"])
    link("GPIO 输入", "button", COL["gpio"])

    # --- notes
    notes = [
        "⚠ 两条电源红线：红色轨 = 3.3V、橙色轨 = 5V，两条正轨绝不能短接；所有器件必须共地。",
        "⚠ MAX30102 / DHT11 / TFT 必须 3.3V；LCD1602 与 HC-SR501 必须 5V —— 接反的后果见 hardware/03。",
        "⚠ 每个 LED 与蜂鸣器的信号线上必须看到限流电阻（220Ω~1kΩ），否则可能烧 GPIO 引脚。",
        "⚠ SPI0 的 CS 固定用 CE1（脚 26），CE0（脚 24）空着不用；再加从设备请另占片选。",
        "ℹ 已废止并删除的器件（不再接线）：蓝牙音箱（板上无可发声输出）、HC-SR04（ECHO 为 5V 电平）、TMP36+MCP3002。",
    ]
    ny = int(max(by + bh, cards_bottom) + 46)
    for i, note in enumerate(notes):
        out.append(text(34, ny + i * 18, note, 11,
                        COL["dim"] if note.startswith("ℹ") else COL["warn"]))

    out.append("</svg>\n")
    return "".join(out)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def render_all() -> Dict[str, str]:
    import health_monitor

    version = getattr(health_monitor, "__version__", "?")
    date = _today()
    info = describe_all()
    rows = build_pin_rows(info)
    return {
        "接线图-引脚.svg": render_pins(rows, version, date),
        "接线图-原理.svg": render_schematic(info, rows, version, date),
    }


def _today() -> str:
    """Real local date -- never guessed (project rule 12)."""
    import datetime

    return datetime.date.today().isoformat()


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate hardware wiring SVGs")
    ap.add_argument("--outdir", default=str(REPO_ROOT / "hardware"))
    ap.add_argument("--check", action="store_true",
                    help="fail (exit 1) if the committed SVGs are out of date")
    args = ap.parse_args()

    outdir = Path(args.outdir)
    rendered = render_all()

    if args.check:
        stale = []
        for name, content in rendered.items():
            target = outdir / name
            if not target.exists():
                stale.append(f"{name}：缺失")
                continue
            if target.read_text(encoding="utf-8") != content:
                stale.append(f"{name}：与 devices.json / describe() 不一致（需重新生成）")
        if stale:
            for item in stale:
                print(f"[FAIL] {item}")
            print("修复： python hardware/tools/gen_wiring_svg.py")
            return 1
        print(f"[OK] {len(rendered)} 张接线图与代码一致")
        return 0

    outdir.mkdir(parents=True, exist_ok=True)
    for name, content in rendered.items():
        _write_lf(outdir / name, content)
        print(f"[OK] 写出 {outdir / name}（{len(content)} 字节）")
    return 0


def _write_lf(path: Path, content: str) -> None:
    """Write UTF-8 with **hard-coded LF**, regardless of platform.

    Why this matters (same family of bug as ERROR.md E32):
    ``Path.write_text`` opens in text mode, so on Windows every ``\\n`` becomes
    ``\\r\\n``.  The file would then differ from what a Linux CI generates from
    the *same* input, and ``--check`` would fail there while passing here --
    a "red only on the other machine" false alarm.  Pinning newline="\\n"
    makes the bytes identical everywhere.
    """
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)


if __name__ == "__main__":
    raise SystemExit(main())
