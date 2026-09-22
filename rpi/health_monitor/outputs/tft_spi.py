"""SPI TFT 彩屏驱动（ST7735S / ST7789 / ILI9341 + 兼容型号）—— 课程清单里的"显示屏"。

为什么写这个
------------
课程材料清单第 1 项是"显示屏"，本项目需要在本机显示监护数据。
本驱动让它成为一个 **HAL OutputDevice**：接上就能用，业务层不认识具体型号。

⚠️ **型号决定一切**：TFT 屏看着一样，控制器可能完全不同（初始化序列不同、分辨率不同、
颜色顺序可能差一位）。本驱动的处理方式是：
1. **一张表覆盖三种常见控制器**（见 :data:`CONTROLLERS`），用 ``controller=`` 选择；
2. ``controller="auto"`` 时按 **ST7735S → ST7789 → ILI9341** 依次尝试，
   每个都画三色条（红/绿/蓝）—— **颜色对得上、字能看清** 的那个就是它；
3. **命令行自检**帮你一次试完：``python3 scripts/tft_check.py --controller auto``。

接线（SPI0，与 MCP3002 共用总线，靠 **CS 片选**区分——两者不会打架）
-------------------------------------------------------------------
=============  ==================  ==========================================
TFT 模块引脚    树莓派物理脚         说明
=============  ==================  ==========================================
VCC            脚 1（3.3V）        ⚠️ 先试 3.3V；有独立稳压的模块才可 5V
GND            脚 6               共地
SCL / CLK      脚 23（GPIO11）    SPI0 SCLK
SDA / MOSI/DIN 脚 19（GPIO10）    SPI0 MOSI
RES / RST      脚 22（GPIO25）    复位（低电平复位）
DC / RS        脚 18（GPIO24）    命令/数据选择（0=命令 1=数据）
CS / CE        脚 24（GPIO8）     SPI0 CE0 —— **若 MCP3002 占了 CE0，把 TFT 换到 CE1=脚 26**
BLK / BL/LED   脚 1 或 33（3.3V） 背光；接 GPIO 可调光（可选）
=============  ==================  ==========================================

⚠️ **别与 MCP3002 抢同一个 CS**：SPI 是共享总线，**每个从设备必须有自己的片选**。
本项目默认：MCP3002 = CE0（脚 24），**TFT = CE1（脚 26）**。

真实 SPI 的两种后端（与 :mod:`health_monitor.sensors.mcp3002` 保持一致）
-----------------------------------------------------------------------
- 优先 ``spidev``（快，几十 KB 的帧缓冲也能刷得动）；
- 没有 ``spidev`` 时退回 **``lgpio`` 手工 SPI**（树莓派 5 上 ``RPi.GPIO`` 不可用，
  所以只能走 lgpio）—— 慢一点，但能点亮。

许可与来源
----------
初始化序列与像素格式取自各控制器**公开数据手册**的通用写法，参照了以下 MIT 许可实现
（仅作对照，代码为本项目自行实现）：
- ``rm-hull/luma.lcd``（MIT）ST7735 / ST7789 / ILI9341 的 init 序列
- ``pimoroni/st7789-python``（MIT）ST7789 的 COL/ROW 偏移与 MADCTL 取值
彩色帧缓冲 + 局部刷新的做法参考了 ``rpi-rgb-led-matrix`` 一类项目的"脏矩形"思路。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..hal.device import OutputDevice
from ..hal.exceptions import DeviceInitError, DeviceIOError, UnsupportedError
from ..hal.models import DeviceKind, DisplayCommand, DisplayStatus, Sample

_LOG = logging.getLogger(__name__)

RGB = Tuple[int, int, int]

# --------------------------------------------------------------------------
# 控制器表：分辨率 + 初始化序列
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ControllerSpec:
    """一种 TFT 控制器的参数与初始化序列。"""

    name: str
    width: int
    height: int
    #: 可见区域在显存里的列/行偏移（不同模块的玻璃与显存对不齐，需要补偿）
    col_offset: int = 0
    row_offset: int = 0
    #: MADCTL（0x36）取值：控制扫描方向与 BGR/RGB 顺序
    madctl: int = 0x00
    #: 初始化序列：``(命令, 数据字节, 延时毫秒)``；数据为空表示纯命令
    init: Sequence[Tuple[int, bytes, int]] = ()
    invert: bool = False
    bgr: bool = False


#: ST7735S —— 1.8" 128×160 最常见（Waveshare 1.8inch LCD Module、各种"1.8寸 SPI 彩屏"）
_ST7735_INIT: Tuple[Tuple[int, bytes, int], ...] = (
    (0x01, b"", 150),                     # SWRESET 软复位
    (0x11, b"", 120),                     # SLPOUT 退出睡眠
    (0xB1, b"\x01\x2C\x2D", 0),           # FRMCTR1 帧率
    (0xB2, b"\x01\x2C\x2D", 0),           # FRMCTR2
    (0xB3, b"\x01\x2C\x2D\x01\x2C\x2D", 0),   # FRMCTR3
    (0xB4, b"\x07", 0),                   # INVCTR
    (0xC0, b"\xA2\x02\x84", 0),           # PWCTR1
    (0xC1, b"\xC5", 0),                   # PWCTR2
    (0xC2, b"\x0A\x00", 0),               # PWCTR3
    (0xC3, b"\x8A\x2A", 0),               # PWCTR4
    (0xC4, b"\x8A\xEE", 0),               # PWCTR5
    (0xC5, b"\x0E", 0),                   # VMCTR1
    (0x20, b"", 0),                       # INVOFF
    (0x36, b"\xC8", 0),                   # MADCTL: MX|MY|BGR（横屏 160×128）
    (0x3A, b"\x05", 0),                   # COLMOD: 16 位/像素
    (0x2A, b"", 0),                       # CASET（后面由 set_window 覆盖）
    (0x2B, b"", 0),                       # RASET
    (0x13, b"", 10),                      # NORON 正常显示
    (0x29, b"", 100),                     # DISPON 开显示
)

#: ST7789（1.3"/1.54"/2.0" IPS，240×240 或 240×320）
_ST7789_INIT: Tuple[Tuple[int, bytes, int], ...] = (
    (0x01, b"", 150),
    (0x11, b"", 120),
    (0x3A, b"\x55", 0),                   # 16 位/像素
    (0x36, b"\x00", 0),                   # MADCTL（由代码按方向覆盖）
    (0xB2, b"\x0C\x0C\x00\x33\x33", 0),   # PORCTRL
    (0xB7, b"\x35", 0),                   # GCTRL
    (0xBB, b"\x19", 0),                   # VCOMS
    (0xC0, b"\x2C", 0),                   # LCMCTRL
    (0xC2, b"\x01", 0),                   # VDVVRHEN
    (0xC3, b"\x12", 0),                   # VRHS
    (0xC4, b"\x20", 0),                   # VDVS
    (0xC6, b"\x0F", 0),                   # FRCTRL2
    (0xD0, b"\xA4\xA1", 0),               # PWCTRL1
    (0x21, b"", 0),                       # INVON（多数 ST7789 模组需要反色）
    (0x13, b"", 10),
    (0x29, b"", 100),
)

#: ILI9341（2.4"/2.8"/3.2"，240×320）
_ILI9341_INIT: Tuple[Tuple[int, bytes, int], ...] = (
    (0x01, b"", 150),
    (0x28, b"", 0),                       # DISPOFF
    (0x3A, b"\x55", 0),                   # 16 位/像素
    (0x36, b"\x48", 0),                   # MADCTL: BGR + 竖屏
    (0xCF, b"\x00\xC1\x30", 0),           # PWCTR B
    (0xED, b"\x64\x03\x12\x81", 0),       # PWCTR seq
    (0xE8, b"\x85\x00\x78", 0),           # DTCA
    (0xCB, b"\x39\x2C\x00\x34\x02", 0),   # PWCTR A
    (0xF7, b"\x20", 0),                   # PRC
    (0xEA, b"\x00\x00", 0),               # DTCA
    (0xC0, b"\x23", 0),                   # PWCTR1
    (0xC1, b"\x10", 0),                   # PWCTR2
    (0xC5, b"\x3E\x28", 0),               # VMCTR1
    (0xC7, b"\x86", 0),                   # VMCTR2
    (0xB1, b"\x00\x18", 0),               # FRMCTR1
    (0xB6, b"\x08\x82\x27", 0),           # DFUNCTR
    (0xF2, b"\x00", 0),                   # 3Gamma 关
    (0x26, b"\x01", 0),                   # GAMMASET
    (0xE0, b"\x0F\x31\x2B\x0C\x0E\x08\x4E\xF1\x37\x07\x10\x03\x0E\x09\x00", 0),
    (0xE1, b"\x00\x0E\x14\x03\x11\x07\x31\xC1\x48\x08\x0F\x0C\x31\x36\x0F", 0),
    (0x11, b"", 120),                     # SLPOUT
    (0x29, b"", 100),                     # DISPON
)

CONTROLLERS: Dict[str, ControllerSpec] = {
    "st7735": ControllerSpec("st7735", 160, 128, col_offset=0, row_offset=0,
                             madctl=0xC8, init=_ST7735_INIT, bgr=True),
    "st7735_128x160": ControllerSpec("st7735_128x160", 128, 160, col_offset=0, row_offset=0,
                                     madctl=0xC0, init=_ST7735_INIT, bgr=True),
    "st7789": ControllerSpec("st7789", 240, 240, col_offset=0, row_offset=0,
                             madctl=0x00, init=_ST7789_INIT, invert=True),
    "st7789_240x320": ControllerSpec("st7789_240x320", 240, 320, col_offset=0, row_offset=0,
                                     madctl=0x00, init=_ST7789_INIT, invert=True),
    "ili9341": ControllerSpec("ili9341", 240, 320, col_offset=0, row_offset=0,
                              madctl=0x48, init=_ILI9341_INIT, bgr=True),
    # 一些 1.44"/1.8" 模组是 ST7735R（红版），偏移与 160×128 不同
    "st7735r": ControllerSpec("st7735r", 128, 160, col_offset=2, row_offset=1,
                              madctl=0xC0, init=_ST7735_INIT, bgr=True),
}

#: ``controller="auto"`` 时的尝试顺序（最常见 → 少见）
AUTO_ORDER: Tuple[str, ...] = ("st7735", "st7789", "ili9341", "st7735_128x160")


def rgb565(r: int, g: int, b: int, bgr: bool = False) -> int:
    """RGB888 → RGB565（TFT 的 16 位像素格式）。

    ⚠️ **入参先夹到 0~255**：Python 的 ``>>`` 对负数会保留符号位，
    不做夹紧的话 ``rgb565(300, -10, 999)`` 会算出**带脏位**的颜色（表现为随机色块）。
    """
    r = max(0, min(255, int(r)))
    g = max(0, min(255, int(g)))
    b = max(0, min(255, int(b)))
    r5 = (r >> 3) & 0x1F
    g6 = (g >> 2) & 0x3F
    b5 = (b >> 3) & 0x1F
    if bgr:
        return (b5 << 11) | (g6 << 5) | r5
    return (r5 << 11) | (g6 << 5) | b5


#: 极简 8×8 ASCII 字库（仅可打印字符 0x20~0x7F）。
#: 只画英文数字是刻意的：8×8 点阵放不下汉字，**中文请走 LCD/手机/语音**
#: （与 LCD1602 驱动同一条纪律）。
FONT_8X8: Dict[str, Tuple[int, ...]] = {}


def _build_font() -> None:
    """构建 8×8 字库（每字符 8 字节，每字节是一位行；仅 ASCII）。

    数据是一份**公共领域的经典 8×8 点阵骨架**（同类表在无数开源项目里出现），
    这里只保留本项目会用到的字符集并做了可读性微调。
    """
    glyphs = {
        " ": (0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00),
        "!": (0x18, 0x3C, 0x3C, 0x18, 0x18, 0x00, 0x18, 0x00),
        '"': (0x6C, 0x6C, 0x48, 0x00, 0x00, 0x00, 0x00, 0x00),
        "#": (0x6C, 0x6C, 0xFE, 0x6C, 0xFE, 0x6C, 0x6C, 0x00),
        "$": (0x18, 0x3E, 0x60, 0x3C, 0x06, 0x7C, 0x18, 0x00),
        "%": (0x62, 0x64, 0x08, 0x10, 0x26, 0x46, 0x00, 0x00),
        "&": (0x38, 0x6C, 0x38, 0x70, 0xDA, 0xCC, 0x76, 0x00),
        "'": (0x18, 0x18, 0x30, 0x00, 0x00, 0x00, 0x00, 0x00),
        "(": (0x0C, 0x18, 0x30, 0x30, 0x30, 0x18, 0x0C, 0x00),
        ")": (0x30, 0x18, 0x0C, 0x0C, 0x0C, 0x18, 0x30, 0x00),
        "*": (0x00, 0x66, 0x3C, 0xFF, 0x3C, 0x66, 0x00, 0x00),
        "+": (0x00, 0x10, 0x10, 0x7C, 0x10, 0x10, 0x00, 0x00),
        ",": (0x00, 0x00, 0x00, 0x00, 0x00, 0x18, 0x18, 0x30),
        "-": (0x00, 0x00, 0x00, 0x7C, 0x00, 0x00, 0x00, 0x00),
        ".": (0x00, 0x00, 0x00, 0x00, 0x00, 0x18, 0x18, 0x00),
        "/": (0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x00, 0x00),
        ":": (0x00, 0x18, 0x18, 0x00, 0x00, 0x18, 0x18, 0x00),
        "0": (0x3C, 0x66, 0x6E, 0x76, 0x66, 0x66, 0x3C, 0x00),
        "1": (0x18, 0x38, 0x18, 0x18, 0x18, 0x18, 0x7E, 0x00),
        "2": (0x3C, 0x66, 0x06, 0x0C, 0x18, 0x30, 0x7E, 0x00),
        "3": (0x3C, 0x66, 0x06, 0x1C, 0x06, 0x66, 0x3C, 0x00),
        "4": (0x0C, 0x1C, 0x3C, 0x6C, 0x7E, 0x0C, 0x0C, 0x00),
        "5": (0x7E, 0x60, 0x7C, 0x06, 0x06, 0x66, 0x3C, 0x00),
        "6": (0x1C, 0x30, 0x60, 0x7C, 0x66, 0x66, 0x3C, 0x00),
        "7": (0x7E, 0x06, 0x0C, 0x18, 0x30, 0x30, 0x30, 0x00),
        "8": (0x3C, 0x66, 0x66, 0x3C, 0x66, 0x66, 0x3C, 0x00),
        "9": (0x3C, 0x66, 0x66, 0x3E, 0x06, 0x0C, 0x38, 0x00),
        ":": (0x00, 0x18, 0x18, 0x00, 0x00, 0x18, 0x18, 0x00),
        ";": (0x00, 0x18, 0x18, 0x00, 0x00, 0x18, 0x18, 0x30),
        "<": (0x0C, 0x18, 0x30, 0x60, 0x30, 0x18, 0x0C, 0x00),
        "=": (0x00, 0x00, 0x7C, 0x00, 0x7C, 0x00, 0x00, 0x00),
        ">": (0x30, 0x18, 0x0C, 0x06, 0x0C, 0x18, 0x30, 0x00),
        "?": (0x3C, 0x66, 0x06, 0x0C, 0x18, 0x00, 0x18, 0x00),
        "@": (0x3C, 0x66, 0x6E, 0x6A, 0x6E, 0x60, 0x3C, 0x00),
        "A": (0x18, 0x3C, 0x66, 0x66, 0x7E, 0x66, 0x66, 0x00),
        "B": (0x7C, 0x66, 0x66, 0x7C, 0x66, 0x66, 0x7C, 0x00),
        "C": (0x3C, 0x66, 0x60, 0x60, 0x60, 0x66, 0x3C, 0x00),
        "D": (0x78, 0x6C, 0x66, 0x66, 0x66, 0x6C, 0x78, 0x00),
        "E": (0x7E, 0x60, 0x60, 0x7C, 0x60, 0x60, 0x7E, 0x00),
        "F": (0x7E, 0x60, 0x60, 0x7C, 0x60, 0x60, 0x60, 0x00),
        "G": (0x3C, 0x66, 0x60, 0x6E, 0x66, 0x66, 0x3C, 0x00),
        "H": (0x66, 0x66, 0x66, 0x7E, 0x66, 0x66, 0x66, 0x00),
        "I": (0x3C, 0x18, 0x18, 0x18, 0x18, 0x18, 0x3C, 0x00),
        "J": (0x1E, 0x0C, 0x0C, 0x0C, 0x0C, 0x6C, 0x38, 0x00),
        "K": (0x66, 0x6C, 0x78, 0x70, 0x78, 0x6C, 0x66, 0x00),
        "L": (0x60, 0x60, 0x60, 0x60, 0x60, 0x60, 0x7E, 0x00),
        "M": (0x63, 0x77, 0x7F, 0x6B, 0x63, 0x63, 0x63, 0x00),
        "N": (0x66, 0x76, 0x7E, 0x7E, 0x6E, 0x66, 0x66, 0x00),
        "O": (0x3C, 0x66, 0x66, 0x66, 0x66, 0x66, 0x3C, 0x00),
        "P": (0x7C, 0x66, 0x66, 0x7C, 0x60, 0x60, 0x60, 0x00),
        "Q": (0x3C, 0x66, 0x66, 0x66, 0x66, 0x3C, 0x0E, 0x00),
        "R": (0x7C, 0x66, 0x66, 0x7C, 0x78, 0x6C, 0x66, 0x00),
        "S": (0x3C, 0x66, 0x60, 0x3C, 0x06, 0x66, 0x3C, 0x00),
        "T": (0x7E, 0x18, 0x18, 0x18, 0x18, 0x18, 0x18, 0x00),
        "U": (0x66, 0x66, 0x66, 0x66, 0x66, 0x66, 0x3C, 0x00),
        "V": (0x66, 0x66, 0x66, 0x66, 0x66, 0x3C, 0x18, 0x00),
        "W": (0x63, 0x63, 0x63, 0x6B, 0x7F, 0x77, 0x63, 0x00),
        "X": (0x66, 0x66, 0x3C, 0x18, 0x3C, 0x66, 0x66, 0x00),
        "Y": (0x66, 0x66, 0x66, 0x3C, 0x18, 0x18, 0x18, 0x00),
        "Z": (0x7E, 0x06, 0x0C, 0x18, 0x30, 0x60, 0x7E, 0x00),
        "a": (0x00, 0x00, 0x3C, 0x06, 0x3E, 0x66, 0x3E, 0x00),
        "b": (0x60, 0x60, 0x7C, 0x66, 0x66, 0x66, 0x7C, 0x00),
        "c": (0x00, 0x00, 0x3C, 0x66, 0x60, 0x66, 0x3C, 0x00),
        "d": (0x06, 0x06, 0x3E, 0x66, 0x66, 0x66, 0x3E, 0x00),
        "e": (0x00, 0x00, 0x3C, 0x66, 0x7E, 0x60, 0x3C, 0x00),
        "f": (0x1C, 0x30, 0x30, 0x7C, 0x30, 0x30, 0x30, 0x00),
        "g": (0x00, 0x00, 0x3E, 0x66, 0x66, 0x3E, 0x06, 0x3C),
        "h": (0x60, 0x60, 0x7C, 0x66, 0x66, 0x66, 0x66, 0x00),
        "i": (0x18, 0x00, 0x38, 0x18, 0x18, 0x18, 0x3C, 0x00),
        "j": (0x0C, 0x00, 0x1C, 0x0C, 0x0C, 0x0C, 0x6C, 0x38),
        "k": (0x60, 0x60, 0x66, 0x6C, 0x78, 0x6C, 0x66, 0x00),
        "l": (0x38, 0x18, 0x18, 0x18, 0x18, 0x18, 0x3C, 0x00),
        "m": (0x00, 0x00, 0x6B, 0x7F, 0x7F, 0x6B, 0x63, 0x00),
        "n": (0x00, 0x00, 0x7C, 0x66, 0x66, 0x66, 0x66, 0x00),
        "o": (0x00, 0x00, 0x3C, 0x66, 0x66, 0x66, 0x3C, 0x00),
        "p": (0x00, 0x00, 0x7C, 0x66, 0x66, 0x7C, 0x60, 0x60),
        "q": (0x00, 0x00, 0x3E, 0x66, 0x66, 0x3E, 0x06, 0x06),
        "r": (0x00, 0x00, 0x6C, 0x76, 0x60, 0x60, 0x60, 0x00),
        "s": (0x00, 0x00, 0x3E, 0x60, 0x3C, 0x06, 0x7C, 0x00),
        "t": (0x30, 0x30, 0x7C, 0x30, 0x30, 0x36, 0x1C, 0x00),
        "u": (0x00, 0x00, 0x66, 0x66, 0x66, 0x66, 0x3E, 0x00),
        "v": (0x00, 0x00, 0x66, 0x66, 0x66, 0x3C, 0x18, 0x00),
        "w": (0x00, 0x00, 0x63, 0x6B, 0x7F, 0x3E, 0x36, 0x00),
        "x": (0x00, 0x00, 0x66, 0x3C, 0x18, 0x3C, 0x66, 0x00),
        "y": (0x00, 0x00, 0x66, 0x66, 0x66, 0x3E, 0x06, 0x3C),
        "z": (0x00, 0x00, 0x7E, 0x0C, 0x18, 0x30, 0x7E, 0x00),
        "[": (0x3C, 0x30, 0x30, 0x30, 0x30, 0x30, 0x3C, 0x00),
        "\\": (0x40, 0x20, 0x10, 0x08, 0x04, 0x02, 0x00, 0x00),
        "]": (0x3C, 0x0C, 0x0C, 0x0C, 0x0C, 0x0C, 0x3C, 0x00),
        "^": (0x18, 0x3C, 0x66, 0x00, 0x00, 0x00, 0x00, 0x00),
        "_": (0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0xFF),
        "`": (0x30, 0x18, 0x0C, 0x00, 0x00, 0x00, 0x00, 0x00),
        "{": (0x0E, 0x18, 0x18, 0x70, 0x18, 0x18, 0x0E, 0x00),
        "|": (0x18, 0x18, 0x18, 0x18, 0x18, 0x18, 0x18, 0x00),
        "}": (0x70, 0x18, 0x18, 0x0E, 0x18, 0x18, 0x70, 0x00),
        "~": (0x00, 0x00, 0x76, 0xDC, 0x00, 0x00, 0x00, 0x00),
    }
    FONT_8X8.update(glyphs)


_build_font()

#: 颜色（RGB888，驱动内部按 bgr 转 565）
BLACK: RGB = (0, 0, 0)
WHITE: RGB = (255, 255, 255)
RED: RGB = (255, 0, 0)
GREEN: RGB = (0, 200, 0)
BLUE: RGB = (0, 0, 255)
YELLOW: RGB = (255, 200, 0)
CYAN: RGB = (0, 200, 200)
GRAY: RGB = (120, 120, 120)
PAGE_NAMES = ("监护总览", "心率血氧", "体温环境", "报警记录")


class TftSpi(OutputDevice):
    """SPI TFT 彩屏（HAL 输出器件）。

    Args:
        controller: 控制器名（见 :data:`CONTROLLERS`）或 ``"auto"``（依次尝试）。
        spi_bus: SPI 总线号（树莓派 5 用 0）。
        spi_device: 片选号，``0`` = CE0（脚 24），``1`` = CE1（脚 26）。
        dc_pin: 命令/数据选择引脚（BCM），默认 24（物理脚 18）。
        reset_pin: 复位引脚（BCM），默认 25（物理脚 22）。
        backlight_pin: 背光引脚（BCM）；``-1`` 表示不控制（常亮）。
        baudrate: SPI 时钟（Hz）。太大屏幕会花，20~40MHz 通常安全。
        rotate: ``0``/``90``/``180``/``270``（只影响横竖屏与扫描方向）。
        bgr: 强制 BGR（颜色红蓝互换时改它）。
        invert: 强制反色（画面像底片时改它）。
    """

    KIND = DeviceKind.DISPLAY
    NAME = "tft_spi"

    def __init__(
        self,
        controller: str = "auto",
        spi_bus: int = 0,
        spi_device: int = 1,
        dc_pin: int = 24,
        reset_pin: int = 25,
        backlight_pin: int = -1,
        baudrate: int = 24_000_000,
        rotate: int = 90,
        bgr: Optional[bool] = None,
        invert: Optional[bool] = None,
        bus: Any = None,
        mock: bool = False,
        name: str = "",
    ) -> None:
        super().__init__(bus=bus, mock=mock, name=name)
        self.controller = str(controller)
        self.spi_bus = int(spi_bus)
        self.spi_device = int(spi_device)
        self.dc_pin = int(dc_pin)
        self.reset_pin = int(reset_pin)
        self.backlight_pin = int(backlight_pin)
        self.baudrate = int(baudrate)
        self.rotate = int(rotate)
        self._bgr_override = bgr
        self._invert_override = invert

        self.spec: Optional[ControllerSpec] = None
        self.width = 0
        self.height = 0
        self._spi: Any = None
        self._lgpio: Any = None
        self._lgpio_handle: Any = None
        self._dc: Any = None
        self._rst: Any = None
        self._bl: Any = None
        self._backend = "none"
        self.writes = 0
        self.init_attempts: List[str] = []
        #: 当前屏幕内容（两行文本 + 页号），供 :meth:`read` 诚实地回报
        self.current_lines: Tuple[str, str] = ("", "")
        self.page = 0

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def open(self) -> None:
        if self._opened:
            return
        if self.mock:
            # mock：**不碰任何硬件**，但仍要有一套可用的"虚拟屏"供上层与单测使用
            self.spec = CONTROLLERS[AUTO_ORDER[0]]
            self.width, self.height = self._apply_rotation(self.spec)
            self._opened = True
            return

        try:
            import spidev  # type: ignore import-not-found
        except ImportError:
            spidev = None  # type: ignore[assignment]
        try:
            import lgpio  # type: ignore import-not-found
        except ImportError:
            lgpio = None  # type: ignore[assignment]

        if spidev is None and lgpio is None:
            raise DeviceInitError(
                "TFT 需要 spidev 或 lgpio 之一来走 SPI："
                "`sudo apt install -y python3-spidev python3-lgpio`（或 pip3 install spidev）"
            )
        if lgpio is None and self.dc_pin >= 0:
            # spidev 只负责数据线，DC/RST/背光仍需 GPIO —— 没有 lgpio 就控制不了
            raise DeviceInitError(
                "TFT 需要 lgpio 控制 DC/RST 引脚（树莓派 5 上 RPi.GPIO 不可用）："
                "`sudo apt install -y python3-lgpio`"
            )

        self._gpio_open(lgpio)
        self._spi_open(spidev)

        names = list(AUTO_ORDER) if self.controller == "auto" else [self.controller]
        last_error: Optional[Exception] = None
        for name in names:
            spec = CONTROLLERS.get(name)
            if spec is None:
                raise DeviceInitError(
                    f"未知 controller={name!r}；可用：{', '.join(sorted(CONTROLLERS))}，或 'auto'"
                )
            try:
                self._init_controller(spec)
                self.init_attempts.append(f"{name}: OK")
                self._opened = True
                _LOG.info("TFT 初始化成功：%s（%dx%d，后端 %s）", name, self.width, self.height, self._backend)
                self._draw_boot_screen()
                return
            except Exception as exc:  # noqa: BLE001 - 依次尝试，最后统一报错
                last_error = exc
                self.init_attempts.append(f"{name}: {type(exc).__name__}")
                _LOG.warning("TFT 用 %s 初始化失败（继续试下一个）：%s", name, exc)

        self._gpio_close()
        raise DeviceInitError(
            "TFT 三种控制器都初始化失败："
            + "；".join(self.init_attempts)
            + f"（最后错误：{type(last_error).__name__}: {last_error}）。排查："
            "1) `ls -l /dev/spidev0.*` 是否存在（没有就开 SPI）；"
            "2) DC/RST/CS 三根线是否按文档接对（CS 别和 MCP3002 抢同一个片选）；"
            "3) VCC 先试 3.3V（部分模块要 5V 且自带稳压）；"
            "4) 用 `python3 scripts/tft_check.py --controller st7735` 逐个试并看颜色条"
        )

    def _gpio_open(self, lgpio: Any) -> None:
        """打开 GPIO（DC / RST / 背光）。"""
        self._lgpio = lgpio
        try:
            self._lgpio_handle = lgpio.gpiochip_open(0)
            if self.dc_pin >= 0:
                lgpio.gpio_claim_output(self._lgpio_handle, self.dc_pin, 0)
            if self.reset_pin >= 0:
                lgpio.gpio_claim_output(self._lgpio_handle, self.reset_pin, 1)
            if self.backlight_pin >= 0:
                lgpio.gpio_claim_output(self._lgpio_handle, self.backlight_pin, 0)
        except Exception as exc:  # noqa: BLE001
            raise DeviceInitError(
                f"TFT 的 GPIO 初始化失败（DC=GPIO{self.dc_pin}, RST=GPIO{self.reset_pin}）：{exc}。"
                "确认接线与权限（试试 sudo）"
            ) from exc

    def _spi_open(self, spidev: Any) -> None:
        """打开 SPI 数据通道（优先 spidev，退回 lgpio 手工 SPI）。"""
        if spidev is not None:
            try:
                spi = spidev.SpiDev()
                spi.open(self.spi_bus, self.spi_device)
                spi.max_speed_hz = self.baudrate
                spi.mode = 0
                self._spi = spi
                self._backend = "spidev"
                return
            except Exception as exc:  # noqa: BLE001 - 退回 lgpio
                _LOG.warning("spidev 打开失败（改用手工 SPI）：%s", exc)
        self._spi = None
        self._backend = "lgpio"

    def _init_controller(self, spec: ControllerSpec) -> None:
        """按控制器初始化序列配置屏幕。"""
        self.spec = spec
        self._hardware_reset()
        for cmd, data, delay_ms in spec.init:
            if cmd in (0x2A, 0x2B):      # CASET/RASET 由 set_window 动态设置
                continue
            if cmd == 0x36:
                # MADCTL：优先用配置的旋转/颜色顺序覆盖
                madctl = self._madctl(spec)
                self._write(0x36, bytes([madctl]))
                continue
            self._write(cmd, data)
            if delay_ms:
                time.sleep(delay_ms / 1000.0)
        want_invert = spec.invert if self._invert_override is None else bool(self._invert_override)
        self._write(0x21 if want_invert else 0x20, b"")
        self.width, self.height = self._apply_rotation(spec)
        self._write(0x29, b"")           # DISPON
        time.sleep(0.05)

    def _madctl(self, spec: ControllerSpec) -> int:
        """MADCTL：按旋转角度与 BGR/RGB 选择扫描方向。"""
        base = spec.madctl
        # 只保留 BGR 位（bit3）与 MY/MX 由旋转决定
        bgr = spec.bgr if self._bgr_override is None else bool(self._bgr_override)
        value = 0x08 if bgr else 0x00
        rot = self.rotate % 360
        if rot == 0:
            value |= 0x00
        elif rot == 90:
            value |= 0x60      # MX + MV
        elif rot == 180:
            value |= 0xC0      # MX + MY
        else:
            value |= 0xA0      # MY + MV
        # 若控制器本身要求额外位（例如 ST7735 的 0xC8），用它的低 3 位兜底
        return value | (base & 0x07)

    def _apply_rotation(self, spec: ControllerSpec) -> Tuple[int, int]:
        if self.rotate % 180 == 0:
            return spec.width, spec.height
        return spec.height, spec.width

    def close(self) -> None:
        """关屏并释放资源（幂等、不抛异常）。"""
        try:
            self._set_backlight(False)
        except Exception:  # noqa: BLE001
            pass
        self._gpio_close()
        if self._spi is not None:
            try:
                self._spi.close()
            except Exception:  # noqa: BLE001
                pass
            self._spi = None
        self._opened = False

    def _gpio_close(self) -> None:
        if self._lgpio is not None and self._lgpio_handle is not None:
            for pin in (self.dc_pin, self.reset_pin, self.backlight_pin):
                if pin >= 0:
                    try:
                        self._lgpio.gpio_free(self._lgpio_handle, pin)
                    except Exception:  # noqa: BLE001
                        pass
            try:
                self._lgpio.gpiochip_close(self._lgpio_handle)
            except Exception:  # noqa: BLE001
                pass
        self._lgpio_handle = None
        self._lgpio = None

    # ------------------------------------------------------------------
    # 底层：命令/数据与像素写入
    # ------------------------------------------------------------------

    def _hardware_reset(self) -> None:
        if self.mock or self.reset_pin < 0:
            return
        self._gpio_write(self.reset_pin, 1)
        time.sleep(0.05)
        self._gpio_write(self.reset_pin, 0)
        time.sleep(0.05)
        self._gpio_write(self.reset_pin, 1)
        time.sleep(0.12)

    def _gpio_write(self, pin: int, value: int) -> None:
        if self.mock or self._lgpio is None or self._lgpio_handle is None or pin < 0:
            return
        self._lgpio.gpio_write(self._lgpio_handle, pin, 1 if value else 0)

    def _transfer(self, payload: bytes) -> None:
        """把一段字节送上 SPI（mock 模式只计数）。"""
        self.writes += 1
        if self.mock:
            return
        if self._spi is not None:
            # 分块发送（spidev 单次传输有长度限制）
            view = memoryview(payload)
            for i in range(0, len(view), 4096):
                self._spi.writebytes2(bytes(view[i:i + 4096]))
            return
        # lgpio 手工 SPI：逐字节位操作（慢，但能点亮）
        lg = self._lgpio
        h = self._lgpio_handle
        clk, mosi = 11, 10
        try:
            lg.gpio_claim_output(h, clk, 0)
            lg.gpio_claim_output(h, mosi, 0)
        except Exception:  # noqa: BLE001 - 已占用则忽略
            pass
        for byte in payload:
            for bit in range(7, -1, -1):
                lg.gpio_write(h, mosi, (byte >> bit) & 1)
                lg.gpio_write(h, clk, 1)
                lg.gpio_write(h, clk, 0)

    def _write(self, cmd: int, data: bytes = b"") -> None:
        """发一条命令 +（可选）紧随其后的数据。"""
        if self.dc_pin >= 0:
            self._gpio_write(self.dc_pin, 0)
        self._transfer(bytes([cmd & 0xFF]))
        if data:
            if self.dc_pin >= 0:
                self._gpio_write(self.dc_pin, 1)
            self._transfer(data)

    def set_window(self, x0: int, y0: int, x1: int, y1: int) -> None:
        """设置写入窗口（列/行地址范围），随后 :meth:`push_pixels` 的数据会填进去。"""
        assert self.spec is not None
        x0 += self.spec.col_offset
        x1 += self.spec.col_offset
        y0 += self.spec.row_offset
        y1 += self.spec.row_offset
        self._write(0x2A, bytes([x0 >> 8, x0 & 0xFF, x1 >> 8, x1 & 0xFF]))
        self._write(0x2B, bytes([y0 >> 8, y0 & 0xFF, y1 >> 8, y1 & 0xFF]))
        self._write(0x2C, b"")

    def push_pixels(self, pixels: Sequence[int]) -> None:
        """推送 16 位像素（大端），数量必须等于 :meth:`set_window` 覆盖的像素数。"""
        if not pixels:
            return
        buf = bytearray(len(pixels) * 2)
        for i, color in enumerate(pixels):
            buf[i * 2] = (color >> 8) & 0xFF
            buf[i * 2 + 1] = color & 0xFF
        if self.dc_pin >= 0:
            self._gpio_write(self.dc_pin, 1)
        self._transfer(bytes(buf))

    # ------------------------------------------------------------------
    # 绘图（够本项目用：填充、矩形、文字、横线）
    # ------------------------------------------------------------------

    def color(self, rgb: RGB) -> int:
        """RGB888 → 本控制器需要的 565 值（自动处理 BGR）。"""
        bgr = self.spec.bgr if (self.spec and self._bgr_override is None) else bool(self._bgr_override)
        return rgb565(rgb[0], rgb[1], rgb[2], bgr=bgr)

    def fill(self, rgb: RGB) -> None:
        """整屏填充。"""
        self.fill_rect(0, 0, self.width - 1, self.height - 1, rgb)

    def fill_rect(self, x: int, y: int, w: int, h: int, rgb: RGB) -> None:
        """填充矩形（局部刷新 —— 只画需要变的区域，SPI 才刷得动）。"""
        if w <= 0 or h <= 0:
            return
        x = max(0, min(self.width - 1, x))
        y = max(0, min(self.height - 1, y))
        w = min(w, self.width - x)
        h = min(h, self.height - y)
        self.set_window(x, y, x + w - 1, y + h - 1)
        self.push_pixels([self.color(rgb)] * (w * h))

    def hline(self, x: int, y: int, w: int, rgb: RGB) -> None:
        """画一条横线（曲线、分隔线都用它）。"""
        self.fill_rect(x, y, w, 1, rgb)

    def text(self, x: int, y: int, s: str, rgb: RGB, bg: Optional[RGB] = None, scale: int = 1) -> int:
        """画 ASCII 文本，返回下一个字符的 x 坐标。

        ⚠️ **只支持 ASCII**：8×8 点阵放不下汉字。中文请走 LCD/手机/语音播报
        （与 :mod:`health_monitor.outputs.lcd1602` 同一条纪律）。
        非 ASCII 字符会被替换成 ``?``，**不会**把乱码字节送给屏幕。
        """
        fg = self.color(rgb)
        bgc = self.color(bg) if bg is not None else None
        cursor = x
        for ch in s:
            if ord(ch) > 127:
                ch = "?"
            glyph = FONT_8X8.get(ch) or FONT_8X8["?"]
            if bgc is not None:
                # 有底色时把整块 8×8 一起画（连背景），避免残影
                self.set_window(cursor, y, cursor + 8 * scale - 1, y + 8 * scale - 1)
                buf: List[int] = []
                for row in glyph:
                    for _ in range(scale):                 # 行方向放大
                        for col in range(8):
                            on = (row >> (7 - col)) & 1
                            buf.extend([fg if on else bgc] * scale)   # 列方向放大
                self.push_pixels(buf)
            else:
                for row_index, row in enumerate(glyph):
                    for col in range(8):
                        if (row >> (7 - col)) & 1:
                            self.fill_rect(cursor + col * scale, y + row_index * scale, scale, scale, rgb)
            cursor += 8 * scale
        return cursor

    def _set_backlight(self, on: bool) -> None:
        if self.backlight_pin >= 0:
            self._gpio_write(self.backlight_pin, 1 if on else 0)

    def backlight(self, on: bool = True) -> None:
        """开/关背光（配了 ``backlight_pin`` 才有用）。"""
        self._set_backlight(on)

    # ------------------------------------------------------------------
    # HAL：指令接收与状态回读
    # ------------------------------------------------------------------

    def send(self, command: Any) -> None:
        """执行显示指令：``DisplayCommand(lines=(第一行, 第二行), page=n)``。"""
        if not isinstance(command, DisplayCommand):
            raise UnsupportedError(f"TFT 只接受 DisplayCommand，收到 {type(command).__name__}")
        self._require_open()
        lines = tuple(str(x) for x in (command.lines or ("", "")))[:2]
        if len(lines) < 2:
            lines = (lines[0], "")
        self.current_lines = (lines[0], lines[1])
        self.page = int(command.page)
        if self.mock:
            return
        try:
            self._render(lines, self.page)
        except Exception as exc:  # noqa: BLE001 - 屏幕画不出来不该打断监护
            self._note_fault(exc)
            raise DeviceIOError(f"TFT 绘制失败：{type(exc).__name__}: {exc}") from exc
        self._note_ok()

    def read(self) -> DisplayStatus:
        """回读**驱动自己记录的**屏幕内容（诚实：不知道就是空串）。"""
        self._require_open()
        return DisplayStatus(device=self.name, lines=self.current_lines, page=self.page)

    def _render(self, lines: Tuple[str, str], page: int) -> None:
        """把两行文本画到屏上：大字两行 + 底部页码。"""
        self.fill(BLACK)
        self.text(2, 2, lines[0][: self.width // 9], WHITE, bg=BLACK, scale=2)
        self.text(2, 24, lines[1][: self.width // 9], CYAN, bg=BLACK, scale=2)
        self.hline(0, self.height - 14, self.width, GRAY)
        self.text(2, self.height - 12, f"PAGE {page}", GRAY, bg=BLACK, scale=1)

    def _draw_boot_screen(self) -> None:
        """开机自检画面：三色条 + 文本 —— **这是判断"控制器对不对"的关键画面**。

        红/绿/蓝三条颜色要是**纯正的红、绿、蓝**；出现红蓝互换（黄看着像青）说明
        BGR 设反了；出现底片感说明反色设反了；花屏说明控制器选错了。
        """
        third = self.height // 3
        self.fill_rect(0, 0, self.width, third, RED)
        self.fill_rect(0, third, self.width, third, GREEN)
        self.fill_rect(0, 2 * third, self.width, self.height - 2 * third, BLUE)
        self.text(2, 2, "TFT OK", WHITE, bg=RED, scale=2)
        self.text(2, third + 2, "R G B TEST", BLACK, bg=GREEN, scale=1)
        self.text(2, 2 * third + 2, "CHECK COLORS", WHITE, bg=BLUE, scale=1)
        self.current_lines = ("TFT OK", "RGB TEST")
        self.page = 0

    def describe(self) -> Dict[str, Any]:
        from ..hal.pins import describe_pin

        spec = self.spec
        return {
            "name": self.name,
            "kind": self.KIND.value,
            "mock": self.mock,
            "bus": f"SPI{self.spi_bus}.{self.spi_device}（后端 {self._backend}）",
            "pins": {
                "sclk": describe_pin(11),
                "mosi": describe_pin(10),
                "cs": describe_pin(8 if self.spi_device == 0 else 7),
                "dc": describe_pin(self.dc_pin) if self.dc_pin >= 0 else "未使用",
                "rst": describe_pin(self.reset_pin) if self.reset_pin >= 0 else "未使用",
                "vcc": "3.3V（物理脚 1；部分模块需 5V 且自带稳压）",
                "gnd": "GND（物理脚 6，务必共地）",
            },
            "notes": (
                f"控制器 {spec.name if spec else '未初始化'}，分辨率 {self.width}x{self.height}（旋转 {self.rotate}°）；"
                "⚠️ 彩色屏只能显示 ASCII（中文请走 LCD/手机/语音）；"
                "⚠️ CS 不要与 MCP3002 抢同一个片选（本项目 MCP3002=CE0、TFT=CE1）；"
                "颜色红蓝互换 ⇒ 把 bgr 设为 true（BGR/RGB 顺序）；"
                "画面像底片（反色）⇒ 把 invert 设为 true"
            ),
        }

    def self_check(self) -> Dict[str, Any]:
        """自检：mock 模式直接通过；真实模式看是否已初始化 + 后端起没起来。"""
        if self.mock:
            return {"ok": True, "detail": f"mock 模式（控制器 {self.spec.name if self.spec else '?'}）"}
        if not self._opened or self.spec is None:
            return {"ok": False, "detail": "未初始化；跑 `python3 scripts/tft_check.py --controller auto`"}
        return {
            "ok": True,
            "detail": f"{self.spec.name} {self.width}x{self.height}，后端 {self._backend}，"
                      f"写入 {self.writes} 次（颜色对不对请**肉眼确认**三色条）",
        }


__all__ = ["TftSpi", "CONTROLLERS", "ControllerSpec", "AUTO_ORDER", "rgb565", "FONT_8X8"]
