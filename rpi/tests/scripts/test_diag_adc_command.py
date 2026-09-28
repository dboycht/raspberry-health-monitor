"""回归：`diag_pin_levels.read_adc()` 的 MCP3002 控制字必须与驱动一致（`ERROR.md` **E48**）。

背景（2026-09-28 真机实测）：`diag_pin_levels.py --adc` 是 `docs/14` 里点名的"ADC 自证"命令，
但它原来发的是 ``[0x60 | (channel << 5), 0x00]``：

1. 只有 **2 字节**（驱动是 3 字节）；
2. ``0x60`` 的 **bit3(MSBF)=0**（应为 1）；
3. ``0x60`` 的 **bit5 已经是 1** ⇒ ``0x60 | (channel << 5)`` 对 CH0/CH1 **算出同一个 0x60**，
   通道号根本传不进去。

⇒ 芯片收不到有效的"启动转换"命令，回读永远是悬空噪声（实测：乱跳、或恒 0 / 恒 1023），
**让人误判成"芯片坏了/接线错了"**，白白排查了一整轮。

本文件的作用：把"诊断脚本的负载必须与驱动逐字节相同"钉死 —— 任何人再改错，这里当场红。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_RPI_DIR = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _RPI_DIR / "scripts"
for _path in (_RPI_DIR, _SCRIPTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import diag_pin_levels  # noqa: E402  必须在 sys.path 处理之后导入
from health_monitor.sensors.mcp3002 import Mcp3002  # noqa: E402


class FakeSpi:
    """记录每次 ``xfer2`` 的负载，并回放给定的回读字节（不碰真硬件）。"""

    def __init__(self, reply) -> None:
        self.reply = list(reply)
        self.payloads = []

    def xfer2(self, payload):
        self.payloads.append(list(payload))
        return list(self.reply)


class TestAdcCommandWord(unittest.TestCase):
    def test_与驱动逐字节一致(self) -> None:
        """★ 判据：诊断脚本发的负载 == `Mcp3002._build_command(channel)`。"""
        dev = Mcp3002(mock=True)  # mock 模式绝不碰硬件
        for ch in (0, 1):
            with self.subTest(channel=ch):
                fake = FakeSpi([0x03, 0xFF, 0x00])
                diag_pin_levels.read_adc(fake, ch, times=1)
                self.assertEqual(
                    fake.payloads[0],
                    list(dev._build_command(ch)),
                    f"CH{ch} 的控制字与驱动不一致（改这里前先读 ERROR.md E48）",
                )

    def test_两个通道的控制字必须不同(self) -> None:
        """旧 bug 的形状：``0x60 | (ch << 5)`` 让两个通道算出同一个字节。"""
        p0, p1 = FakeSpi([0, 0, 0]), FakeSpi([0, 0, 0])
        diag_pin_levels.read_adc(p0, 0, times=1)
        diag_pin_levels.read_adc(p1, 1, times=1)
        self.assertNotEqual(p0.payloads[0], p1.payloads[0], "通道号没传进控制字（E48 那个 bug）")
        self.assertEqual(p0.payloads[0][0], 0x68, "CH0 控制字应为 0x68")
        self.assertEqual(p1.payloads[0][0], 0x78, "CH1 控制字应为 0x78")
        self.assertEqual(len(p0.payloads[0]), 3, "应为 3 字节（[控制字, 0x00, 0x00]）")

    def test_位定义与数据手册一致(self) -> None:
        """bit7=前置 0、bit6=START、bit5=SGL/DIFF、bit4=通道号、bit3=MSBF。"""
        for ch in (0, 1):
            fake = FakeSpi([0x00, 0x00, 0x00])
            diag_pin_levels.read_adc(fake, ch, times=1)
            word = fake.payloads[0][0]
            with self.subTest(channel=ch, word=hex(word)):
                self.assertEqual(word & 0b1000_0000, 0, "bit7 应为前置 0")
                self.assertEqual(word & 0b0100_0000, 0b0100_0000, "START 位（bit6）应为 1")
                self.assertEqual(word & 0b0010_0000, 0b0010_0000, "SGL/DIFF 位（bit5）应为 1")
                self.assertEqual(word & 0b0000_1000, 0b0000_1000, "MSBF 位（bit3）应为 1")
                self.assertEqual((word >> 4) & 1, ch, "bit4 必须是通道号")

    def test_解码仍是十位(self) -> None:
        """回读 ``0x03 0xFF`` ⇒ 1023；``0x00 0x00`` ⇒ 0。"""
        self.assertEqual(
            diag_pin_levels.read_adc(FakeSpi([0x03, 0xFF, 0x00]), 0, times=1)[0], 1023
        )
        self.assertEqual(
            diag_pin_levels.read_adc(FakeSpi([0x00, 0x00, 0x00]), 0, times=1)[0], 0
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
