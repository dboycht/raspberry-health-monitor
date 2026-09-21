"""LCD1602（I2C 转接板）驱动测试。

覆盖四件事（对应 docs 里的验收点）：
1. 4 位模式初始化序列**逐字节**核验（0x33→0x32→0x28→0x08→0x01→0x06→0x0C）；
2. 地址自动探测（0x27 / 0x3F / 0x20 / 0x38）与"一个都没找到"的报错；
3. ``send()`` 的 **按 cols 截断 + 补空格**（第二行残留必须被覆盖）；
4. **中文降级**为 ``?``（LCD 是 ASCII 字库，中文会花屏）。
"""

from __future__ import annotations

import unittest

from health_monitor.hal import (
    AlarmDispatchError,
    DeviceInitError,
    DeviceKind,
    DeviceNotReady,
    DisplayCommand,
    DisplayStatus,
    LightCommand,
    MockBus,
    UnsupportedError,
)
from health_monitor.outputs.lcd1602 import CANDIDATE_ADDRESSES, Lcd1602


def make_bus(addresses=(0x27, 0x3F, 0x20, 0x38)) -> MockBus:
    """造一个"这些地址假装在线"的 MockBus（模拟 i2cdetect 的结果）。"""
    bus = MockBus()
    for addr in addresses:
        bus.set_i2c_reply(addr, b"\x00")
    return bus


class DeadBus:
    """『什么地址都不在线』的假总线：任何读都失败。

    注意不能直接用空 ``MockBus`` 冒充"总线上没东西"——MockBus 对**未注册钩子**的
    地址会返回默认字节（假装一切都在线），空总线反而是"所有地址都在线"。
    """

    def __init__(self) -> None:
        self._reads: list = []
        self._writes: list = []

    def i2c_scan(self) -> list:
        return []

    def i2c_read(self, bus: int, address: int, length: int) -> bytes:
        from health_monitor.hal import DeviceIOError

        self._reads.append((bus, address, length))
        raise DeviceIOError(f"0x{address:02X} 无应答（模拟『总线上没有这个器件』）")

    def i2c_write(self, bus: int, address: int, data: bytes) -> None:
        self._writes.append((bus, address, bytes(data)))


def decode_writes(bus, address: int) -> list:
    """把写到某个地址的 I2C 字节流水，还原成 ``[('cmd'|'data', 值), …]``。"""
    ops = [
        op[1][2] for op in bus.operations("i2c_write") if op[1][1] == address
    ]
    flat = [b for payload in ops for b in payload]  # MockBus 记录的是 bytes，展平成 int
    return Lcd1602(mock=True)._codec.decode_ops(flat)  # 借用驱动的纯逻辑解码器


class TestPcf8574Codec(unittest.TestCase):
    """纯逻辑：位拼装（不碰总线）。"""

    def setUp(self) -> None:
        self.lcd = Lcd1602(mock=True)
        self.codec = self.lcd._codec

    def test_一个字节编成四个EN脉冲(self) -> None:
        """4 位模式：高半字节 2 拍 + 低半字节 2 拍 = 4 个待写字节。"""
        ops = self.codec.encode_nibbles(0x28, rs=0)
        self.assertEqual(len(ops), 4)
        # 第 1、3 拍 EN 应为高（锁存），第 2、4 拍 EN 为低
        self.assertTrue(ops[0] & 0x04)
        self.assertFalse(ops[1] & 0x04)
        self.assertTrue(ops[2] & 0x04)
        self.assertFalse(ops[3] & 0x04)
        # D4~D7 = 高半字节 0x2，随后是低半字节 0x8
        self.assertEqual((ops[0] >> 4) & 0x0F, 0x2)
        self.assertEqual((ops[2] >> 4) & 0x0F, 0x8)

    def test_RS位区分指令与数据(self) -> None:
        cmd = self.codec.encode_nibbles(0x28, rs=0)
        data = self.codec.encode_nibbles(ord("A"), rs=1)
        self.assertFalse(cmd[0] & 0x01, "指令时 RS 应为 0")
        self.assertTrue(data[0] & 0x01, "数据时 RS 应为 1")

    def test_背光位体现在P3(self) -> None:
        on = Lcd1602(mock=True, backlight=True)
        off = Lcd1602(mock=True, backlight=False)
        self.assertTrue(on._codec.idle_byte & 0x08)
        self.assertFalse(off._codec.idle_byte & 0x08)

    def test_EN空闲时为低(self) -> None:
        """总线静止时必须停在 EN=0，否则会误锁存。"""
        self.assertFalse(self.codec.idle_byte & 0x04)

    def test_解码器能还原写入的字节(self) -> None:
        """编码→解码必须可逆：解码器也是"初始化序列是否正确"的核验手段。"""
        payload = self.codec.encode_nibbles(0x33, rs=0) + self.codec.encode_nibbles(ord("H"), rs=1)
        self.assertEqual(
            self.codec.decode_ops(payload),
            [("cmd", 0x33), ("data", 0x48)],
        )

    def test_DDRAM地址指令拼接(self) -> None:
        """第 2 行的 DDRAM 起始地址是 0x40 → 指令 0x80|0x40 = 0xC0（HD44780 规定）。"""
        decoded = self.codec.decode_ops(self.codec.set_ddram_address(0x40))
        self.assertEqual(decoded, [("cmd", 0xC0)])
        self.assertEqual(
            self.codec.decode_ops(self.codec.set_ddram_address(0x00)), [("cmd", 0x80)]
        )


class TestLcd1602Mock(unittest.TestCase):
    def test_未open就send必须报错(self) -> None:
        lcd = Lcd1602(mock=True)
        with self.assertRaises(DeviceNotReady):
            lcd.send(DisplayCommand(lines=("a", "b")))

    def test_未open就read必须报错(self) -> None:
        lcd = Lcd1602(mock=True)
        with self.assertRaises(DeviceNotReady):
            lcd.read()

    def test_mock模式可用且自动探测到0x27(self) -> None:
        lcd = Lcd1602(bus=1, mock=True)
        lcd.open()
        self.assertEqual(lcd.address, 0x27, "候选顺序是 0x27 → 0x3F → 0x20 → 0x38")
        self.assertTrue(lcd.mock)
        self.assertEqual(lcd.KIND, DeviceKind.DISPLAY)
        self.assertIsInstance(lcd._handle, MockBus, "mock 模式下总线必须是 MockBus，不能碰真实 I2C")
        lcd.close()

    def test_自动探测会跳过不在线的地址(self) -> None:
        """只有 0x3F 在线时（很常见的"另一种背包"），必须探到 0x3F。"""
        lcd = Lcd1602(mock=True)
        lcd._handle = make_bus(addresses=(0x3F,))
        lcd.open()
        self.assertEqual(lcd.address, 0x3F)
        lcd.close()

    def test_显式地址时跳过探测(self) -> None:
        lcd = Lcd1602(address=0x38, mock=True)
        lcd.open()
        self.assertEqual(lcd.address, 0x38)
        self.assertFalse(lcd.status()["address_auto"])
        lcd.close()

    def test_探测不到地址时抛DeviceInitError且带i2cdetect线索(self) -> None:
        lcd = Lcd1602(mock=True)
        lcd._handle = DeadBus()          # 一个"什么地址都不在线"的总线
        with self.assertRaises(DeviceInitError) as ctx:
            lcd.open()
        text = str(ctx.exception)
        self.assertIn("i2cdetect", text)
        for addr in CANDIDATE_ADDRESSES:
            self.assertIn(f"0x{addr:02X}", text)

    def test_候选地址覆盖常见与备选(self) -> None:
        self.assertEqual(CANDIDATE_ADDRESSES, (0x27, 0x3F, 0x20, 0x38))
        self.assertIn("0x3F", str(Lcd1602(mock=True, address=0x27).describe()["candidates"]))

    # -- 初始化序列 ------------------------------------------------------

    def test_4位模式初始化序列逐字节正确(self) -> None:
        """0x33 → 0x32 → 功能设置 0x28 → 显示关 0x08 → 清屏 0x01 → 输入模式 0x06 → 显示开 0x0C。"""
        bus = make_bus()
        lcd = Lcd1602(mock=True)
        lcd._handle = bus
        lcd.open()
        decoded = [v for kind, v in decode_writes(bus, lcd.address) if kind == "cmd"]
        self.assertEqual(decoded, [0x33, 0x32, 0x28, 0x08, 0x01, 0x06, 0x0C])
        lcd.close()

    def test_初始化后屏幕内容为空(self) -> None:
        """清屏之后驱动记录的内容必须归零（否则 read() 会撒谎）。"""
        lcd = Lcd1602(mock=True)
        lcd._handle = make_bus()
        lcd.open()
        status = lcd.read()
        self.assertEqual(status.lines, ("", ""))
        self.assertEqual(status.page, 0)
        lcd.close()

    def test_初始化后背光已打开(self) -> None:
        bus = make_bus()
        lcd = Lcd1602(mock=True)
        lcd._handle = bus
        lcd.open()
        last = bus.operations("i2c_write")[-1][1][2][0]
        self.assertTrue(last & 0x08, "初始化最后一步应把背光位拉高")
        lcd.close()


class TestLcd1602Text(unittest.TestCase):
    """文本处理：截断 / 补空格 / 中文降级。"""

    def setUp(self) -> None:
        self.lcd = Lcd1602(mock=True)
        self.lcd._handle = make_bus()
        self.lcd.open()

    def tearDown(self) -> None:
        self.lcd.close()

    def test_超过列数被截断(self) -> None:
        out = self.lcd.sanitize("12345678901234567890")
        self.assertEqual(len(out), 16)
        self.assertEqual(out, "1234567890123456")

    def test_不足列数补空格(self) -> None:
        out = self.lcd.sanitize("HR 72")
        self.assertEqual(len(out), 16)
        self.assertEqual(out, "HR 72           ")
        self.assertEqual(out.rstrip(), "HR 72")

    def test_中文被降级为问号而不是乱码(self) -> None:
        """LCD1602 是 ASCII 字库：非 ASCII 必须安全降级，绝不能让非法字节进 I2C。"""
        out = self.lcd.sanitize("心率 72")
        self.assertEqual(out, "?? 72           ")
        self.assertNotIn("心", out)
        self.assertTrue(all(ord(c) < 128 for c in out))
        self.assertEqual(self.lcd.status()["non_ascii_replaced"], 2)

    def test_降级字符可配置(self) -> None:
        lcd = Lcd1602(mock=True, non_ascii_fallback=".")
        self.assertEqual(lcd.sanitize("心率")[0:2], "..")

    def test_send后read返回记录的内容(self) -> None:
        self.lcd.send(DisplayCommand(lines=("HR 72 bpm", "SpO2 98%"), page=2))
        status = self.lcd.read()
        self.assertIsInstance(status, DisplayStatus)
        self.assertEqual(status.lines, ("HR 72 bpm       ", "SpO2 98%        "))
        self.assertEqual(status.page, 2)
        self.assertEqual(status.device, "lcd1602")

    def test_第二行不足时补空格覆盖残留(self) -> None:
        """先写长文本再写短文本：短的那行必须被空格覆盖，不留残影。"""
        self.lcd.send(DisplayCommand(lines=("ALARM: HR HIGH", "ALARM: SpO2 LOW")))
        self.lcd.send(DisplayCommand(lines=("HR 72 bpm", "OK")))
        lines = self.lcd.read().lines
        self.assertEqual(lines[0], "HR 72 bpm       ")
        self.assertEqual(lines[1], "OK              ")
        self.assertNotIn("ALARM", lines[0] + lines[1], "残留字符必须被空格覆盖掉")
        self.assertNotIn("LOW", lines[1])

    def test_每次显示都重定位到两行行首(self) -> None:
        """必须发 0x80 与 0xC0 两条 DDRAM 地址指令，否则第二行会接在第一行后面。"""
        bus = self.lcd._handle
        bus.clear()
        self.lcd.send(DisplayCommand(lines=("A", "B")))
        decoded = [v for kind, v in decode_writes(bus, self.lcd.address) if kind == "cmd"]
        self.assertIn(0x80, decoded, "第 1 行应定位到 DDRAM 0x00")
        self.assertIn(0xC0, decoded, "第 2 行应定位到 DDRAM 0x40")

    def test_空行也能清掉上一屏内容(self) -> None:
        self.lcd.send(DisplayCommand(lines=("AAAAAAAAAAAAAAAA", "BBBBBBBBBBBBBBBB")))
        self.lcd.send(DisplayCommand(lines=("", "")))
        self.assertEqual(self.lcd.read().lines, (" " * 16, " " * 16))


class TestLcd1602Errors(unittest.TestCase):
    def setUp(self) -> None:
        self.lcd = Lcd1602(mock=True)
        self.bus = make_bus()
        self.lcd._handle = self.bus
        self.lcd.open()

    def tearDown(self) -> None:
        self.lcd.close()

    def test_不认识的指令抛UnsupportedError而不是静默忽略(self) -> None:
        with self.assertRaises(UnsupportedError):
            self.lcd.send(LightCommand(color="green"))

    def test_指令类型错误也要留痕(self) -> None:
        with self.assertRaises(UnsupportedError):
            self.lcd.send(object())
        self.assertEqual(self.lcd.status()["fault_count"], 1)

    def test_I2C写失败抛AlarmDispatchError(self) -> None:
        self.bus.fail_next()
        with self.assertRaises(AlarmDispatchError) as ctx:
            self.lcd.send(DisplayCommand(lines=("X", "Y")))
        self.assertIn("LCD1602", str(ctx.exception))

    def test_写失败时不更新记录的屏幕内容(self) -> None:
        """半途失败绝不能把"我以为写进去了"当成事实（read() 必须诚实）。"""
        self.lcd.send(DisplayCommand(lines=("GOOD", "LINE")))
        before = self.lcd.read().lines
        self.bus.fail_next()
        with self.assertRaises(AlarmDispatchError):
            self.lcd.send(DisplayCommand(lines=("BAD", "LINE")))
        self.assertEqual(self.lcd.read().lines, before)

    def test_close可重复调用(self) -> None:
        lcd = Lcd1602(mock=True)
        lcd.open()
        lcd.close()
        lcd.close()
        self.assertFalse(lcd._opened)

    def test_clear与set_backlight可用(self) -> None:
        self.lcd.send(DisplayCommand(lines=("A", "B")))
        self.lcd.clear()
        self.assertEqual(self.lcd.read().lines, ("", ""))
        self.lcd.set_backlight(False)
        self.assertFalse(self.lcd.backlight)
        self.lcd.set_backlight(True)
        self.assertTrue(self.lcd.backlight)

    def test_未open时clear与set_backlight也要报错(self) -> None:
        lcd = Lcd1602(mock=True)
        with self.assertRaises(DeviceNotReady):
            lcd.clear()
        with self.assertRaises(DeviceNotReady):
            lcd.set_backlight(False)

    def test_真实模式I2C失败抛DeviceInitError(self) -> None:
        """真实模式（mock=False）且总线上探测不到任何候选地址时，必须抛带线索的初始化错误。"""
        lcd = Lcd1602(mock=False)
        lcd._handle = DeadBus()      # 假装总线上什么都没有
        with self.assertRaises(DeviceInitError) as ctx:
            lcd.open()
        self.assertIn("i2cdetect", str(ctx.exception))
        self.assertFalse(lcd._opened)

    def test_status与describe有内容(self) -> None:
        st = self.lcd.status()
        self.assertEqual(st["driver"], "Lcd1602")
        self.assertEqual(st["address"], "0x27")
        self.assertEqual(st["cols"], 16)
        self.assertEqual(st["fault_count"], 0)
        desc = self.lcd.describe()
        self.assertEqual(desc["name"], "lcd1602")
        self.assertIn("物理脚 3", desc["pins"]["sda"])
        self.assertIn("物理脚 5", desc["pins"]["scl"])
        self.assertIn("MAX30102", desc["bus"], "必须写清与 MAX30102 共用 I2C 总线")
        self.assertIn("中文显示不了", desc["notes"])

    def test_self_check在mock下ok(self) -> None:
        self.assertTrue(self.lcd.self_check()["ok"])
        lcd = Lcd1602(mock=True)
        self.assertFalse(lcd.self_check()["ok"], "未 open() 时自检应为失败")


if __name__ == "__main__":
    unittest.main(verbosity=2)
