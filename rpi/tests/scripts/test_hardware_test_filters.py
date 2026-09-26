"""`hardware_test.py --only / --exclude / --list` 的纯逻辑测试 + 两条"登记"守卫。

为什么单独测（2026-09-26）
--------------------------
阶梯式验收（见 `docs/14-分步实施路线图.md`）要"**一次只加一个器件**"，
所以 `hardware_test.py` 需要"只验我这一级新加的那个"。这类筛选最坏的失败不是报错，
而是**静默地什么都没查、却给出全绿报告**（假通过比假失败更危险）。
因此这里既测筛选逻辑本身，也钉住两条"必须登记"的约定：

1. 注册表里**每个驱动**都要在 `DRIVER_NEEDS` 里声明"需要哪些检查类别"；
2. 默认配置里**每个设备名**要么有专门检查（`CHECKED_DEVICE_NAMES`），
   要么在 `COMPOSED_DEVICE_HINTS` 里说明"用哪个脚本验"。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_RPI_DIR = Path(__file__).resolve().parents[2]      # .../rpi
_SCRIPTS_DIR = _RPI_DIR / "scripts"
for _path in (_RPI_DIR, _SCRIPTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import hardware_test  # noqa: E402  必须在 sys.path 处理之后导入
from health_monitor.core.config import DEFAULT_CONFIG  # noqa: E402
from health_monitor.hal.registry import MANIFEST  # noqa: E402


class TestParseNameList(unittest.TestCase):
    def test_空与None(self) -> None:
        self.assertEqual(hardware_test.parse_name_list(None), set())
        self.assertEqual(hardware_test.parse_name_list([]), set())
        self.assertEqual(hardware_test.parse_name_list([""]), set())

    def test_逗号与重复与空白(self) -> None:
        self.assertEqual(
            hardware_test.parse_name_list(["ambient,display", " display ", "ambient"]),
            {"ambient", "display"},
        )


class TestNeedsOf(unittest.TestCase):
    def test_按驱动给出检查类别(self) -> None:
        self.assertEqual(hardware_test.needs_of(["dht11"]), {"gpio"})
        self.assertEqual(hardware_test.needs_of(["max30102"]), {"i2c"})
        self.assertEqual(hardware_test.needs_of(["lcd1602"]), {"i2c"})
        self.assertEqual(hardware_test.needs_of(["tmp36"]), {"spi"})
        self.assertEqual(hardware_test.needs_of(["bt_speaker"]), {"audio"})
        self.assertEqual(hardware_test.needs_of(["tft_spi"]), {"gpio", "spi"})

    def test_多个驱动取并集(self) -> None:
        self.assertEqual(
            hardware_test.needs_of(["dht11", "max30102", "bt_speaker"]),
            {"gpio", "i2c", "audio"},
        )

    def test_未知驱动按gpio处理(self) -> None:
        """宁可多查一项，也不能因为"没登记"就**跳过**检查（那会变成漏查）。"""
        self.assertEqual(hardware_test.needs_of(["某个还没登记的驱动"]), {"gpio"})

    def test_空列表(self) -> None:
        self.assertEqual(hardware_test.needs_of([]), set())


class TestSelectDevices(unittest.TestCase):
    """`--only` / `--exclude` 的选择逻辑（含三种必须报错的情形）。"""

    CONFIGURED = [
        ("vitals", "max30102"),
        ("ambient", "dht11"),
        ("display", "lcd1602"),
        ("distance", "hc_sr04"),
    ]
    ENABLED = {"vitals", "ambient", "display"}          # distance 是 disabled

    def test_不限制时是全部已启用(self) -> None:
        selected, disabled, unknown_only, unknown_excl = hardware_test.select_devices(
            self.CONFIGURED, self.ENABLED, set(), set()
        )
        self.assertEqual(selected, ["vitals", "ambient", "display"])
        self.assertEqual((disabled, unknown_only, unknown_excl), ([], [], []))

    def test_only只选指定的且保持配置顺序(self) -> None:
        selected, *_ = hardware_test.select_devices(
            self.CONFIGURED, self.ENABLED, {"display", "vitals"}, set()
        )
        self.assertEqual(selected, ["vitals", "display"])

    def test_exclude排除指定的(self) -> None:
        selected, *_ = hardware_test.select_devices(
            self.CONFIGURED, self.ENABLED, set(), {"ambient"}
        )
        self.assertEqual(selected, ["vitals", "display"])

    def test_only与exclude同时命中时排除优先(self) -> None:
        selected, *_ = hardware_test.select_devices(
            self.CONFIGURED, self.ENABLED, {"ambient"}, {"ambient"}
        )
        self.assertEqual(selected, [])

    def test_名字写错要能被发现(self) -> None:
        """★ 关键：写错名字**绝不能**变成"什么都没查却全绿"。"""
        selected, _, unknown_only, unknown_excl = hardware_test.select_devices(
            self.CONFIGURED, self.ENABLED, {"ambientt"}, {"nope"}
        )
        self.assertEqual(selected, [])
        self.assertEqual(unknown_only, ["ambientt"])
        self.assertEqual(unknown_excl, ["nope"])

    def test_点名了disabled的器件要能被发现(self) -> None:
        selected, disabled, *_ = hardware_test.select_devices(
            self.CONFIGURED, self.ENABLED, {"distance"}, set()
        )
        self.assertEqual(selected, [])
        self.assertEqual(disabled, ["distance"])


class TestScopeFilters(unittest.TestCase):
    """前置检查也要跟着收窄：只验 DHT11 时不该因为"没接 I2C 器件"而报红。"""

    def test_设备节点检查(self) -> None:
        self.assertFalse(hardware_test.node_check_needed("I2C 总线 /dev/i2c-1", {"gpio"}))
        self.assertTrue(hardware_test.node_check_needed("I2C 总线 /dev/i2c-1", {"i2c"}))
        self.assertFalse(hardware_test.node_check_needed("SPI /dev/spidev0.0", {"i2c"}))
        self.assertTrue(hardware_test.node_check_needed("SPI /dev/spidev0.0", {"spi"}))
        self.assertFalse(hardware_test.node_check_needed("GPIO 设备", {"i2c"}))
        self.assertTrue(hardware_test.node_check_needed("GPIO 设备", {"gpio"}))

    def test_工具与库检查(self) -> None:
        self.assertFalse(hardware_test.tool_check_needed("python 模块 smbus2", {"spi"}))
        self.assertTrue(hardware_test.tool_check_needed("python 模块 smbus2", {"i2c"}))
        self.assertTrue(hardware_test.tool_check_needed("i2cdetect", {"i2c"}))
        self.assertTrue(hardware_test.tool_check_needed("python 模块 spidev", {"spi"}))
        self.assertFalse(hardware_test.tool_check_needed("python 模块 spidev", {"gpio"}))
        self.assertTrue(hardware_test.tool_check_needed("espeak-ng", {"audio"}))
        self.assertFalse(hardware_test.tool_check_needed("aplay", {"gpio"}))


class TestLedColorsToCheck(unittest.TestCase):
    """LED 检查要点哪些颜色：**以器件自报为准**（防"点不存在的红灯"造成假红）。

    2026-09-26 规划 T2 时发现：配置在 9-24 去掉了红灯（GPIO24 让给 TFT 的 DC），
    而脚本硬写"绿→黄→红" ⇒ 驱动按设计抛 `UnsupportedError` ⇒ 好灯被判 FAIL。
    """

    class _Led:
        def __init__(self, colors=None, boom=False) -> None:
            self._colors = colors
            self._boom = boom

        def available_colors(self):
            if self._boom:
                raise RuntimeError("器件自报失败")
            return list(self._colors)

    def test_按器件自报的颜色(self) -> None:
        led = self._Led(["green", "off", "yellow"])
        self.assertEqual(hardware_test.led_colors_to_check(led), ["green", "yellow"])

    def test_没有红灯就只点绿黄(self) -> None:
        """本项目现状：只有 green/yellow。"""
        led = self._Led(["green", "yellow", "off"])
        self.assertNotIn("red", hardware_test.led_colors_to_check(led))

    def test_器件不提供颜色时退回默认三色(self) -> None:
        self.assertEqual(
            hardware_test.led_colors_to_check(object()),
            list(hardware_test.DEFAULT_LED_COLORS),
        )

    def test_自报失败也要退回默认(self) -> None:
        led = self._Led(["green"], boom=True)
        self.assertEqual(
            hardware_test.led_colors_to_check(led),
            list(hardware_test.DEFAULT_LED_COLORS),
        )

    def test_自报为空时退回默认(self) -> None:
        led = self._Led(["off"])
        self.assertEqual(
            hardware_test.led_colors_to_check(led),
            list(hardware_test.DEFAULT_LED_COLORS),
        )


class TestRegistrationGuards(unittest.TestCase):
    """新增驱动 / 新建设备时"必须登记"，否则守卫会红 —— 这是防漏查的机器判据。"""

    def test_注册表里的每个驱动都登记了检查类别(self) -> None:
        missing = sorted(set(MANIFEST) - set(hardware_test.DRIVER_NEEDS))
        self.assertEqual(
            missing, [],
            msg=(
                "这些驱动没在 hardware_test.DRIVER_NEEDS 里登记『检查类别』 —— "
                "`--only` 会因此算错需要的总线/依赖检查（可能漏查 I2C/SPI 是否就绪）："
                f"{missing}"
            ),
        )

    def test_检查类别只用约定的四种(self) -> None:
        allowed = {"i2c", "spi", "gpio", "audio"}
        used = set()
        for needs in hardware_test.DRIVER_NEEDS.values():
            used |= set(needs)
        self.assertTrue(used <= allowed, f"出现了未约定的检查类别：{sorted(used - allowed)}")

    def test_默认配置里的设备都有检查或明确提示(self) -> None:
        covered = hardware_test.CHECKED_DEVICE_NAMES | set(hardware_test.COMPOSED_DEVICE_HINTS)
        missing = sorted(set(DEFAULT_CONFIG["devices"]) - covered)
        self.assertEqual(
            missing, [],
            msg=(
                "默认配置里的这些设备既没有专门检查、也没有在 COMPOSED_DEVICE_HINTS 里说明"
                "用什么脚本验 —— 用户 `--only <它>` 时会看到『打开成功』就以为验过了："
                f"{missing}"
            ),
        )
