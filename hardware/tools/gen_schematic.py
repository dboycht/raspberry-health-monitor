#!/usr/bin/env python3
"""Generate the point-to-point schematic (SVG): every wire, both endpoints.

What "schematic" means here (and why the earlier diagram was not one)
--------------------------------------------------------------------
The first diagram was a *block* diagram: a card that says "VCC -> pin 2".
That tells you the pin number but never shows the device as a part with its
own pins, so you cannot follow a wire with your eye.

This module draws a real point-to-point schematic:

* the Raspberry Pi is a **symbol with 40 numbered pins** (two columns);
* every device is a **symbol with its own pin names** taken from the module
  silkscreen (MAX30102: VIN / GND / SCL / SDA / INT; HC-SR501: VCC / OUT / GND;
  the buzzer: + / -; ...);
* every connection is a **wire drawn from a Pi pin to a device pin**, with a
  net label (`SDA1`, `GPIO4`, `GND`, ...) and the series part (220R) shown
  inline where the project requires one.

Style: traditional black-and-white line art (no fill colours), so it prints
and photocopies cleanly and looks like what a marker expects from "原理图".

Pin numbers are read from ``devices.json`` and the drivers' ``describe()`` --
see ``gen_wiring_svg.py`` for the same anti-drift contract.  This file only
decides *where on the page* things go.

Usage::

    python hardware/tools/gen_schematic.py            # write hardware/原理图.svg
    python hardware/tools/gen_schematic.py --check    # fail if it is stale

ASCII-only source; all Chinese lives in the generated SVG.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
RPI_DIR = REPO_ROOT / "rpi"
if str(RPI_DIR) not in sys.path:
    sys.path.insert(0, str(RPI_DIR))

OUT_NAME = "原理图.svg"

INK = "#111111"
GRAY = "#8a8a8a"
LIGHT = "#d8d8d8"
NET = "#333333"
WARN = "#111111"


def esc(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------------------
# model: pins, nets, wires
# ---------------------------------------------------------------------------

#: Which Pi physical pin carries which net name (fixed function or GPIO name).
#: Derived from hal/pins.py roles -- NOT hand-invented numbers.
def net_name_for(phys: int, bcm: Optional[int], power: str) -> str:
    if power:
        return power
    fixed = {
        3: "SDA1", 5: "SCL1", 19: "MOSI", 21: "MISO", 23: "SCLK", 24: "CE0", 26: "CE1",
    }
    if phys in fixed:
        return fixed[phys]
    if bcm is not None:
        return f"GPIO{bcm}"
    return ""


@dataclass
class DevicePin:
    """One pin on a device symbol (the name printed on the module)."""
    name: str
    side: str          # "left" | "right"
    net: str           # net it connects to, "" = not connected
    phys: Optional[int] = None   # Pi physical pin it lands on
    series: str = ""   # series component drawn inline (e.g. "220R")
    target: str = ""   # draw the wire to this device instead of the Pi header


@dataclass
class Device:
    """A device symbol."""
    key: str
    title: str
    subtitle: str
    x: float
    y: float
    pins: List[DevicePin] = field(default_factory=list)
    note: str = ""

    @property
    def height(self) -> float:
        return max(64.0, 26 + 22 * len(self.pins) + 10)


def build_devices(info: Dict[str, dict]) -> List[Device]:
    """Place devices in two outer columns; their pins face inward (toward the Pi).

    ``DevicePin.side`` therefore means *which column the device is in*, not
    which edge the pin is drawn on: left column -> pins on the right edge.
    """
    def pin(key: str, info_key: str) -> Optional[int]:
        """Read a BCM pin number out of ``devices.json`` params (never hand-typed)."""
        params = (info.get(info_key, {}).get("params") or {})
        if key == "pin":
            return int(params["pin"]) if "pin" in params else None
        if key == "dc_pin":
            return int(params["dc_pin"]) if "dc_pin" in params else None
        if key == "reset_pin":
            return int(params["reset_pin"]) if "reset_pin" in params else None
        return None

    led = ((info.get("status_led", {}).get("params") or {}).get("pins") or {})

    LEFT_X, RIGHT_X = 34.0, 1298.0
    left: List[Device] = []
    left.append(Device(
        "vitals", "MAX30102", "心率血氧  I2C-1 0x57", LEFT_X, 150, [
            DevicePin("VIN", "left", "3.3V", 1),
            DevicePin("GND", "left", "GND", 6),
            DevicePin("SDA", "left", "SDA1", 3),
            DevicePin("SCL", "left", "SCL1", 5),
            DevicePin("INT", "left", "", None),
        ], "INT 悬空（驱动轮询读 FIFO）"))
    left.append(Device(
        "display", "LCD1602", "液晶  I2C 转接板 0x27/0x3F", LEFT_X, 340, [
            DevicePin("VCC", "left", "5V", 2),
            DevicePin("GND", "left", "GND", 9),
            DevicePin("SDA", "left", "SDA1", 3),
            DevicePin("SCL", "left", "SCL1", 5),
        ], "5V 供电（3.3V 时对比度极低）"))
    left.append(Device(
        "ambient", "DHT11", "温湿度  单总线", LEFT_X, 530, [
            DevicePin("VCC", "left", "3.3V", 1),
            DevicePin("DATA", "left", net_name_for(7, pin("pin", "ambient"), ""),
                      pin("pin", "ambient")),
            DevicePin("GND", "left", "GND", 14),
        ], "裸四针需 4k7~10k 上拉到 3.3V"))
    left.append(Device(
        "motion", "HC-SR501", "人体红外  PIR", LEFT_X, 710, [
            DevicePin("VCC", "left", "5V", 2),
            DevicePin("OUT", "left", net_name_for(11, pin("pin", "motion"), ""),
                      pin("pin", "motion")),
            DevicePin("GND", "left", "GND", 20),
        ], "必须 5V；OUT 为 3.3V 可直连"))

    right: List[Device] = []
    right.append(Device(
        "tft", "TFT ST7735S", "彩屏 128x160  SPI0.1", RIGHT_X, 150, [
            DevicePin("VCC", "right", "3.3V", 1),
            DevicePin("GND", "right", "GND", 39),
            DevicePin("SCK", "right", "SCLK", 23),
            DevicePin("SDA", "right", "MOSI", 19),
            DevicePin("CS", "right", "CE1", 26),
            DevicePin("DC", "right", net_name_for(18, pin("dc_pin", "tft"), ""),
                      pin("dc_pin", "tft")),
            DevicePin("RST", "right", net_name_for(22, pin("reset_pin", "tft"), ""),
                      pin("reset_pin", "tft")),
        ], "CS 独占 CE1；背光接 3.3V 常亮"))
    right.append(Device(
        "alarm_buzzer", "蜂鸣器", "有源  高电平响", RIGHT_X, 430, [
            DevicePin("+", "right", net_name_for(12, pin("pin", "alarm_buzzer"), ""),
                      pin("pin", "alarm_buzzer"), series="220R"),
            DevicePin("-", "right", "GND", 39),
        ], "必须串 220R~1k 限流"))
    right.append(Device(
        "status_led", "LED 绿 / 黄", "状态灯  共阴", RIGHT_X, 550, [
            DevicePin("绿 an", "right", net_name_for(15, led.get("green"), ""),
                      led.get("green"), series="220R"),
            DevicePin("黄 an", "right", net_name_for(16, led.get("yellow"), ""),
                      led.get("yellow"), series="220R"),
            DevicePin("共阴", "right", "GND", 39),
        ], "任意时刻只亮一种颜色"))
    right.append(Device(
        "sos_button", "求救键", "长按 >=1s 触发", RIGHT_X, 730, [
            DevicePin("A", "right", net_name_for(13, pin("pin", "sos_button"), ""),
                      pin("pin", "sos_button")),
            DevicePin("B", "right", "GND", 25),
        ], "内部上拉，按下读到低"))
    right.append(Device(
        "spo2_button", "测血氧键", "按需启动测量", RIGHT_X, 850, [
            DevicePin("A", "right", net_name_for(33, pin("pin", "spo2_button"), ""),
                      pin("pin", "spo2_button")),
            DevicePin("B", "right", "GND", 34),
        ], ""))
    return left + right


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

PIN_PITCH = 20.0
PI_W = 150.0
DEV_W = 168.0


def svg_open(w: int, h: int, title: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
        f'viewBox="0 0 {w} {h}" font-family="Consolas, Microsoft YaHei, monospace">\n'
        f"<title>{esc(title)}</title>\n"
        f'<rect width="{w}" height="{h}" fill="#ffffff"/>\n'
    )


def line(x1: float, y1: float, x2: float, y2: float, w: float = 1.2,
         color: str = INK, dash: str = "") -> str:
    d = f' stroke-dasharray="{dash}"' if dash else ""
    return (f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
            f'stroke="{color}" stroke-width="{w}"{d}/>\n')


def poly(points: List[Tuple[float, float]], w: float = 1.2,
         color: str = INK, dash: str = "") -> str:
    d = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    da = f' stroke-dasharray="{dash}"' if dash else ""
    return (f'<polyline points="{d}" fill="none" stroke="{color}" '
            f'stroke-width="{w}" stroke-linejoin="round"{da}/>\n')


def box(x: float, y: float, w: float, h: float, sw: float = 1.4) -> str:
    return (f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" '
            f'fill="none" stroke="{INK}" stroke-width="{sw}"/>\n')


def txt(x: float, y: float, s: str, size: float = 12, color: str = INK,
        anchor: str = "start", weight: str = "normal", style: str = "") -> str:
    st = f' font-style="{style}"' if style else ""
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{color}" '
            f'text-anchor="{anchor}" font-weight="{weight}"{st}>{esc(s)}</text>\n')


def junction(x: float, y: float) -> str:
    """A filled dot = wires joined here (standard schematic notation)."""
    return f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{INK}"/>\n'


def net_label(x: float, y: float, name: str, anchor: str = "middle") -> str:
    """A net label sitting on the wire, with a white halo so the wire does not
    strike through the text."""
    w = 7.0 * len(name) + 6
    left = x - w / 2 if anchor == "middle" else (x - w if anchor == "end" else x)
    return (
        f'<rect x="{left:.1f}" y="{y - 11:.1f}" width="{w:.1f}" height="14" fill="#ffffff"/>\n'
        + txt(x, y, name, 10, NET, anchor=anchor)
    )


def render_schematic(info: Dict[str, dict], version: str) -> str:
    devices = build_devices(info)

    W = 1500
    pi_w = 190.0
    pi_x = (W - pi_w) / 2
    pi_top = 130.0
    pi_h = 20 * PIN_PITCH + 56
    H = int(max(pi_top + pi_h,
                max(d.y + d.height for d in devices)) + 250)

    out = [svg_open(W, H, "Raspberry Pi health monitor schematic")]

    # ---------------- title block
    out.append(txt(44, 50, "树莓派健康监护系统 · 原理图（端口对端口）", 23, INK, weight="bold"))
    out.append(txt(44, 74, "Raspberry Pi 5 Model B  |  居家老人健康监护系统 · 课程设计",
                   12, NET))
    out.append(txt(44, 92, f"引脚号由 rpi/config/devices.json 与各驱动 describe() 读出"
                           f"  ·  版本 {version}", 10.5, GRAY))
    out.append(line(44, 106, W - 44, 106, 1.0, LIGHT))

    # ---------------- the Pi symbol: 40 numbered pins, two columns
    out.append(box(pi_x, pi_top, pi_w, pi_h, 2.0))
    out.append(txt(pi_x + pi_w / 2, pi_top + 26, "树莓派 5", 16, INK,
                   anchor="middle", weight="bold"))
    out.append(txt(pi_x + pi_w / 2, pi_top + 44, "40-pin 排针", 11, NET, anchor="middle"))

    from health_monitor.hal.pins import BCM_TO_PHYSICAL

    phys_to_bcm = {v: k for k, v in BCM_TO_PHYSICAL.items()}
    power_of = {1: "3.3V", 17: "3.3V", 2: "5V", 4: "5V",
                6: "GND", 9: "GND", 14: "GND", 20: "GND",
                25: "GND", 30: "GND", 34: "GND", 39: "GND"}

    def pin_y(phys: int) -> float:
        """Odd pins down the left column, even down the right -- like the real
        connector, so the drawing can be read against the board."""
        return pi_top + 62 + ((phys - 1) // 2) * PIN_PITCH

    pi_pin_anchor: Dict[int, Tuple[float, float, str]] = {}
    stub = 16.0
    for phys in range(1, 41):
        bcm = phys_to_bcm.get(phys)
        y = pin_y(phys)
        is_left = (phys % 2 == 1)
        ax = (pi_x - stub) if is_left else (pi_x + pi_w + stub)
        out.append(line(pi_x if is_left else pi_x + pi_w, y, ax, y, 1.2))
        num_x = (pi_x - 6) if is_left else (pi_x + pi_w + 6)
        out.append(txt(num_x, y + 4, str(phys), 10.5, INK,
                       anchor="end" if is_left else "start", weight="bold"))
        pi_pin_anchor[phys] = (ax, y, net_name_for(phys, bcm, power_of.get(phys, "")))

    # ---------------- device symbols
    # Devices sit in two outer columns and their pins face INWARD, so every
    # wire goes straight toward the Pi instead of crossing the whole sheet.
    dev_anchor: Dict[str, Dict[str, Tuple[float, float]]] = {}
    lead = 20.0

    for d in devices:
        h = d.height
        out.append(box(d.x, d.y, DEV_W, h, 1.6))
        out.append(txt(d.x + DEV_W / 2, d.y + 22, d.title, 13.5, INK,
                       anchor="middle", weight="bold"))
        out.append(txt(d.x + DEV_W / 2, d.y + 38, d.subtitle, 9.5, NET, anchor="middle"))
        out.append(line(d.x + 10, d.y + 46, d.x + DEV_W - 10, d.y + 46, 0.8, LIGHT))
        if d.note:
            out.append(txt(d.x + DEV_W / 2, d.y + h - 6, d.note, 9, GRAY, anchor="middle"))

        anchors: Dict[str, Tuple[float, float]] = {}
        for i, p in enumerate(d.pins):
            py = d.y + 62 + i * 22
            inward = (p.side == "left")          # left column -> pins on right edge
            if inward:
                ex = d.x + DEV_W + lead
                out.append(line(d.x + DEV_W, py, ex, py, 1.2))
                out.append(txt(d.x + DEV_W - 8, py + 4, p.name, 11, INK, anchor="end"))
            else:
                ex = d.x - lead
                out.append(line(d.x, py, ex, py, 1.2))
                out.append(txt(d.x + 8, py + 4, p.name, 11, INK))
            anchors[p.name] = (ex, py)
        dev_anchor[d.key] = anchors

    wires: List[str] = []

    def draw_signal(dev: Device, p: DevicePin) -> None:
        """One wire from a device pin to its Pi pin: out, across, in.

        The vertical channel must sit in the GAP between the device column and
        the Pi symbol -- if it lands under the Pi box the wire is drawn across
        the processor symbol, which reads as a short circuit.
        """
        if p.phys is None or p.phys not in pi_pin_anchor:
            return
        ax, ay = dev_anchor[dev.key][p.name]
        px, py, _ = pi_pin_anchor[p.phys]
        left_side = dev.x < pi_x
        # channel: 30% of the way from the device edge toward the Pi pin
        if left_side:
            gap_lo, gap_hi = ax, pi_x - 34
        else:
            gap_lo, gap_hi = pi_x + pi_w + 34, ax
        chan = gap_lo + (gap_hi - gap_lo) * 0.35
        wires.append(poly([(ax, ay), (chan, ay), (chan, py), (px, py)], 1.3))

        # series resistor sits right next to the pin; the net label goes further
        # out on the same horizontal run so the two never overlap
        lbl_x = ax + (14 if left_side else -14)
        if p.series:
            rx = ax + (30 if left_side else -30)
            wires.append(
                f'<rect x="{rx - 12:.1f}" y="{ay - 6.5:.1f}" width="24" height="13" '
                f'fill="#ffffff" stroke="{INK}" stroke-width="1.2"/>\n')
            wires.append(txt(rx, ay + 4, p.series, 8.5, INK, anchor="middle"))
            lbl_x = ax + (52 if left_side else -52)
        wires.append(net_label(lbl_x, ay - 5, p.net,
                               anchor="start" if left_side else "end"))

    for d in devices:
        for p in d.pins:
            if p.net in ("3.3V", "5V", "GND") or not p.net:
                continue
            draw_signal(d, p)

    out.extend(wires)

    # ---------------- power / ground: standard "net label on a stub" notation
    # A real schematic does NOT run one long wire from the Pi to every device
    # for 3.3V/5V/GND -- it puts a named stub at each pin ("3.3V", "GND") and the
    # reader knows same-named nets are joined.  We follow that: it keeps the
    # drawing readable and matches what a marker expects.
    def power_stub(dev: Device, p: DevicePin, inward_side: bool) -> None:
        ax, ay = dev_anchor[dev.key][p.name]
        dirn = 1 if inward_side else -1
        # 34px of stub keeps the label clear of the device body outline
        out.append(poly([(ax, ay), (ax + dirn * 34, ay)], 1.3))
        out.append(net_label(ax + dirn * 42, ay - 5, p.net,
                             anchor="start" if inward_side else "end"))

    for d in devices:
        left_col = d.x < pi_x
        for p in d.pins:
            if p.net in ("3.3V", "5V", "GND"):
                power_stub(d, p, inward_side=left_col)

    # Pi side: label only the power/ground pins that some device actually uses,
    # otherwise the symbol is buried under a wall of repeated "GND" labels.
    used_power: Dict[str, int] = {}
    for d in devices:
        for p in d.pins:
            if p.net in ("3.3V", "5V", "GND") and p.phys:
                used_power.setdefault(p.net, p.phys)

    for name, phys in used_power.items():
        ax, ay, _ = pi_pin_anchor[phys]
        outward = -1 if (phys % 2 == 1) else 1
        out.append(net_label(ax + outward * 14, ay - 5, name,
                             anchor="end" if outward < 0 else "start"))

    # ---------------- legend: how to read the power notation
    leg_y = pi_top + pi_h + 34
    out.append(txt(pi_x + pi_w / 2, leg_y,
                   "3.3V / 5V / GND 为同名网络：实物接面包板同一条电源轨（不逐根画线）",
                   10, GRAY, anchor="middle"))

    # ---------------- notes
    notes = [
        "注 1  3.3V / 5V / GND 用「同名网络」标注（每个引脚一根短线带网名）：实物上它们接面包板同一条电源轨，规范画法不逐根连线。",
        "注 2  ⚠ 3.3V 器件：MAX30102 / DHT11 / TFT；5V 器件：LCD1602 / HC-SR501。接错会烧器件或对比度极低。",
        "注 3  ⚠ 蜂鸣器与两只 LED 的信号线上必须串 220Ω~1kΩ 限流电阻（图中方框），否则可能烧 GPIO 引脚。",
        "注 4  I2C-1 为多从机总线（MAX30102 0x57 与 LCD 转接板 0x27/0x3F 并挂同一条 SDA1/SCL1）。",
        "注 5  SPI0 只挂 TFT 一个从设备，片选独占 CE1（物理脚 26）；CE0（脚 24）空着不用。",
        "注 6  MAX30102 的 INT 悬空不接；按键另一端接 GND（驱动启用内部上拉，按下读到低电平）。",
        "注 7  方框内左列数字为树莓派物理脚号（插线看它），网名里的 GPIOxx 为 BCM 编号（写代码看它）。",
    ]
    ny = H - 120
    for i, n in enumerate(notes):
        out.append(txt(44, ny + i * 17, n, 10.5, NET))

    out.append("</svg>\n")
    return "".join(out)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def _describe_all() -> Dict[str, dict]:
    from health_monitor.hal.registry import create_device

    cfg = json.loads((RPI_DIR / "config" / "devices.json").read_text(encoding="utf-8"))
    out: Dict[str, dict] = {}
    for name, spec in cfg["devices"].items():
        dev = create_device(spec["driver"], spec.get("params", {}), mock=True, name=name)
        out[name] = {"driver": spec["driver"], "params": spec.get("params", {}),
                     "pins": dev.describe().get("pins", {})}
    return out


def render_all() -> Dict[str, str]:
    """Render the schematic from config. **No wall-clock input.**

    A date stamped into the drawing made ``--check`` fail the very next day
    (CI #124, 2026-10-29) although every pin was correct -- the gate compares
    bytes, and "today" is a different byte tomorrow.  The build time is
    recorded by git, which is the right place for it.
    """
    import health_monitor

    return {OUT_NAME: render_schematic(_describe_all(),
                                       getattr(health_monitor, "__version__", "?"))}


def _write_lf(path: Path, content: str) -> None:
    """LF on every platform -- otherwise Windows and CI bytes differ and
    ``--check`` goes red only on CI (see ERROR.md E90)."""
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate the point-to-point schematic")
    ap.add_argument("--outdir", default=str(REPO_ROOT / "hardware"))
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    outdir = Path(args.outdir)
    rendered = render_all()

    if args.check:
        stale = []
        for name, content in rendered.items():
            target = outdir / name
            if not target.exists():
                stale.append(f"{name}：缺失")
            elif target.read_text(encoding="utf-8") != content:
                stale.append(f"{name}：与 devices.json / describe() 不一致（需重新生成）")
        if stale:
            for item in stale:
                print(f"[FAIL] {item}")
            print("修复： python hardware/tools/gen_schematic.py")
            return 1
        print(f"[OK] {len(rendered)} 张原理图与代码一致")
        return 0

    outdir.mkdir(parents=True, exist_ok=True)
    for name, content in rendered.items():
        _write_lf(outdir / name, content)
        print(f"[OK] 写出 {outdir / name}（{len(content)} 字节）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
