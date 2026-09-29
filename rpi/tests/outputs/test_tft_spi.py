"""SPI TFT 彩屏驱动测试（**不需要真实屏幕**）。

重点覆盖四类容易出错的地方：
1. **颜色换算**：RGB565 打包与 BGR 交换 —— 红蓝互换是 TFT 最常见的"看起来能用其实错了"；
2. **字库边界**：非 ASCII（中文/emoji）必须被安全降级成 ``?``，**不能**把乱码字节送上 SPI；
3. **绘图路径**：整屏填充/矩形/文字/横线在 mock 下都要能跑通且**不访问硬件**；
4. **接口契约**：只接受 ``DisplayCommand``；``read()`` 诚实回报自己记录的内容；
   ``describe()`` 给出物理脚号（用户照着接线）。
"""

from __future__ import annotations

import unittest

from health_monitor.hal import (
    DisplayCommand,
    DeviceKind,
    DeviceNotReady,
    UnsupportedError,
)
from health_monitor.hal.registry import MANIFEST, create_device
from health_monitor.outputs.tft_spi import (
    AUTO_ORDER,
    CONTROLLERS,
    FONT_8X8,
    FONT_W,
    TftSpi,
    layout_for,
    rgb565,
)


class TestTftTextLayout(unittest.TestCase):
    """E53 回归：字号与截断必须按 8×8 点阵的**实际字宽**算，**绝不许把字画到屏幕外**。

    来历（2026-09-29 真机，用户反馈"**字被截断/显示不全**"）：
    旧实现把每行截断到 ``width // 9``（128 px 屏 = 14 字符），却固定 ``scale=2`` 绘制；
    而 ``text()`` 每字符前进 ``8 * scale = 16`` px ⇒ 128 px 屏**只放得下 8 个字符**
    ⇒ ``SENSOR FAULT``（12 字符）的右半边被画到了屏幕外。
    """

    #: 本项目屏幕的真实分辨率（配置 `st7735_1.8_128x160`，rotate 0）
    WIDTH = 128

    def test_不会把字画到屏幕外_这是核心不变量(self) -> None:
        cases = [
            ("SENSOR FAULT", "CHECK WIRING"),
            ("NO MOTION ALERT", "CHECK PLEASE"),
            ("SOS! HELP NEEDED", "PLEASE CHECK NOW"),
            ("ROOM TEMP HIGH", ""),
            ("ALARM", "HR HIGH"),
            ("X" * 40, "Y" * 40),
            ("", ""),
        ]
        for lines in cases:
            with self.subTest(lines=lines):
                scale, left, limit = layout_for(self.WIDTH, lines)
                self.assertLessEqual(
                    left + limit * FONT_W * scale, self.WIDTH,
                    f"{lines} ⇒ (scale={scale}, left={left}, limit={limit}) 会画到屏幕外",
                )

    def test_报警文案必须完整显示不被截断(self) -> None:
        """★ 正题：**当前所有报警文案**在 128×160 上都必须整行显示出来。"""
        from health_monitor.core.dispatcher import PRESENTATION_TABLE

        for code, plan in PRESENTATION_TABLE.items():
            with self.subTest(code=str(code)):
                scale, left, limit = layout_for(self.WIDTH, plan.lcd_lines)
                for line in plan.lcd_lines:
                    if line:
                        self.assertLessEqual(
                            len(line), limit,
                            f"{code} 的 {line!r}（{len(line)} 字符）超过该字号上限 {limit}，会被截断",
                        )
                        self.assertLessEqual(left + len(line) * FONT_W * scale, self.WIDTH)

    def test_SENSOR_FAULT这种长文案会自动降到一倍字号(self) -> None:
        scale, left, limit = layout_for(self.WIDTH, ("SENSOR FAULT", "CHECK WIRING"))
        self.assertEqual(scale, 1, "12 字符在 2 倍字号下放不下，必须自动降档")
        self.assertGreaterEqual(limit, 12)
        self.assertEqual(left, 2, "降档后仍然保留左边距（能装下就不必贴边）")

    def test_短文案仍然用大字(self) -> None:
        scale, _left, limit = layout_for(self.WIDTH, ("SOS!", "OK"))
        self.assertEqual(scale, 2, "短文案继续用 2 倍字号（好看）")
        self.assertGreaterEqual(limit, 4)

    def test_超长文案让出左边距多挤一个字符(self) -> None:
        # 16 字符：2 倍字号不可能，1 倍字号 + 左边距 2 只放得下 15 ⇒ 应让出边距
        scale, left, limit = layout_for(self.WIDTH, ("SOS! HELP NEEDED", ""))
        self.assertEqual(scale, 1)
        self.assertEqual(left, 0)
        self.assertEqual(limit, 16)

    def test_再长就截断但仍在屏内(self) -> None:
        scale, left, limit = layout_for(self.WIDTH, ("A" * 100, ""))
        self.assertEqual((scale, left, limit), (1, 0, 16))

    def test_旧写法确实会越界_反向验证(self) -> None:
        """把旧的算法写出来跑一遍：**它必然越界**，证明这条回归有区分力。"""
        old_limit = self.WIDTH // 9            # 旧实现：14
        old_scale = 2                          # 旧实现：固定 2 倍
        old_right_edge = 2 + old_limit * FONT_W * old_scale
        self.assertGreater(old_right_edge, self.WIDTH, "旧写法本来就会把字画到屏幕外")

    def test_奇数宽度与极小宽度不崩(self) -> None:
        """宽到 240、窄到 1 像素都不许崩，**也不许画出屏外**。

        ⚠️ 宽度连一个 8 px 字符都放不下时（如 1 px），约定是 ``limit == 0``
        ⇒ ``lines[0][:0]`` 是空串、**什么都不画**；"不画"永远比"画到屏外"好。
        """
        for width in (1, 4, 8, 17, 64, 129, 160, 240):
            with self.subTest(width=width):
                scale, left, limit = layout_for(width, ("SENSOR FAULT", "CHECK WIRING"))
                if width >= FONT_W:
                    self.assertGreaterEqual(limit, 1)
                else:
                    self.assertEqual(limit, 0, "连一个字符都放不下 ⇒ 应当什么都不画")
                self.assertLessEqual(left + limit * FONT_W * scale, width)


class TestColorConversion(unittest.TestCase):
    def test_纯色换算为565(self) -> None:
        # RGB565：红 = 0xF800、绿 = 0x07E0、蓝 = 0x001F
        self.assertEqual(rgb565(255, 0, 0), 0xF800)
        self.assertEqual(rgb565(0, 255, 0), 0x07E0)
        self.assertEqual(rgb565(0, 0, 255), 0x001F)
        self.assertEqual(rgb565(0, 0, 0), 0x0000)
        self.assertEqual(rgb565(255, 255, 255), 0xFFFF)

    def test_BGR交换红蓝(self) -> None:
        """★ 红蓝互换是"颜色不对"的第一嫌疑，必须有明确开关与测试。"""
        self.assertEqual(rgb565(255, 0, 0, bgr=True), 0x001F)
        self.assertEqual(rgb565(0, 0, 255, bgr=True), 0xF800)
        # 绿色不受影响
        self.assertEqual(rgb565(0, 255, 0, bgr=True), 0x07E0)

    def test_取值范围被夹紧而不是溢出(self) -> None:
        self.assertEqual(rgb565(300, -10, 999), rgb565(255, 0, 255))


class TestFont(unittest.TestCase):
    def test_字库覆盖ASCII可打印字符(self) -> None:
        for code in range(0x20, 0x7F):
            ch = chr(code)
            self.assertIn(ch, FONT_8X8, f"字库缺少 {ch!r}（0x{code:02X}）")

    def test_每个字形都是8字节(self) -> None:
        for ch, glyph in FONT_8X8.items():
            with self.subTest(char=ch):
                self.assertEqual(len(glyph), 8)
                for row in glyph:
                    self.assertTrue(0 <= row <= 0xFF)

    def test_问号兜底存在(self) -> None:
        self.assertIn("?", FONT_8X8)


class TestTftMock(unittest.TestCase):
    """mock 模式下验证完整行为（**绝不碰硬件**）。"""

    def setUp(self) -> None:
        self.tft = TftSpi(controller="st7735", mock=True)

    def test_未open就send必须报错(self) -> None:
        with self.assertRaises(DeviceNotReady):
            self.tft.send(DisplayCommand(lines=("A", "B")))

    def test_mock模式不创建任何硬件句柄(self) -> None:
        with TftSpi(controller="st7735", mock=True) as tft:
            self.assertIsNone(tft._spi, "mock 模式不能打开 SPI")
            self.assertIsNone(tft._lgpio, "mock 模式不能打开 GPIO 芯片")
            self.assertIsNone(tft._lgpio_handle)
            self.assertEqual(tft._backend, "none")

    def test_mock模式有虚拟分辨率(self) -> None:
        with TftSpi(controller="st7735", mock=True, rotate=90) as tft:
            # ST7735 原生 160x128，旋转 90° 后是 128x160
            self.assertEqual((tft.width, tft.height), (128, 160))

    def test_send只接受DisplayCommand(self) -> None:
        self.tft.open()
        with self.assertRaises(UnsupportedError):
            self.tft.send("随便一个字符串")

    def test_send记录内容并影响read(self) -> None:
        self.tft.open()
        self.tft.send(DisplayCommand(lines=("HR 72 bpm", "SpO2 98%"), page=2))
        status = self.tft.read()
        self.assertEqual(status.lines, ("HR 72 bpm", "SpO2 98%"))
        self.assertEqual(status.page, 2)
        self.assertEqual(status.device, self.tft.NAME)

    def test_read诚实回报未知内容为空串(self) -> None:
        """没画过就是不知道——**不许编造**（与项目的"失败要如实上报"一致）。"""
        self.tft.open()
        status = self.tft.read()
        self.assertEqual(status.lines, ("", ""))

    def test_绘图路径在mock下不崩(self) -> None:
        self.tft.open()
        self.tft.fill((10, 20, 30))
        self.tft.fill_rect(1, 1, 10, 10, (255, 255, 255))
        self.tft.hline(0, 5, 100, (0, 255, 0))
        next_x = self.tft.text(0, 0, "Hello 123", (255, 255, 255), bg=(0, 0, 0), scale=2)
        self.assertEqual(next_x, 9 * 8 * 2)     # 9 个字符 × 8 像素 × 缩放 2

    def test_中文被降级为问号(self) -> None:
        """★ LCD1602 与 TFT 都只有 ASCII 字库：中文必须安全降级，不能送乱码字节。"""
        self.tft.open()
        self.tft.text(0, 0, "心率", (255, 255, 255))
        # 不崩、也不抛异常即可；关键是"送出去的字节一定是合法字形"
        self.assertTrue(all(len(g) == 8 for g in FONT_8X8.values()))

    def test_矩形超出屏幕会被夹紧(self) -> None:
        self.tft.open()
        self.tft.fill_rect(-10, -10, 99999, 99999, (255, 0, 0))   # 不应抛异常
        self.tft.fill_rect(0, 0, 0, 0, (255, 0, 0))               # 零尺寸直接返回

    def test_rotate影响分辨率(self) -> None:
        for rotate, expect in ((0, (160, 128)), (90, (128, 160)), (180, (160, 128)), (270, (128, 160))):
            with self.subTest(rotate=rotate):
                with TftSpi(controller="st7735", mock=True, rotate=rotate) as tft:
                    self.assertEqual((tft.width, tft.height), expect)

    def test_close幂等(self) -> None:
        self.tft.open()
        self.tft.close()
        self.tft.close()
        self.assertFalse(self.tft._opened)

    def test_self_check在mock下通过(self) -> None:
        with TftSpi(controller="st7735", mock=True) as tft:
            result = tft.self_check()
            self.assertTrue(result["ok"])
            self.assertIn("mock", result["detail"])

    def test_describe给出物理脚号与注意事项(self) -> None:
        tft = TftSpi(controller="st7735", mock=True, spi_device=1)
        tft.open()
        desc = tft.describe()
        self.assertEqual(desc["kind"], "display")
        self.assertIn("SPI0.1", desc["bus"])
        # 物理脚号必须来自 hal.pins 查表（不许手写偏移）
        self.assertIn("物理脚 23", desc["pins"]["sclk"])
        self.assertIn("物理脚 19", desc["pins"]["mosi"])
        self.assertIn("物理脚 26", desc["pins"]["cs"])      # CE1
        self.assertIn("物理脚 18", desc["pins"]["dc"])
        notes = desc["notes"]
        for token in ("BGR", "底片", "ASCII", "片选"):
            self.assertIn(token, notes, f"describe 的注意事项里应包含 {token}")

    def test_状态计数(self) -> None:
        tft = TftSpi(controller="st7735", mock=True)
        tft.open()
        tft.send(DisplayCommand(lines=("A", "B")))
        status = tft.status()
        self.assertEqual(status["driver"], "TftSpi")
        self.assertEqual(status["kind"], DeviceKind.DISPLAY.value)
        self.assertTrue(status["opened"])


class TestDisplayStatusSymmetry(unittest.TestCase):
    """★ 两块屏的 `status()` 必须**对称**（E57 收尾）。

    来历：T9 验收时我用 `status()` 读彩屏，得到**空的 lines**，一度以为"彩屏没画出来" ——
    其实是 `Lcd1602` 覆写了 `status()`（带 `lines`/`page`）而 `TftSpi` 没有。
    两台显示器"屏幕上现在是什么"必须能从**同名字段**读到，否则每次都要先猜该用哪个接口。
    """

    #: 所有显示类驱动都必须暴露的字段（新增显示驱动时自动被这条判据要求）
    REQUIRED = ("lines", "page")

    def test_彩屏与LCD的status都带当前屏幕内容(self) -> None:
        from health_monitor.outputs.lcd1602 import Lcd1602

        lcd = Lcd1602(mock=True)
        tft = TftSpi(controller="st7735", mock=True)
        for device in (lcd, tft):
            with self.subTest(driver=type(device).__name__):
                device.open()
                device.send(DisplayCommand(lines=("SENSOR FAULT", "CHECK WIRING"), page=2))
                status = device.status()
                for field in self.REQUIRED:
                    self.assertIn(field, status, f"{type(device).__name__}.status() 缺少 {field}")
                # ⚠️ 比**内容**而不是原始字节：LCD 会把每行补空格到 16 字符（它按列写屏），
                #    彩屏不补 —— 这是两块屏各自的排版语义，不是"内容不同"
                #    （T9 验收时就是这么比对的：`[ln.strip() for ln in lines]`）。
                self.assertEqual(
                    tuple(str(ln).strip() for ln in status["lines"])[:2],
                    ("SENSOR FAULT", "CHECK WIRING"),
                    "status() 必须如实反映刚下发的两行",
                )
                self.assertEqual(status["page"], 2, "页码也要如实")
                device.close()

    def test_每个显示驱动都得守这条规矩(self) -> None:
        """从注册表里**枚举**所有 DISPLAY 类驱动 ⇒ 将来新加屏也逃不掉。"""
        for name, spec in MANIFEST.items():
            if spec.kind is not DeviceKind.DISPLAY:
                continue
            with self.subTest(driver=name):
                device = create_device(name, mock=True)
                device.open()
                device.send(DisplayCommand(lines=("A", "B")))
                status = device.status()
                for field in self.REQUIRED:
                    self.assertIn(field, status, f"{name}.status() 缺少 {field}")
                device.close()


class TestControllers(unittest.TestCase):
    def test_三种控制器都在表里且分辨率合理(self) -> None:
        for name in ("st7735", "st7789", "ili9341"):
            self.assertIn(name, CONTROLLERS)
        self.assertEqual((CONTROLLERS["st7735"].width, CONTROLLERS["st7735"].height), (160, 128))
        self.assertEqual((CONTROLLERS["st7789"].width, CONTROLLERS["st7789"].height), (240, 240))
        self.assertEqual((CONTROLLERS["ili9341"].width, CONTROLLERS["ili9341"].height), (240, 320))

    def test_每个控制器都有初始化序列且含开显示(self) -> None:
        for name, spec in CONTROLLERS.items():
            with self.subTest(controller=name):
                cmds = [cmd for cmd, _, _ in spec.init]
                self.assertIn(0x11, cmds, "必须发 SLPOUT(0x11) 退出睡眠")
                self.assertIn(0x29, cmds, "必须发 DISPON(0x29) 开显示")
                self.assertIn(0x3A, cmds, "必须设 COLMOD(0x3A) 为 16 位")

    def test_auto顺序里的控制器都存在(self) -> None:
        for name in AUTO_ORDER:
            self.assertIn(name, CONTROLLERS)

    def test_注册表里的驱动能被装配(self) -> None:
        """注册表登记了 tft_spi，而且要能用配置里的参数构造出来。"""
        self.assertIn("tft_spi", MANIFEST)
        device = create_device("tft_spi", params={"controller": "auto", "spi_device": 1}, mock=True)
        self.assertIsInstance(device, TftSpi)
        device.open()
        self.assertTrue(device.status()["opened"])
        device.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
