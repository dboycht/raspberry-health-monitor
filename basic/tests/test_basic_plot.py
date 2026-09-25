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
from basic.plot import CJK_FONTS, Runner, configure_cjk_font, pick_cjk_font, read_once
from basic.series import Series
from basic.store import CsvStore


class TestCjkFontSelection(unittest.TestCase):
    """中文字体要**问过系统**再选，不能写死一个名字（2026-09-25 真机实测）。

    真机踩到：树莓派上没装 fonts-noto-cjk，而原来的清单末尾挂着 **DejaVu Sans**
    ⇒ matplotlib 静默退到它 ⇒ 每帧刷一排 `Glyph … missing from font(s) DejaVu Sans`，
    导出的 PNG 里中文全变方框（终端日志却一切正常）。

    判据：
    1. 系统装了候选字体 ⇒ 选它（例如树莓派上的 `Droid Sans Fallback`）；
    2. 一个候选都没有 ⇒ 返回 None（调用方据此**提醒用户**装字体），而不是悄悄出方框图；
    3. 名字大小写/发行版命名差异要能匹配上（用"名字含 cjk/wqy/droid sans fallback"兜底）。
    """

    def test_装了候选字体就选它(self):
        self.assertEqual(pick_cjk_font(["DejaVu Sans", "Noto Sans CJK SC"]), "Noto Sans CJK SC")

    def test_一个候选都没有时返回None(self):
        self.assertIsNone(pick_cjk_font(["DejaVu Sans", "Arial"]))

    def test_名字大小写与别名兜底(self):
        # 精确匹配（归一化后）：`dejavu sans` 这种大小写差异要能对上候选清单
        self.assertEqual(pick_cjk_font(["dejavu sans", "wqy-zenhei"]), "wqy-zenhei",
                         "带连字符的系统名应当被识别为中文字体（返回系统里的真名）")
        self.assertEqual(pick_cjk_font(["DejaVu Sans", "noto sans cjk sc"]), "noto sans cjk sc")
        # 兜底：名字里带 droid sans fallback ⇒ 树莓派上实际可用的那个
        self.assertEqual(pick_cjk_font(["Droid Sans Fallback"]), "Droid Sans Fallback")

    def test_本机选择结果可用且会写进rcParams(self):
        """本机跑：选中的字体必须真的在系统字体名里（挑不到就返回 None，也不许抛异常）。"""
        import matplotlib

        matplotlib.use("Agg")
        chosen = configure_cjk_font()
        if chosen is not None:
            import matplotlib.font_manager as fm

            names = {n.lower() for n in fm.get_font_names()}
            self.assertIn(chosen.lower(), names, "配置的字体必须真的存在，否则等于没配")
        self.assertIn("axes.unicode_minus", matplotlib.rcParams)

    def test_字体链里拉丁字体必须排在中文字体前面(self):
        """★★ 2026-09-25 真机回归（ERROR.md E39）：**数字变方框**的坑。

        树莓派上可用的 `DroidSansFallbackFull.ttf` **只含 CJK 字形、没有拉丁数字**；
        我们一开始把 CJK 字体放在字体链**第一位** ⇒ 那张曲线图里所有数字/单位全变方框。
        正确顺序 = 拉丁字体在前、CJK 在后（matplotlib 会逐字回退）。
        """
        import matplotlib

        matplotlib.use("Agg")
        configure_cjk_font()
        chain = [str(name) for name in matplotlib.rcParams["font.sans-serif"]]
        self.assertTrue(chain, "字体链不能为空")
        self.assertEqual(chain[0].lower(), "dejavu sans",
                         f"第一位必须是含拉丁字形的通用字体（否则数字会变方框）：{chain[:3]}")
        chosen = pick_cjk_font()
        if chosen is not None:
            self.assertIn(chosen, chain, "选中的中文字体必须在链里（兜底用）")
            self.assertGreater(chain.index(chosen), 0, "中文字体不能排在第一位")

    def test_候选清单里不再挂没有中文字形的字体(self):
        """★ 回归：清单里**不许**出现 DejaVu Sans 这种"没有中文字形"的兜底项。

        它就是"静默退化成方框"的入口 —— 系统一个中文字体都没有时，
        正确的行为是返回 None（让调用方提醒用户），而不是假装配好了。
        """
        self.assertNotIn("DejaVu Sans", CJK_FONTS)


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
            # 1.0 秒时"还差"报的是 **扣掉容差之后**的等待时间，与门禁口径一致
            self.assertAlmostEqual(
                reader.wait_remaining(reader.last_read_at + 1.0),
                MIN_INTERVAL_S - 1.0 - reader.interval_tolerance(),
                places=6,
            )
            self.assertEqual(reader.wait_remaining(reader.last_read_at + 5.0), 0.0)
        finally:
            reader.close()

    def test_门槛是硬件下限而不是采样周期(self):
        """★★ 2026-09-25 真机回归（ERROR.md E38）：门禁按**硬件下限**，节奏归主循环。

        真机踩到的现象：`run.py` 默认 `--interval 3`，两次读取之间还夹着
        "读传感器 + 存 CSV + 刷曲线"（实测 60ms）⇒ 真实间隔 **2.94 秒**；
        而门禁被传成了采样周期 3.0 秒 ⇒ **隔一次被拒**（4 次采样成功 2 次失败 2 次）。

        判据（三条都要成立）：
        1. 默认 `Dht11Reader()` 的门槛就是硬件下限 2.0 秒（不再等于采样周期）；
        2. 2.9 秒（真机上真实出现的间隔）与 2.0 秒 ⇒ 放行；只等 1 秒 ⇒ 仍然拒绝；
        3. 容差按**间隔的 15%**；间隔比下限高出一个容差以上时容差为 0（不放宽门禁）。
        """
        from basic.dht11read import MIN_INTERVAL_S

        reader = Dht11Reader(mock=True, explicit_mock=(25.0, 58.0), sleep=lambda _s: None)
        self.assertEqual(reader.min_interval_s, MIN_INTERVAL_S, "默认门槛应当是硬件下限")
        self.assertAlmostEqual(reader.interval_tolerance(), MIN_INTERVAL_S * 0.15, places=6)
        # ⚠️ 用**固定基准时间**，不要用 last_read_at（它是真实时钟，边界断言会飘）
        base = 1000.0
        reader._last_read_at = base
        self.assertEqual(reader.wait_remaining(base + 2.9), 0.0, "2.9 秒（真机实测的间隔）必须放行")
        self.assertEqual(reader.wait_remaining(base + MIN_INTERVAL_S), 0.0, "刚好下限也该放行")
        self.assertGreater(reader.wait_remaining(base + 1.0), 0.0, "只等 1 秒必须仍然拒绝")
        reader.close()

        strict = Dht11Reader(mock=True, explicit_mock=(25.0, 58.0), min_interval_s=3.0,
                             sleep=lambda _s: None)
        self.assertEqual(strict.interval_tolerance(), 0.0, "间隔离下限够远时不该再放宽")
        strict.close()


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
