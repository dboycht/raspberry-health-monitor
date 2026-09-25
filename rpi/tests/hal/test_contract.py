"""契约层测试：数据模型 / MockBus / 注册表。

这些测试是**全项目的"地基检测"**：任何驱动作者改了契约层而没同步，
这里会先红（而不是等到别人的代码莫名其妙跑不起来）。
"""

from __future__ import annotations

import json
import unittest

from health_monitor.hal import (
    AlarmCode,
    AlarmEvent,
    AmbientSample,
    BeepCommand,
    Device,
    DeviceIOError,
    DeviceKind,
    DeviceNotFoundError,
    LightCommand,
    MANIFEST,
    MockBus,
    MotionSample,
    MotionState,
    PrecisionTempSample,
    Severity,
    SpeakCommand,
    VitalSignsSample,
    create_device,
    get_spec,
    list_drivers,
    snapshot,
)


class TestModels(unittest.TestCase):
    def test_样本默认ok且带时间戳(self) -> None:
        s = AmbientSample(temperature_c=26.5, humidity_percent=55.0)
        self.assertTrue(s.ok)
        self.assertGreater(s.ts, 0)
        self.assertAlmostEqual(s.temperature_c, 26.5)

    def test_心率未测出时必须是None而不是0(self) -> None:
        """0 bpm 是"物理上不可能"，填 0 会被报警引擎误判成"心率过低"。"""
        v = VitalSignsSample()
        self.assertIsNone(v.heart_rate_bpm)
        self.assertIsNone(v.spo2_percent)
        self.assertFalse(v.finger_detected)

    def test_精密温度保留原始ADC值(self) -> None:
        """报告里要写"电压→温度"的换算推导，所以 raw_adc / voltage_v 必须保留。"""
        s = PrecisionTempSample(temperature_c=25.0, raw_adc=465, voltage_v=0.75)
        self.assertEqual(s.raw_adc, 465)
        self.assertAlmostEqual(s.voltage_v, 0.75)

    def test_严重度可比较(self) -> None:
        self.assertGreater(Severity.CRITICAL, Severity.WARNING)
        self.assertGreater(Severity.WARNING, Severity.NOTICE)
        self.assertEqual(int(Severity.NORMAL), 0)

    def test_运动样本UNKNOWN不冒充检测到人(self) -> None:
        m = MotionSample(state=MotionState.UNKNOWN)
        self.assertFalse(m.detected)

    def test_报警事件可JSON序列化(self) -> None:
        ev = AlarmEvent(
            code=AlarmCode.HR_TOO_HIGH,
            severity=Severity.WARNING,
            message="心率偏高",
            value=128.0,
            unit="bpm",
            source="max30102",
        )
        payload = json.dumps(ev.to_dict(), ensure_ascii=False)
        back = json.loads(payload)
        self.assertEqual(back["code"], "hr_too_high")
        self.assertEqual(back["severity"], 2)
        self.assertEqual(back["message"], "心率偏高")

    def test_报警文案不许含markdown标记(self) -> None:
        """界面/音箱文本会原样显示，markdown 标记会露出来（见 memory/16）。"""
        for code in AlarmCode:
            self.assertNotIn("*", code.value)
            self.assertNotIn("`", code.value)

    def test_指令对象存在且默认值合理(self) -> None:
        self.assertEqual(SpeakCommand(text="请注意安全").text, "请注意安全")
        self.assertEqual(BeepCommand().times, 1)
        self.assertEqual(LightCommand().color, "green")


class TestMockBus(unittest.TestCase):
    def test_未注册钩子时返回等长全零(self) -> None:
        bus = MockBus()
        self.assertEqual(bus.i2c_read(1, 0x57, 4), b"\x00\x00\x00\x00")

    def test_注册I2C钩子后返回指定字节(self) -> None:
        bus = MockBus().set_i2c_reply(0x27, b"\xab\xcd")
        self.assertEqual(bus.i2c_read(1, 0x27, 2), b"\xab\xcd")

    def test_钩子长度与请求不一致时报错(self) -> None:
        """钩子返回长度不对 = 钩子写错了，必须马上暴露，而不是静默截断。"""
        bus = MockBus().set_i2c_reply(0x57, b"\x01")
        with self.assertRaises(DeviceIOError):
            bus.i2c_read(1, 0x57, 6)

    def test_记录操作流水(self) -> None:
        bus = MockBus()
        bus.i2c_write(1, 0x27, b"\x01\x02")
        bus.i2c_read(1, 0x27, 2)
        self.assertEqual(bus.op_count(), 2)
        self.assertEqual(bus.operations("i2c_write")[0][1], (1, 0x27, b"\x01\x02"))

    def test_故障注入只影响下一次(self) -> None:
        bus = MockBus().fail_next()
        with self.assertRaises(DeviceIOError):
            bus.i2c_read(1, 0x57, 1)
        self.assertEqual(bus.i2c_read(1, 0x57, 1), b"\x00")

    def test_SPI_XFER与I2C独立(self) -> None:
        """SPI 是全双工等长传输：钩子必须返回与输入等长的字节。"""
        bus = MockBus().hook_spi(0, lambda _b, data: bytes([data[0] | 0x80, data[1]]))
        out = bus.spi_xfer(0, 0, b"\x01\x80")
        self.assertEqual(out[0], 0x81)
        self.assertEqual(out[1], 0x80)
        self.assertEqual(bus.operations("spi_xfer")[0][1][0], 0)

    def test_SPI钩子返回长度不一致时报错(self) -> None:
        bus = MockBus().hook_spi(0, lambda _b, _data: b"\x00")
        with self.assertRaises(DeviceIOError):
            bus.spi_xfer(0, 0, b"\x01\x02")

    def test_i2c_scan反映已注册地址(self) -> None:
        bus = MockBus().set_i2c_reply(0x3F, b"\x00")
        self.assertEqual(bus.i2c_scan(), [0x3F])


class _FakeSmbus:
    """一个"每次操作都报 Errno 121"的假 smbus2（模拟器件不在总线上）。

    为什么要有这类测试（2026-09-25 真机实测，见 ERROR.md E34）：
    驱动**只按契约捕获 `DeviceIOError` / `DeviceTimeout`**，而 `RealBus` 原来把
    smbus2 的裸 `OSError` 直接漏了出去 —— 器件没插时用户看到的是
    `OSError: [Errno 121] Remote I/O error`，没有任何排查线索，
    还会让真机验收测试整批变红（而器件只是没接）。
    """

    @staticmethod
    def _boom(*_args, **_kwargs):
        raise OSError(121, "Remote I/O error")

    read_i2c_block_data = _boom
    write_i2c_block_data = _boom
    read_byte = _boom


class TestRealBusI2CErrors(unittest.TestCase):
    """`RealBus` 的 I2C 失败必须翻译成契约里的 `DeviceIOError`（带排查线索）。"""

    def _bus(self):
        from health_monitor.hal.mock_bus import RealBus

        bus = RealBus(i2c_bus=1)
        bus._smbus = _FakeSmbus()          # 注入假句柄，不碰真实硬件
        return bus

    def test_读失败翻译成DeviceIOError(self) -> None:
        with self.assertRaises(DeviceIOError) as ctx:
            self._bus().i2c_read(1, 0x57, 1)
        text = str(ctx.exception)
        self.assertIn("0x57", text, "要指出是哪个地址")
        self.assertIn("121", text, "要保留内核 errno（排错第一线索）")
        self.assertIn("i2cdetect", text, "要给下一条可执行命令")

    def test_写失败翻译成DeviceIOError(self) -> None:
        with self.assertRaises(DeviceIOError):
            self._bus().i2c_write(1, 0x27, b"\x01\x02")

    def test_读写组合失败翻译成DeviceIOError(self) -> None:
        with self.assertRaises(DeviceIOError):
            self._bus().i2c_write_read(1, 0x57, b"\x21", 1)

    def test_保留原始异常作为cause(self) -> None:
        """`raise ... from exc` 不能省：栈里要能看到底层 errno。"""
        with self.assertRaises(DeviceIOError) as ctx:
            self._bus().i2c_read(1, 0x57, 1)
        self.assertIsInstance(ctx.exception.__cause__, OSError)

    def test_没装smbus2时不冒充IO错误(self) -> None:
        """`UnsupportedError`（没装 smbus2）不是"器件没应答"，不许被翻译成 `DeviceIOError`。

        判据 = 让 `import smbus2` 失败，断言抛的是 `UnsupportedError` 而不是 `DeviceIOError`：
        两者的处置完全不同（一个去装包，一个去查接线）。
        """
        import builtins
        from unittest import mock

        from health_monitor.hal.exceptions import UnsupportedError

        bus = self._bus()
        bus._smbus = None
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "smbus2":
                raise ImportError("simulated: smbus2 not installed")
            return real_import(name, *args, **kwargs)

        with mock.patch.object(builtins, "__import__", side_effect=fake_import):
            with self.assertRaises(UnsupportedError) as ctx:
                bus.i2c_read(1, 0x57, 1)
        self.assertIn("smbus2", str(ctx.exception))
        self.assertNotIn("Errno", str(ctx.exception), "装包问题不该被说成总线错误")


class TestRegistry(unittest.TestCase):
    def test_清单里每个驱动都必须继承Device且参数可构造(self) -> None:
        """装配期就能发现"类名写错/没继承 Device/参数不匹配"三类错误。"""
        for name in sorted(MANIFEST):
            with self.subTest(driver=name):
                spec = get_spec(name)
                cls = spec.load()          # 模块必须存在
                self.assertTrue(issubclass(cls, Device))
                self.assertEqual(cls.NAME, name, "类的 NAME 必须与驱动名一致")

    def test_每个驱动都要有中文名与说明(self) -> None:
        for name, spec in MANIFEST.items():
            with self.subTest(driver=name):
                self.assertTrue(spec.label.strip(), f"{name} 缺少中文名")
                self.assertIn(spec.option, ("核心", "温控", "活动", "拓展"))

    def test_未知驱动名报错且列出可用项(self) -> None:
        with self.assertRaises(DeviceNotFoundError) as ctx:
            get_spec("不存在的驱动")
        self.assertIn("max30102", str(ctx.exception))

    def test_list_drivers可按大类过滤(self) -> None:
        vital = list_drivers(kind=DeviceKind.VITAL)
        self.assertEqual([s.name for s in vital], ["max30102"])
        self.assertTrue(all(s.kind is DeviceKind.VITAL for s in vital))

    def test_create_device返回未初始化的实例(self) -> None:
        dev = create_device("button", mock=True)
        self.assertFalse(dev.status()["opened"])
        self.assertTrue(dev.mock)

    def test_create_device参数类型错误给出可读提示(self) -> None:
        from health_monitor.hal.exceptions import ConfigError

        with self.assertRaises(ConfigError) as ctx:
            create_device("button", {"不存在的参数": 1}, mock=True)
        self.assertIn("接口规格说明书", str(ctx.exception))

    def test_snapshot能给出全体驱动体检结果(self) -> None:
        """注意：未实现的驱动会体现在 error 字段里，而不是让整个体检崩掉。"""
        report = snapshot(mock=True)
        self.assertIn("devices", report)
        self.assertIn("button", report["devices"])
        self.assertTrue(report["devices"]["button"]["loaded"])


class TestAlarmContract(unittest.TestCase):
    def test_所有报警码都有中文可读名(self) -> None:
        """报警码要出现在手机端与 LCD 上，必须能翻译成中文。"""
        mapping = {
            AlarmCode.HR_TOO_HIGH: "心率过高",
            AlarmCode.SPO2_TOO_LOW: "血氧过低",
            AlarmCode.SOS_PRESSED: "紧急求助",
            AlarmCode.NO_MOTION_TOO_LONG: "长时间无活动",
        }
        for code, text in mapping.items():
            self.assertIsInstance(code.value, str)
            self.assertTrue(text.endswith(("高", "低", "助", "动")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
