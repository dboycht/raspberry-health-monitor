"""引脚映射与"撞脚检测"测试。

为什么值得单独写测试：**引脚冲突是真实发生过的高危缺陷**——
`hc_sr501`（人体红外）与 `button`（求救键）曾同时写 GPIO17，
照驱动文档接线的同学会让 PIR 一检测到人就"顺带"触发紧急求助。
这类问题代码不会报错、单测也不会红，只能靠"配置级的交叉检查"抓。
"""

from __future__ import annotations

import unittest

from health_monitor.core.config import DEFAULT_CONFIG
from health_monitor.hal import (
    BCM_TO_PHYSICAL,
    bcm_to_physical,
    describe_pin,
    find_conflicts,
)


class TestPinMapping(unittest.TestCase):
    def test_已知引脚的物理脚号(self) -> None:
        """这些映射是照着树莓派 40-pin 图逐个核对的，改动会被这里拦住。"""
        self.assertEqual(bcm_to_physical(2), 3)
        self.assertEqual(bcm_to_physical(3), 5)
        self.assertEqual(bcm_to_physical(4), 7)
        self.assertEqual(bcm_to_physical(17), 11)
        self.assertEqual(bcm_to_physical(27), 13)
        self.assertEqual(bcm_to_physical(22), 15)
        self.assertEqual(bcm_to_physical(18), 12)
        self.assertEqual(bcm_to_physical(8), 24)

    def test_物理脚28不是GPIO27(self) -> None:
        """**回归测试**：曾用 `pin + 1` 公式，把 GPIO27 报成"物理脚 28"。

        物理脚 28 是 HAT ID_SC（保留），照错的文案插线会插到保留脚上。
        """
        self.assertEqual(bcm_to_physical(27), 13)
        self.assertNotEqual(bcm_to_physical(27), 28)
        self.assertNotIn(28, BCM_TO_PHYSICAL.values(), "物理脚 28 是 HAT ID_SC，不该出现在可用映射里")

    def test_每个映射都不能用偏移公式解释(self) -> None:
        """钉住"BCM 与物理脚不是偏移关系"这一事实，防止有人又图省事写公式。"""
        offsets = {phys - bcm for bcm, phys in BCM_TO_PHYSICAL.items()}
        self.assertGreater(len(offsets), 1, "物理脚 - BCM 不是常数：绝不能写偏移公式")

    def test_未知引脚返回None而不是瞎猜(self) -> None:
        self.assertIsNone(bcm_to_physical(99))
        self.assertIn("不是树莓派", describe_pin(99))

    def test_描述文案含物理脚号(self) -> None:
        text = describe_pin(27)
        self.assertIn("GPIO27", text)
        self.assertIn("物理脚 13", text)


class TestConflictDetection(unittest.TestCase):
    def test_无冲突时返回空列表(self) -> None:
        self.assertEqual(find_conflicts([("motion", 17), ("sos_button", 27)]), [])

    def test_两设备抢同一引脚必须被报出来(self) -> None:
        conflicts = find_conflicts([("motion", 17), ("sos_button", 17)])
        self.assertEqual(len(conflicts), 1)
        self.assertIn("GPIO17", conflicts[0])
        self.assertIn("motion", conflicts[0])
        self.assertIn("sos_button", conflicts[0])

    def test_同一引脚三设备也报一条(self) -> None:
        conflicts = find_conflicts([("a", 5), ("b", 5), ("c", 5)])
        self.assertEqual(len(conflicts), 1)
        self.assertIn("3 个设备", conflicts[0])


class TestProjectConfigPins(unittest.TestCase):
    """★ 对**项目真实配置**做交叉检查：这是抓"撞脚"的关键测试。"""

    #: 哪些驱动的参数表示"独占型 GPIO"（不能共用）
    EXCLUSIVE_PINS = {
        "dht11": ("pin",),
        "hc_sr501": ("pin",),
        "hc_sr04": ("trig_pin", "echo_pin"),
        "buzzer": ("pin",),
        "button": ("pin",),
    }

    def _claims(self) -> list:
        claims = []
        for name, item in DEFAULT_CONFIG["devices"].items():
            driver = item["driver"]
            params = item.get("params", {})
            for key in self.EXCLUSIVE_PINS.get(driver, ()):
                if key in params:
                    claims.append((f"{name}({driver}.{key})", int(params[key])))
            # LED 是多路：pins 是个字典
            if driver == "led":
                for color, pin in (params.get("pins") or {}).items():
                    claims.append((f"{name}(led.{color})", int(pin)))
        return claims

    def test_默认配置里没有引脚冲突(self) -> None:
        conflicts = find_conflicts(self._claims())
        self.assertEqual(conflicts, [], "默认配置存在 GPIO 撞脚：\n" + "\n".join(conflicts))

    def test_被禁用的设备不参与冲突判定(self) -> None:
        """HC-SR04 默认关闭且其默认引脚与 PIR 重叠——关闭时不应算冲突。"""
        enabled_claims = []
        for name, item in DEFAULT_CONFIG["devices"].items():
            if not item.get("enabled", True):
                continue
            driver = item["driver"]
            params = item.get("params", {})
            for key in self.EXCLUSIVE_PINS.get(driver, ()):
                if key in params:
                    enabled_claims.append((f"{name}({driver}.{key})", int(params[key])))
        self.assertEqual(find_conflicts(enabled_claims), [])

    def test_每个独占引脚都是合法的树莓派引脚(self) -> None:
        for name, pin in self._claims():
            with self.subTest(claim=name):
                self.assertIsNotNone(bcm_to_physical(pin), f"{name} 用了非法 GPIO{pin}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
