"""报警规则引擎测试 —— **本项目最重要的测试**（判错会误报或漏报）。

覆盖六类行为：
1. 正常值不报警；
2. 越界报警（含正确的 code / severity / 数值）；
3. **数据缺失不误报、也不错误解除**（最危险的假阴性）；
4. 冷却抑制（不刷屏）与迟滞（不抖动）；
5. 恢复发 ALL_CLEAR；
6. 久无活动 / 夜间起夜 / 传感器故障。
"""

from __future__ import annotations

import unittest
from datetime import datetime

from health_monitor.core.config import Thresholds
from health_monitor.core.rules import ReadingSnapshot, RuleEngine
from health_monitor.hal.models import (
    AlarmCode,
    AmbientSample,
    MotionSample,
    MotionState,
    PrecisionTempSample,
    Severity,
    VitalSignsSample,
)


def vitals(hr=None, spo2=None, finger=True, ok=True, ts=1000.0) -> VitalSignsSample:
    return VitalSignsSample(
        ts=ts, device="max30102", ok=ok,
        heart_rate_bpm=hr, spo2_percent=spo2,
        finger_detected=finger, quality=0.9,
    )


def night_ts(hour: int) -> float:
    """构造当天某个小时的时间戳（用于夜间起夜规则）。"""
    return datetime(2026, 9, 21, hour, 0, 0).timestamp()


class TestNormalValues(unittest.TestCase):
    def test_全部正常时不报警(self) -> None:
        engine = RuleEngine()
        snap = ReadingSnapshot(
            ts=1000.0,
            vitals=vitals(hr=72, spo2=98),
            body_temp=PrecisionTempSample(ts=1000.0, device="tmp36", temperature_c=36.5),
            ambient=AmbientSample(ts=1000.0, device="dht11", temperature_c=24.0, humidity_percent=55.0),
            motion=MotionSample(ts=1000.0, device="hc_sr501", state=MotionState.DETECTED),
        )
        self.assertEqual(engine.evaluate(snap), [])

    def test_空快照不产生任何正常结论(self) -> None:
        """什么都没有时不能报"正常"，也不能崩。"""
        engine = RuleEngine()
        self.assertEqual(engine.evaluate(ReadingSnapshot(ts=1000.0)), [])


class TestVitalSigns(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = RuleEngine()

    def test_心率过高触发WARNING(self) -> None:
        events = self.engine.evaluate(ReadingSnapshot(ts=1000.0, vitals=vitals(hr=128, spo2=97)))
        codes = [e.code for e in events]
        self.assertIn(AlarmCode.HR_TOO_HIGH, codes)
        ev = next(e for e in events if e.code is AlarmCode.HR_TOO_HIGH)
        self.assertEqual(ev.severity, Severity.WARNING)
        self.assertAlmostEqual(ev.value, 128.0)
        self.assertEqual(ev.unit, "bpm")

    def test_心率过低触发WARNING(self) -> None:
        events = self.engine.evaluate(ReadingSnapshot(ts=1000.0, vitals=vitals(hr=42, spo2=97)))
        self.assertIn(AlarmCode.HR_TOO_LOW, [e.code for e in events])

    def test_血氧过低触发CRITICAL(self) -> None:
        events = self.engine.evaluate(ReadingSnapshot(ts=1000.0, vitals=vitals(hr=75, spo2=89)))
        ev = next(e for e in events if e.code is AlarmCode.SPO2_TOO_LOW)
        self.assertEqual(ev.severity, Severity.CRITICAL)

    def test_未检测到手指时不判定心率(self) -> None:
        """没贴手指时心率是 None：**绝不能**报"心率过低"。"""
        events = self.engine.evaluate(
            ReadingSnapshot(ts=1000.0, vitals=vitals(hr=None, spo2=None, finger=False))
        )
        self.assertEqual(events, [])

    def test_心率缺失不冒充正常(self) -> None:
        """有手指、值还没算出来：不报警，也不得产生"正常"结论。"""
        events = self.engine.evaluate(
            ReadingSnapshot(ts=1000.0, vitals=vitals(hr=None, spo2=None, finger=True))
        )
        self.assertEqual(events, [])

    def test_传感器读取失败不算正常(self) -> None:
        events = self.engine.evaluate(
            ReadingSnapshot(ts=1000.0, vitals=vitals(ok=False, hr=None, finger=False))
        )
        self.assertEqual(events, [])


class TestBodyAndAmbient(unittest.TestCase):
    def test_体温偏高(self) -> None:
        engine = RuleEngine()
        events = engine.evaluate(
            ReadingSnapshot(ts=1000.0, body_temp=PrecisionTempSample(ts=1000.0, device="tmp36", temperature_c=38.5))
        )
        self.assertIn(AlarmCode.BODY_TEMP_HIGH, [e.code for e in events])

    def test_体温偏低(self) -> None:
        engine = RuleEngine()
        events = engine.evaluate(
            ReadingSnapshot(ts=1000.0, body_temp=PrecisionTempSample(ts=1000.0, device="tmp36", temperature_c=34.0))
        )
        self.assertIn(AlarmCode.BODY_TEMP_LOW, [e.code for e in events])

    def test_室温异常只是NOTICE(self) -> None:
        engine = RuleEngine()
        events = engine.evaluate(
            ReadingSnapshot(ts=1000.0, ambient=AmbientSample(ts=1000.0, device="dht11", temperature_c=33.0, humidity_percent=50.0))
        )
        ev = next(e for e in events if e.code is AlarmCode.AMBIENT_TEMP_HIGH)
        self.assertEqual(ev.severity, Severity.NOTICE, "室温问题不该按紧急报警处理")

    def test_湿度过高(self) -> None:
        engine = RuleEngine()
        events = engine.evaluate(
            ReadingSnapshot(ts=1000.0, ambient=AmbientSample(ts=1000.0, device="dht11", temperature_c=24.0, humidity_percent=88.0))
        )
        self.assertIn(AlarmCode.HUMIDITY_HIGH, [e.code for e in events])


class TestCooldownAndHysteresis(unittest.TestCase):
    def test_同一报警在冷却期内不重复(self) -> None:
        engine = RuleEngine(Thresholds(repeat_cooldown_s=300))
        snap = ReadingSnapshot(ts=1000.0, vitals=vitals(hr=130, spo2=97))
        first = engine.evaluate(snap)
        self.assertIn(AlarmCode.HR_TOO_HIGH, [e.code for e in first])
        second = engine.evaluate(ReadingSnapshot(ts=1010.0, vitals=vitals(hr=130, spo2=97)))
        self.assertNotIn(AlarmCode.HR_TOO_HIGH, [e.code for e in second], "冷却期内不该重复报警")
        third = engine.evaluate(ReadingSnapshot(ts=1400.0, vitals=vitals(hr=130, spo2=97)))
        self.assertIn(AlarmCode.HR_TOO_HIGH, [e.code for e in third], "超过冷却期应再次提醒")

    def test_迟滞避免阈值附近抖动(self) -> None:
        engine = RuleEngine(Thresholds(hr_max=110, hysteresis=3, repeat_cooldown_s=0))
        engine.evaluate(ReadingSnapshot(ts=1000.0, vitals=vitals(hr=120, spo2=97)))
        # 回落到 109（>110-3=107）仍在报警态：不应产生 ALL_CLEAR
        events = engine.evaluate(ReadingSnapshot(ts=1001.0, vitals=vitals(hr=109, spo2=97)))
        self.assertNotIn(AlarmCode.ALL_CLEAR, [e.code for e in events])
        self.assertIn(AlarmCode.HR_TOO_HIGH, engine.active_alarms())
        # 回落到 105（<107）才解除
        events = engine.evaluate(ReadingSnapshot(ts=1002.0, vitals=vitals(hr=105, spo2=97)))
        self.assertIn(AlarmCode.ALL_CLEAR, [e.code for e in events])
        self.assertNotIn("hr_too_high", engine.active_alarms())

    def test_恢复事件带出被解除的报警码(self) -> None:
        engine = RuleEngine(Thresholds(repeat_cooldown_s=0))
        engine.evaluate(ReadingSnapshot(ts=1000.0, vitals=vitals(hr=125, spo2=97)))
        events = engine.evaluate(ReadingSnapshot(ts=1001.0, vitals=vitals(hr=70, spo2=97)))
        clear = next(e for e in events if e.code is AlarmCode.ALL_CLEAR)
        self.assertEqual(clear.detail.get("recovered_code"), "hr_too_high")
        self.assertEqual(clear.severity, Severity.NORMAL)

    def test_数据缺失不解除报警(self) -> None:
        """读数读不到 = 未知，**绝不等于恢复正常**（否则手机端会误以为安全）。"""
        engine = RuleEngine(Thresholds(repeat_cooldown_s=0))
        engine.evaluate(ReadingSnapshot(ts=1000.0, vitals=vitals(hr=125, spo2=97)))
        events = engine.evaluate(
            ReadingSnapshot(ts=1001.0, vitals=vitals(hr=None, spo2=None, finger=False))
        )
        self.assertNotIn(AlarmCode.ALL_CLEAR, [e.code for e in events])
        self.assertIn("hr_too_high", engine.active_alarms())


class TestMotionRules(unittest.TestCase):
    def test_久无活动触发CRITICAL(self) -> None:
        engine = RuleEngine(Thresholds(no_motion_timeout_s=1800))
        snap = ReadingSnapshot(
            ts=1000.0,
            motion=MotionSample(ts=1000.0, device="hc_sr501", state=MotionState.IDLE),
            motion_silent_s=2000.0,
        )
        events = engine.evaluate(snap)
        ev = next(e for e in events if e.code is AlarmCode.NO_MOTION_TOO_LONG)
        self.assertEqual(ev.severity, Severity.CRITICAL)
        self.assertAlmostEqual(ev.value, 2000.0, places=1)

    def test_检测到人时不报久无活动(self) -> None:
        engine = RuleEngine()
        snap = ReadingSnapshot(
            ts=1000.0,
            motion=MotionSample(ts=1000.0, device="hc_sr501", state=MotionState.DETECTED),
            motion_silent_s=0.0,
        )
        self.assertNotIn(AlarmCode.NO_MOTION_TOO_LONG, [e.code for e in engine.evaluate(snap)])

    def test_运动状态UNKNOWN时不报久无活动(self) -> None:
        """PIR 读失败时不能推断"没人动"（那是臆测）。"""
        engine = RuleEngine()
        snap = ReadingSnapshot(
            ts=1000.0,
            motion=MotionSample(ts=1000.0, device="hc_sr501", state=MotionState.UNKNOWN),
            motion_silent_s=99999.0,
        )
        self.assertNotIn(AlarmCode.NO_MOTION_TOO_LONG, [e.code for e in engine.evaluate(snap)])

    def test_夜间频繁起夜(self) -> None:
        engine = RuleEngine(Thresholds(night_wake_count=3, night_window_s=7200, repeat_cooldown_s=0))
        events = []
        for i in range(3):
            ts = night_ts(23) + i * 60
            events = engine.evaluate(
                ReadingSnapshot(
                    ts=ts,
                    motion=MotionSample(ts=ts, device="hc_sr501", state=MotionState.DETECTED),
                )
            )
        self.assertIn(AlarmCode.NIGHT_FREQUENT_WAKE, [e.code for e in events])

    def test_白天起夜不计入夜间统计(self) -> None:
        engine = RuleEngine(Thresholds(night_wake_count=3, night_window_s=7200, repeat_cooldown_s=0))
        events = []
        for i in range(5):
            ts = night_ts(14) + i * 60  # 下午 2 点
            events = engine.evaluate(
                ReadingSnapshot(
                    ts=ts,
                    motion=MotionSample(ts=ts, device="hc_sr501", state=MotionState.DETECTED),
                )
            )
        self.assertNotIn(AlarmCode.NIGHT_FREQUENT_WAKE, [e.code for e in events])


class TestSensorFault(unittest.TestCase):
    def test_连续失败达到阈值才报故障(self) -> None:
        engine = RuleEngine(Thresholds(sensor_fault_after=3, repeat_cooldown_s=0))
        for count in (1, 2):
            events = engine.evaluate(
                ReadingSnapshot(ts=1000.0 + count, sensor_failures={"max30102": count})
            )
            self.assertNotIn(AlarmCode.SENSOR_FAULT, [e.code for e in events])
        events = engine.evaluate(ReadingSnapshot(ts=1010.0, sensor_failures={"max30102": 3}))
        ev = next(e for e in events if e.code is AlarmCode.SENSOR_FAULT)
        self.assertIn("max30102", ev.message)
        self.assertEqual(ev.severity, Severity.WARNING)

    def test_多个传感器故障各自报警(self) -> None:
        engine = RuleEngine(Thresholds(sensor_fault_after=2, repeat_cooldown_s=0))
        events = engine.evaluate(
            ReadingSnapshot(ts=1000.0, sensor_failures={"max30102": 3, "dht11": 4})
        )
        faults = [e for e in events if e.code is AlarmCode.SENSOR_FAULT]
        self.assertEqual(len(faults), 2, "两个设备都应各自报警（用 device 字段区分）")
        self.assertEqual({e.source for e in faults}, {"max30102", "dht11"})


class TestSnapshotSummary(unittest.TestCase):
    def test_摘要里缺失值必须是None而不是0(self) -> None:
        """手机端把 None 显示为"未知"，把 0 显示成"0 bpm"会吓死人。"""
        summary = ReadingSnapshot(ts=1000.0).health_summary()
        for key in ("heart_rate_bpm", "spo2_percent", "body_temp_c", "ambient_temp_c", "motion_state"):
            self.assertIsNone(summary[key], f"{key} 缺失时必须为 None")

    def test_摘要包含已采到的值(self) -> None:
        snap = ReadingSnapshot(
            ts=1000.0,
            vitals=vitals(hr=72, spo2=98),
            ambient=AmbientSample(ts=1000.0, device="dht11", temperature_c=24.5, humidity_percent=55.0),
        )
        s = snap.health_summary()
        self.assertEqual(s["heart_rate_bpm"], 72)
        self.assertEqual(s["ambient_temp_c"], 24.5)
        self.assertIsNone(s["body_temp_c"])

    def test_新鲜度字段(self) -> None:
        """★ 让手机端能区分"没有数据"与"数据是旧的"。

        没有这两个字段时，App 只能看到一堆 null，无法判断是"传感器没接"还是
        "整个采集早就停了"——后一种情况需要立刻提醒用户。
        """
        never = ReadingSnapshot(ts=1000.0)
        self.assertIsNone(never.health_summary()["data_age_s"])
        self.assertTrue(never.data_stale, "从未读到过数据 ⇒ 必须判为陈旧")

        fresh = ReadingSnapshot(ts=1000.0, data_age_s=1.5, data_stale_after_s=27.0)
        self.assertFalse(fresh.data_stale)
        self.assertEqual(fresh.health_summary()["data_age_s"], 1.5)

        old = ReadingSnapshot(ts=1000.0, data_age_s=120.0, data_stale_after_s=27.0)
        self.assertTrue(old.data_stale, "超过阈值必须判为陈旧")
        self.assertTrue(old.health_summary()["data_stale"])


class TestThresholdValidation(unittest.TestCase):
    def test_阈值上下限颠倒要报错(self) -> None:
        from health_monitor.hal.exceptions import ConfigError

        with self.assertRaises(ConfigError):
            Thresholds(hr_min=120, hr_max=60).validate()

    def test_血氧阈值越界要报错(self) -> None:
        from health_monitor.hal.exceptions import ConfigError

        with self.assertRaises(ConfigError):
            Thresholds(spo2_min=120).validate()


if __name__ == "__main__":
    unittest.main(verbosity=2)
