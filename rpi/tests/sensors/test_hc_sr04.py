"""HC-SR04 驱动测试。

覆盖：
1. **纯逻辑**：``echo_us_to_cm`` 的已知数值断言（20°C 时 5800µs ≈ 99.59cm，
   手册给的"约 58µs/cm"）以及 ``cm_to_echo_us`` 互逆；
2. **重试/中位数/超时/量程**：用测试替身 ``FakeHcSr04`` 覆写唯一接触硬件的方法
   ``_measure_once``，于是这些逻辑全部能在没有硬件的情况下验证；
3. **超时策略**：本驱动选定"返回 ``ok=False``"（不抛异常），测试把这个选择固化下来。
"""

from __future__ import annotations

import unittest

from health_monitor.hal import (
    ConfigError,
    DeviceInitError,
    DeviceKind,
    DeviceNotReady,
    DeviceTimeout,
    RangeSample,
    UnsupportedError,
)
from health_monitor.sensors.hc_sr04 import (
    HcSr04,
    cm_to_echo_us,
    echo_us_to_cm,
    sound_speed_m_per_s,
)


class FakeHcSr04(HcSr04):
    """测试替身：只替换"碰硬件"的那一步，重试/中位数/量程/超时逻辑保持原样。

    ``readings`` 里每个元素要么是 ``(distance_cm, echo_us)``，要么是一个异常实例
    （用来模拟回波超时/接线故障）。读完后继续要数据会抛 ``DeviceTimeout``，
    于是"某几次失败"也能被精确构造出来。
    """

    def __init__(self, readings: list, **kwargs) -> None:
        kwargs.setdefault("mock", False)
        super().__init__(**kwargs)
        self._readings = list(readings)
        self.attempts = 0

    def open(self) -> None:  # 不碰 GPIO
        self._opened = True

    def close(self) -> None:
        self._opened = False

    def _measure_once(self):
        self.attempts += 1
        if not self._readings:
            raise DeviceTimeout("测试替身：没有更多读数了")
        item = self._readings.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


# ==========================================================================
# 一、纯逻辑：回波时长 ↔ 距离
# ==========================================================================


class TestHcSr04PureMath(unittest.TestCase):
    def test_声速公式已知值(self) -> None:
        self.assertAlmostEqual(sound_speed_m_per_s(20.0), 343.42, places=6)
        self.assertAlmostEqual(sound_speed_m_per_s(0.0), 331.3, places=6)
        self.assertAlmostEqual(sound_speed_m_per_s(40.0), 355.54, places=6)

    def test_已知数值_20度时5800微秒约等于100厘米(self) -> None:
        """HC-SR04 手册给的例子：约 58µs/cm，5800µs ↔ 约 100cm。"""
        distance_cm = echo_us_to_cm(5800.0, 20.0)
        self.assertAlmostEqual(distance_cm, 99.5918, places=3)
        self.assertAlmostEqual(distance_cm, 100.0, delta=1.0)

    def test_回波到厘米的其他已知值(self) -> None:
        self.assertEqual(echo_us_to_cm(0.0, 20.0), 0.0)
        self.assertAlmostEqual(echo_us_to_cm(1000.0, 20.0), 17.171, places=3)
        self.assertAlmostEqual(echo_us_to_cm(58.2, 20.0), 1.0, delta=0.01)

    def test_温度补偿方向正确(self) -> None:
        """声速随温度升高而变快 ⇒ 同样回波时长换算出的距离更大。"""
        cold = echo_us_to_cm(5800.0, 0.0)
        normal = echo_us_to_cm(5800.0, 20.0)
        hot = echo_us_to_cm(5800.0, 40.0)
        self.assertLess(cold, normal)
        self.assertLess(normal, hot)
        self.assertAlmostEqual(cold, 96.077, places=3)
        self.assertAlmostEqual(hot, 103.1066, places=3)

    def test_反函数与正函数互逆(self) -> None:
        for echo_us in (0.0, 580.0, 5800.0, 29000.0):
            with self.subTest(echo_us=echo_us):
                self.assertAlmostEqual(
                    cm_to_echo_us(echo_us_to_cm(echo_us, 20.0), 20.0),
                    echo_us,
                    places=6,
                )

    def test_负值与海量值不许崩(self) -> None:
        with self.assertRaises(ConfigError):
            echo_us_to_cm(-1.0, 20.0)
        with self.assertRaises(ConfigError):
            cm_to_echo_us(-1.0, 20.0)
        # 海量值：给出巨大但有限的数字即可（不抛奇怪的 OverflowError）
        huge = echo_us_to_cm(1e9, 20.0)
        self.assertGreater(huge, 1e6)
        self.assertTrue(huge == huge)  # 不是 NaN

    def test_温度超出物理范围被拒(self) -> None:
        for bad_temp in (-300.0, 1000.0):
            with self.subTest(temperature_c=bad_temp):
                with self.assertRaises(ConfigError):
                    echo_us_to_cm(1000.0, bad_temp)
                with self.assertRaises(ConfigError):
                    sound_speed_m_per_s(bad_temp)


# ==========================================================================
# 二、驱动：mock 模式
# ==========================================================================


class TestHcSr04Mock(unittest.TestCase):
    def test_未open就read必须报错(self) -> None:
        drv = HcSr04(mock=True)
        with self.assertRaises(DeviceNotReady):
            drv.read()

    def test_mock模式可用且不碰硬件(self) -> None:
        with HcSr04(mock=True, name="sonar") as drv:
            self.assertTrue(drv.mock)
            self.assertEqual(drv.KIND, DeviceKind.RANGE)
            self.assertEqual(drv.NAME, "hc_sr04")
            self.assertIsNone(drv._sensor, "mock 模式不允许创建真实 gpiozero 对象")
            self.assertIsNone(drv._lgpio_handle, "mock 模式不允许打开 lgpio chip")

            sample = drv.read()
            self.assertIsInstance(sample, RangeSample)
            self.assertTrue(sample.ok)
            self.assertGreaterEqual(sample.distance_cm, 30.0)
            self.assertLessEqual(sample.distance_cm, 120.0)
            self.assertGreater(sample.echo_us, 0.0)

    def test_mock给出合理波动值(self) -> None:
        drv = HcSr04(mock=True)
        drv.open()
        values = [drv.read().distance_cm for _ in range(40)]
        self.assertTrue(all(30.0 <= v <= 120.0 for v in values), (min(values), max(values)))
        self.assertGreater(max(values) - min(values), 30.0, "应当有明显波动，而不是常数")
        drv.close()

    def test_注入距离生效(self) -> None:
        drv = HcSr04(mock=True)
        drv.open()
        drv.inject_distance(50.0)
        sample = drv.read()
        self.assertTrue(sample.ok)
        self.assertAlmostEqual(sample.distance_cm, 50.0, places=9)
        self.assertAlmostEqual(sample.echo_us, cm_to_echo_us(50.0, 20.0), places=6)
        drv.close()

    def test_注入超量程距离返回ok为False(self) -> None:
        drv = HcSr04(mock=True)
        drv.open()
        for bad_cm in (500.0, 1.0):
            with self.subTest(distance_cm=bad_cm):
                drv.inject_distance(bad_cm)
                sample = drv.read()
                self.assertFalse(sample.ok)
                self.assertIsNone(sample.distance_cm)
                self.assertIn("超出量程", sample.error or "")
        self.assertEqual(drv.status()["fault_count"], 2)
        drv.close()

    def test_注入非法距离被拒(self) -> None:
        drv = HcSr04(mock=True)
        drv.open()
        with self.assertRaises(ConfigError):
            drv.inject_distance(0.0)
        with self.assertRaises(ConfigError):
            drv.inject_distance(-10.0)
        drv.close()

    def test_真实模式下注入被拒绝(self) -> None:
        drv = HcSr04(mock=False)
        with self.assertRaises(UnsupportedError):
            drv.inject_distance(50.0)


# ==========================================================================
# 三、驱动：重试 / 中位数 / 超时 / 量程（测试替身）
# ==========================================================================


class TestHcSr04RetryLogic(unittest.TestCase):
    def test_重试三次取中位数(self) -> None:
        drv = FakeHcSr04([(100.0, 5800.0), (120.0, 7000.0), (110.0, 6400.0)], retries=3)
        drv.open()
        sample = drv.read()
        self.assertTrue(sample.ok)
        self.assertEqual(sample.distance_cm, 110.0, "100/120/110 的中位数是 110")
        self.assertEqual(sample.echo_us, 6400.0)
        self.assertEqual(drv.attempts, 3)
        self.assertEqual(drv.status()["read_count"], 1)
        self.assertEqual(drv.status()["fault_count"], 0)
        drv.close()

    def test_失败后重试直到成功(self) -> None:
        drv = FakeHcSr04(
            [DeviceTimeout("第一次超时"), (105.0, 6100.0), (107.0, 6200.0)], retries=3
        )
        drv.open()
        sample = drv.read()
        self.assertTrue(sample.ok)
        self.assertEqual(drv.attempts, 3)
        # 两次有效读数（105/107）取偏大的那个，规则固定可预期
        self.assertEqual(sample.distance_cm, 107.0)
        drv.close()

    def test_全部超时返回ok为False且不抛异常(self) -> None:
        """本驱动的超时策略：**返回 ok=False 的样本**（不打断上层轮询），但要留痕。"""
        drv = FakeHcSr04([DeviceTimeout("回波超时")] * 3, retries=3)
        drv.open()
        sample = drv.read()  # 不应抛异常
        self.assertFalse(sample.ok)
        self.assertIsNone(sample.distance_cm)
        self.assertIsNone(sample.echo_us)
        self.assertIn("回波超时", sample.error or "")
        st = drv.status()
        self.assertEqual(st["fault_count"], 1, "超时必须计入故障，不能静默")
        self.assertEqual(st["read_count"], 0)
        self.assertIn("超时", st["last_error"] or "")
        self.assertEqual(drv.attempts, 3, "失败也要重试满 3 次")
        drv.close()

    def test_全部超量程返回ok为False(self) -> None:
        drv = FakeHcSr04([(500.0, 29000.0), (1.0, 60.0), (600.0, 35000.0)], retries=3)
        drv.open()
        sample = drv.read()
        self.assertFalse(sample.ok)
        self.assertIsNone(sample.distance_cm)
        self.assertIn("超出量程", sample.error or "")
        self.assertEqual(drv.status()["fault_count"], 1)
        drv.close()

    def test_部分超量程仍返回有效中位数(self) -> None:
        drv = FakeHcSr04([(0.5, 30.0), (50.0, 2900.0), (500.0, 29000.0)], retries=3)
        drv.open()
        sample = drv.read()
        self.assertTrue(sample.ok)
        self.assertEqual(sample.distance_cm, 50.0)
        self.assertEqual(sample.echo_us, 2900.0)
        drv.close()

    def test_底层抛意外异常也能兜住(self) -> None:
        drv = FakeHcSr04([OSError("GPIO 打不开")] * 3, retries=3)
        drv.open()
        sample = drv.read()  # 不许崩
        self.assertFalse(sample.ok)
        self.assertIn("OSError", sample.error or "")
        self.assertEqual(drv.status()["fault_count"], 1)
        drv.close()

    def test_重试次数为1时只测一次(self) -> None:
        drv = FakeHcSr04([(60.0, 3500.0)], retries=1)
        drv.open()
        self.assertEqual(drv.read().distance_cm, 60.0)
        self.assertEqual(drv.attempts, 1)
        drv.close()

    def test_量程边界点(self) -> None:
        drv = FakeHcSr04([(2.0, 116.0)], retries=1)
        drv.open()
        self.assertTrue(drv.read().ok, "2cm 是量程下界，应当有效")
        drv.close()

        drv = FakeHcSr04([(400.0, 23300.0)], retries=1)
        drv.open()
        self.assertTrue(drv.read().ok, "400cm 是量程上界，应当有效")
        drv.close()

        for bad in (1.99, 400.01):
            with self.subTest(distance_cm=bad):
                drv = FakeHcSr04([(bad, 1.0)], retries=1)
                drv.open()
                self.assertFalse(drv.read().ok)
                drv.close()


# ==========================================================================
# 四、参数 / 说明 / 生命周期
# ==========================================================================


class TestHcSr04Contract(unittest.TestCase):
    def test_构造参数非法直接报错(self) -> None:
        with self.assertRaises(ConfigError):
            HcSr04(trig_pin=17, echo_pin=17, mock=True)
        with self.assertRaises(ConfigError):
            HcSr04(trig_pin=99, mock=True)
        with self.assertRaises(ConfigError):
            HcSr04(echo_pin=-1, mock=True)
        with self.assertRaises(ConfigError):
            HcSr04(timeout_us=0, mock=True)
        with self.assertRaises(ConfigError):
            HcSr04(retries=0, mock=True)
        with self.assertRaises(ConfigError):
            HcSr04(temperature_c=-300.0, mock=True)

    def test_describe写清TXS0102与5V与树莓派5注意事项(self) -> None:
        drv = HcSr04(trig_pin=17, echo_pin=27, mock=True)
        desc = drv.describe()
        self.assertEqual(desc["name"], "hc_sr04")
        self.assertEqual(desc["kind"], "range")
        self.assertIn("物理脚 2", desc["pins"]["vcc"])
        self.assertIn("5V", desc["pins"]["vcc"])
        # 引脚必须同时给出 BCM 与**物理脚号**（且物理脚号来自 hal.pins 查表，不是偏移公式）
        self.assertIn("GPIO17", desc["pins"]["trig"])
        self.assertIn("物理脚 11", desc["pins"]["trig"])
        self.assertIn("GPIO27", desc["pins"]["echo"])
        self.assertIn("物理脚 13", desc["pins"]["echo"])
        self.assertIn("TXS0102", desc["pins"]["echo"])
        self.assertIn("TXS0102", desc["pins"]["level_shifter"])
        notes = desc["notes"]
        self.assertIn("ok=False", notes, "必须写明超时策略")
        self.assertIn("RPi.GPIO", notes, "必须写明树莓派 5 上 RPi.GPIO 不可用")
        self.assertIn("中位数", notes)

    def test_默认引脚不占用PIR与按键的引脚(self) -> None:
        """**回归测试**：HC-SR04 默认引脚曾写成 GPIO17/GPIO27，
        与 HC-SR501（GPIO17）、求救按钮（GPIO27）撞脚，启用拓展件时会三方抢线。
        默认值必须避开它们（现为 GPIO5/GPIO6）。
        """
        drv = HcSr04(mock=True)
        self.assertNotIn(drv.trig_pin, (17, 27), "TRIG 默认值不能占用 PIR(GPIO17) 或按钮(GPIO27)")
        self.assertNotIn(drv.echo_pin, (17, 27), "ECHO 默认值不能占用 PIR(GPIO17) 或按钮(GPIO27)")
        self.assertNotEqual(drv.trig_pin, drv.echo_pin)

    def test_status与self_check(self) -> None:
        drv = HcSr04(mock=True)
        drv.open()
        self.assertTrue(drv.self_check()["ok"])
        st = drv.status()
        self.assertEqual(st["driver"], "HcSr04")
        self.assertEqual(st["kind"], "range")
        self.assertGreaterEqual(st["read_count"], 1)
        self.assertEqual(st["fault_count"], 0)
        drv.close()

    def test_close幂等(self) -> None:
        drv = HcSr04(mock=True)
        drv.open()
        drv.close()
        drv.close()  # 幂等，不应抛异常
        self.assertFalse(drv._opened)

    def test_真实模式open失败必须抛DeviceInitError且带线索(self) -> None:
        drv = HcSr04(mock=False)
        try:
            drv.open()
        except DeviceInitError as exc:
            text = str(exc)
            self.assertIn("GPIO", text)
            self.assertIn("5V", text)
        except Exception as exc:  # noqa: BLE001 - 在真树莓派上会成功，跳过
            self.skipTest(f"本机环境不支持真实 GPIO：{type(exc).__name__}")
        else:
            drv.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
