"""启动时从历史库补"上一次测量结果"的测试（2026-10-01）。

**这是实测确认过的真缺陷，不是预防性代码**：`_spo2_last_result` / `_temp_trend` 是内存态，
进程一重启就空；而网页的"最后一次记录"直接查历史库 ⇒ 重启后**同一块板子上两边说法相反**：

    彩屏：`--` / `NO MEASURE YET` / `PRESS BUTTON`
    网页：`最后一次 97 %（3 小时前）· 数据已过期`

用户的要求是"血氧测量后，**屏上**和**后台**都能看到" —— 重启一次就不算数显然不对。

所以本文件里**最重要的一条**不是"补上了"，而是 `test_重启后屏与网页的说法必须一致`。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from health_monitor.core.config import AppConfig
from health_monitor.hal.models import AmbientSample, VitalSignsSample
from health_monitor.net.webui import render_page
from health_monitor.playback import PlaybackRuntime
from health_monitor.service import display_page_frame

DEMO = {
    "thresholds": {"no_motion_timeout_s": 60, "repeat_cooldown_s": 0,
                   "spo2_remind_interval_s": 120, "spo2_remind_timeout_s": 30,
                   "spo2_measure_s": 10},
    "devices": {
        "vitals": {"driver": "max30102", "read_interval_s": 1.0},
        "ambient": {"driver": "dht11", "read_interval_s": 3.0},
        "display": {"driver": "lcd1602", "read_interval_s": 1.0},
        "tft": {"driver": "tft_spi", "read_interval_s": 2.0,
                "params": {"controller": "st7735", "spi_device": 1}},
    },
}

T0 = 1_700_000_000.0


def empty_snap():
    return SimpleNamespace(ambient=None, motion=None, vitals=None,
                           sensor_failures={}, data_age_s=2.0)


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = str(Path(self._tmp.name) / "history.db")

    def _runtime(self, now: float = T0, store: bool = True) -> PlaybackRuntime:
        rt = PlaybackRuntime(config=AppConfig.from_dict(DEMO), clock=lambda: now,
                             store_path=self.db if store else None)
        rt.open()
        self.addCleanup(rt.close)
        return rt

    @staticmethod
    def _tft_vitals_frame(rt: PlaybackRuntime, now: float = T0) -> dict:
        """彩屏第 2 页（心率血氧）会显示什么 —— 与 `_rotate_display_page` 传法一致。"""
        return display_page_frame(2 - 1, empty_snap(), now=now,
                                  spo2_last=rt._spo2_last_result)


class TestHydrateLastMeasurement(_Fixture):
    def test_重启后屏与网页的说法必须一致(self) -> None:
        """★ 本文件的核心断言：同一份历史库，两边**不能**一个说 97、一个说"还没测过"。"""
        first = self._runtime()
        first.store.save_sample(VitalSignsSample(
            ts=T0, device="max30102", ok=True, heart_rate_bpm=68.0, spo2_percent=97.0,
            finger_detected=True, quality=0.9))
        first.close()

        restarted = self._runtime(now=T0 + 3600)          # 一小时后重启
        frame = self._tft_vitals_frame(restarted, now=T0 + 3600)
        page = render_page(restarted, refresh_s=0)        # 网页同一时刻

        self.assertIn("97", frame["value"], f"彩屏该显示补回来的血氧，实际 {frame['value']!r}")
        self.assertNotEqual(frame["rows"][0], "NO MEASURE YET", "重启后不该说'还没测过'")
        self.assertIn("97", page, "网页也必须显示那条记录")
        # 两边都带"过期"语义：彩屏 stale=True（30 分钟前的测量），网页由卡片负责
        self.assertTrue(frame["stale"], "一小时前的测量在屏上也该标过期")

    def test_补回来的记录带hydrated标记(self) -> None:
        """能一眼区分"本次测的"与"启动时从库补的"（排查时有用）。"""
        first = self._runtime()
        first.store.save_sample(VitalSignsSample(
            ts=T0, device="max30102", ok=True, heart_rate_bpm=70.0, spo2_percent=96.0,
            finger_detected=True, quality=0.9))
        first.close()
        restarted = self._runtime(now=T0 + 60)
        self.assertTrue(restarted._spo2_last_result["hydrated"])
        self.assertEqual(restarted._spo2_last_result["heart_rate_bpm"], 70.0)
        self.assertEqual(restarted._spo2_last_result["spo2_percent"], 96.0)

    def test_空库不补_保持原来的没有(self) -> None:
        rt = self._runtime()
        self.assertIsNone(rt._spo2_last_result, "库里没有就不能编一个出来")
        frame = self._tft_vitals_frame(rt)
        self.assertEqual(frame["rows"][0], "NO MEASURE YET")

    def test_只有心率没有血氧也能补(self) -> None:
        first = self._runtime()
        first.store.save_sample(VitalSignsSample(
            ts=T0, device="max30102", ok=True, heart_rate_bpm=66.0, spo2_percent=None,
            finger_detected=True, quality=0.9))
        first.close()
        restarted = self._runtime(now=T0 + 60)
        got = restarted._spo2_last_result
        self.assertEqual(got["heart_rate_bpm"], 66.0)
        self.assertIsNone(got["spo2_percent"])

    def test_没有历史库时也不崩(self) -> None:
        rt = self._runtime(store=False)
        self.assertIsNone(rt._spo2_last_result)

    def test_顺带补上室温趋势(self) -> None:
        """同一类问题：趋势线重启后也不该空一段。"""
        first = self._runtime()
        for i in range(5):
            first.store.save_sample(AmbientSample(
                ts=T0 - i * 3, device="dht11", ok=True,
                temperature_c=23.0 + i * 0.1, humidity_percent=57.0))
        first.close()
        restarted = self._runtime(now=T0 + 60)
        self.assertEqual(len(restarted._temp_trend), 5, "启动时该把最近的室温补进趋势缓冲")
        self.assertAlmostEqual(restarted._temp_trend[-1], 23.0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
