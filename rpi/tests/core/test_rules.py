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

import json
import math
import unittest
from datetime import datetime

from health_monitor.core.config import Thresholds
from health_monitor.core.rules import (
    ReadingSnapshot,
    RuleEngine,
    finite_or_none,
    format_duration_s,
)
from health_monitor.hal.models import (
    AlarmCode,
    AmbientSample,
    MotionSample,
    MotionState,
    Severity,
    VitalSignsSample,
)


def vitals(hr=None, spo2=None, finger=True, ok=True, ts=1000.0,
           awaiting=False) -> VitalSignsSample:
    """造一个心率血氧样本。

    ``awaiting=True`` 表示"器件正常、只是这次没测出东西"（没贴手指 / 窗没攒够），
    与"真的读失败"（``ok=False, awaiting=False``）是**两回事**（`ERROR.md` **E66**）。
    """
    return VitalSignsSample(
        ts=ts, device="max30102", ok=ok,
        heart_rate_bpm=hr, spo2_percent=spo2,
        finger_detected=finger, quality=0.9, awaiting_data=awaiting,
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


class TestAmbient(unittest.TestCase):
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

    def test_没人正在测时报警要能自己解除(self) -> None:
        """★ `ERROR.md` **E66**：器件**正常**、只是"这次没测出东西"（没贴手指）
        ⇒ 必须**算数据不缺**、报警要能**自己解除**。

        真机事故（2026-09-30 晚）：血氧改成"按需测量"之后，**"没贴手指"是常态**；
        而原来 `_is_missing()` 把 `finger_detected=False` 也算成"数据缺失" ⇒
        **一次低血氧读数会让报警永久卡住**，每 5 秒重发一次 ——
        实测到**第 32 次**还在响（红灯一直闪 + 4 声蜂鸣 + 两块屏刷 `ALARM: SPO2 LOW`）。

        触发与解除原来是两套判据：**触发**只在"有手指且有值"时判，
        **解除**却把"没手指"当成数据缺失而拒绝 ⇒ 两边一撞就锁死。
        """
        engine = RuleEngine(Thresholds(repeat_cooldown_s=0))
        engine.evaluate(ReadingSnapshot(ts=1000.0, vitals=vitals(hr=70, spo2=88)))
        self.assertIn("spo2_too_low", engine.active_alarms(), "前置条件：先真的报过警")

        # 手指拿开 —— 真驱动给的就是这个形状（ok=False + awaiting_data=True，见 E54）
        no_finger = vitals(hr=None, spo2=None, finger=False, ok=False, awaiting=True)
        events = engine.evaluate(ReadingSnapshot(ts=1001.0, vitals=no_finger))

        self.assertIn(AlarmCode.ALL_CLEAR, [e.code for e in events],
                      "拿开手指后报警必须自己解除（否则会永远卡在报警态）")
        self.assertNotIn("spo2_too_low", engine.active_alarms())

    def test_真的读失败时仍然不解除报警(self) -> None:
        """反向钉子：**器件真的读不到**（I2C 出错 / 掉线）⇒ 仍是"数据缺失"，**不许**解除。

        `ok=False` 有两种含义，必须分开（E66）：
        ``awaiting_data=True`` = 器件正常只是没测出东西（可以解除）；
        ``awaiting_data=False`` = 真失败（不解除，否则手机端会误以为安全）。
        """
        engine = RuleEngine(Thresholds(repeat_cooldown_s=0))
        engine.evaluate(ReadingSnapshot(ts=1000.0, vitals=vitals(hr=125, spo2=97)))
        broken = vitals(hr=None, spo2=None, finger=False, ok=False, awaiting=False)
        events = engine.evaluate(ReadingSnapshot(ts=1001.0, vitals=broken))
        self.assertNotIn(AlarmCode.ALL_CLEAR, [e.code for e in events])
        self.assertIn("hr_too_high", engine.active_alarms())

    def test_心率报警同样会随手指拿开而解除(self) -> None:
        """E66 对心率一视同仁（两者共用同一条 `_is_missing` 分支）。"""
        engine = RuleEngine(Thresholds(repeat_cooldown_s=0))
        engine.evaluate(ReadingSnapshot(ts=1000.0, vitals=vitals(hr=125, spo2=97)))
        no_finger = vitals(hr=None, spo2=None, finger=False, ok=False, awaiting=True)
        events = engine.evaluate(ReadingSnapshot(ts=1001.0, vitals=no_finger))
        self.assertIn(AlarmCode.ALL_CLEAR, [e.code for e in events])
        self.assertNotIn("hr_too_high", engine.active_alarms())


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

    def test_从未检测到人不会一启动就报警_E55(self) -> None:
        """★★ E55（2026-09-29 真机）：PIR 报 `inf`（从未检测到人）时，服务**刚起来就报**
        CRITICAL「久无活动」—— 实测起服务 3 秒就开始黄灯闪 + 报警刷屏，是**误报**。

        根因：`inf >= no_motion_timeout_s` 恒成立。正确语义 = **从"开始监护"的时刻起算**，
        满阈值才报警（这段时间本来也可能真的没人动，但那是"还没到计时"而不是"已经超时"）。
        """
        engine = RuleEngine(Thresholds(no_motion_timeout_s=1800))
        first = ReadingSnapshot(
            ts=1000.0,
            motion=MotionSample(ts=1000.0, device="hc_sr501", state=MotionState.IDLE),
            motion_silent_s=math.inf,
        )
        codes = [e.code for e in engine.evaluate(first)]
        self.assertNotIn(AlarmCode.NO_MOTION_TOO_LONG, codes, "刚启动不该报久无活动")

        # 监护跑了 10 分钟（还没到 30 分钟阈值）⇒ 仍然不报
        mid = ReadingSnapshot(
            ts=1000.0 + 600.0,
            motion=MotionSample(ts=1000.0 + 600.0, device="hc_sr501", state=MotionState.IDLE),
            motion_silent_s=math.inf,
        )
        self.assertNotIn(AlarmCode.NO_MOTION_TOO_LONG, [e.code for e in engine.evaluate(mid)])

        # 满阈值之后 ⇒ 必须报（否则真出事就不报警了）
        late = ReadingSnapshot(
            ts=1000.0 + 1801.0,
            motion=MotionSample(ts=1000.0 + 1801.0, device="hc_sr501", state=MotionState.IDLE),
            motion_silent_s=math.inf,
        )
        ev = [e for e in engine.evaluate(late) if e.code is AlarmCode.NO_MOTION_TOO_LONG]
        self.assertEqual(len(ev), 1, "满阈值后必须报警")
        self.assertIn("至今未检测到", ev[0].message)

    def test_检测到人之后再静默满阈值仍然报警_E55反向(self) -> None:
        """反向守一手：真的"先动过、然后长时间不动"必须照旧报警。"""
        engine = RuleEngine(Thresholds(no_motion_timeout_s=60, repeat_cooldown_s=0))
        seen = ReadingSnapshot(
            ts=1000.0,
            motion=MotionSample(ts=1000.0, device="hc_sr501", state=MotionState.DETECTED),
            motion_silent_s=0.0,
        )
        engine.evaluate(seen)
        quiet = ReadingSnapshot(
            ts=1000.0 + 61.0,
            motion=MotionSample(ts=1000.0 + 61.0, device="hc_sr501", state=MotionState.IDLE),
            motion_silent_s=61.0,
        )
        codes = [e.code for e in engine.evaluate(quiet)]
        self.assertIn(AlarmCode.NO_MOTION_TOO_LONG, codes)

    def test_从没检测到人时文案与数值都必须是合法JSON(self) -> None:
        """★ 真机 T4 验收踩到（ERROR.md **E47**）。

        PIR 的"**从没**检测到人"语义是 ``inf`` 秒（刻意不是 0）；旧代码把它塞进
        ``f"{silent / 60:.0f} 分钟"`` 与 ``value=round(silent, 1)``，后果是：
        ① 报警文案成了"已有 **inf** 分钟未检测到活动"；
        ② ``value=inf`` 经 ``json.dumps`` 变成裸 ``Infinity``（**非法 JSON**），
           实测被 OneNET 整包拒收（``err_code 98 illegal data``）。
        """
        engine = RuleEngine(Thresholds(no_motion_timeout_s=1800))
        snap = ReadingSnapshot(
            ts=1000.0,
            motion=MotionSample(ts=1000.0, device="hc_sr501", state=MotionState.IDLE),
            motion_silent_s=math.inf,
        )
        # ⚠️ E55 之后，"从未检测到人"从**开始监护**起算 ⇒ 先要跑满阈值才会报警。
        #    所以这里先推时间到阈值之后，再检查文案与数值（E47 的保证不变）。
        engine.evaluate(snap)                                   # t=1000：开始监护
        later = ReadingSnapshot(
            ts=1000.0 + 1801.0,
            motion=MotionSample(ts=1000.0 + 1801.0, device="hc_sr501", state=MotionState.IDLE),
            motion_silent_s=math.inf,
        )
        ev = next(e for e in engine.evaluate(later) if e.code is AlarmCode.NO_MOTION_TOO_LONG)
        # ① 文案如实说"至今未检测到"，不许出现 "inf"
        self.assertIn("至今未检测到", ev.message)
        self.assertNotIn("inf", ev.message)
        # ② 数值降级成 None（宁可说"未知"，也不发非法 JSON）
        self.assertIsNone(ev.value)
        self.assertEqual(ev.unit, "")
        # ③ 判据：输出必须能过**严格** JSON（allow_nan=False 会把 Infinity/NaN 当场拒掉）
        payload = json.dumps(ev.to_dict(), ensure_ascii=False, allow_nan=False)
        self.assertNotIn("Infinity", payload)
        self.assertIsNone(json.loads(payload)["value"])

    def test_短时长文案用秒而不是0分钟(self) -> None:
        """★ 真机 T4 的第二个坑：临时阈值 20 秒 ⇒ 旧文案"已有 **0** 分钟未检测到活动"，
        读起来像"还没超时"，验收人当场以为没触发（阈值 1800 时反而正常，所以开发机测不出来）。"""
        engine = RuleEngine(Thresholds(no_motion_timeout_s=20, repeat_cooldown_s=0))
        snap = ReadingSnapshot(
            ts=1000.0,
            motion=MotionSample(ts=1000.0, device="hc_sr501", state=MotionState.IDLE),
            motion_silent_s=20.0,
        )
        ev = next(e for e in engine.evaluate(snap) if e.code is AlarmCode.NO_MOTION_TOO_LONG)
        self.assertIn("20 秒", ev.message)
        self.assertNotIn("0 分钟", ev.message)
        self.assertEqual(ev.value, 20.0)
        self.assertEqual(ev.unit, "s")

    def test_长时长文案用分钟与小时(self) -> None:
        engine = RuleEngine(Thresholds(no_motion_timeout_s=1800, repeat_cooldown_s=0))
        snap = ReadingSnapshot(
            ts=1000.0,
            motion=MotionSample(ts=1000.0, device="hc_sr501", state=MotionState.IDLE),
            motion_silent_s=5400.0,      # 1.5 小时
        )
        ev = next(e for e in engine.evaluate(snap) if e.code is AlarmCode.NO_MOTION_TOO_LONG)
        self.assertIn("1 小时 30 分钟", ev.message)

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

    # ------------------------------------------------------------------
    # ★ 回归（2026-09-26 真机 T2 闭环实测踩到）：故障**恢复**要发 ALL_CLEAR
    # ------------------------------------------------------------------

    def test_故障会登记为仍在报警(self) -> None:
        """★ 回归：`SENSOR_FAULT` 原来**只上报、不登记** `_active` ⇒ 手机端/状态页看不到，
        且恢复时 `_recoveries()` 根本不知道它需要被解除。"""
        engine = RuleEngine(Thresholds(sensor_fault_after=3, repeat_cooldown_s=0))
        engine.evaluate(ReadingSnapshot(ts=1000.0, sensor_failures={"ambient": 3}))
        self.assertIn(
            "sensor_fault", engine.active_alarms(),
            "传感器故障必须出现在 active_alarms（否则手机端/状态页与恢复逻辑都看不到它）",
        )

    def test_故障恢复要发ALL_CLEAR(self) -> None:
        """★ 真机现场：拔掉 DHT11 → 黄灯闪 + 蜂鸣；插回去 → 灯还一直黄（ALL_CLEAR 没发）。"""
        engine = RuleEngine(Thresholds(sensor_fault_after=3, repeat_cooldown_s=0))
        engine.evaluate(ReadingSnapshot(ts=1000.0, sensor_failures={"ambient": 3}))
        events = engine.evaluate(ReadingSnapshot(ts=1010.0, sensor_failures={}))
        clears = [e for e in events if e.code is AlarmCode.ALL_CLEAR]
        self.assertTrue(clears, "传感器恢复后必须发 ALL_CLEAR（把灯/屏复位）")
        self.assertEqual(clears[0].detail.get("recovered_code"), "sensor_fault")
        self.assertNotIn("sensor_fault", engine.active_alarms())

    def test_仍在故障中不许发ALL_CLEAR(self) -> None:
        """传感器**还坏着**的时候不能判成"已恢复"（否则报警会闪烁）。"""
        engine = RuleEngine(Thresholds(sensor_fault_after=3, repeat_cooldown_s=0))
        engine.evaluate(ReadingSnapshot(ts=1000.0, sensor_failures={"ambient": 3}))
        events = engine.evaluate(ReadingSnapshot(ts=1010.0, sensor_failures={"ambient": 9}))
        self.assertNotIn(AlarmCode.ALL_CLEAR, [e.code for e in events])
        self.assertIn("sensor_fault", engine.active_alarms())

    def test_恢复后再次故障还会重新报警(self) -> None:
        """★ 恢复时要清掉 `sensor_fault:<device>` 冷却键：否则"再坏"被 5 分钟冷却挡住，
        表现为"传感器又坏了、灯却还是绿的"。"""
        engine = RuleEngine(Thresholds(sensor_fault_after=3, repeat_cooldown_s=300))
        engine.evaluate(ReadingSnapshot(ts=1000.0, sensor_failures={"ambient": 3}))
        engine.evaluate(ReadingSnapshot(ts=1010.0, sensor_failures={}))     # 恢复
        events = engine.evaluate(ReadingSnapshot(ts=1020.0, sensor_failures={"ambient": 3}))
        self.assertIn(
            AlarmCode.SENSOR_FAULT, [e.code for e in events],
            "恢复之后同一设备再次故障，必须能重新报警（不能被上一轮的冷却键挡住）",
        )


class Test时长文案与JSON安全(unittest.TestCase):
    """两个边界工具（ERROR.md E47）：**判据是"跨出去的值必须是合法 JSON"**。"""

    def test_时长文案分段与边界(self) -> None:
        cases = [
            (0.0, "0 秒"),
            (20.0, "20 秒"),
            (59.9, "60 秒"),        # <60 走"秒"分支
            (60.0, "1 分钟"),
            (90.0, "1 分钟"),       # 整除，不四舍五入
            (1800.0, "30 分钟"),    # 项目默认阈值
            (3599.0, "59 分钟"),    # ★ 不许凑成"60 分钟"
            (3600.0, "1 小时 0 分钟"),
            (5400.0, "1 小时 30 分钟"),
        ]
        for seconds, expected in cases:
            with self.subTest(seconds=seconds):
                self.assertEqual(format_duration_s(seconds), expected)

    def test_非有限数一律None(self) -> None:
        self.assertIsNone(finite_or_none(math.inf))
        self.assertIsNone(finite_or_none(-math.inf))
        self.assertIsNone(finite_or_none(math.nan))
        self.assertIsNone(finite_or_none(None))
        self.assertEqual(finite_or_none(0.0), 0.0)
        self.assertEqual(finite_or_none(12), 12.0)

    def test_非数字输入不抛异常(self) -> None:
        """驱动给错类型时宁可返回"未知"，也不要让 /api/v1/current 整个 500。"""
        self.assertIsNone(finite_or_none("很久"))
        self.assertIsNone(finite_or_none(object()))


class TestSnapshotSummary(unittest.TestCase):
    def test_摘要里缺失值必须是None而不是0(self) -> None:
        """手机端把 None 显示为"未知"，把 0 显示成"0 bpm"会吓死人。"""
        summary = ReadingSnapshot(ts=1000.0).health_summary()
        for key in ("heart_rate_bpm", "spo2_percent", "ambient_temp_c", "motion_state"):
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

    def test_摘要里不许出现inf或nan(self) -> None:
        """★ 真机 T4 实测（ERROR.md E47）：PIR "从没检测到人"时 ``motion_silent_s`` 是 ``inf``，
        旧代码原样透出 ⇒ ``/api/v1/current`` 的 JSON 里出现裸 ``Infinity``（**非法 JSON**），
        手机端的严格解析器会当场失败。摘要出口统一降级成 ``None``（对客户端就是"未知"）。"""
        snap = ReadingSnapshot(
            ts=1000.0,
            motion=MotionSample(ts=1000.0, device="hc_sr501", state=MotionState.IDLE),
            motion_silent_s=math.inf,
            data_age_s=math.inf,
        )
        summary = snap.health_summary()
        self.assertIsNone(summary["motion_silent_s"])
        self.assertIsNone(summary["data_age_s"])
        # 判据：整个摘要必须能过严格 JSON（有 Infinity/NaN 会在这里抛 ValueError）
        text = json.dumps(summary, ensure_ascii=False, allow_nan=False)
        self.assertNotIn("Infinity", text)
        self.assertNotIn("NaN", text)

    def test_摘要里正常的有限值原样保留(self) -> None:
        """降级只针对非有限数，别把正常读数一起吃掉。"""
        snap = ReadingSnapshot(
            ts=1000.0,
            motion=MotionSample(ts=1000.0, device="hc_sr501", state=MotionState.IDLE),
            motion_silent_s=12.5,
            data_age_s=0.4,
        )
        summary = snap.health_summary()
        self.assertEqual(summary["motion_silent_s"], 12.5)
        self.assertEqual(summary["data_age_s"], 0.4)

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
