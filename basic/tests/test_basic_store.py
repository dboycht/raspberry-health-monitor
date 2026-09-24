#!/usr/bin/env python3
"""CSV 存档测试：**读不到就是空，绝不写 0**。

本项目最核心的一条设计红线（主项目三层业务与基础版一致）：
传感器读不到时字段必须是 ``None`` / 空，而不是 0 —— 0 ℃ 会被当成"结冰"，0 % 会被
当成"极其干燥"，都是凭空编出来的数据。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from basic.model import CSV_HEADER, Reading
from basic.store import CsvStore, DEMO_FILE_NAME, default_csv_path, default_data_dir, load_rows


class TestReading(unittest.TestCase):
    def test_成功记录的行内容(self):
        reading = Reading(1, 1_700_000_000.5, 25.0, 58.0, ok=True)
        row = reading.to_csv_row()
        self.assertEqual(len(row), len(CSV_HEADER))
        self.assertEqual(row[0], "1")
        self.assertEqual(row[2], "25.0")
        self.assertEqual(row[3], "58.0")
        self.assertEqual(row[4], "ok")
        self.assertIn("2023-", row[1])                  # 本地时间字符串
        self.assertTrue(row[1].endswith(".500"))        # 毫秒保留三位

    def test_失败记录不写0而是空字段(self):
        reading = Reading(2, 1_700_000_000.0, None, None, ok=False, note="校验和不符")
        row = reading.to_csv_row()
        self.assertEqual(row[2], "")
        self.assertEqual(row[3], "")
        self.assertEqual(row[4], "fail")
        self.assertEqual(row[5], "校验和不符")

    def test_摘要文案与任务书示例一致(self):
        reading = Reading(1, 1_700_000_000.0, 25.0, 58.0)
        self.assertEqual(reading.summary(), "温度: 25.0 ℃，湿度: 58 %")
        self.assertIn("没读到数据", Reading(2, 1_700_000_000.0, None, None, ok=False,
                                        note="无应答").summary())


class TestCsvStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "dht11_test.csv"
        self.store = CsvStore(self.path)

    def tearDown(self):
        self._tmp.cleanup()

    def test_首行写表头且逐行落盘(self):
        self.store.save(Reading(1, 1_700_000_000.0, 25.0, 58.0))
        self.store.save(Reading(2, 1_700_000_002.5, 25.4, 57.5))
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0].split(","), list(CSV_HEADER))
        self.assertEqual(len(lines), 3)                 # 表头 + 两行数据
        self.assertEqual(self.store.rows_written, 2)

    def test_再次打开同一个文件不重复写表头(self):
        self.store.save(Reading(1, 1_700_000_000.0, 25.0, 58.0))
        CsvStore(self.path).save(Reading(2, 1_700_000_002.5, 25.1, 58.1))
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0].split(","), list(CSV_HEADER))
        self.assertEqual(len(lines), 3)

    def test_读回时缺失值为None而不是0(self):
        self.store.save(Reading(1, 1_700_000_000.0, 25.0, 58.0))
        self.store.save(Reading(2, 1_700_000_002.0, None, None, ok=False, note="无应答"))
        rows = self.store.load()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][1], 25.0)
        self.assertIsNone(rows[1][1])
        self.assertIsNone(rows[1][2])

    def test_读回按时间升序且可限量(self):
        for offset in (5.0, 1.0, 3.0):                  # 故意乱序写入
            self.store.save(Reading(1, 1_700_000_000.0 + offset, 25.0, 58.0))
        rows = self.store.load()
        self.assertEqual([r[0] for r in rows], sorted(r[0] for r in rows))
        self.assertEqual(len(self.store.load(limit=2)), 2)

    def test_文件不存在时读回空列表(self):
        self.assertEqual(CsvStore(Path(self._tmp.name) / "nope.csv").load(), [])

    def test_坏行被跳过而不是崩溃(self):
        self.path.write_text(
            ",".join(CSV_HEADER) + "\n"
            "1,不是时间,25.0,58.0,ok,\n"
            "2,2026-09-24 20:15:03.000,26.0,57.0,ok,\n",
            encoding="utf-8",
        )
        rows = self.store.load()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], 26.0)

    def test_非追加模式会清空旧文件(self):
        self.store.save(Reading(1, 1_700_000_000.0, 25.0, 58.0))
        CsvStore(self.path, append=False).save(Reading(1, 1_700_000_010.0, 26.0, 57.0))
        self.assertEqual(len(self.store.load()), 1)

    def test_运行小结写成JSON(self):
        target = self.store.save_summary({"samples": 3, "ok": 3})
        self.assertTrue(target.exists())
        self.assertIn('"samples": 3', target.read_text(encoding="utf-8"))

    def test_默认文件名带日期与时刻(self):
        path = default_csv_path(now=1_700_000_000.0)
        self.assertEqual(path.parent, default_data_dir())
        self.assertRegex(path.name, r"^dht11_\d{8}_\d{6}\.csv$")


class TestReadOnlyLoad(unittest.TestCase):
    """离线回放必须**只读**：读完文件还得在（这条踩过：回放把演示数据删了）。"""

    def test_load_rows不删也不改文件(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "demo.csv"
            store = CsvStore(path)
            store.save(Reading(1, 1_700_000_000.0, 25.0, 58.0))
            before = path.read_text(encoding="utf-8")
            rows = load_rows(path)
            self.assertEqual(len(rows), 1)
            self.assertTrue(path.exists(), "load_rows 竟然把数据文件删掉了")
            self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_反复回放不会越读越少(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "demo.csv"
            CsvStore(path).save(Reading(1, 1_700_000_000.0, 25.0, 58.0))
            for _ in range(3):
                self.assertEqual(len(load_rows(path)), 1)

    def test_文件不存在返回空且不抛异常(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_rows(Path(tmp) / "nope.csv"), [])
            self.assertEqual(CsvStore(Path(tmp) / "nope2.csv").load(), [])


class TestDemoSample(unittest.TestCase):
    """演示样本（入库文件）必须真的能用：格式正确、数值在量程内。"""

    def test_样本可读且是连续变化的数据(self):
        path = default_data_dir() / DEMO_FILE_NAME
        self.assertTrue(path.exists(), f"缺少演示数据 {path}（没硬件的同学靠它跑通画图）")
        rows = CsvStore(path).load()
        self.assertGreaterEqual(len(rows), 20)
        temperatures = [r[1] for r in rows if r[1] is not None]
        humidities = [r[2] for r in rows if r[2] is not None]
        self.assertGreaterEqual(len(temperatures), 20)
        self.assertGreaterEqual(len(humidities), 20)
        self.assertTrue(all(0.0 <= t <= 50.0 for t in temperatures))
        self.assertTrue(all(20.0 <= h <= 90.0 for h in humidities))
        self.assertGreater(len(set(temperatures)), 3, "样本不该是一条直线")


if __name__ == "__main__":
    unittest.main()
