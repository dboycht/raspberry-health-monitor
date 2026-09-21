"""DHT11 驱动测试 —— 覆盖"纯逻辑 + 驱动契约 + 2 秒间隔缓存策略"三层。

要点（照抄 ``test_button.py`` 范本）：
1. **纯逻辑用假时钟测**：``CachePolicy`` 的 2 秒限制、量程校验都不 sleep、不偶发失败；
2. **mock 模式断言"绝不创建真实 GPIO 对象"**（``_sensor is None``）；
3. **缓存策略要测死**：间隔不足必须返回 ``ok=False`` 的缓存值，
   而且**不能伪装成刚刚测的**（时间戳仍是上次真正测量的时刻）；
4. **失败路径要测**：打开失败抛 ``DeviceInitError`` 且带排查线索。
"""

from __future__ import annotations

import unittest

from health_monitor.hal import (
    AmbientSample,
    ConfigError,
    DeviceIOError,
    DeviceInitError,
    DeviceKind,
    DeviceNotReady,
    UnsupportedError,
    create_device,
)
from health_monitor.sensors.dht11 import (
    DHT11_HUMI_MAX_PCT,
    DHT11_HUMI_MIN_PCT,
    DHT11_MIN_INTERVAL_S,
    DHT11_TEMP_MAX_C,
    DHT11_TEMP_MIN_C,
    CachePolicy,
    Dht11,
    mock_environment,
    physical_pin,
    validate_reading,
)


class FakeClock:
    """假时钟：单测里"瞬间"跨越 2 秒，杜绝 sleep 与偶发失败。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> float:
        self.t += seconds
        return self.t


# ==========================================================================
# 一、纯逻辑：CachePolicy（DHT11 的 2 秒限制）
# ==========================================================================


class TestCachePolicy(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.policy = CachePolicy(min_interval_s=2.0, clock=self.clock)

    def test_从未读过时立刻可读(self) -> None:
        wait, remaining = self.policy.needs_wait()
        self.assertFalse(wait)
        self.assertEqual(remaining, 0.0)
        self.assertIsNone(self.policy.last_read_at)

    def test_间隔不足时必须等待并给出还差多久(self) -> None:
        self.policy.mark_read()
        self.clock.advance(0.5)
        wait, remaining = self.policy.needs_wait()
        self.assertTrue(wait, "距上次只过了 0.5s，必须等待（否则读到陈旧数据）")
        self.assertAlmostEqual(remaining, 1.5, places=3)

    def test_间隔够了就放行(self) -> None:
        self.policy.mark_read()
        self.clock.advance(DHT11_MIN_INTERVAL_S)
        wait, remaining = self.policy.needs_wait()
        self.assertFalse(wait)
        self.assertEqual(remaining, 0.0)

    def test_刚好2秒的边界算够(self) -> None:
        self.policy.mark_read(now=100.0)
        wait, _ = self.policy.needs_wait(now=102.0)
        self.assertFalse(wait, "恰好 2.0s 应视为满足限制")

    def test_mark_read记录真实时刻(self) -> None:
        self.policy.mark_read()
        self.assertEqual(self.policy.last_read_at, 1000.0)
        self.clock.advance(3.0)
        self.policy.mark_read()
        self.assertEqual(self.policy.last_read_at, 1003.0)

    def test_reset后立刻可读(self) -> None:
        self.policy.mark_read()
        self.policy.reset()
        wait, _ = self.policy.needs_wait()
        self.assertFalse(wait)


# ==========================================================================
# 二、纯逻辑：量程校验 + mock 合成数据
# ==========================================================================


class TestValidateReading(unittest.TestCase):
    def test_正常值通过(self) -> None:
        self.assertEqual(validate_reading(25.0, 55.0), "")
        self.assertEqual(validate_reading(DHT11_TEMP_MIN_C, DHT11_HUMI_MIN_PCT), "")
        self.assertEqual(validate_reading(DHT11_TEMP_MAX_C, DHT11_HUMI_MAX_PCT), "")

    def test_None与NaN要有原因(self) -> None:
        self.assertNotEqual(validate_reading(None, 50.0), "")
        self.assertNotEqual(validate_reading(25.0, None), "")
        self.assertIn("NaN", validate_reading(float("nan"), 50.0))

    def test_超量程要被拒绝(self) -> None:
        self.assertIn("温度", validate_reading(60.0, 50.0))     # 60℃ 超 DHT11 量程
        self.assertIn("温度", validate_reading(-5.0, 50.0))     # 0℃ 以下
        self.assertIn("湿度", validate_reading(25.0, 95.0))     # 95% 超量程
        self.assertIn("湿度", validate_reading(25.0, 10.0))     # 太干
        self.assertIn("物理", validate_reading(25.0, 120.0))    # 物理上不可能

    def test_自定义更严的量程生效(self) -> None:
        """用户配置 max_temperature_c=40 时，41℃ 必须被判超量程。"""
        self.assertEqual(validate_reading(39.0, 50.0, temp_limits=(0.0, 40.0)), "")
        self.assertIn("温度", validate_reading(41.0, 50.0, temp_limits=(0.0, 40.0)))

    def test_极端值不崩(self) -> None:
        for temp in (1e9, -1e9, float("inf")):
            with self.subTest(temp=temp):
                self.assertNotEqual(validate_reading(temp, 50.0), "")


class TestMockEnvironment(unittest.TestCase):
    def test_合成值始终在DHT11量程内(self) -> None:
        """模拟 0~600 秒内的任何时刻，温湿度都必须在量程内（否则演示必然报错）。"""
        for elapsed in range(0, 600, 7):
            with self.subTest(elapsed=elapsed):
                temperature_c, humidity_percent = mock_environment(float(elapsed))
                self.assertEqual(validate_reading(temperature_c, humidity_percent), "")
                self.assertGreaterEqual(temperature_c, 24.0)
                self.assertLessEqual(temperature_c, 27.0)

    def test_不是一条直线(self) -> None:
        """演示数据要"缓慢变化"，不能固定一个值（否则看起来像假的）。"""
        values = {mock_environment(float(t))[0] for t in range(0, 200, 5)}
        self.assertGreater(len(values), 5, "温度应当随时间波动")

    def test_物理脚号换算(self) -> None:
        self.assertIn("物理脚 7", physical_pin(4))
        self.assertIn("物理脚 11", physical_pin(17))
        self.assertIn("非标准", physical_pin(99))


# ==========================================================================
# 三、驱动：契约与生命周期
# ==========================================================================


class TestDht11Driver(unittest.TestCase):
    def test_未open就read必须报错(self) -> None:
        dev = Dht11(mock=True)
        with self.assertRaises(DeviceNotReady):
            dev.read()

    def test_类属性与注册表一致(self) -> None:
        self.assertEqual(Dht11.NAME, "dht11")
        self.assertEqual(Dht11.KIND, DeviceKind.AMBIENT)

    def test_默认参数符合注册表提示(self) -> None:
        dev = Dht11(mock=True)
        self.assertEqual(dev.pin, 4)
        self.assertEqual(dev.retries, 3)
        self.assertEqual(dev.min_interval_s, DHT11_MIN_INTERVAL_S)

    def test_参数非法要抛ConfigError(self) -> None:
        with self.assertRaises(ConfigError):
            Dht11(pin=99, mock=True)
        with self.assertRaises(ConfigError):
            Dht11(retries=0, mock=True)
        with self.assertRaises(ConfigError):
            Dht11(min_interval_s=0.2, mock=True)  # 低于硬件要求的 2s
        with self.assertRaises(ConfigError):
            Dht11(max_temperature_c=0.0, mock=True)

    def test_mock模式不创建真实硬件对象(self) -> None:
        with Dht11(mock=True) as dev:
            self.assertTrue(dev.mock)
            self.assertIsNone(dev._sensor, "mock 模式下不允许创建 gpiozero 对象")
            sample = dev.read()
            self.assertIsNone(dev._sensor, "读一次也不该因此建出真实 GPIO 对象")
            self.assertTrue(sample.ok, sample.error)

    def test_mock模式读数落在合理范围(self) -> None:
        dev = Dht11(mock=True)
        dev.open()
        sample = dev.read()
        self.assertIsInstance(sample, AmbientSample)
        self.assertTrue(sample.ok, sample.error)
        self.assertEqual(sample.device, "dht11")
        self.assertGreaterEqual(sample.temperature_c, 24.0)
        self.assertLessEqual(sample.temperature_c, 27.0)
        self.assertGreaterEqual(sample.humidity_percent, 40.0)
        self.assertLessEqual(sample.humidity_percent, 60.0)
        self.assertEqual(validate_reading(sample.temperature_c, sample.humidity_percent), "")
        dev.close()

    def test_inject可以喂指定温湿度(self) -> None:
        dev = Dht11(mock=True)
        dev.open()
        dev.inject(33.5, 78.0)
        sample = dev.read()
        self.assertTrue(sample.ok, sample.error)
        self.assertAlmostEqual(sample.temperature_c, 33.5, places=1)
        self.assertAlmostEqual(sample.humidity_percent, 78.0, places=1)
        dev.close()

    def test_inject超量程要如实报ok为False(self) -> None:
        """注入 60℃（超 DHT11 量程）必须返回 ok=False，且不能把 60 当正常值报出去。"""
        dev = Dht11(mock=True)
        dev.open()
        dev.inject(60.0, 50.0)
        sample = dev.read()
        self.assertFalse(sample.ok)
        self.assertIsNone(sample.temperature_c, "超量程值不许填进样本")
        self.assertIsNone(sample.humidity_percent)
        self.assertIn("温度", sample.error)
        dev.close()

    def test_真实模式下inject必须被拒绝(self) -> None:
        dev = Dht11(mock=False)
        with self.assertRaises(UnsupportedError):
            dev.inject(30.0, 60.0)
        with self.assertRaises(UnsupportedError):
            dev.set_mock_fault()

    def test_注入故障时抛IOError并留痕(self) -> None:
        dev = Dht11(mock=True)
        dev.open()
        dev.set_mock_fault()
        with self.assertRaises(DeviceIOError):
            dev.read()
        status = dev.status()
        self.assertEqual(status["fault_count"], 1, "失败必须计数，静默失败是一级缺陷")
        self.assertIn("注入的故障", status["last_error"])
        self.assertTrue(dev.read().ok, "故障只影响一次")
        dev.close()

    def test_close可重复调用(self) -> None:
        dev = Dht11(mock=True)
        dev.open()
        dev.read()
        dev.close()
        dev.close()  # 幂等，不应抛异常
        self.assertFalse(dev._opened)
        self.assertIsNone(dev._sensor)

    def test_open可重复调用(self) -> None:
        dev = Dht11(mock=True)
        dev.open()
        dev.open()  # 不应重建对象或抛异常
        self.assertTrue(dev._opened)
        dev.close()

    def test_describe写清物理脚号(self) -> None:
        dev = Dht11(pin=4, mock=True)
        desc = dev.describe()
        self.assertEqual(desc["name"], "dht11")
        self.assertEqual(desc["kind"], "ambient")
        self.assertIn("GPIO4", desc["pins"]["data"])
        self.assertIn("物理脚 7", desc["pins"]["data"])
        self.assertIn("3.3V", desc["pins"]["vcc"])
        self.assertIn("2.0s", desc["notes"])

    def test_status计数正确(self) -> None:
        dev = Dht11(mock=True)
        dev.open()
        self.assertEqual(dev.status()["read_count"], 0)
        dev.read()
        status = dev.status()
        self.assertEqual(status["read_count"], 1)
        self.assertEqual(status["fault_count"], 0)
        self.assertEqual(status["driver"], "Dht11")
        self.assertTrue(status["opened"])
        # 第二次读会被 2 秒限制挡住，但仍是"成功返回一个样本"，计数继续累加
        dev.read()
        self.assertEqual(dev.status()["read_count"], 2)
        self.assertEqual(dev.status()["fault_count"], 0)
        dev.close()

    def test_self_check可用(self) -> None:
        dev = Dht11(mock=True)
        dev.inject(26.5, 55.0)
        result = dev.self_check()
        self.assertTrue(result["ok"], result["detail"])
        self.assertIn("温度", result["detail"])
        # 自检不应被 2 秒限制挡住：紧接着再自检一次也要能读（会读新的合成值）
        again = dev.self_check()
        self.assertTrue(again["ok"], again["detail"])
        self.assertIn("温度", again["detail"])
        dev.close()

    def test_真实模式初始化失败抛DeviceInitError且带线索(self) -> None:
        """PC 上（没装 gpiozero）打开真实模式必须抛 DeviceInitError 并提示安装命令。"""
        dev = Dht11(pin=4, mock=False)
        try:
            dev.open()
        except DeviceInitError as exc:
            text = str(exc)
            self.assertIn("DHT11", text)
            self.assertIn("gpiozero", text)
            self.assertIn("python3-gpiozero", text)
        except Exception as exc:  # noqa: BLE001 - 真树莓派上可能真的成功，跳过
            self.skipTest(f"本机环境不支持真实 GPIO：{type(exc).__name__}")
        finally:
            dev.close()

    def test_装配方式与注册表一致(self) -> None:
        dev = create_device("dht11", mock=True)
        self.assertIsInstance(dev, Dht11)
        self.assertFalse(dev.status()["opened"])
        self.assertTrue(dev.mock)


# ==========================================================================
# 四、缓存策略（本次任务的重点：间隔不足必须如实标注）
# ==========================================================================


class TestDht11CacheStrategy(unittest.TestCase):
    def _make(self) -> tuple[Dht11, FakeClock]:
        dev = Dht11(mock=True, min_interval_s=2.0)
        clock = FakeClock()
        dev._cache.clock = clock  # 注入假时钟：单测不 sleep、不偶发失败
        dev.open()
        return dev, clock

    def test_间隔不足返回缓存值且ok为False(self) -> None:
        dev, clock = self._make()
        first = dev.read()
        self.assertTrue(first.ok, first.error)

        clock.advance(0.5)
        second = dev.read()
        self.assertFalse(second.ok, "间隔不足必须 ok=False，不能假装是新数据")
        self.assertIsNotNone(second.error)
        self.assertIn("不足", second.error)
        self.assertIn("缓存", second.error)
        # 缓存值照填（LCD/日志仍能显示环境值）
        self.assertEqual(second.temperature_c, first.temperature_c)
        self.assertEqual(second.humidity_percent, first.humidity_percent)
        dev.close()

    def test_缓存样本的时间戳是上次真正测量的时刻(self) -> None:
        """关键安全点：不能把缓存值的时间戳改成"现在"，那等于伪造测量时间。"""
        dev, clock = self._make()
        first = dev.read()
        clock.advance(1.0)
        second = dev.read()
        self.assertEqual(second.ts, first.ts, "缓存样本的时间戳必须还是上次测量的时刻")
        dev.close()

    def test_间隔够了就重新测量(self) -> None:
        dev, clock = self._make()
        first = dev.read()
        clock.advance(DHT11_MIN_INTERVAL_S)
        second = dev.read()
        self.assertTrue(second.ok, second.error)
        self.assertAlmostEqual(
            second.temperature_c, first.temperature_c, places=1,
            msg="假时钟只推进了 2 秒，合成温度只应缓慢变化 0.1℃ 量级",
        )
        dev.close()

    def test_重试期间不会绕过缓存策略(self) -> None:
        """间隔不足时**不该去碰硬件**：注入故障也不会被触发（证明压根没读）。"""
        dev, clock = self._make()
        dev.read()
        dev.set_mock_fault()          # 若驱动真的去读，就会抛异常
        clock.advance(0.1)
        sample = dev.read()           # 应当直接返回缓存值，不触发故障
        self.assertFalse(sample.ok)
        self.assertEqual(dev.status()["fault_count"], 0)
        dev.close()

    def test_无效读数不覆盖缓存(self) -> None:
        """超量程读数**不能污染缓存**：缓存里永远是最后一次可信的值。"""
        dev, clock = self._make()
        good = dev.read()
        self.assertTrue(good.ok, good.error)
        self.assertEqual(validate_reading(good.temperature_c, good.humidity_percent), "")

        clock.advance(DHT11_MIN_INTERVAL_S)  # 过了 2 秒，允许真正去读
        dev.inject(60.0, 50.0)        # 60℃ 超出 DHT11 量程 → 坏点
        bad = dev.read()
        self.assertFalse(bad.ok)
        self.assertIn("温度", bad.error)

        clock.advance(DHT11_MIN_INTERVAL_S)  # 又来一次"间隔够了"，这次是合成值
        fresh = dev.read()
        self.assertTrue(fresh.ok, fresh.error)
        self.assertEqual(validate_reading(fresh.temperature_c, fresh.humidity_percent), "")
        dev.close()

    def test_无效读数不推进上次测量时刻(self) -> None:
        """超量程那次不是一次有效测量：不许把"上次测量时刻"往后推。

        （注意 ``inject()`` 会解除 2 秒限制，所以这里的"上次测量时刻"是坏点那一次
        之前的状态；关键是坏点**不能**把它推到更晚。）
        """
        dev, clock = self._make()
        good = dev.read()
        self.assertTrue(good.ok, good.error)

        clock.advance(DHT11_MIN_INTERVAL_S)
        dev.inject(60.0, 50.0)        # 这一次数据非法
        bad = dev.read()
        self.assertFalse(bad.ok)
        self.assertIn("温度", bad.error)
        self.assertEqual(dev.status()["fault_count"], 0, "超量程是坏点，不是总线故障")

        # 非法读数之后，缓存**内容**仍是上次的有效值（没有被 60℃ 污染）
        dev.inject(60.0, 50.0)        # 再喂一次坏值（注入值只生效一次）
        again = dev.read()
        self.assertFalse(again.ok)
        self.assertIn("温度", again.error)
        self.assertEqual(dev._cached[0], good.temperature_c, "缓存内容不能被坏值覆盖")
        self.assertEqual(dev._cached[1], good.humidity_percent)
        dev.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
