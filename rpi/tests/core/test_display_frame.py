"""彩屏"富帧"构造函数 `display_page_frame()` 测试（2026-10-01）。

用户原话：「那个 TFT 屏幕平时就不显示东西，你应该多多使用他」，
澄清后是「**上面平时显示的内容好少**」——
老版只送两行字，128×160 的屏中间约 100 px 全是黑的。

这里钉的几件事，每一条都对应一种"屏上会出错"的方式：

1. **必须纯 ASCII** —— 彩屏只有 8×8 点阵字库，中文会变成 `?`（`ERROR.md` E53）；
2. **读不到时不许画 0**，而要 `--` + 标过期（与网页同一条约定）；
3. **趋势线缺口用 None 传下去**（驱动负责断线，不跨缺口连线）；
4. **`lines` 必须一直在** —— 它是"这块屏现在显示什么"的两行摘要，
   E57 要求两块屏 `status()/read()` 能读到同一个口径；
5. **脏时间戳不许打崩渲染**。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from health_monitor.hal.models import AmbientSample, MotionSample, MotionState, VitalSignsSample
from health_monitor.service import display_page_frame, display_page_lines


def snap(ambient=None, motion=None, vitals=None, failures=None, data_age_s=2.0):
    return SimpleNamespace(
        ambient=ambient, motion=motion, vitals=vitals,
        sensor_failures=failures or {}, data_age_s=data_age_s,
    )


def ambient(temp=23.4, hum=57.0, ok=True):
    return AmbientSample(ts=1000.0, device="dht11", ok=ok,
                         temperature_c=temp if ok else None,
                         humidity_percent=hum if ok else None)


def motion(state=MotionState.IDLE):
    # `detected` 是 MotionSample 的**只读属性**（由 state 推出来），不是构造参数
    return MotionSample(ts=1000.0, device="hc_sr501", ok=True, state=state)


NOW = 1_700_000_000.0


class TestAsciiOnly(unittest.TestCase):
    """★ 彩屏只有 ASCII 字库：帧里任何一段文字都必须能被点阵字库画出来。"""

    def _all_text(self, frame):
        parts = [frame.get("label", ""), frame.get("value", ""), frame.get("unit", ""),
                 frame.get("clock", ""), frame.get("footer", "")]
        parts += list(frame.get("rows") or [])
        parts += [str(x) for x in (frame.get("lines") or ())]
        return parts

    def test_所有页面所有字段都只有ASCII(self):
        cases = [
            (0, snap(ambient=ambient(), motion=motion())),
            (0, snap(ambient=ambient(ok=False))),
            (1, snap(ambient=ambient())),
            (2, snap(failures={"ambient": 3})),
        ]
        for page, s in cases:
            with self.subTest(page=page):
                frame = display_page_frame(page, s, now=NOW,
                                           spo2_last={"ok": True, "heart_rate_bpm": 68.0,
                                                      "spo2_percent": 97.0, "ts": NOW - 60})
                for text in self._all_text(frame):
                    self.assertTrue(all(ord(ch) < 128 for ch in str(text)),
                                    f"{text!r} 含非 ASCII，屏上会变成 ?")


class TestEnvPage(unittest.TestCase):
    def test_正常环境页给大字与两行小字(self):
        frame = display_page_frame(0, snap(ambient=ambient(), motion=motion()), now=NOW)
        self.assertEqual(frame["label"], "ROOM")
        self.assertEqual(frame["value"], "23.4")
        self.assertEqual(frame["unit"], "C")
        self.assertFalse(frame["stale"])
        self.assertIn("HUM", frame["rows"][0])
        self.assertIn("MOTION", frame["rows"][1])

    def test_读不到环境时画横线并标过期_绝不画0(self):
        """★ "读不到"与"读数是 0"必须能分开（项目既有硬纪律，E58）。"""
        frame = display_page_frame(0, snap(ambient=ambient(ok=False)), now=NOW)
        self.assertEqual(frame["value"], "--")
        self.assertTrue(frame["stale"], "读不到就要标成过期")
        self.assertNotIn("0", [frame["value"]], "绝不许拿 0 冒充没有数据")

    def test_趋势线原样传给驱动_缺口保持None(self):
        trend = [23.0, 23.1, None, None, 23.4]
        frame = display_page_frame(0, snap(ambient=ambient()), now=NOW, temp_trend=trend)
        self.assertEqual(frame["trend"], trend, "缺口必须原样传下去（驱动负责断线）")

    def test_趋势点太多时只留最近的(self):
        frame = display_page_frame(0, snap(ambient=ambient()), now=NOW,
                                   temp_trend=[float(i) for i in range(100)])
        self.assertLessEqual(len(frame["trend"]), 32)

    def test_页脚带报警数与数据年龄(self):
        frame = display_page_frame(0, snap(ambient=ambient(), data_age_s=7.0), now=NOW,
                                   alarm_count=2)
        self.assertIn("ALARMS 2", frame["footer"])
        self.assertIn("7", frame["footer"])


class TestVitalsPage(unittest.TestCase):
    def test_测完不久显示大字血氧与心率(self):
        frame = display_page_frame(1, snap(), now=NOW,
                                   spo2_last={"ok": True, "heart_rate_bpm": 68.0,
                                              "spo2_percent": 97.0, "ts": NOW - 60})
        self.assertEqual(frame["label"], "SPO2")
        self.assertEqual(frame["value"], "97")
        self.assertEqual(frame["unit"], "%")
        self.assertFalse(frame["stale"], "一分钟前不算过期")
        self.assertIn("68", frame["rows"][0])

    def test_测量很久以前算过期(self):
        """与网页同一条约定：旧值可以显示，但要标出来。"""
        frame = display_page_frame(1, snap(), now=NOW,
                                   spo2_last={"ok": True, "heart_rate_bpm": 68.0,
                                              "spo2_percent": 97.0, "ts": NOW - 1800})
        self.assertTrue(frame["stale"])
        self.assertIn("LAST 30m AGO", frame["footer"])

    def test_从没测过时明确说按需测量(self):
        frame = display_page_frame(1, snap(), now=NOW, spo2_last=None)
        self.assertEqual(frame["value"], "--")
        self.assertTrue(frame["stale"])
        self.assertEqual(frame["rows"][0], "NO MEASURE YET")
        self.assertEqual(frame["footer"], "BY DEMAND ONLY")

    def test_上次测量失败也明确显示没有值(self):
        frame = display_page_frame(1, snap(), now=NOW,
                                   spo2_last={"ok": False, "heart_rate_bpm": None,
                                              "spo2_percent": None, "ts": NOW - 60})
        self.assertEqual(frame["value"], "--")
        self.assertTrue(frame["stale"])


class TestStatusPage(unittest.TestCase):
    def test_有故障时列出设备名(self):
        frame = display_page_frame(2, snap(failures={"ambient": 3, "motion": 1}), now=NOW,
                                   alarm_count=1)
        self.assertTrue(frame["stale"])
        self.assertIn("FAULT ambient", frame["rows"])
        self.assertEqual(frame["footer"], "CHECK DEVICE")

    def test_状态页刻意不画时钟(self):
        """★ 2026-10-01 预览图发现的：画上时钟后第一行要给时钟让位，
        `FAULT ambient` 会被截成 `FAULT ambi` —— 而这一页的时钟本来是多余的，设备名更重要。"""
        frame = display_page_frame(2, snap(failures={"ambient": 3}), now=NOW)
        self.assertEqual(frame["clock"], "")
        self.assertIn("FAULT ambient", frame["rows"])

    def test_没有故障时明确说正常(self):
        frame = display_page_frame(2, snap(), now=NOW, alarm_count=0, last_alarm="all_clear")
        self.assertFalse(frame["stale"])
        self.assertEqual(frame["rows"][0], "ALL OK")
        self.assertEqual(frame["footer"], "ALL CLEAR")


class TestInvariants(unittest.TestCase):
    def test_lines始终存在且与纯文本函数一致(self):
        """★ E57：`lines` 是"这块屏现在显示什么"的**两行摘要**，不许因为加富帧就丢。"""
        for page in (0, 1, 2):
            for s in (snap(ambient=ambient(), motion=motion()), snap()):
                with self.subTest(page=page):
                    frame = display_page_frame(page, s, now=NOW, alarm_count=1)
                    self.assertEqual(
                        tuple(frame["lines"]),
                        display_page_lines(page, s, alarm_count=1, last_alarm=""),
                    )

    def test_脏时间戳不打崩(self):
        for bad in (-1e18, 1e30, float("nan")):
            with self.subTest(now=bad):
                frame = display_page_frame(0, snap(ambient=ambient()), now=bad)
                self.assertIn("value", frame)          # 不抛异常就算过

    def test_页号越界按取模处理(self):
        a = display_page_frame(0, snap(ambient=ambient()), now=NOW)
        b = display_page_frame(3, snap(ambient=ambient()), now=NOW)
        self.assertEqual(a["value"], b["value"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
