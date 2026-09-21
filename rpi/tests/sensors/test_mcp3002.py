"""MCP3002 驱动测试。

覆盖（照抄 ``test_button.py`` 的三条纪律）：
1. **纯逻辑**（raw→电压、电压→温度）用已知数值断言，不碰硬件；
2. **mock 模式**可用，且断言"绝不创建真实 SPI/GPIO 对象"；
3. **错误路径**要测：未 open、通道非法、总线故障留痕、钩子字节数不符。
"""

from __future__ import annotations

import unittest

from health_monitor.hal import (
    ConfigError,
    DeviceIOError,
    DeviceInitError,
    DeviceKind,
    DeviceNotReady,
    MockBus,
    PrecisionTempSample,
    UnsupportedError,
)
from health_monitor.sensors.mcp3002 import (
    ADC_MAX_RAW,
    Mcp3002,
    raw_to_voltage,
    voltage_to_celsius,
)


# ==========================================================================
# 一、纯逻辑：raw → 电压 → 温度
# ==========================================================================


class TestAdcPureMath(unittest.TestCase):
    def test_原始值到电压的已知值(self) -> None:
        """1023/3.3 = 310，所以 155→0.5V、465→1.5V、775→2.5V 都是"整"值，便于出题。"""
        self.assertEqual(raw_to_voltage(0, 3.3), 0.0)
        self.assertEqual(raw_to_voltage(ADC_MAX_RAW, 3.3), 3.3)
        self.assertAlmostEqual(raw_to_voltage(155, 3.3), 0.5, places=9)
        self.assertAlmostEqual(raw_to_voltage(465, 3.3), 1.5, places=9)
        self.assertAlmostEqual(raw_to_voltage(775, 3.3), 2.5, places=9)

    def test_边界原始值不崩(self) -> None:
        """raw=0 与 raw=1023 是**合法**输入（传感器掉线/输入过压时真机会读到它们）。"""
        self.assertEqual(raw_to_voltage(0), 0.0)
        self.assertEqual(raw_to_voltage(1023), 3.3)

    def test_非法原始值被拒且不静默(self) -> None:
        for bad in (-1, 1024, 99999, 10 ** 9):
            with self.subTest(raw=bad):
                with self.assertRaises(ConfigError):
                    raw_to_voltage(bad)

    def test_参考电压必须为正(self) -> None:
        for bad_vref in (0.0, -3.3):
            with self.subTest(vref=bad_vref):
                with self.assertRaises(ConfigError):
                    raw_to_voltage(100, bad_vref)

    def test_电压到温度已知值(self) -> None:
        """TMP36：10mV/°C、25°C→750mV。"""
        self.assertAlmostEqual(voltage_to_celsius(0.75), 25.0, places=9)
        self.assertAlmostEqual(voltage_to_celsius(1.00), 50.0, places=9)
        self.assertAlmostEqual(voltage_to_celsius(0.50), 0.0, places=9)
        self.assertAlmostEqual(voltage_to_celsius(0.10), -40.0, places=9)

    def test_电压到温度对负值与海量值不崩(self) -> None:
        self.assertAlmostEqual(voltage_to_celsius(-1.0), -150.0, places=9)
        self.assertAlmostEqual(voltage_to_celsius(1000.0), 99950.0, places=6)
        with self.assertRaises(ConfigError):
            voltage_to_celsius(1.0, mv_per_c=0.0)


# ==========================================================================
# 二、驱动：Mcp3002
# ==========================================================================


class TestMcp3002Driver(unittest.TestCase):
    def test_未open就read必须报错(self) -> None:
        adc = Mcp3002(mock=True)
        with self.assertRaises(DeviceNotReady):
            adc.read()
        with self.assertRaises(DeviceNotReady):
            adc.read_raw(0)

    def test_mock模式可用且不碰硬件(self) -> None:
        with Mcp3002(mock=True, name="adc") as adc:
            self.assertTrue(adc.mock)
            self.assertEqual(adc.KIND, DeviceKind.PRECISION)
            self.assertEqual(adc.NAME, "mcp3002")
            self.assertIsNone(adc.bus, "mock 模式不应自建总线")
            self.assertIsNone(adc._cs, "mock 模式不允许创建真实 GPIO/SPI 对象")
            self.assertEqual(adc.mock_source, "synthetic")

            sample = adc.read()
            self.assertIsInstance(sample, PrecisionTempSample)
            self.assertTrue(sample.ok)
            self.assertIsNotNone(sample.raw_adc)
            self.assertIsNotNone(sample.voltage_v)
            self.assertIsNone(sample.temperature_c, "单读 ADC 不知道挂的是什么传感器")

    def test_mock合成波形在量程内且会变化(self) -> None:
        adc = Mcp3002(mock=True)
        adc.open()
        raws = [adc.read_raw(0) for _ in range(12)]
        self.assertTrue(all(0 <= r <= ADC_MAX_RAW for r in raws), raws)
        self.assertGreater(len(set(raws)), 1, "合成波形应当随时间变化，而不是一个常数")
        adc.close()

    # -- SPI 时序 ------------------------------------------------------

    def test_SPI发送3字节且控制字符合数据手册(self) -> None:
        """起始位=1(bit6)、单端=1(bit5)、MSBF=1(bit3)、通道在 bit4。"""
        bus = MockBus()
        sent: list = []
        bus.hook_spi(0, lambda _b, data: (sent.append(bytes(data)), b"\x00\x00\x00")[1])
        adc = Mcp3002(spi_bus=0, spi_device=0, channel=0, bus=bus, mock=True)
        adc.open()
        adc.read_raw(0)
        adc.read_raw(1)

        self.assertEqual(len(sent), 2)
        self.assertEqual(len(sent[0]), 3, "MCP3002 一次事务发 3 字节")
        self.assertEqual(sent[0], bytes([0x68, 0x00, 0x00]), "通道 0 控制字应为 0x68")
        self.assertEqual(sent[1], bytes([0x78, 0x00, 0x00]), "通道 1 控制字应为 0x78")

        cmd0 = sent[0][0]
        self.assertEqual((cmd0 >> 6) & 1, 1, "bit6 必须为 START=1")
        self.assertEqual((cmd0 >> 5) & 1, 1, "bit5 必须为 SGL/DIFF=1（单端）")
        self.assertEqual((cmd0 >> 4) & 1, 0, "bit4 是通道号，通道 0 应为 0")
        self.assertEqual((cmd0 >> 3) & 1, 1, "bit3 必须为 MSBF=1（高位先行）")
        self.assertEqual(cmd0 >> 7, 0, "bit7 是数据手册要求的前置 0 位")
        self.assertEqual((sent[1][0] >> 4) & 1, 1, "通道 1 的 bit4 应为 1")

        # 驱动确实把传输记进了总线流水
        self.assertEqual(bus.op_count("spi_xfer"), 2)
        self.assertEqual(bus.operations("spi_xfer")[0][1][0], 0)
        adc.close()

    def test_回读字节按10位解码(self) -> None:
        """Rx = xxxxx0RR RRRRRRRR：第 1 字节低 2 位是结果高位。"""
        cases = {
            bytes([0x00, 0x00, 0x00]): 0,
            bytes([0x03, 0xFF, 0x00]): 1023,
            bytes([0x02, 0xFF, 0x00]): 767,
            bytes([0xFF, 0x03, 0x00]): 771,  # 高位无关位必须被掩掉
        }
        for reply, expected in cases.items():
            with self.subTest(reply=reply):
                bus = MockBus().hook_spi(0, lambda _b, _d, r=reply: r)
                adc = Mcp3002(bus=bus, mock=True)
                adc.open()
                self.assertEqual(adc.read_raw(0), expected)
                adc.close()

    def test_回读字节不足直接报错(self) -> None:
        with self.assertRaises(DeviceIOError):
            Mcp3002._decode(b"\x02")

    def test_read返回带原始值与电压的样本(self) -> None:
        # raw=233 → 0.7516V；温度由 TMP36 负责算，这里必须是 None
        bus = MockBus().hook_spi(0, lambda _b, _d: bytes([0x00, 233, 0x00]))
        adc = Mcp3002(vref=3.3, bus=bus, mock=True)
        adc.open()
        sample = adc.read()
        self.assertEqual(sample.raw_adc, 233)
        self.assertAlmostEqual(sample.voltage_v, 0.7516, places=4)
        self.assertIsNone(sample.temperature_c)
        self.assertEqual(adc.status()["read_count"], 1)
        adc.close()

    def test_总线故障要留痕并抛DeviceIOError(self) -> None:
        bus = MockBus().hook_spi(0, lambda _b, _d: b"\x00\x00\x00")
        bus.fail_next()
        adc = Mcp3002(bus=bus, mock=True)
        adc.open()
        with self.assertRaises(DeviceIOError):
            adc.read_raw(0)
        st = adc.status()
        self.assertEqual(st["fault_count"], 1)
        # last_error 记录的是**根因**（总线抛出的原始异常），便于排障
        self.assertIn("MockBus", st["last_error"] or "")
        self.assertIn("DeviceIOError", st["last_error"] or "")
        # 故障注入只影响下一次：恢复后应能正常读数
        self.assertEqual(adc.read_raw(0), 0)
        self.assertEqual(adc.status()["read_count"], 1)
        adc.close()

    def test_钩子返回长度不对时报错而不是静默(self) -> None:
        bus = MockBus().hook_spi(0, lambda _b, _d: b"\x00")  # 长度 1 ≠ 3
        adc = Mcp3002(bus=bus, mock=True)
        adc.open()
        with self.assertRaises(DeviceIOError):
            adc.read_raw(0)
        self.assertEqual(adc.status()["fault_count"], 1)
        adc.close()

    # -- 参数与注入 ----------------------------------------------------

    def test_构造参数非法直接报错(self) -> None:
        with self.assertRaises(ConfigError):
            Mcp3002(channel=2, mock=True)
        with self.assertRaises(ConfigError):
            Mcp3002(channel=-1, mock=True)
        with self.assertRaises(ConfigError):
            Mcp3002(vref=0.0, mock=True)
        with self.assertRaises(ConfigError):
            Mcp3002(spi_device=2, mock=True)
        with self.assertRaises(ConfigError):
            Mcp3002(spi_bus=-1, mock=True)
        with self.assertRaises(ConfigError):
            Mcp3002(spi_device=-1, mock=True)

    def test_read_raw通道非法报ConfigError(self) -> None:
        adc = Mcp3002(mock=True)
        adc.open()
        for bad in (-1, 2, 99, None, "x"):
            with self.subTest(channel=bad):
                with self.assertRaises(ConfigError):
                    adc.read_raw(bad)  # type: ignore[arg-type]
        adc.close()

    def test_注入原始值与电压(self) -> None:
        adc = Mcp3002(mock=True)
        adc.open()
        adc.inject_raw(ADC_MAX_RAW)
        self.assertEqual(adc.mock_source, "inject")
        self.assertEqual(adc.read_raw(0), 1023)
        self.assertEqual(adc.read().voltage_v, 3.3)

        adc.inject_voltage(1.5)  # 1.5/3.3*1023 = 465（正好整除）
        self.assertEqual(adc.read_raw(0), 465)
        self.assertAlmostEqual(adc.read().voltage_v, 1.5, places=9)

        adc.clear_inject()
        self.assertEqual(adc.mock_source, "synthetic")
        adc.close()

    def test_注入越界被拒(self) -> None:
        adc = Mcp3002(mock=True)
        adc.open()
        with self.assertRaises(ConfigError):
            adc.inject_raw(-1)
        with self.assertRaises(ConfigError):
            adc.inject_raw(99999)
        with self.assertRaises(ConfigError):
            adc.inject_voltage(5.0)
        with self.assertRaises(ConfigError):
            adc.inject_voltage(-0.1)
        adc.close()

    def test_真实模式下注入被拒绝(self) -> None:
        """防止误把 mock 注入接口用到真机上（真机上不该有"人造采样值"）。"""
        adc = Mcp3002(mock=False)
        with self.assertRaises(UnsupportedError):
            adc.inject_raw(100)
        with self.assertRaises(UnsupportedError):
            adc.inject_voltage(1.0)
        with self.assertRaises(UnsupportedError):
            adc.clear_inject()

    # -- 说明 / 状态 / 生命周期 ----------------------------------------

    def test_describe写清SPI物理脚与软件CS(self) -> None:
        adc = Mcp3002(cs_pin=25, mock=True)
        desc = adc.describe()
        self.assertEqual(desc["name"], "mcp3002")
        self.assertEqual(desc["kind"], "precision")
        self.assertIn("脚 23", desc["pins"]["clk"])
        self.assertIn("脚 21", desc["pins"]["d_out_miso"])
        self.assertIn("脚 19", desc["pins"]["d_in_mosi"])
        self.assertIn("3.3V", desc["pins"]["vdd_vref"])
        self.assertIn("GPIO25", desc["pins"]["cs"])
        self.assertIn("软件控制", desc["notes"], "必须说明 CS 可以换成任意 GPIO 软件控制")

    def test_硬件片选时describe给出CE0(self) -> None:
        desc = Mcp3002(mock=True).describe()
        self.assertIn("脚 24", desc["pins"]["cs"])

    def test_self_check可用(self) -> None:
        adc = Mcp3002(mock=True)
        adc.open()
        result = adc.self_check()
        self.assertTrue(result["ok"])
        adc.close()

    def test_close幂等且status计数正确(self) -> None:
        adc = Mcp3002(mock=True)
        adc.open()
        adc.read()
        adc.read_raw(0)
        st = adc.status()
        self.assertEqual(st["driver"], "Mcp3002")
        self.assertEqual(st["read_count"], 2)
        self.assertEqual(st["fault_count"], 0)
        self.assertTrue(st["opened"])

        adc.close()
        adc.close()  # 幂等，不应抛异常
        self.assertFalse(adc._opened)
        self.assertFalse(adc.status()["opened"])

    def test_真实模式SPI打不开必须抛DeviceInitError且带线索(self) -> None:
        adc = Mcp3002(mock=False)
        try:
            adc.open()
        except DeviceInitError as exc:
            text = str(exc)
            self.assertIn("SPI", text)
            self.assertIn("raspi-config", text)
        except Exception as exc:  # noqa: BLE001 - 在真树莓派上会成功，跳过
            self.skipTest(f"本机环境不支持真实 SPI：{type(exc).__name__}")
        else:
            adc.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
