"""彩屏信息页轮播 + LCD 调试面板（E57）的测试：**两块屏各管各的**。

为什么值得单独立一组：
* 用户 2026-09-30 定调：**彩屏 = 给主人看的信息面板（以"环境条件"为主页），
  LCD = 开发/调试面板**。所以轮播页序改成 3 页（环境 / 心率血氧 / 状态与报警），
  且页 0（环境）停留时间是其他页的 2 倍；
* LCD 与彩屏**同属 `DeviceKind.DISPLAY`** —— 任一侧广播出去都会毁掉对方的用途：
  信息页广播会冲掉 LCD 上的报警文案（E57），调试信息广播会冲掉彩屏上的信息页（同一个坑的另一半）。
  所以"信息页只发彩屏 / 调试面板只发 LCD / 两边都报警优先"必须有机器判据钉住；
* 顺带钉住文案纪律：纯 ASCII、每行 ≤16 字符（彩屏是 8×8 点阵字库，中文会变 `?`；超长会被截断）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_RPI_DIR = Path(__file__).resolve().parents[2]
if str(_RPI_DIR) not in sys.path:
    sys.path.insert(0, str(_RPI_DIR))

from health_monitor.core.config import AppConfig  # noqa: E402
from health_monitor.hal.models import MotionState  # noqa: E402
from health_monitor.playback import PlaybackRuntime  # noqa: E402
from health_monitor.service import (  # noqa: E402
    DEBUG_PANEL_INTERVAL_S,
    DISPLAY_PAGE_INTERVAL_S,
    PAGE_DWELL_UNITS,
    debug_lines,
    display_page_lines,
)
from tests.core.fakes import FakeAmbientSensor, FakeVitalSensor  # noqa: E402

#: 最小可用配置（与 `test_status_page.py` 同源）：**必须含 display 与 tft**，
#: 因为本组测试的核心判据就是"信息页只发给彩屏、不碰 LCD"。
DEMO = {
    "thresholds": {"no_motion_timeout_s": 60, "repeat_cooldown_s": 0},
    "devices": {
        "vitals": {"driver": "max30102", "read_interval_s": 1.0},
        "ambient": {"driver": "dht11", "read_interval_s": 3.0},
        "motion": {"driver": "hc_sr501", "read_interval_s": 0.5},
        "display": {"driver": "lcd1602", "read_interval_s": 1.0},
        "tft": {"driver": "tft_spi", "read_interval_s": 2.0,
                "params": {"controller": "st7735", "spi_device": 1}},
        "status_led": {"driver": "led", "read_interval_s": 1.0},
    },
}


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def make_runtime(clock: _Clock) -> PlaybackRuntime:
    return PlaybackRuntime(
        AppConfig.from_dict(DEMO),
        clock=clock,
        sleep=lambda _s: None,
        verbose_outputs=False,
    )


def _vital(**kwargs):
    """造一个"已打开"的假心率样本（假传感器必须先 open 才能 read）。"""
    device = FakeVitalSensor(**kwargs)
    device.open()
    return device.read()


def _ambient(**kwargs):
    device = FakeAmbientSensor(**kwargs)
    device.open()
    return device.read()


class TestPageTexts(unittest.TestCase):
    """四页文案本身（纯函数）。"""

    def test_每页都只有两行且是纯ASCII不超过16字符(self) -> None:
        snap = type("S", (), {"vitals": None, "ambient": None, "sensor_failures": {}})()
        for page in range(len(PAGE_DWELL_UNITS)):
            with self.subTest(page=page):
                lines = display_page_lines(page, snap, alarm_count=2, last_alarm="hr_high")
                self.assertEqual(len(lines), 2)
                for line in lines:
                    self.assertTrue(line.isascii(), f"彩屏只能显示 ASCII：{line!r}")
                    self.assertLessEqual(len(line), 16, f"超过 16 字符会被截断：{line!r}")

    def test_页序与驱动里的页码名字对应(self) -> None:
        from health_monitor.outputs.tft_spi import PAGE_ASCII, PAGE_NAMES

        self.assertEqual(
            len(PAGE_ASCII), len(PAGE_DWELL_UNITS),
            "彩屏英文页名数必须与轮播页数（PAGE_DWELL_UNITS）一致，否则页码错位",
        )
        self.assertEqual(len(PAGE_NAMES), len(PAGE_ASCII), "中文页名与英文页名必须一一对应")
        self.assertEqual(PAGE_ASCII[0], "ROOM", "2026-09-30：页 0 必须是环境页（用户要求）")

    def test_心率页在没手指时说人话(self) -> None:
        snap = type("S", (), {"vitals": _vital(finger=False), "ambient": None,
                              "sensor_failures": {}})()
        lines = display_page_lines(1, snap)
        self.assertIn("FINGER", " ".join(lines))

    def test_心率页在有读数时给数值(self) -> None:
        snap = type("S", (), {"vitals": _vital(hr=72.0, spo2=98.0), "ambient": None,
                              "sensor_failures": {}})()
        lines = display_page_lines(1, snap)
        self.assertIn("72", lines[0])
        self.assertIn("98", lines[1])

    def test_环境页是第一页且给温湿度(self) -> None:
        snap = type("S", (), {"vitals": None,
                              "ambient": _ambient(temperature_c=23.4, humidity_percent=61.0),
                              "sensor_failures": {}})()
        lines = display_page_lines(0, snap)
        self.assertIn("23.4", lines[0])
        self.assertIn("61", lines[1])

    def test_环境页没数据时用横线而不是0(self) -> None:
        snap = type("S", (), {"vitals": None, "ambient": None, "sensor_failures": {}})()
        lines = display_page_lines(0, snap)
        self.assertIn("--", lines[0])
        self.assertIn("--", lines[1])

    def test_环境页带上家里有没有人(self) -> None:
        """★ 2026-09-30：环境页要回答"家里有没有人"——它比心率血氧"长驻"
        （心率血氧要人主动伸手去测，没人测的时候是空窗）。"""
        snap = type("S", (), {
            "vitals": None,
            "ambient": _ambient(temperature_c=23.4, humidity_percent=61.0),
            "motion": type("M", (), {"state": MotionState.DETECTED})(),
            "sensor_failures": {},
        })()
        self.assertIn("PIR Y", display_page_lines(0, snap)[1])
        snap.motion = type("M", (), {"state": MotionState.IDLE})()
        self.assertIn("PIR N", display_page_lines(0, snap)[1])

    def test_环境页没接人体红外时标横线(self) -> None:
        snap = type("S", (), {"vitals": None,
                              "ambient": _ambient(temperature_c=23.4, humidity_percent=61.0),
                              "motion": None, "sensor_failures": {}})()
        lines = display_page_lines(0, snap)
        self.assertIn("PIR -", lines[1])
        self.assertTrue(lines[1].isascii())
        self.assertLessEqual(len(lines[1]), 16)

    def test_报警页在有故障时提示FAULT(self) -> None:
        snap = type("S", (), {"vitals": None, "ambient": None,
                              "sensor_failures": {"body_temp": 3}})()
        lines = display_page_lines(2, snap, alarm_count=5, last_alarm="sensor_fault")
        self.assertIn("FAULT", lines[0])

    def test_页码取模不会越界(self) -> None:
        snap = type("S", (), {"vitals": None, "ambient": None, "sensor_failures": {}})()
        n = len(PAGE_DWELL_UNITS)
        self.assertEqual(display_page_lines(3 * n, snap), display_page_lines(0, snap))
        self.assertEqual(display_page_lines(3 * n + n - 1, snap),
                         display_page_lines(n - 1, snap))


class TestPageRotation(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = _Clock()
        self.rt = make_runtime(self.clock)
        self.rt.open()
        self.rt.set_vitals(heart_rate=72.0, spo2=98.0)
        self.rt.set_ambient(temperature_c=23.0, humidity_percent=60.0)
        self.rt.set_motion(MotionState.DETECTED, silent_s=0.0)
        self.rt.tick()

    def tearDown(self) -> None:
        self.rt.close()

    def _advance_and_tick(self, seconds: float) -> None:
        self.clock.advance(seconds)
        self.rt.tick()

    def _tft_lines(self) -> tuple:
        return tuple(self.rt.devices["tft"].read().lines)

    def _lcd_lines(self) -> tuple:
        return tuple(self.rt.devices["display"].read().lines)

    def test_闲时会按周期翻页(self) -> None:
        self._advance_and_tick(DISPLAY_PAGE_INTERVAL_S + 1.0)
        first = self._tft_lines()
        self._advance_and_tick(DISPLAY_PAGE_INTERVAL_S + 1.0)
        second = self._tft_lines()
        self.assertNotEqual(first, second, "到点应当翻页")

    def test_没到时间不翻页(self) -> None:
        self._advance_and_tick(DISPLAY_PAGE_INTERVAL_S + 1.0)
        before = self._tft_lines()
        self._advance_and_tick(0.5)      # 远不到一个周期
        self.assertEqual(before, self._tft_lines(), "没到点不该翻页")

    def test_信息页只进彩屏_调试面板只进LCD(self) -> None:
        """★ 屏分工（2026-09-30）：信息页只许进彩屏、调试面板只许进 LCD。

        判据用**格式**而不是内容：LCD 两行必须是 ``T=…`` / ``N=…``（调试面板），
        彩屏两行必须**不出现**这两种调试格式。这样任一方向的"串屏"都会被当场抓住。
        """
        for _ in range(5):
            self._advance_and_tick(DISPLAY_PAGE_INTERVAL_S + 1.0)
        lcd = self._lcd_lines()
        tft = self._tft_lines()
        self.assertTrue(lcd[0].startswith("T="), f"LCD 应是调试面板：{lcd!r}")
        self.assertTrue(lcd[1].startswith("N="), f"LCD 应是调试面板：{lcd!r}")
        self.assertFalse(tft[0].startswith("T="), f"调试面板文案串到彩屏了：{tft!r}")
        self.assertFalse(tft[1].startswith("N="), f"调试面板文案串到彩屏了：{tft!r}")

    def test_环境页停得比其他页久(self) -> None:
        """2026-09-30：页 0 = 环境页，停留倍数是其他页的 2 倍（它是"主页面"）。"""
        self.assertGreater(PAGE_DWELL_UNITS[0], max(PAGE_DWELL_UNITS[1:]),
                           "页 0（环境）必须比其他页停得久")
        # setUp 已把彩屏停在页 0；再走"一个基本周期"还**不该**翻页
        self._advance_and_tick(DISPLAY_PAGE_INTERVAL_S + 1.0)
        self.assertIn("ROOM", self._tft_lines()[0], "环境页应当还在（还没停够）")

    def test_开机第一眼是环境页(self) -> None:
        """``_page`` 初值 -1 ⇒ 第一次轮播落到页 0（环境），而不是先闪一下别的页。"""
        self.assertIn("ROOM", self._tft_lines()[0], f"开机应是环境页：{self._tft_lines()!r}")

    def test_有活动报警时不翻页(self) -> None:
        self._advance_and_tick(DISPLAY_PAGE_INTERVAL_S + 1.0)
        self.rt.sos()                                    # 触发一个活动报警（SOS）
        self.rt.tick()
        frozen = self._tft_lines()
        for _ in range(3):
            self._advance_and_tick(DISPLAY_PAGE_INTERVAL_S + 1.0)
        self.assertEqual(frozen, self._tft_lines(), "报警期间不许被信息页冲掉")
        self.assertIn("SOS", " ".join(self._lcd_lines() + self._tft_lines()), "报警文案要显示出来")


class TestDebugPanel(unittest.TestCase):
    """LCD 调试面板（2026-09-30 屏分工：**LCD 专职当开发/调试面板**）。"""

    def test_文案合规且带关键计数(self) -> None:
        snap = type("S", (), {"vitals": None,
                              "ambient": _ambient(temperature_c=23.4, humidity_percent=57.0),
                              "sensor_failures": {}})()
        lines = debug_lines(snap, ticks=1234, failures=0, alarm_count=0)
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertTrue(line.isascii(), f"LCD1602 只有 ASCII 字库：{line!r}")
            self.assertLessEqual(len(line), 16, f"超过 16 字符会被截断：{line!r}")
        self.assertIn("23.4", lines[0])
        self.assertIn("57", lines[0])
        self.assertIn("N=1234", lines[1])
        self.assertIn("F=0", lines[1])
        self.assertIn("A=0", lines[1])

    def test_没读数时用横线而不是0(self) -> None:
        """E58 的同一课：**"没有读数" 与 "读数是 0" 必须能分开**。"""
        snap = type("S", (), {"vitals": None, "ambient": None, "sensor_failures": {}})()
        self.assertIn("--", debug_lines(snap)[0])

    def _runtime(self):
        clock = _Clock()
        rt = make_runtime(clock)
        rt.open()
        rt.set_ambient(temperature_c=23.0, humidity_percent=60.0)
        return clock, rt

    def test_闲时LCD是调试面板而彩屏是环境页(self) -> None:
        _clock, rt = self._runtime()
        try:
            rt.tick()
            lcd = tuple(rt.devices["display"].read().lines)
            tft = tuple(rt.devices["tft"].read().lines)
            self.assertTrue(lcd[0].startswith("T="), f"LCD 应是调试面板：{lcd!r}")
            self.assertIn("ROOM", tft[0], f"彩屏应是环境信息页：{tft!r}")
        finally:
            rt.close()

    def test_有活动报警时不刷调试面板(self) -> None:
        """★ 报警文案要留在 LCD 上，调试面板不许把它冲掉（与彩屏轮播对称）。"""
        clock, rt = self._runtime()
        try:
            rt.tick()
            rt.sos()
            rt.tick()
            frozen = tuple(rt.devices["display"].read().lines)
            self.assertIn("SOS", " ".join(frozen), "SOS 应当显示在屏上")
            for _ in range(5):
                clock.advance(DEBUG_PANEL_INTERVAL_S + 1.0)
                rt.tick()
            self.assertEqual(frozen, tuple(rt.devices["display"].read().lines),
                             "报警期间不许被调试面板冲掉")
        finally:
            rt.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
