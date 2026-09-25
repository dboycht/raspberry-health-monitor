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


class TestDescribeNotDoubleWrapped(unittest.TestCase):
    """`describe_pin()` 已经含 `GPIO{n}（…）`，调用方**不许再套一层括号**。

    为什么单独钉这条（2026-09-25 真机实测，ERROR.md E35）：`Dht11Reader.describe()`
    打印成了 `GPIO4（GPIO4（物理脚 7，…））` —— 出现在**用户真机上最常看的那行诊断输出**里，
    而所有测试都是绿的（没人断言"这行文本长什么样"）。判据 = 输出里不许出现
    `GPIO4（GPIO4` 这种**自嵌套**。
    """

    def test_驱动描述不自嵌套(self):
        from basic.dht11read import Dht11Reader

        # ⚠️ 用 mock=False 直接调用 describe()：mock=True 时它返回的是
        #    "模拟数据源（…）"那一行，**根本不含引脚描述** ⇒ 断言会失去意义（本轮踩到）。
        text = Dht11Reader(pin=4, mock=False).describe()
        self.assertIn("GPIO4", text, f"真机路径的描述应当含引脚号：{text}")
        self.assertNotIn("GPIO4（GPIO", text, f"引脚描述重复套括号：{text}")
        self.assertNotIn("GPIO4 = GPIO", text, f"引脚描述重复写了两遍：{text}")
        self.assertEqual(text.count("物理脚"), 1, f"物理脚号出现了不止一次：{text}")

    def test_诊断脚本的表头不自嵌套(self):
        from basic.tools import diag_dht_line

        source = (diag_dht_line.__file__,)
        text = open(source[0], encoding="utf-8").read()
        self.assertNotIn("GPIO{pin} = {describe_pin", text, "又在 describe_pin 外面套了 GPIO 前缀")
        self.assertNotIn("GPIO{args.pin} = {describe_pin", text, "又在 describe_pin 外面套了 GPIO 前缀")


if __name__ == "__main__":
    unittest.main()
