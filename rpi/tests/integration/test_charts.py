"""图表模块（``health_monitor.net.charts``）测试。

这个模块的每一条断言，对应的都是**图表会说谎的一种方式**：

1. 跨缺口连线 ⇒ 视觉上"这段时间一直有数据"（等于用旧值冒充当前状态）；
2. 没有数据却画一条平线 ⇒ 看着像"一直很稳定"；
3. 不画阈值 ⇒ 只能在报警之后才从历史里反推"当时离越界有多近"；
4. 过期不变灰 ⇒ 三个月前的旧值看起来和此刻一样新。

所以测试钉的是**这些会撒谎的地方**，而不是"SVG 里有没有那个标签"。
"""

from __future__ import annotations

import unittest

from health_monitor.net.charts import line_chart, sparkline


class TestNoData(unittest.TestCase):
    def test_空数据不画假线(self) -> None:
        svg = line_chart([])
        self.assertIn("暂无数据", svg)
        self.assertNotIn("<polyline", svg, "没数据就**不许**画线（假的平线会被读成'一直很稳'）")

    def test_全是缺口也不画假线(self) -> None:
        svg = line_chart([(1.0, None), (2.0, None)])
        self.assertIn("暂无数据", svg)
        self.assertNotIn("<polyline", svg)

    def test_占位文案可定制(self) -> None:
        self.assertIn("未测出", line_chart([], empty_note="未测出"))


class TestGaps(unittest.TestCase):
    """★ 本模块最重要的一条：**缺口必须断线**。"""

    def test_缺口断线_同样的首尾点画成两条线(self) -> None:
        # 中间 (3.0, None) 是缺口
        gapped = line_chart([(1.0, 10), (2.0, 12), (3.0, None), (4.0, 30), (5.0, 32)])
        joined = line_chart([(1.0, 10), (2.0, 12), (3.0, 21), (4.0, 30), (5.0, 32)])

        self.assertEqual(gapped.count("<polyline"), 2, "缺口两侧必须各画一条")
        self.assertEqual(joined.count("<polyline"), 1, "没有缺口就该是一条")

    def test_缺口两侧都只有一个点时不连线(self) -> None:
        svg = line_chart([(1.0, 10), (2.0, None), (3.0, 30)])
        self.assertNotIn("<polyline", svg, "单点之间**绝不许**连一条线")
        self.assertEqual(svg.count("<circle"), 2, "两个孤点各画一个圆点")

    def test_开头结尾的缺口不影响中间的线(self) -> None:
        svg = line_chart([(0.0, None), (1.0, 10), (2.0, 12), (3.0, 14), (4.0, None)])
        self.assertEqual(svg.count("<polyline"), 1)

    def test_单点画圆不画线(self) -> None:
        svg = line_chart([(1.0, 5)])
        self.assertEqual(svg.count("<polyline"), 0)
        self.assertIn("<circle", svg)


class TestThresholds(unittest.TestCase):
    def test_范围内画阈值虚线(self) -> None:
        svg = line_chart(
            [(1.0, 20), (2.0, 25), (3.0, 22)],
            thresholds=[(30.0, "室温上限 30")],
            y_min=0, y_max=40,
        )
        self.assertIn("stroke-dasharray", svg, "阈值要画成虚线")
        self.assertIn("室温上限 30", svg)

    def test_范围外的阈值标在边缘而不是丢掉(self) -> None:
        """★ 量程外的阈值**不许消失**（2026-10-01 真实数据验出来的问题）。

        实测湿度在 57.9~59.1% 跳、而报警阈值 80% ⇒ 阈值在量程外。
        若直接不画，用户**看不出一离报警还有多远**；而自动缩放又会把 DHT11 那 1%
        的正常台阶放大得像剧烈波动 —— 两件事凑一起就是误导。
        """
        svg = line_chart([(1.0, 20), (2.0, 25)], thresholds=[(99.0, "上限 99")], y_min=0, y_max=40)
        self.assertIn("上限 99", svg, "量程外的阈值必须有交代，不能悄悄消失")
        self.assertIn("↑", svg, "高于量程要标成向上箭头")
        self.assertNotIn("stroke-dasharray", svg, "但它不该画在绘图区里（那会拉伸量程）")

    def test_低于量程的阈值标向下箭头(self) -> None:
        svg = line_chart([(1.0, 20), (2.0, 25)], thresholds=[(5.0, "下限 5")], y_min=15, y_max=40)
        self.assertIn("↓", svg)
        self.assertIn("下限 5", svg)

    def test_可以同时画上限与下限(self) -> None:
        svg = line_chart(
            [(1.0, 20), (2.0, 25)],
            thresholds=[(30.0, "上限"), (10.0, "下限")],
            y_min=0, y_max=40,
        )
        self.assertEqual(svg.count("stroke-dasharray"), 2)


class TestStale(unittest.TestCase):
    """⚠️ 与网页那条约定配套：**可以显示旧值，但必须一眼看出它旧**。"""

    def test_过期变灰并标注(self) -> None:
        svg = line_chart(
            [(1000.0, 23.0), (1010.0, 23.5)],
            now=1010.0 + 600.0,          # 最后一点之后过了 10 分钟
            stale_after_s=60.0,
        )
        self.assertIn("数据已过期", svg)
        self.assertIn("10 分钟前", svg)
        self.assertIn("#9aa0a6", svg, "过期要用灰线（默认蓝线 #1a73e8）")
        self.assertNotIn("#1a73e8", svg)

    def test_没过期不加过期标注(self) -> None:
        svg = line_chart([(1000.0, 23.0), (1010.0, 23.5)], now=1015.0, stale_after_s=60.0)
        self.assertNotIn("数据已过期", svg)
        self.assertIn("#1a73e8", svg)

    def test_不传now就不判过期(self) -> None:
        svg = line_chart([(1000.0, 23.0)], stale_after_s=1.0)
        self.assertNotIn("数据已过期", svg)

    def test_刚过期用秒而不是分钟(self) -> None:
        svg = line_chart([(1000.0, 23.0)], now=1030.0, stale_after_s=10.0)
        self.assertIn("30 秒前", svg)


class TestRobustness(unittest.TestCase):
    def test_全平的一条线不崩(self) -> None:
        """所有值相同 ⇒ Y 范围是 0 ⇒ 不能除以 0。"""
        svg = line_chart([(1.0, 25.0), (2.0, 25.0), (3.0, 25.0)])
        self.assertIn("<svg", svg)
        self.assertIn("<polyline", svg)

    def test_时间为同一个点也不崩(self) -> None:
        svg = line_chart([(5.0, 10.0), (5.0, 20.0)])
        self.assertIn("<svg", svg)

    def test_负数与小数都能显示(self) -> None:
        svg = line_chart([(1.0, -3.5), (2.0, -1.25)], unit="°C")
        self.assertIn("°C", svg)

    def test_文本被转义_不留注入口(self) -> None:
        svg = line_chart([(1.0, 5.0)], unit='<script>x</script>')
        self.assertNotIn("<script", svg)
        self.assertIn("&lt;script&gt;", svg)

    def test_自适应_有viewBox且宽度撑满(self) -> None:
        """双端适配靠这两条：viewBox 让它可缩放，width:100% 让它撑满容器。"""
        svg = line_chart([(1.0, 5.0), (2.0, 6.0)])
        self.assertIn("viewBox=", svg)
        self.assertIn("width:100%", svg)


class TestTimeGaps(unittest.TestCase):
    """★ 时间上有洞也必须断线（2026-10-01 用**板子上的真实历史**验出来的）。

    历史库**只存成功读数**：设备停摆/连续读失败期间**一行都没有**，
    所以那些点根本不会以 ``None`` 的形态出现。
    只按 ``None`` 断线 ⇒ 一次 10 分钟停摆会被画成一条直线 ——
    正是"缺口断线"这条纪律要防的那个谎。
    真实数据：300 行环境温度**全是有效值、一个 None 都没有**，
    但采样间隔 3 秒、其中若干处达二十几秒（E63 那次播报卡停主循环的窗口）。
    """

    def test_正常等间隔不会被误断(self) -> None:
        pts = [(i * 3.0, 20.0 + i * 0.1) for i in range(10)]
        self.assertEqual(line_chart(pts).count("<polyline"), 1, "等间隔不许被误判成缺口")

    def test_时间空档超过三倍中位间隔就断线(self) -> None:
        # 前 6 个点每 3 秒一个，然后**空掉 10 分钟**，再来 6 个
        pts = [(i * 3.0, 20.0) for i in range(6)]
        base = 5 * 3.0 + 600.0
        pts += [(base + i * 3.0, 21.0) for i in range(6)]
        self.assertEqual(line_chart(pts).count("<polyline"), 2,
                         "十分钟的空档必须断开，不能画成一条直线")

    def test_可以显式指定空档上限(self) -> None:
        pts = [(0.0, 1.0), (1.0, 2.0), (5.0, 3.0), (6.0, 4.0)]
        self.assertEqual(line_chart(pts, max_gap_s=2.0).count("<polyline"), 2)
        self.assertEqual(line_chart(pts, max_gap_s=10.0).count("<polyline"), 1)

    def test_传0可以关掉时间断线(self) -> None:
        pts = [(0.0, 1.0), (1.0, 2.0), (10000.0, 3.0), (10001.0, 4.0)]
        self.assertEqual(line_chart(pts, max_gap_s=0).count("<polyline"), 1)

    def test_点太少时不做时间推断(self) -> None:
        """只有两三个点时推不出"正常间隔"，不该乱断。"""
        self.assertEqual(line_chart([(0.0, 1.0), (9999.0, 2.0)]).count("<polyline"), 1)

    def test_时间断线与None断线可以叠加(self) -> None:
        pts = [(0.0, 1.0), (1.0, 2.0), (2.0, None), (3.0, 3.0), (4.0, 4.0),
               (5000.0, 5.0), (5001.0, 6.0)]
        svg = line_chart(pts)
        # 段：(0,1)-(1,2) / (3,3)-(4,4) / (5000..)-(5001..)  ⇒ 3 条
        self.assertEqual(svg.count("<polyline"), 3)


class TestSparkline(unittest.TestCase):
    def test_迷你线也会断口(self) -> None:
        svg = sparkline([1, 2, None, 4, 5])
        self.assertEqual(svg.count("<polyline"), 2)

    def test_没有数据返回空串(self) -> None:
        self.assertEqual(sparkline([None, None]), "")
        self.assertEqual(sparkline([]), "")

    def test_单点不画线(self) -> None:
        self.assertEqual(sparkline([5]), "")

    def test_正常序列是一条线(self) -> None:
        self.assertEqual(sparkline([1, 2, 3]).count("<polyline"), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
