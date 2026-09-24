#!/usr/bin/env python3
"""引脚映射的测试：**钉死"只查表、不许写偏移公式"**。

为什么基础版也要测这个：报错信息里的物理脚号是接线时唯一看得懂的东西，
一旦报错说"物理脚 28"（而 28 其实是 HAT ID_SC），同学就会插错线。
"""

from __future__ import annotations

import unittest

from basic.pins import (
    BCM_TO_PHYSICAL,
    bcm_to_physical,
    describe_pin,
    find_conflicts,
    full_table,
    physical_to_bcm,
)


class TestPinMapping(unittest.TestCase):
    def test_GPIO4是物理脚7(self):
        """DHT11 默认数据脚：GPIO4 = 物理脚 7（基础版最常用的一条）。"""
        self.assertEqual(bcm_to_physical(4), 7)
        self.assertIn("物理脚 7", describe_pin(4))

    def test_物理脚号不是偏移公式(self):
        """GPIO27 → 物理脚 13（**不是 28**）：BCM 与物理脚号之间没有固定偏移。"""
        self.assertEqual(bcm_to_physical(27), 13)
        self.assertEqual(bcm_to_physical(17), 11)
        # 若有人写 pin + 1 的"公式"，27 会得到 28 —— 这里把这条钉死
        self.assertNotEqual(bcm_to_physical(27), 28)

    def test_非法引脚如实说明(self):
        self.assertIsNone(bcm_to_physical(99))
        self.assertIn("不是树莓派", describe_pin(99))

    def test_反向查表往返一致(self):
        for bcm, phys in BCM_TO_PHYSICAL.items():
            self.assertEqual(physical_to_bcm(phys), bcm)

    def test_物理脚号唯一且都在1到40(self):
        physicals = list(BCM_TO_PHYSICAL.values())
        self.assertEqual(len(physicals), len(set(physicals)), "同一个物理脚被映射给了两个 BCM 编号")
        self.assertTrue(all(1 <= p <= 40 for p in physicals))

    def test_full_table按物理脚排序(self):
        table = full_table()
        self.assertEqual([p for p, _ in table], sorted(p for p, _ in table))

    def test_撞脚检查(self):
        self.assertEqual(find_conflicts([("dht11", 4), ("pir", 17)]), [])
        conflicts = find_conflicts([("dht11", 4), ("buzzer", 4)])
        self.assertEqual(len(conflicts), 1)
        self.assertIn("GPIO4", conflicts[0])


if __name__ == "__main__":
    unittest.main()
