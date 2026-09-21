"""报警规则引擎 —— **纯逻辑，最容易写单测的部分**。

设计要点（这些是"做对了"的判据，改代码前先读一遍）
--------------------------------------------------
1. **同一个报警不刷屏**：带冷却时间（``repeat_cooldown_s``）与"状态沿"判定，
   数值在阈值附近抖动时不会每帧都报警。
2. **迟滞（hysteresis）**：心率 111 触发、回落到 108 才解除，
   避免在 110 上下反复"报警→解除→报警"。
3. **恢复要报 ALL_CLEAR**：从报警态回到正常，必须发一条解除事件，
   否则手机端会永远停在"报警中"。
4. **数据缺失 ≠ 正常**：传感器读不到时**不产生"正常"结论**，只报 ``SENSOR_FAULT``；
   绝不能把"读不到"当成"一切正常"（这是健康监护类系统最危险的假阴性）。
5. **久无活动**用 PIR 的 `seconds_since_motion()`；**夜间起夜**用窗口内 DETECTED 次数。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..hal.models import (
    AlarmCode,
    AlarmEvent,
    AmbientSample,
    MotionSample,
    MotionState,
    PrecisionTempSample,
    Severity,
    VitalSignsSample,
    now_ts,
)
from .config import Thresholds


@dataclass
class ReadingSnapshot:
    """某一时刻系统看到的全部读数（缺失的传感器为 ``None``）。

    ⚠️ 字段为 ``None`` 表示"**没有数据**"，业务层不得解释为"正常"。
    """

    ts: float = field(default_factory=now_ts)
    vitals: Optional[VitalSignsSample] = None
    body_temp: Optional[PrecisionTempSample] = None
    ambient: Optional[AmbientSample] = None
    motion: Optional[MotionSample] = None
    motion_silent_s: Optional[float] = None       # 距上次检测到人的秒数
    sensor_failures: Dict[str, int] = field(default_factory=dict)  # 设备名 -> 连续失败次数
    sensor_errors: Dict[str, str] = field(default_factory=dict)    # 设备名 -> 最后一次错误
    #: 距"最近一次成功读到新数据"的秒数；``None`` 表示从未读到过。
    #: 用来回答"服务在跑但数据是不是旧的"——手机端据此显示"数据可能已过期"。
    data_age_s: Optional[float] = None
    #: 判定"快照已陈旧"的秒数阈值。由采集器按各设备周期算出
    #: （= 设备数 × 3 × 最长周期，见 :meth:`Collector.snapshot`），**不写死**：
    #: 3 个设备各 3 秒周期 ⇒ 27 秒，与"所有设备都该至少更新过一轮"对齐。
    data_stale_after_s: float = 30.0

    @property
    def data_stale(self) -> bool:
        """整份快照是否已经"不值得相信"（没有任何一个传感器在正常更新）。

        判据：从未读到过数据（``data_age_s is None``），或者最近一次成功读取已经超过
        ``data_stale_after_s``。手机端据此显示"数据可能已过期"，而不是拿旧值当实时值。
        """
        if self.data_age_s is None:
            return True
        return self.data_age_s > self.data_stale_after_s

    def health_summary(self) -> Dict[str, Any]:
        """给 HTTP API / 安卓端用的扁平摘要（**只放已采到的值，缺失就是 None**）。

        ⚠️ 即使某个传感器对象存在，其内部字段仍可能是 ``None``；
        安卓端必须把 ``None`` 渲染成"未知"，**不能渲染成 0**。
        """
        v = self.vitals
        b = self.body_temp
        a = self.ambient
        m = self.motion
        return {
            "ts": self.ts,
            "heart_rate_bpm": v.heart_rate_bpm if v else None,
            "spo2_percent": v.spo2_percent if v else None,
            "finger_detected": v.finger_detected if v else None,
            "vitals_quality": v.quality if v else None,
            "body_temp_c": b.temperature_c if b else None,
            "ambient_temp_c": a.temperature_c if a else None,
            "humidity_percent": a.humidity_percent if a else None,
            "motion_state": m.state.value if m else None,
            "motion_silent_s": self.motion_silent_s,
            "sensor_failures": dict(self.sensor_failures),
            # ---- 新鲜度：让手机端能区分"没有数据"与"数据是旧的" ----
            "data_age_s": (None if self.data_age_s is None else round(self.data_age_s, 1)),
            "data_stale": self.data_stale,
        }


class RuleEngine:
    """规则引擎：消费读数快照，产出报警事件。

    典型用法::

        engine = RuleEngine(thresholds)
        events = engine.evaluate(snapshot)     # 可能返回 []、1 条或多条
        for ev in events: dispatcher.dispatch(ev)
    """

    def __init__(self, thresholds: Optional[Thresholds] = None) -> None:
        self.th = thresholds or Thresholds()
        self.th.validate()
        # 上一次实际发出的报警（用于"沿判定"与恢复检测）
        self._active: Dict[AlarmCode, float] = {}   # 报警码 -> 首次触发时间
        self._last_emit: Dict[AlarmCode, float] = {}  # 报警码 -> 上次发出时间
        self._night_wakes: List[float] = []          # 夜间起夜时间戳
        self._last_motion_seen: Optional[float] = None

    # ------------------------------------------------------------------
    # 对外主入口
    # ------------------------------------------------------------------

    def evaluate(self, snap: ReadingSnapshot) -> List[AlarmEvent]:
        """评估一次快照，返回本次**应当下发**的报警事件列表。

        内部把两件事**刻意分开**（这是本模块最容易写错的地方）：
        - ``triggered``：**本轮是否处于报警条件**（决定"要不要发 ALL_CLEAR"）。
          只要条件成立就必须登记，**不受冷却影响**——否则冷却期内会被误判成"已恢复"。
        - ``events``：**本轮哪些报警需要真的发给用户**（受 ``repeat_cooldown_s`` 抑制）。
        """
        events: List[AlarmEvent] = []
        triggered: Dict[AlarmCode, AlarmEvent] = {}

        # ---- 1. 生理指标（只有真的读到值才判定） ----
        v = snap.vitals
        if v is not None and v.ok:
            if not v.finger_detected:
                # 没检测到手指：**不做生理判定**（否则会误报"心率过低"）
                pass
            else:
                if v.heart_rate_bpm is not None:
                    ev = self._check_high_low(
                        snap.ts, AlarmCode.HR_TOO_HIGH, AlarmCode.HR_TOO_LOW,
                        v.heart_rate_bpm, self.th.hr_min, self.th.hr_max, "bpm",
                        "心率", v.device,
                    )
                    if ev:
                        triggered[ev.code] = ev
                if v.spo2_percent is not None and self._below_bad(
                    AlarmCode.SPO2_TOO_LOW, v.spo2_percent, self.th.spo2_min
                ):
                    triggered[AlarmCode.SPO2_TOO_LOW] = AlarmEvent(
                        ts=snap.ts, code=AlarmCode.SPO2_TOO_LOW, severity=Severity.CRITICAL,
                        message=f"血氧偏低 {v.spo2_percent:.0f}%，请查看老人状态",
                        value=v.spo2_percent, unit="%", source=v.device,
                    )

        # ---- 2. 精密体温 ----
        bt = snap.body_temp
        if bt is not None and bt.ok and bt.temperature_c is not None:
            ev = self._check_high_low(
                snap.ts, AlarmCode.BODY_TEMP_HIGH, AlarmCode.BODY_TEMP_LOW,
                bt.temperature_c, self.th.body_temp_min, self.th.body_temp_max, "°C",
                "体温", bt.device,
            )
            if ev:
                triggered[ev.code] = ev

        # ---- 3. 环境温湿度（只提示，不紧急） ----
        am = snap.ambient
        if am is not None and am.ok:
            if am.temperature_c is not None:
                ev = self._check_high_low(
                    snap.ts, AlarmCode.AMBIENT_TEMP_HIGH, AlarmCode.AMBIENT_TEMP_LOW,
                    am.temperature_c, self.th.ambient_temp_min, self.th.ambient_temp_max,
                    "°C", "室温", am.device, severity=Severity.NOTICE,
                )
                if ev:
                    triggered[ev.code] = ev
            if am.humidity_percent is not None and self._above_bad(
                AlarmCode.HUMIDITY_HIGH, am.humidity_percent, self.th.humidity_max
            ):
                triggered[AlarmCode.HUMIDITY_HIGH] = AlarmEvent(
                    ts=snap.ts, code=AlarmCode.HUMIDITY_HIGH, severity=Severity.NOTICE,
                    message=f"湿度偏高 {am.humidity_percent:.0f}%，建议通风",
                    value=am.humidity_percent, unit="%", source=am.device,
                )

        # ---- 4. 久无活动 / 夜间频繁起夜 ----
        motion = snap.motion
        if motion is not None and motion.state is not MotionState.UNKNOWN:
            if motion.detected:
                self._last_motion_seen = snap.ts
                if self._in_night_window(snap.ts):
                    self._night_wakes.append(snap.ts)
            silent = snap.motion_silent_s
            if silent is None and self._last_motion_seen is not None:
                silent = snap.ts - self._last_motion_seen
            if silent is not None and silent >= self.th.no_motion_timeout_s:
                triggered[AlarmCode.NO_MOTION_TOO_LONG] = AlarmEvent(
                    ts=snap.ts, code=AlarmCode.NO_MOTION_TOO_LONG, severity=Severity.CRITICAL,
                    message=f"已有 {silent / 60:.0f} 分钟未检测到活动，请确认老人是否安全",
                    value=round(silent, 1), unit="s", source=motion.device,
                )
            # 夜间起夜统计（只在夜间窗口内计数，窗口外的记录会被清理）
            self._prune_night_wakes(snap.ts)
            if len(self._night_wakes) >= self.th.night_wake_count:
                triggered[AlarmCode.NIGHT_FREQUENT_WAKE] = AlarmEvent(
                    ts=snap.ts, code=AlarmCode.NIGHT_FREQUENT_WAKE, severity=Severity.WARNING,
                    message=f"夜间起夜已 {len(self._night_wakes)} 次，建议关注睡眠情况",
                    value=float(len(self._night_wakes)), unit="次", source=motion.device,
                )

        # ---- 5. 登记报警态（★必须在冷却过滤之前） ----
        for code, ev in triggered.items():
            self._active.setdefault(code, ev.ts)

        # ---- 6. 冷却过滤：同一报警在冷却期内只提醒一次（但不影响"仍在报警"这个状态） ----
        for code, ev in triggered.items():
            if self._cooldown_ok(code, ev.ts):
                events.append(ev)

        # ---- 7. 传感器故障（数据缺失必须显式上报，绝不当成正常） ----
        for device, count in snap.sensor_failures.items():
            if count >= self.th.sensor_fault_after:
                key = f"{AlarmCode.SENSOR_FAULT.value}:{device}"
                if self._cooldown_ok_str(key, snap.ts):
                    events.append(
                        AlarmEvent(
                            ts=snap.ts, code=AlarmCode.SENSOR_FAULT, severity=Severity.WARNING,
                            message=f"传感器 {device} 连续 {count} 次读取失败，请检查接线",
                            value=float(count), unit="次", source=device,
                            detail={"error": snap.sensor_errors.get(device, "")},
                        )
                    )

        # ---- 8. 恢复（ALL_CLEAR） ----
        events.extend(self._recoveries(snap, triggered))

        events.sort(key=lambda e: -int(e.severity))
        return events

    # ------------------------------------------------------------------
    # 内部：单指标判定
    # ------------------------------------------------------------------

    def _check_high_low(
        self,
        ts: float,
        high_code: AlarmCode,
        low_code: AlarmCode,
        value: float,
        low: float,
        high: float,
        unit: str,
        label: str,
        source: str,
        severity: Severity = Severity.WARNING,
    ) -> Optional[AlarmEvent]:
        if self._above_bad(high_code, value, high):
            return AlarmEvent(
                ts=ts, code=high_code, severity=severity,
                message=f"{label}偏高 {value:.1f}{unit}，请注意查看",
                value=value, unit=unit, source=source,
            )
        if self._below_bad(low_code, value, low):
            return AlarmEvent(
                ts=ts, code=low_code, severity=severity,
                message=f"{label}偏低 {value:.1f}{unit}，请注意查看",
                value=value, unit=unit, source=source,
            )
        return None

    def _above_bad(self, code: AlarmCode, value: float, threshold: float) -> bool:
        """判断"偏高类"是否仍处于报警条件。

        新触发：``value >= threshold``；已在报警态：要回落到 ``threshold - 迟滞`` 才算恢复。
        迟滞让"心率 111→108→111"这种抖动不会产生 报警/解除 的反复横跳。
        """
        if code in self._active:
            return value > (threshold - self.th.hysteresis)
        return value >= threshold

    def _below_bad(self, code: AlarmCode, value: float, threshold: float) -> bool:
        """判断"偏低类"是否仍处于报警条件（与 :meth:`_above_bad` 对称）。"""
        if code in self._active:
            return value < (threshold + self.th.hysteresis)
        return value <= threshold

    def _in_night_window(self, ts: float) -> bool:
        hour = datetime.fromtimestamp(ts).hour
        start, end = self.th.night_start_hour, self.th.night_end_hour
        if start <= end:
            return start <= hour < end
        return hour >= start or hour < end  # 跨零点，例如 22:00~06:00

    def _prune_night_wakes(self, ts: float) -> None:
        cutoff = ts - self.th.night_window_s
        self._night_wakes = [t for t in self._night_wakes if t >= cutoff and self._in_night_window(t)]

    # ------------------------------------------------------------------
    # 内部：冷却与恢复
    # ------------------------------------------------------------------

    def _cooldown_ok(self, code: AlarmCode, ts: float) -> bool:
        return self._cooldown_ok_str(code.value, ts)

    def _cooldown_ok_str(self, key: str, ts: float) -> bool:
        last = self._last_emit.get(key)  # type: ignore[arg-type]
        if last is None or (ts - last) >= self.th.repeat_cooldown_s:
            self._last_emit[key] = ts  # type: ignore[index]
            return True
        return False

    def _recoveries(
        self, snap: ReadingSnapshot, triggered: Dict[AlarmCode, AlarmEvent]
    ) -> List[AlarmEvent]:
        """对"上一轮在报警、这一轮不再触发"的项发出 ALL_CLEAR。"""
        recovered: List[AlarmEvent] = []
        for code in list(self._active):
            if code in triggered:
                continue
            # 数据缺失时**不解除**报警（读数读不到不代表恢复正常）
            if self._is_missing(snap, code):
                continue
            self._active.pop(code, None)
            self._last_emit.pop(code.value, None)
            recovered.append(
                AlarmEvent(
                    ts=snap.ts, code=AlarmCode.ALL_CLEAR, severity=Severity.NORMAL,
                    message=f"{self._label(code)}已恢复正常",
                    source="",
                    detail={"recovered_code": code.value},
                )
            )
        return recovered

    @staticmethod
    def _is_missing(snap: ReadingSnapshot, code: AlarmCode) -> bool:
        """判断某个报警码依赖的数据这一轮是否缺失。"""
        if code in (AlarmCode.HR_TOO_HIGH, AlarmCode.HR_TOO_LOW, AlarmCode.SPO2_TOO_LOW):
            v = snap.vitals
            return v is None or not v.ok or not v.finger_detected
        if code in (AlarmCode.BODY_TEMP_HIGH, AlarmCode.BODY_TEMP_LOW):
            b = snap.body_temp
            return b is None or not b.ok or b.temperature_c is None
        if code in (AlarmCode.AMBIENT_TEMP_HIGH, AlarmCode.AMBIENT_TEMP_LOW, AlarmCode.HUMIDITY_HIGH):
            a = snap.ambient
            return a is None or not a.ok
        if code in (AlarmCode.NO_MOTION_TOO_LONG, AlarmCode.NIGHT_FREQUENT_WAKE):
            return snap.motion is None or snap.motion.state is MotionState.UNKNOWN
        return False

    @staticmethod
    def _label(code: AlarmCode) -> str:
        return {
            AlarmCode.HR_TOO_HIGH: "心率偏高",
            AlarmCode.HR_TOO_LOW: "心率偏低",
            AlarmCode.SPO2_TOO_LOW: "血氧偏低",
            AlarmCode.BODY_TEMP_HIGH: "体温偏高",
            AlarmCode.BODY_TEMP_LOW: "体温偏低",
            AlarmCode.AMBIENT_TEMP_HIGH: "室温偏高",
            AlarmCode.AMBIENT_TEMP_LOW: "室温偏低",
            AlarmCode.HUMIDITY_HIGH: "湿度偏高",
            AlarmCode.NO_MOTION_TOO_LONG: "长时间无活动",
            AlarmCode.NIGHT_FREQUENT_WAKE: "夜间频繁起夜",
        }.get(code, code.value)

    # ------------------------------------------------------------------
    # 状态查询（给 Web/手机端展示"当前在报警什么"）
    # ------------------------------------------------------------------

    def active_alarms(self) -> Dict[str, float]:
        """当前处于报警态的项：``{报警码: 首次触发时间}``。"""
        return {k.value: v for k, v in self._active.items()}

    def clear_active(self, code: Optional[AlarmCode] = None) -> None:
        """人工/自动消音后清除报警态（只影响"是否在报警"，不影响历史记录）。"""
        if code is None:
            self._active.clear()
            self._last_emit.clear()
        else:
            self._active.pop(code, None)
            self._last_emit.pop(code.value, None)


__all__ = ["RuleEngine", "ReadingSnapshot"]
