#!/usr/bin/env python3
"""读取器与采集主循环的测试（**全部不碰硬件**）。

判据集中在三件事上：
1. 间隔不足会**明确拒绝**（DHT11 读太快会返回陈旧数据）；
2. 失败时**不编造数值**，连续失败会停下来（避免对着坏接线刷屏）；
3. 采集主循环把每次采样都**如实落盘**（包括失败那几次）。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from basic.dht11read import Dht11Error, Dht11Reader, MIN_INTERVAL_S, ReadResult
from basic.plot import Runner, read_once
from basic.series import Series
from basic.store import CsvStore


class FakeBackend:
    """假后端：按预设顺序返回结果，用来构造"读出坏数据/抛异常"的确定场景。"""

    name = "fake"

    def __init__(self, results):
        self._results = list(results)
        self.calls = 0

    def read_once(self):
        self.calls += 1
        value = self._results.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def close(self):
        return None


class TestReaderInterval(unittest.TestCase):
    def test_间隔下限被拒绝(self):
        with self.assertRaises(ValueError) as ctx:
            Dht11Reader(min_interval_s=0.5)
        self.assertIn("DHT11", str(ctx.exception))

    def test_非法引脚被拒绝(self):
        with self.assertRaises(ValueError):
            Dht11Reader(pin=99)

    def test_首次读取无需等待_读过后要等满间隔(self):
        reader = Dht11Reader(mock=True, explicit_mock=(25.0, 58.0), sleep=lambda _s: None)
        reader.open()
        try:
            self.assertEqual(reader.wait_remaining(1000.0), 0.0)
            self.assertTrue(reader.read().ok)
            self.assertAlmostEqual(
                reader.wait_remaining(reader.last_read_at + 1.0), MIN_INTERVAL_S - 1.0, places=6
            )
            self.assertEqual(reader.wait_remaining(reader.last_read_at + 5.0), 0.0)
        finally:
            reader.close()


class TestReaderMock(unittest.TestCase):
    def test_mock读取返回指定值(self):
        reader = Dht11Reader(mock=True, explicit_mock=(26.0, 45.0), sleep=lambda _s: None)
        result = reader.read()
        self.assertTrue(result.ok)
        self.assertEqual((result.temperature_c, result.humidity_percent), (26.0, 45.0))
        self.assertEqual(reader.backend_name, "mock")
        self.assertIn("模拟", reader.describe())

    def test_读失败不编造数值且计数(self):
        reader = Dht11Reader(mock=True, retries=1, sleep=lambda _s: None)
        reader._backend = FakeBackend([Dht11Error("注入的时序失败")])
        result = reader.read()
        self.assertFalse(result.ok)
        self.assertIsNone(result.temperature_c)
        self.assertIsNone(result.humidity_percent)
        self.assertIn("时序失败", result.note)
        self.assertEqual(reader.consecutive_failures, 1)

    def test_超量程读数被判为失败(self):
        reader = Dht11Reader(mock=True, retries=1, sleep=lambda _s: None)
        reader._backend = FakeBackend([(60.0, 58.0)])
        result = reader.read()
        self.assertFalse(result.ok)
        self.assertIn("量程", result.note)

    def test_失败后不会刷新上次读取时间(self):
        """失败要能立刻重试，不被 2 秒间隔拖住（成功才需要限速）。"""
        reader = Dht11Reader(mock=True, retries=1, sleep=lambda _s: None)
        reader._backend = FakeBackend([Dht11Error("x"), (25.0, 58.0)])
        self.assertFalse(reader.read().ok)
        self.assertIsNone(reader._last_read_at)
        self.assertTrue(reader.read().ok)
        self.assertIsNotNone(reader._last_read_at)

    def test_重试次数够就能读成功(self):
        reader = Dht11Reader(mock=True, retries=3, sleep=lambda _s: None)
        reader._backend = FakeBackend([Dht11Error("第一次抖动"), (25.0, 58.0)])
        result = reader.read()
        self.assertTrue(result.ok)
        self.assertEqual(reader.consecutive_failures, 0)

    def test_间隔不足时拒绝读取并说明还差几秒(self):
        """DHT11 读太快会返回陈旧数据 —— 所以"太早"必须是失败，而不是拿旧值冒充新值。"""
        from basic.tools.selfcheck import StepClock

        clock = StepClock()
        reader = Dht11Reader(mock=True, explicit_mock=(25.0, 58.0), clock=clock, sleep=lambda _s: None)
        self.assertTrue(reader.read().ok)
        too_soon = reader.read()
        self.assertFalse(too_soon.ok)
        self.assertIn("不足", too_soon.note)
        self.assertIn("还差", too_soon.note)
        clock.value += 3.0                                  # 时间够了 → 可以再读
        self.assertTrue(reader.read().ok)


class TestReadOnce(unittest.TestCase):
    def test_成功记录带序号与时间(self):
        reader = Dht11Reader(mock=True, explicit_mock=(25.0, 58.0), sleep=lambda _s: None)
        reading = read_once(reader, index=7, now=1_700_000_000.0)
        self.assertTrue(reading.ok)
        self.assertEqual(reading.index, 7)
        self.assertEqual(reading.ts, 1_700_000_000.0)
        self.assertEqual(reading.summary(), "温度: 25.0 ℃，湿度: 58 %")

    def test_失败记录保留原因(self):
        reader = Dht11Reader(mock=True, retries=1, sleep=lambda _s: None)
        reader._backend = FakeBackend([Dht11Error("无应答")])
        reading = read_once(reader, index=8, now=1_700_000_000.0)
        self.assertFalse(reading.ok)
        self.assertIsNone(reading.temperature_c)
        self.assertIn("无应答", reading.note)


class TestRunner(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = CsvStore(Path(self._tmp.name) / "run.csv")
        self.sleeps = []
        self.reader = Dht11Reader(mock=True, retries=1, sleep=lambda _s: None)
        self.series = Series(window=10)

    def tearDown(self):
        self._tmp.cleanup()

    def test_每次采样都落盘(self):
        self.reader._backend = FakeBackend([(25.0, 58.0), Dht11Error("抖动"), (26.0, 57.0)])
        runner = Runner(self.reader, self.store, self.series, interval_s=3.0, sleep=self.sleeps.append)
        for _ in range(3):
            runner.tick()
            self.reader.last_read_at = None      # 模拟"主循环已经等了 3 秒"
        rows = self.store.load()
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0][1], 25.0)
        self.assertIsNone(rows[1][1])                   # 失败那一次是 None，不是 0
        self.assertEqual(rows[2][1], 26.0)
        self.assertEqual(self.series.stats()["failed"], 1)

    def test_连续失败到阈值就停下(self):
        self.reader._backend = FakeBackend([Dht11Error("无应答")] * 6)
        runner = Runner(self.reader, self.store, self.series, max_failures=2, sleep=self.sleeps.append)
        runner.tick()
        self.assertFalse(runner.should_stop)
        runner.tick()
        self.assertTrue(runner.should_stop)
        self.assertIn("连续", runner.stopped_reason)
        self.assertIn("上拉电阻", runner.stopped_reason)   # 排查提示必须给出来

    def test_可以关掉CSV只显示(self):
        runner = Runner(self.reader, None, self.series, sleep=self.sleeps.append)
        self.reader._backend = FakeBackend([(25.0, 58.0)])
        runner.tick()
        self.assertEqual(self.series.stats()["samples"], 1)
        self.assertIsNone(runner.store)

    def test_运行小结字段完整(self):
        self.reader._backend = FakeBackend([(25.0, 58.0), (26.0, 57.0)])
        runner = Runner(self.reader, self.store, self.series, sleep=self.sleeps.append)
        runner.tick()
        self.reader.last_read_at = None          # 模拟"主循环已经等了 3 秒"
        runner.tick()
        summary = runner.summary(started_at=1_700_000_000.0)
        for key in ("samples", "ok", "failed", "csv", "last_reading", "source", "backend"):
            self.assertIn(key, summary)
        self.assertEqual(summary["samples"], 2)
        self.assertEqual(summary["temperature_min_c"], 25.0)
        self.assertEqual(summary["temperature_max_c"], 26.0)

    def test_睡到下一轮会扣掉读取耗时(self):
        runner = Runner(self.reader, None, self.series, interval_s=3.0, sleep=self.sleeps.append)
        runner.sleep_until_next(started_at=__import__("time").monotonic())
        self.assertEqual(len(self.sleeps), 1)
        self.assertLessEqual(self.sleeps[0], 3.0)


if __name__ == "__main__":
    unittest.main()
