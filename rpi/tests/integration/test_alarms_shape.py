"""报警接口的**形状契约（板子这一侧）**：`pushed` 是布尔、`detail` 是开放的 JSON 对象。

来历（2026-10-02，手机真机验收 A5 抓到的真缺陷）
---------------------------------------------------
安卓端把这两个字段声明成了 `pushed: Int?` 与 `detail: Map<String, String>`，而板子实际发的是：

    "pushed": false,                                     ← **布尔**（不是 0/1）
    "detail": {"ok": false, "heart_rate_bpm": null,      ← **混合类型**的开放字典
               "spo2_percent": null, "valid_samples": 0}

⇒ 真机上"报警事件（0 条）/ 响应格式不对：树莓派返回的不是预期 JSON"，
而**所有单测全绿** —— 因为 App 那边的样本是**照契约文档手写**的
（文档写 `pushed: 0`、样本 `detail` 恰好是空对象），恰好绕开了真机形状。

这条守卫从**板子侧**把形状钉死；App 侧另有 `JsonSamples.ALARMS_REAL_DEVICE`
（真机原样报文）做对应的回归钉子 —— **两边一起才能防住"文档/实现/客户端三方漂移"**。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from health_monitor.core.store import Store
from health_monitor.hal.models import AlarmCode, AlarmEvent, Severity
from health_monitor.playback import PlaybackRuntime
from health_monitor.core.config import AppConfig
from health_monitor.demo import DEMO_CONFIG
from health_monitor.net.web import WebApi


def _event(code: AlarmCode, detail: dict, ts: float) -> AlarmEvent:
    return AlarmEvent(ts=ts, code=code, severity=Severity.NOTICE,
                      message="室温偏高 22.7°C，请注意查看", value=22.7, unit="°C",
                      source="ambient", detail=detail)


class TestStoreAlarmRowShape(unittest.TestCase):
    """`Store.recent_alarms()` 是 `pushed` 类型的**源头**（那里做了 `bool(...)`）。"""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._dir.name) / "history.db")
        self.addCleanup(self._dir.cleanup)
        self.addCleanup(self.store.close)

    def test_pushed_是布尔而不是整数(self) -> None:
        self.store.save_alarm(_event(AlarmCode.AMBIENT_TEMP_HIGH, {}, 100.0), pushed=False)
        self.store.save_alarm(_event(AlarmCode.HR_TOO_HIGH, {}, 200.0), pushed=True)
        rows = {r["code"]: r for r in self.store.recent_alarms(limit=10)}

        self.assertIsInstance(rows["ambient_temp_high"]["pushed"], bool,
                              "pushed 必须是布尔：App 侧按 Boolean 解析（原来写 0/1 就会崩）")
        self.assertIs(rows["ambient_temp_high"]["pushed"], False)
        self.assertIs(rows["hr_too_high"]["pushed"], True)

    def test_detail_是开放字典_混合类型都要原样保留(self) -> None:
        detail = {"ok": False, "heart_rate_bpm": None, "valid_samples": 0,
                  "error": "数据陈旧（超过 3 × 0.2s 未更新）"}
        self.store.save_alarm(_event(AlarmCode.SPO2_MEASURED, detail, 150.0),
                              pushed=False)
        row = self.store.recent_alarms(limit=1)[0]

        self.assertIsInstance(row["detail"], dict)
        self.assertIs(row["detail"]["ok"], False, "布尔不能被转成字符串")
        self.assertIsNone(row["detail"]["heart_rate_bpm"], "null 要保留成 None")
        self.assertEqual(row["detail"]["valid_samples"], 0, "整数要保持整数")
        self.assertIn("数据陈旧", row["detail"]["error"])

    def test_detail_坏JSON不能把接口打崩(self) -> None:
        """库里要是存了坏 JSON，读出来必须是空字典，而不是抛异常。"""
        with self.store._connect() as conn:  # noqa: SLF001 - 守卫就是要构造这种坏数据
            conn.execute(
                "INSERT INTO alarms (ts, code, severity, message, value, unit, source, detail, pushed)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (300.0, "ambient_temp_high", 1, "坏 detail", 22.7, "°C", "ambient", "{not json", 0),
            )
        row = self.store.recent_alarms(limit=1)[0]
        self.assertEqual(row["detail"], {})


class TestAlarmsEndpointJsonShape(unittest.TestCase):
    """整条接口的 JSON 形状：必须是 JSON 布尔与 JSON 对象（不是 0/1、不是字符串）。"""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.rt = PlaybackRuntime(
            AppConfig.from_dict(DEMO_CONFIG), sleep=lambda _s: None,
            verbose_outputs=False, store_path=str(Path(self._dir.name) / "h.db"),
        )
        self.rt.open()
        self.addCleanup(self.rt.close)
        self.addCleanup(self.rt.stop)

    def test_序列化后是_json_布尔与对象(self) -> None:
        with self.rt.store._connect() as conn:  # noqa: SLF001
            conn.execute(
                "INSERT INTO alarms (ts, code, severity, message, value, unit, source, detail, pushed)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (1790912692.5, "ambient_temp_high", 1, "室温偏高 22.7°C，请注意查看", 22.7, "°C",
                 "ambient", json.dumps({"ok": False, "valid_samples": 0}), 0),
            )
        status, payload = WebApi(self.rt).handle("GET", "/api/v1/alarms", {}, {})
        self.assertEqual(status, 200)
        row = payload["history"][0]

        # ① Python 侧类型正确
        self.assertIsInstance(row["pushed"], bool)
        self.assertIsInstance(row["detail"], dict)

        # ② **序列化之后**也必须是 JSON 的 true/false 与对象 ——
        #    这正是 App 解析时看到的东西，也是当初出问题的地方。
        raw = json.dumps(payload["history"], ensure_ascii=False)
        self.assertIn('"pushed": false', raw, "必须是 JSON 布尔 false，不能是 0")
        self.assertNotIn('"pushed": 0', raw)
        self.assertIn('"detail": {"ok": false, "valid_samples": 0}', raw,
                      "detail 必须是 JSON 对象且混合类型原样保留")

    def test_finger缺失时detail里的null保持null(self) -> None:
        """`{"heart_rate_bpm": null}` 这种形状真机上有过，不能被写成字符串 "null"。"""
        with self.rt.store._connect() as conn:  # noqa: SLF001
            conn.execute(
                "INSERT INTO alarms (ts, code, severity, message, value, unit, source, detail, pushed)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (1790912701.4, "spo2_measured", 1, "血氧测量未取得有效读数", None, "", "spo2",
                 json.dumps({"ok": False, "heart_rate_bpm": None, "valid_samples": 0}), 0),
            )
        _status, payload = WebApi(self.rt).handle("GET", "/api/v1/alarms", {}, {})
        raw = json.dumps(payload["history"], ensure_ascii=False)
        self.assertIn('"heart_rate_bpm": null', raw)
        self.assertNotIn('"heart_rate_bpm": "null"', raw)


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
