"""温湿度动态曲线脚本的**纯逻辑**测试（课程任务 H）。

GUI 窗口本身没法自动断言（要靠人眼），但脚本里真正容易写错的是这些纯逻辑：
1. **滚动窗口**：超过窗口长度要丢最旧的，且四个队列必须**同步丢**（长度不一致会在绘图时报错）；
2. **x 轴取值**：任务书要求"标出采样顺序或时间"，两种都要能算对；
3. **双指标合并**：温度和湿度分开取回后按时间对齐，**缺失的一方是 None，不能补 0**
   （与本项目"缺失绝不当成正常"一致）；
4. **统计量**：忽略 None（否则 min/max 会崩）。

注意：本文件**不 import matplotlib**（那是可选依赖），只测纯逻辑。
"""

from __future__ import annotations

import unittest
from typing import Any, Dict, List, Optional, Tuple

import live_plot
from live_plot import ApiSource, Series


class TestSeries(unittest.TestCase):
    def test_滚动窗口丢最旧(self) -> None:
        s = Series(window=3)
        for i in range(5):
            s.append(1000.0 + i, 20.0 + i, 50.0 + i)
        self.assertEqual(len(s.times), 3)
        self.assertEqual(len(s.temps), 3)
        self.assertEqual(len(s.humids), 3)
        self.assertEqual(len(s.indices), 3)
        self.assertEqual(s.total, 5, "累计样本数要如实累计（窗口只是显示范围）")
        self.assertEqual(list(s.indices), [2, 3, 4], "保留的应当是最后 3 个采样序号")
        self.assertEqual(s.latest, (24.0, 54.0))

    def test_四个队列长度始终一致(self) -> None:
        """★ 长度不一致会在绘图赋值时报错，必须同步丢。"""
        s = Series(window=2)
        for i in range(10):
            s.append(float(i), float(i), None if i % 2 else float(i))
        lengths = {len(s.times), len(s.temps), len(s.humids), len(s.indices)}
        self.assertEqual(len(lengths), 1, f"队列长度不一致：{lengths}")

    def test_x轴序号模式(self) -> None:
        s = Series(window=5)
        for i in range(3):
            s.append(1000.0 + i * 2, 20.0, 50.0)
        self.assertEqual(s.x_axis(use_index=True), [0, 1, 2])

    def test_x轴时间模式从0开始(self) -> None:
        s = Series(window=5)
        for i in range(3):
            s.append(1000.0 + i * 2, 20.0, 50.0)
        self.assertEqual(s.x_axis(use_index=False), [0.0, 2.0, 4.0])

    def test_空序列的x轴为空(self) -> None:
        self.assertEqual(Series().x_axis(True), [])
        self.assertEqual(Series().x_axis(False), [])

    def test_统计量忽略None(self) -> None:
        """读数缺失（None）时 min/max 不能崩，也不能把 None 当 0。"""
        s = Series(window=10)
        s.append(1.0, 25.0, 50.0)
        s.append(2.0, None, None)      # 这次没读到
        s.append(3.0, 27.0, 55.0)
        stats = s.stats()
        self.assertEqual(stats["samples"], 3)
        self.assertEqual(stats["temp_min"], 25.0)
        self.assertEqual(stats["temp_max"], 27.0)
        self.assertEqual(stats["humidity_min"], 50.0)
        self.assertEqual(stats["humidity_max"], 55.0)

    def test_全是None时统计量为None(self) -> None:
        s = Series(window=5)
        s.append(1.0, None, None)
        stats = s.stats()
        self.assertIsNone(stats["temp_min"])
        self.assertIsNone(stats["humidity_max"])

    def test_历史预填充只保留窗口内(self) -> None:
        s = Series(window=3)
        rows = [(1000.0 + i, 20.0 + i, 50.0 + i) for i in range(10)]
        s.load_history(rows)
        self.assertEqual(len(s.times), 3)
        self.assertEqual(s.temps[-1], 29.0)


class FakeApiSource(ApiSource):
    """假的 ApiSource：把 HTTP 换成内存数据（**单测不发真实请求**）。"""

    def __init__(self, temp_rows: List[Dict[str, Any]], humid_rows: List[Dict[str, Any]]) -> None:
        super().__init__("http://127.0.0.1:0")
        self._temp_rows = temp_rows
        self._humid_rows = humid_rows
        self.describe = "假数据源"

    def _get(self, path: str) -> dict:  # type: ignore[override]
        if "ambient_temp_c" in path:
            return {"ok": True, "data": self._temp_rows}
        if "humidity_percent" in path:
            return {"ok": True, "data": self._humid_rows}
        return {"ok": True, "data": {"ambient_temp_c": 26.0, "humidity_percent": 58.0}}


class TestApiSourceMerging(unittest.TestCase):
    """温度与湿度是两次请求取回的，必须按时间对齐。"""

    def test_同时间戳合并成一行(self) -> None:
        src = FakeApiSource(
            temp_rows=[{"ts": 1000.0, "value": 25.0}, {"ts": 1010.0, "value": 26.0}],
            humid_rows=[{"ts": 1000.0, "value": 55.0}, {"ts": 1010.0, "value": 56.0}],
        )
        merged = src.history(limit=10)
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0], (1000.0, 25.0, 55.0))
        self.assertEqual(merged[1], (1010.0, 26.0, 56.0))

    def test_只有温度时湿度是None而不是0(self) -> None:
        """★ 与本项目红线一致：缺失必须是 None，不能补 0。"""
        src = FakeApiSource(
            temp_rows=[{"ts": 1000.0, "value": 25.0}],
            humid_rows=[],
        )
        merged = src.history(limit=10)
        self.assertEqual(merged, [(1000.0, 25.0, None)])

    def test_只有湿度时温度是None(self) -> None:
        src = FakeApiSource(temp_rows=[], humid_rows=[{"ts": 2000.0, "value": 60.0}])
        self.assertEqual(src.history(limit=10), [(2000.0, None, 60.0)])

    def test_按时间升序返回(self) -> None:
        src = FakeApiSource(
            temp_rows=[{"ts": 3000.0, "value": 30.0}, {"ts": 1000.0, "value": 10.0}],
            humid_rows=[{"ts": 2000.0, "value": 20.0}],
        )
        times = [row[0] for row in src.history(limit=10)]
        self.assertEqual(times, sorted(times))

    def test_read取当前值(self) -> None:
        src = FakeApiSource(temp_rows=[], humid_rows=[])
        self.assertEqual(src.read(), (26.0, 58.0))


class TestArgumentGuards(unittest.TestCase):
    def test_模块可导入且暴露关键接口(self) -> None:
        for name in ("Series", "ApiSource", "DeviceSource", "run_plot", "main"):
            self.assertTrue(hasattr(live_plot, name), f"live_plot 缺少 {name}")

    def test_轮询间隔下限保护在main里(self) -> None:
        """DHT11 硬件限制 ≥2 秒：main() 里必须自动抬高间隔（这里断言常量的存在与含义）。"""
        import inspect

        source = inspect.getsource(live_plot.main)
        self.assertIn("args.interval < 2.0", source)
        self.assertIn("args.interval = 2.0", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
