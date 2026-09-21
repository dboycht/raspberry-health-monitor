"""TMP36 驱动测试（模拟输出器件，经 MCP3002 读取）。

三条重点（与 ``test_button.py`` 同一套纪律）：
1. **纯逻辑**：``voltage_to_celsius`` 用已知数值断言（0.75V→25°C 等）；
2. **完整换算链路**：用 MockBus 的 SPI 钩子喂 raw=233，验证 ``raw→电压→温度``
   真的走到了 25.16°C（而不是"驱动直接编了个数"）；
3. **坏点要响亮**：raw=0 / raw=1023 这类真机可能出现的坏值必须抛
   ``DataInvalidError`` 并在 ``status()["fault_count"]`` 里留痕。
"""

from __future__ import annotations

import unittest

from health_monitor.hal import (
    ConfigError,
    DataInvalidError,
    DeviceInitError,
    DeviceKind,
    DeviceNotReady,
    MockBus,
    PrecisionTempSample,
    UnsupportedError,
)
from health_monitor.sensors.mcp3002 import ADC_MAX_RAW
from health_monitor.sensors.tmp36 import (
    Tmp36,
    celsius_to_voltage,
    voltage_to_celsius,
)


# ==========================================================================
# 一、纯逻辑：电压 ↔ 温度
# ==========================================================================


class TestTmp36PureMath(unittest.TestCase):
    def test_电压到温度已知值(self) -> None:
        """TMP36 数据手册：10mV/°C、25°C→750mV、0°C→500mV、-40°C→100mV。"""
        self.assertAlmostEqual(voltage_to_celsius(0.75), 25.0, places=9)
        self.assertAlmostEqual(voltage_to_celsius(1.00), 50.0, places=9)
        self.assertAlmostEqual(voltage_to_celsius(0.50), 0.0, places=9)
        self.assertAlmostEqual(voltage_to_celsius(0.10), -40.0, places=9)
        self.assertAlmostEqual(voltage_to_celsius(0.751), 25.1, places=9)

    def test_电压到温度对负值与海量值不崩(self) -> None:
        self.assertAlmostEqual(voltage_to_celsius(0.0), -50.0, places=9)
        self.assertAlmostEqual(voltage_to_celsius(-0.5), -100.0, places=9)
        self.assertAlmostEqual(voltage_to_celsius(1000.0), 99950.0, places=6)
        with self.assertRaises(ConfigError):
            voltage_to_celsius(1.0, mv_per_c=-10.0)

    def test_反函数与正函数互逆(self) -> None:
        self.assertAlmostEqual(celsius_to_voltage(25.0), 0.75, places=12)
        self.assertAlmostEqual(celsius_to_voltage(0.0), 0.50, places=12)
        self.assertAlmostEqual(celsius_to_voltage(100.0), 1.50, places=12)
        for t in (-40.0, 20.0, 25.0, 36.6, 125.0):
            with self.subTest(t=t):
                self.assertAlmostEqual(
                    voltage_to_celsius(celsius_to_voltage(t)), t, places=9
                )

    def test_原始值到温度的实际数字(self) -> None:
        """把报告里要写的推导固定成断言：raw → V=raw/1023*3.3 → °C。"""
        self.assertAlmostEqual(232 / 1023 * 3.3, 0.748387, places=6)
        self.assertAlmostEqual(voltage_to_celsius(232 / 1023 * 3.3), 24.8387, places=4)
        self.assertAlmostEqual(voltage_to_celsius(233 / 1023 * 3.3), 25.1613, places=4)


# ==========================================================================
# 二、驱动：Tmp36
# ==========================================================================


class TestTmp36Driver(unittest.TestCase):
    def test_未open就read必须报错(self) -> None:
        tmp = Tmp36(mock=True)
        with self.assertRaises(DeviceNotReady):
            tmp.read()

    def test_mock模式可用且不碰硬件(self) -> None:
        with Tmp36(mock=True, name="tmp36") as tmp:
            self.assertTrue(tmp.mock)
            self.assertEqual(tmp.KIND, DeviceKind.PRECISION)
            self.assertEqual(tmp.NAME, "tmp36")
            self.assertIsNone(tmp.adc.bus, "mock 模式不应自建总线")
            self.assertIsNone(tmp.adc._cs, "mock 模式不允许创建真实 SPI/GPIO 对象")

            sample = tmp.read()
            self.assertIsInstance(sample, PrecisionTempSample)
            self.assertTrue(sample.ok)
            # mock 走的是"温度→电压→10位量化→电压→温度"，所以温度只要求落在合理区间。
            # ⚠️ 中心值是 **36.5°C（体温）**：本器件在本项目里贴的是体温通道，
            #    默认阈值区间 35.5~37.5°C；若合成室温（如 25°C）会一启动就误报"体温偏低"。
            self.assertGreaterEqual(sample.temperature_c, 35.5)
            self.assertLessEqual(sample.temperature_c, 37.5)
            self.assertIsNotNone(sample.raw_adc)
            self.assertIsNotNone(sample.voltage_v)

    def test_mock给出平滑变化的模拟温度(self) -> None:
        """验证合成温度确实在"变化"，而不是一个常数。

        ⚠️ 判据要按**真实的量化分辨率**来定：10 位 ADC（vref 3.3V）+ 10mV/°C
        ⇒ 1 LSB ≈ 3.23mV ≈ **0.32°C**。合成正弦跨度 0.6°C 时，
        实测只会出现 3 个不同读数（36.13 / 36.45 / 36.77）——
        如果这里硬要求"≥4 个不同值"，测的其实是 ADC 分辨率而不是驱动逻辑。
        """
        tmp = Tmp36(mock=True)
        tmp.open()
        temps = [tmp.read().temperature_c for _ in range(60)]  # 一个完整正弦周期约 52 次
        self.assertTrue(all(36.0 <= t <= 37.0 for t in temps), (min(temps), max(temps)))
        self.assertGreater(max(temps) - min(temps), 0.5, "一个周期应跨过约 0.6°C（36.2~36.8）")
        self.assertGreaterEqual(len(set(temps)), 3, "至少应出现 3 个不同读数（量化步长约 0.32°C）")
        tmp.close()

    def test_注入温度生效(self) -> None:
        tmp = Tmp36(mock=True)
        tmp.open()
        tmp.inject_temperature(37.0)
        sample = tmp.read()
        # 10 位量化误差 ≈ ±0.16°C，取 ±0.4 的容差
        self.assertAlmostEqual(sample.temperature_c, 37.0, delta=0.4)
        tmp.close()

    def test_注入电压生效(self) -> None:
        tmp = Tmp36(mock=True)
        tmp.open()
        tmp.inject_voltage(0.75)
        self.assertAlmostEqual(tmp.read().temperature_c, 25.0, delta=0.4)
        tmp.close()

    def test_用SPI钩子验证完整换算链路(self) -> None:
        """raw=233 → 0.7516V → 25.16°C：驱动器件的**真实**换算路径。"""
        bus = MockBus().hook_spi(0, lambda _b, _d: bytes([0x00, 233, 0x00]))
        tmp = Tmp36(spi_bus=0, spi_device=0, channel=0, vref=3.3, bus=bus, mock=True)
        tmp.open()
        sample = tmp.read()
        self.assertTrue(sample.ok)
        self.assertEqual(sample.raw_adc, 233)
        self.assertAlmostEqual(sample.voltage_v, 0.7516, places=4)
        self.assertAlmostEqual(sample.temperature_c, 25.1613, places=3)
        # 总线确实收到了 3 字节控制字
        self.assertEqual(len(bus.operations("spi_xfer")), 1)
        self.assertEqual(bus.operations("spi_xfer")[0][1][2], bytes([0x68, 0x00, 0x00]))
        tmp.close()

    def test_越界原始值抛DataInvalidError并留痕(self) -> None:
        cases = {0: -50.0, ADC_MAX_RAW: 280.0}
        for raw, expected_temp in cases.items():
            with self.subTest(raw=raw):
                tmp = Tmp36(mock=True)
                tmp.open()
                tmp.inject_raw(raw)
                with self.assertRaises(DataInvalidError) as ctx:
                    tmp.read()
                self.assertIn("越界", str(ctx.exception))
                self.assertIn("3.3V", str(ctx.exception), "排查线索里要提醒供电是 3.3V")
                st = tmp.status()
                self.assertEqual(st["fault_count"], 1)
                self.assertEqual(st["read_count"], 0)
                self.assertIn("DataInvalidError", st["last_error"] or "")
                tmp.close()
                self.assertAlmostEqual(
                    voltage_to_celsius(raw / 1023 * 3.3), expected_temp, places=6
                )

    def test_量程边界点(self) -> None:
        """raw=31 → 正好 -40.0°C（有效）；raw=30 → -40.3°C（无效）。"""
        tmp = Tmp36(mock=True)
        tmp.open()
        tmp.inject_raw(31)
        self.assertAlmostEqual(tmp.read().temperature_c, -40.0, places=6)

        tmp.inject_raw(542)
        self.assertAlmostEqual(tmp.read().temperature_c, 124.8387, places=4)

        for bad_raw in (30, 543):
            with self.subTest(raw=bad_raw):
                tmp.inject_raw(bad_raw)
                with self.assertRaises(DataInvalidError):
                    tmp.read()
        tmp.close()

    def test_标定偏移生效(self) -> None:
        raw_driver = Tmp36(mock=True, calibration_offset_c=0.0)
        cal_driver = Tmp36(mock=True, calibration_offset_c=2.0)
        raw_driver.open()
        cal_driver.open()
        raw_driver.inject_voltage(0.75)
        cal_driver.inject_voltage(0.75)
        diff = cal_driver.read().temperature_c - raw_driver.read().temperature_c
        self.assertAlmostEqual(diff, 2.0, places=9)
        raw_driver.close()
        cal_driver.close()

    def test_构造参数非法直接报错(self) -> None:
        with self.assertRaises(ConfigError):
            Tmp36(mv_per_c=0.0, mock=True)
        with self.assertRaises(ConfigError):
            Tmp36(v25=0.75, vref=0.75, mock=True)
        with self.assertRaises(ConfigError):
            Tmp36(v25=0.0, mock=True)
        with self.assertRaises(ConfigError):
            Tmp36(channel=2, mock=True)

    def test_注入越界被拒(self) -> None:
        tmp = Tmp36(mock=True)
        tmp.open()
        with self.assertRaises(ConfigError):
            tmp.inject_temperature(200.0)
        with self.assertRaises(ConfigError):
            tmp.inject_temperature(-100.0)
        with self.assertRaises(ConfigError):
            tmp.inject_voltage(-0.1)
        with self.assertRaises(ConfigError):
            tmp.inject_voltage(5.0)
        with self.assertRaises(ConfigError):
            tmp.inject_raw(-1)
        with self.assertRaises(ConfigError):
            tmp.inject_raw(1024)
        tmp.close()

    def test_真实模式下注入被拒绝(self) -> None:
        tmp = Tmp36(mock=False)
        with self.assertRaises(UnsupportedError):
            tmp.inject_temperature(30.0)
        with self.assertRaises(UnsupportedError):
            tmp.inject_voltage(0.8)
        with self.assertRaises(UnsupportedError):
            tmp.inject_raw(233)

    def test_describe写清供电电压与标定方法(self) -> None:
        tmp = Tmp36(mock=True)
        desc = tmp.describe()
        self.assertEqual(desc["name"], "tmp36")
        self.assertEqual(desc["kind"], "precision")
        self.assertIn("3.3V", desc["pins"]["tmp36_vs"])
        self.assertIn("不能接 5V", desc["pins"]["tmp36_vs"])
        self.assertIn("CH0", desc["pins"]["tmp36_vout"])
        self.assertIn("脚 23", desc["pins"]["adc_spi"])
        self.assertIn("MCP3002", desc["notes"])
        self.assertIn("标定", desc["notes"])

    def test_self_check与status(self) -> None:
        tmp = Tmp36(mock=True)
        tmp.open()
        self.assertTrue(tmp.self_check()["ok"])
        st = tmp.status()
        self.assertEqual(st["driver"], "Tmp36")
        self.assertGreaterEqual(st["read_count"], 1)
        self.assertEqual(st["fault_count"], 0)
        tmp.close()

    def test_close幂等(self) -> None:
        tmp = Tmp36(mock=True)
        tmp.open()
        tmp.close()
        tmp.close()  # 幂等，不应抛异常
        self.assertFalse(tmp._opened)
        self.assertFalse(tmp.adc._opened, "内层 ADC 也必须一起关闭")

    def test_真实模式打不开必须抛DeviceInitError且带线索(self) -> None:
        tmp = Tmp36(mock=False)
        try:
            tmp.open()
        except DeviceInitError as exc:
            text = str(exc)
            self.assertIn("MCP3002", text)
            self.assertIn("3.3V", text)
        except Exception as exc:  # noqa: BLE001 - 在真树莓派上会成功，跳过
            self.skipTest(f"本机环境不支持真实 SPI：{type(exc).__name__}")
        else:
            tmp.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
