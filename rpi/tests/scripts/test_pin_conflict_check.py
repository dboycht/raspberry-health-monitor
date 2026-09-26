"""`validate.py` 的**撞脚检查**测试（含 2026-09-26 补上的那个洞）。

背景：撞脚检查原来只统计 `pin` / `trig_pin` / `echo_pin` + LED 的 `pins`，
**完全没统计 TFT 的 `dc_pin` / `reset_pin`** ⇒ 默认配置里
`status_led.red=24` 与 `tft.dc_pin=24` **真撞脚**，`validate.py` 却报"7 个独占引脚无冲突"。
（是"把红灯接到 GPIO12"时才发现的；这类"检查器漏了参数名"的洞必须用测试钉住。）
"""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

_RPI_DIR = Path(__file__).resolve().parents[2]      # .../rpi
_SCRIPTS_DIR = _RPI_DIR / "scripts"
for _path in (_RPI_DIR, _SCRIPTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import validate  # noqa: E402
from health_monitor.core.config import DEFAULT_CONFIG  # noqa: E402
from health_monitor.hal import find_conflicts  # noqa: E402


class TestExclusivePinClaims(unittest.TestCase):
    def test_包含TFT的DC与RST(self) -> None:
        """★ 回归：这两个脚原来没被统计，导致"红灯撞 TFT 的 DC"没被发现。"""
        claims = dict(validate.exclusive_pin_claims(DEFAULT_CONFIG))
        self.assertIn("tft.dc_pin", claims)
        self.assertIn("tft.reset_pin", claims)
        self.assertEqual(claims["tft.dc_pin"], 24)

    def test_包含LED各色与其它独占脚(self) -> None:
        claims = dict(validate.exclusive_pin_claims(DEFAULT_CONFIG))
        self.assertEqual(claims["status_led.led.green"], 22)
        self.assertEqual(claims["status_led.led.yellow"], 23)
        self.assertEqual(claims["status_led.led.red"], 12, "红灯已从 24 挪到 12（避开 TFT 的 DC）")
        self.assertEqual(claims["ambient.pin"], 4)
        self.assertEqual(claims["sos_button.pin"], 27)
        self.assertEqual(claims["motion.pin"], 17)
        self.assertEqual(claims["alarm_buzzer.pin"], 18)

    def test_负数引脚视为未使用(self) -> None:
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg["devices"]["tft"]["params"]["backlight_pin"] = -1
        claims = dict(validate.exclusive_pin_claims(cfg))
        self.assertNotIn("tft.backlight_pin", claims)
        cfg["devices"]["tft"]["params"]["backlight_pin"] = 19
        claims = dict(validate.exclusive_pin_claims(cfg))
        self.assertEqual(claims["tft.backlight_pin"], 19)

    def test_未启用的设备也参与检查(self) -> None:
        """★ 刻意**不**按 `enabled` 过滤（2026-09-26 决定）。

        未启用的设备将来会被启用，它的引脚声明**现在**就该参与撞脚检查 ——
        否则"红灯=24 与 TFT 的 DC=24"这种雷只有在两者都开的时候才爆，而那时已经是现场了。
        """
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg["devices"]["motion"]["enabled"] = False
        claims = dict(validate.exclusive_pin_claims(cfg))
        self.assertIn("motion.pin", claims, "未启用的设备也要参与撞脚检查")


class TestDefaultConfigHasNoConflict(unittest.TestCase):
    def test_默认配置没有撞脚(self) -> None:
        conflicts = find_conflicts(validate.exclusive_pin_claims(DEFAULT_CONFIG))
        self.assertEqual(conflicts, [], f"默认配置不该撞脚：{conflicts}")

    def test_把红灯改回24必须被抓到(self) -> None:
        """★ **反向验证**（注入式）：判据必须有区分力 —— 造出旧配置那个撞脚，检查器必须报。"""
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg["devices"]["status_led"]["params"]["pins"]["red"] = 24
        conflicts = find_conflicts(validate.exclusive_pin_claims(cfg))
        self.assertTrue(conflicts, "红灯与 TFT 的 DC 都用 GPIO24，必须被报成撞脚")
        self.assertIn("24", " ".join(conflicts))

    def test_检查项本身能跑通(self) -> None:
        ok, detail = validate.check_pin_conflicts()
        self.assertTrue(ok, detail)


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
