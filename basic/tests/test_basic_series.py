#!/usr/bin/env python3
"""曲线数据窗口测试（滚动窗口 / 横轴 / 统计量），**不依赖 matplotlib**。"""

from __future__ import annotations

import unittest

from basic.series import Series


class TestSeries(unittest.TestCase):
    def test_滚动窗口只保留最近N个点(self):
        series = Series(window=3)
        for index in range(1, 6):
            series.append(1000.0 + index, 20.0 + index, 50.0 + index)
        self.assertEqual(len(series.ts), 3)
        self.assertEqual(list(series.indices), [3, 4, 5])
        self.assertEqual(series.total, 5)                # 累计次数不受窗口影响

    def test_窗口至少保留一个点(self):
        series = Series(window=0)
        series.append(1.0, 25.0, 58.0)
        self.assertEqual(len(series.ts), 1)

    def test_横轴用相对时间(self):
        series = Series()
        for offset in (10.0, 12.5, 15.0):
            series.append(1000.0 + offset, 25.0, 58.0)
        self.assertEqual(series.x_axis(use_index=False), [0.0, 2.5, 5.0])

    def test_横轴用采样序号(self):
        series = Series()
        for _ in range(3):
            series.append(1000.0, 25.0, 58.0)
        self.assertEqual(series.x_axis(use_index=True), [1, 2, 3])

    def test_空窗口的横轴与最新值(self):
        series = Series()
        self.assertEqual(series.x_axis(False), [])
        self.assertIsNone(series.latest)

    def test_缺失值保持None不被当成0(self):
        series = Series()
        series.append(1000.0, 25.0, 58.0)
        series.append(1003.0, None, None)                # 读失败
        series.append(1006.0, 26.0, 57.0)
        self.assertEqual(list(series.temps), [25.0, None, 26.0])
        self.assertEqual(series.latest, (26.0, 57.0))

    def test_统计量跳过None(self):
        series = Series()
        series.append(1000.0, 25.0, 58.0)
        series.append(1003.0, None, None)
        series.append(1006.0, 27.0, 56.0)
        stats = series.stats()
        self.assertEqual(stats["samples"], 3)
        self.assertEqual(stats["ok"], 2)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual((stats["temp_min"], stats["temp_max"]), (25.0, 27.0))
        self.assertEqual((stats["humidity_min"], stats["humidity_max"]), (56.0, 58.0))

    def test_全是失败时统计量为None(self):
        series = Series()
        for index in range(2):
            series.append(1000.0 + index, None, None)
        stats = series.stats()
        self.assertIsNone(stats["temp_min"])
        self.assertIsNone(stats["humidity_min"])
        self.assertEqual(stats["ok"], 0)

    def test_可以指定采样序号(self):
        series = Series()
        series.append(1000.0, 25.0, 58.0, index=42)
        self.assertEqual(series.indices[-1], 42)


if __name__ == "__main__":
    unittest.main()
