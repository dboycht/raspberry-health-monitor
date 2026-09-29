"""彩屏信息页轮播（E57）的测试：**只在没有活动报警时翻页，而且只发给彩屏**。

为什么值得单独立一组：
* 用户要的是"比字符屏好看的信息面板：4 页（监护总览 / 心率血氧 / 体温环境 / 报警记录）"；
* 但 LCD 与彩屏**同属 `DeviceKind.DISPLAY`** —— 轮播若广播出去，会把 LCD 上的**报警文案冲掉**
  （LCD 是报警显示，`docs/07` H11）。所以"只发给彩屏"和"报警优先"这两条都必须有机器判据；
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
    DISPLAY_PAGE_INTERVAL_S,
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

    def test_四页都只有两行且是纯ASCII不超过16字符(self) -> None:
        snap = type("S", (), {"vitals": None, "ambient": None, "sensor_failures": {}})()
        for page in range(4):
            with self.subTest(page=page):
                lines = display_page_lines(page, snap, alarm_count=2, last_alarm="hr_high")
                self.assertEqual(len(lines), 2)
                for line in lines:
                    self.assertTrue(line.isascii(), f"彩屏只能显示 ASCII：{line!r}")
                    self.assertLessEqual(len(line), 16, f"超过 16 字符会被截断：{line!r}")

    def test_页序与驱动里的页码名字对应(self) -> None:
        from health_monitor.outputs.tft_spi import PAGE_ASCII

        self.assertEqual(len(PAGE_ASCII), 4, "彩屏英文页名必须正好 4 个（与轮播页数一致）")

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

    def test_环境页给温湿度(self) -> None:
        snap = type("S", (), {"vitals": None,
                              "ambient": _ambient(temperature_c=23.4, humidity_percent=61.0),
                              "sensor_failures": {}})()
        lines = display_page_lines(2, snap)
        self.assertIn("23.4", lines[0])
        self.assertIn("61", lines[1])

    def test_环境页没数据时用横线而不是0(self) -> None:
        snap = type("S", (), {"vitals": None, "ambient": None, "sensor_failures": {}})()
        lines = display_page_lines(2, snap)
        self.assertIn("--", lines[0])
        self.assertIn("--", lines[1])

    def test_报警页在有故障时提示FAULT(self) -> None:
        snap = type("S", (), {"vitals": None, "ambient": None,
                              "sensor_failures": {"body_temp": 3}})()
        lines = display_page_lines(3, snap, alarm_count=5, last_alarm="sensor_fault")
        self.assertIn("FAULT", lines[0])

    def test_页码取模不会越界(self) -> None:
        snap = type("S", (), {"vitals": None, "ambient": None, "sensor_failures": {}})()
        self.assertEqual(display_page_lines(7, snap), display_page_lines(3, snap))


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

    def test_翻页不会动LCD(self) -> None:
        """★ LCD 是报警显示：信息页轮播**绝不能**写到它上面。"""
        lcd_before = self._lcd_lines()
        for _ in range(4):
            self._advance_and_tick(DISPLAY_PAGE_INTERVAL_S + 1.0)
        self.assertEqual(lcd_before, self._lcd_lines(), "轮播只许发给彩屏")

    def test_有活动报警时不翻页(self) -> None:
        self._advance_and_tick(DISPLAY_PAGE_INTERVAL_S + 1.0)
        self.rt.sos()                                    # 触发一个活动报警（SOS）
        self.rt.tick()
        frozen = self._tft_lines()
        for _ in range(3):
            self._advance_and_tick(DISPLAY_PAGE_INTERVAL_S + 1.0)
        self.assertEqual(frozen, self._tft_lines(), "报警期间不许被信息页冲掉")
        self.assertIn("SOS", " ".join(self._lcd_lines() + self._tft_lines()), "报警文案要显示出来")


if __name__ == "__main__":
    unittest.main(verbosity=2)
