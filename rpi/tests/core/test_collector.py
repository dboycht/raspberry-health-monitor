"""采集调度测试。

重点验证三条"监护系统红线"：
1. 各设备按**自己的周期**被调度（DHT11 不会被读太快）；
2. **陈旧数据必须自曝**（不允许用旧值冒充当前状态）；
3. 单个设备失败**不影响**其它设备，且失败计数会传给规则引擎。
"""

from __future__ import annotations

import unittest
from typing import Any, Optional

from health_monitor.core.collector import Collector
from health_monitor.core.config import AppConfig, Thresholds
from health_monitor.core.store import Store
from health_monitor.hal import Device, DeviceIOError, DeviceKind, MotionSample, MotionState, VitalSignsSample
from health_monitor.hal.models import AmbientSample, Sample

from . import fakes


class FakeClock:
    def __init__(self, start: float = 10_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> float:
        self.t += seconds
        return self.t


def make_config() -> AppConfig:
    return AppConfig.from_dict(
        {
            "thresholds": {"sensor_fault_after": 3},
            "devices": {
                "vitals": {"driver": "max30102", "read_interval_s": 1.0},
                "ambient": {"driver": "dht11", "read_interval_s": 3.0},
                "motion": {"driver": "hc_sr501", "read_interval_s": 0.5},
            },
        }
    )


class TestCollectorScheduling(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.config = make_config()
        self.devices = {
            "vitals": fakes.FakeVitalSensor(),
            "ambient": fakes.FakeAmbientSensor(),
            "motion": fakes.FakeMotionSensor(),
        }
        self.collector = Collector(self.config, self.devices, store=None, clock=self.clock)
        self.collector.open_all()   # 必须先打开：未 open 时读取会失败（这本身是契约）

    def test_首次全部到期(self) -> None:
        got = self.collector.collect_due()
        self.assertEqual({name for name, _ in got}, {"vitals", "ambient", "motion"})

    def test_各设备按自己的周期被读取(self) -> None:
        self.collector.collect_due()          # t=0：全部读一次
        self.clock.advance(0.6)               # motion(0.5s) 到期，vitals(1s)/ambient(3s) 未到期
        names = {name for name, _ in self.collector.collect_due()}
        self.assertEqual(names, {"motion"})
        self.clock.advance(0.5)               # t=1.1：vitals(距上次 1.1s) 与 motion(距上次 0.5s) 都到期
        names = {name for name, _ in self.collector.collect_due()}
        self.assertEqual(names, {"vitals", "motion"})
        self.clock.advance(0.5)               # t=1.6：motion 又一次到期（1.1 + 0.5）
        names = {name for name, _ in self.collector.collect_due()}
        self.assertEqual(names, {"motion"})
        self.clock.advance(1.6)               # t=3.2：ambient(3s) 也到期
        names = {name for name, _ in self.collector.collect_due()}
        self.assertEqual(names, {"vitals", "ambient", "motion"})

    def test_next_due_in给出等待时间(self) -> None:
        """注意：**读取失败也会推进下次到期时间**（否则坏设备会被无限重试打爆总线）。"""
        self.collector.collect_due()                       # t=0
        self.assertAlmostEqual(self.collector.next_due_in(), 0.5, places=6)  # motion 周期最短
        self.clock.advance(0.5)
        self.assertAlmostEqual(self.collector.next_due_in(), 0.0, places=6)

    def test_读取计数正确(self) -> None:
        self.collector.collect_due()
        self.clock.advance(1.0)
        self.collector.collect_due()
        self.assertEqual(self.collector.status()["devices"]["vitals"]["reads"], 2)


class TestCollectorSnapshot(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.config = make_config()
        self.vitals = fakes.FakeVitalSensor()
        self.ambient = fakes.FakeAmbientSensor()
        self.motion = fakes.FakeMotionSensor()
        self.devices = {"vitals": self.vitals, "ambient": self.ambient, "motion": self.motion}
        self.collector = Collector(self.config, self.devices, store=None, clock=self.clock)
        self.collector.open_all()

    def test_快照包含各字段(self) -> None:
        self.collector.collect_due()
        snap = self.collector.snapshot()
        self.assertIsNotNone(snap.vitals)
        self.assertIsNotNone(snap.ambient)
        self.assertIsNotNone(snap.motion)
        self.assertEqual(snap.health_summary()["heart_rate_bpm"], 72)
        self.assertEqual(snap.health_summary()["ambient_temp_c"], 26.0)

    def test_陈旧数据被置空而不是沿用旧值(self) -> None:
        """这条是红线：读不到就必须说"不知道"，不能拿 5 分钟前的值当当前状态。"""
        self.collector.collect_due()
        self.clock.advance(1.0 * 3.0 + 0.5)     # 超过 stale_factor(3) × interval(1s)
        snap = self.collector.snapshot()
        self.assertIsNone(snap.vitals, "陈旧的心率数据必须置 None，绝不能用旧值冒充当前")
        self.assertIn("vitals", snap.sensor_failures)
        self.assertIn("陈旧", snap.sensor_errors["vitals"])
        self.assertIsNone(snap.health_summary()["heart_rate_bpm"])

    def test_失败计数传给规则引擎(self) -> None:
        self.vitals.fail_with = DeviceIOError("I2C 无应答")
        for _ in range(3):
            self.collector.collect_due()
            self.clock.advance(1.0)
        snap = self.collector.snapshot()
        self.assertGreaterEqual(snap.sensor_failures.get("vitals", 0), 3)

    def test_单设备失败不影响其他设备(self) -> None:
        self.vitals.fail_with = DeviceIOError("挂了")
        got = dict(self.collector.collect_due())
        self.assertFalse(got["vitals"].ok)
        self.assertTrue(got["ambient"].ok)
        self.assertTrue(got["motion"].ok)

    def test_失败样本保留原类型而不是变成未知类型(self) -> None:
        self.collector.collect_due()
        self.vitals.fail_with = DeviceIOError("掉了")
        self.clock.advance(1.0)
        got = dict(self.collector.collect_due())
        self.assertIsInstance(got["vitals"], VitalSignsSample, "失败时也应是同一种样本类型")
        self.assertFalse(got["vitals"].ok)

    def test_恢复后失败计数清零(self) -> None:
        self.vitals.fail_with = DeviceIOError("抖动")
        self.collector.collect_due()
        self.assertEqual(self.collector.snapshot().sensor_failures.get("vitals"), 1)
        self.vitals.fail_with = None
        self.clock.advance(1.0)
        self.collector.collect_due()
        snap = self.collector.snapshot()
        self.assertNotIn("vitals", snap.sensor_failures)

    def test_PIR运动静默秒数来自驱动(self) -> None:
        self.motion.silent_s = 900.0
        self.clock.advance(0.5)
        self.collector.collect_due()
        snap = self.collector.snapshot()
        self.assertAlmostEqual(snap.motion_silent_s or -1, 900.0, places=1)

    def test_运动状态UNKNOWN不会变成久无活动(self) -> None:
        self.motion.state = MotionState.UNKNOWN
        self.clock.advance(0.5)
        self.collector.collect_due()
        snap = self.collector.snapshot()
        self.assertFalse(snap.motion.detected)

    def test_新鲜度随采集更新(self) -> None:
        """★ 手机端要靠这两个字段判断"服务在跑但数据是旧的"。"""
        self.collector.collect_due()
        snap = self.collector.snapshot()
        self.assertIsNotNone(snap.data_age_s)
        # 注意：刚采集完 data_age_s 恰好是 0.0，而 `0.0 or 999` 会得到 999（0.0 是假值）
        # —— 这类"用 or 兜底"的写法在数值断言里是陷阱，必须显式判 None。
        age = snap.data_age_s
        assert age is not None
        self.assertLess(age, 1.0, "刚采集完，数据年龄应该接近 0")
        self.assertFalse(snap.data_stale)
        # 阈值应随设备数与最长周期缩放（3 设备 × 3 × 最长 3 秒 = 27 秒）
        self.assertAlmostEqual(snap.data_stale_after_s, 27.0, delta=0.1)

    def test_全部设备停摆后判为陈旧(self) -> None:
        self.collector.collect_due()
        self.clock.advance(self.collector.snapshot().data_stale_after_s + 5.0)
        for entry in self.collector.entries.values():   # 模拟"读不到任何新数据"
            entry.failures = 1
            entry.last_error = "模拟停摆"
        snap = self.collector.snapshot()
        self.assertTrue(snap.data_stale, "全部设备停摆时快照必须标记为陈旧")


class TestCollectorWithStore(unittest.TestCase):
    def test_采集结果会落库(self) -> None:
        clock = FakeClock()
        store = Store(":memory:")
        collector = Collector(
            make_config(),
            {
                "vitals": fakes.FakeVitalSensor(),
                "ambient": fakes.FakeAmbientSensor(),
                "motion": fakes.FakeMotionSensor(),
            },
            store=store,
            clock=clock,
        )
        collector.open_all()
        collector.collect_due()
        self.assertEqual(store.count("vitals"), 1)
        self.assertGreaterEqual(store.count("readings"), 3)  # 温度/湿度/运动

    def test_存储失败被Store自己吞掉不影响采集(self) -> None:
        """历史库坏掉不该让监护停摆：这个兜底责任在 ``Store.save_sample`` 内。

        验证方式：把表全部删掉，断言 ``collect_due()`` 仍正常返回、不抛异常，
        且写失败被**记数**（不能静默）。
        """
        clock = FakeClock()
        store = Store(":memory:")
        collector = Collector(
            make_config(),
            {"vitals": fakes.FakeVitalSensor(), "ambient": fakes.FakeAmbientSensor(), "motion": fakes.FakeMotionSensor()},
            store=store,
            clock=clock,
        )
        collector.open_all()
        store.drop_all_tables()
        got = collector.collect_due()  # 不应抛异常
        self.assertEqual(len(got), 3)
        # 表已被删掉，所以不能再查行数（count/stats 都会失败）；
        # 直接读私有计数是一个刻意选择：判据是"写失败必须被记数，不能静默"。
        self.assertGreater(store._write_errors, 0, "写失败必须被记数，不能静默")  # noqa: SLF001
    def test_open_all返回失败清单而不是抛异常(self) -> None:
        clock = FakeClock()
        devices = {"vitals": fakes.FakeVitalSensor(open_error=DeviceIOError("没接"))}
        cfg = AppConfig.from_dict(
            {"devices": {"vitals": {"driver": "max30102", "read_interval_s": 1.0}}}
        )
        collector = Collector(cfg, devices, store=None, clock=clock)
        errors = collector.open_all()
        self.assertIn("vitals", errors)
        self.assertIn("没接", errors["vitals"])


class Test实体按键事件队列(unittest.TestCase):
    """按键**动作事件**的攒与取（2026-09-26 接上"实体键 → 业务层"这条链路时新增）。

    为什么不让它进快照：快照表示"当前状态"，而按键是"刚刚发生了一次动作"——
    放进快照会被下一帧的 ``action=NONE`` 覆盖掉，表现为"按一下时灵时不灵"。
    所以采集器单独攒一个队列，由运行时每帧 `drain_button_events()` 取走。
    """

    def setUp(self) -> None:
        from health_monitor.playback import SimButton

        self.clock = FakeClock()
        self.config = AppConfig.from_dict(
            {
                "thresholds": {"sensor_fault_after": 3},
                "devices": {"sos_button": {"driver": "button", "read_interval_s": 0.2}},
            }
        )
        self.button = SimButton(name="sos_button")
        self.collector = Collector(
            self.config, {"sos_button": self.button}, store=None, clock=self.clock
        )
        self.collector.open_all()

    def test_短按与长按都会被攒下来(self) -> None:
        from health_monitor.hal.models import ButtonAction

        self.button.press(ButtonAction.CLICK)
        self.collector.collect_due()
        self.clock.advance(0.5)
        self.button.press(ButtonAction.LONG_PRESS)
        self.collector.collect_due()

        actions = [e.action for e in self.collector.drain_button_events()]
        self.assertEqual(actions, [ButtonAction.CLICK, ButtonAction.LONG_PRESS])

    def test_取过一次就清空(self) -> None:
        from health_monitor.hal.models import ButtonAction

        self.button.press(ButtonAction.CLICK)
        self.collector.collect_due()
        self.assertEqual(len(self.collector.drain_button_events()), 1)
        self.assertEqual(self.collector.drain_button_events(), [], "取过之后必须清空，不能重复触发")

    def test_空闲的NONE不会被攒(self) -> None:
        self.collector.collect_due()   # 队列空 → 驱动返回 action=NONE
        self.assertEqual(self.collector.drain_button_events(), [])

    def test_关闭设备会清掉没消费的事件(self) -> None:
        from health_monitor.hal.models import ButtonAction

        self.button.press(ButtonAction.CLICK)
        self.collector.collect_due()
        self.collector.close_all()
        self.assertEqual(self.collector.drain_button_events(), [], "停机后不该再残留按键动作")

    def test_队列有上限不会被撑爆(self) -> None:
        from health_monitor.hal.models import ButtonAction

        for _ in range(100):          # 造 100 个事件但一直不消费
            self.button.press(ButtonAction.CLICK)
            self.collector.collect_due()
            self.clock.advance(0.5)
        self.assertLessEqual(len(self.collector.drain_button_events()), 64)


if __name__ == "__main__":
    unittest.main(verbosity=2)
