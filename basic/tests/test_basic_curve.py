#!/usr/bin/env python3
"""画图部分的测试（**没有 matplotlib 时自动跳过**，不拖累主流程）。

这里刻意测一件容易复发、又只能靠"几何断言"发现的事：
**底部状态行不许压住 x 轴标签**（实测导出 PNG 时发现过文字重叠）。
判据是像素矩形不相交，不是"看着还行"。
"""

from __future__ import annotations

import unittest

from basic.series import Series


def _matplotlib_available() -> bool:
    try:
        import matplotlib  # noqa: F401
    except ImportError:
        return False
    return True


@unittest.skipUnless(_matplotlib_available(), "未安装 matplotlib")
class TestCurveWindow(unittest.TestCase):
    def setUp(self):
        import matplotlib

        matplotlib.use("Agg")                     # 无窗口后端：CI/无显示器也能跑
        import matplotlib.pyplot as plt

        from basic.plot import CJK_FONTS, CurveWindow

        # 与 run.py 用同一份中文字体优先级：否则中文会变成方框，
        # 文字宽度也完全不同（几何断言就失去意义）
        plt.rcParams["font.sans-serif"] = CJK_FONTS
        plt.rcParams["axes.unicode_minus"] = False
        self.window_cls = CurveWindow
        self.series = Series(window=5)
        for index in range(5):
            self.series.append(1000.0 + index * 2.5, 24.0 + index, 50.0 + index)

    def test_底部状态行不压住横轴标签(self):
        window = self.window_cls(self.series, use_index=False, source_text="单测", interval_s=2.5)
        window.refresh()
        window.fig.canvas.draw()
        xlabel = window.xlabel_bbox()
        footer = window.footer_bbox()
        self.assertFalse(
            footer.overlaps(xlabel),
            f"底部状态行与横轴标签重叠：footer y={footer.y0:.1f}~{footer.y1:.1f}，"
            f"xlabel y={xlabel.y0:.1f}~{xlabel.y1:.1f}",
        )

    def test_标题显示最新一次读数(self):
        window = self.window_cls(self.series, use_index=False, source_text="单测", interval_s=2.5)
        window.refresh()
        text = window.title.get_text()
        self.assertIn("最新一次读数", text)
        self.assertIn("28.0", text)               # 最后一个点是 24 + 4 = 28 ℃
        self.assertIn("54", text)                 # 湿度 50 + 4 = 54 %
        self.assertIn("温度:", text)

    def test_未知值显示未知而不是0(self):
        series = Series(window=3)
        series.append(1.0, 25.0, 58.0)
        series.append(2.0, None, None)            # 读失败
        window = self.window_cls(series, use_index=True, source_text="单测", interval_s=2.5)
        window.refresh()
        self.assertIn("未知", window.title.get_text())      # 最新一次是失败 → 未知
        footer = window.footer.get_text()
        self.assertIn("失败 1", footer)                      # 失败次数如实统计
        self.assertIn("25.0 ~ 25.0", footer)                 # 有效值范围不含那个 None

    def test_全程失败时状态行写未知(self):
        series = Series(window=3)
        series.append(1.0, None, None)
        series.append(2.0, None, None)
        window = self.window_cls(series, use_index=False, source_text="单测", interval_s=2.5)
        window.refresh()
        self.assertIn("未知", window.footer.get_text())
        self.assertIn("失败 2", window.footer.get_text())

    def test_可以导出PNG(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "curve.png"
            window = self.window_cls(self.series, use_index=False, source_text="单测",
                                     interval_s=2.5, save_path=str(target))
            window.refresh()
            window.save()
            self.assertTrue(target.exists())
            self.assertGreater(target.stat().st_size, 1000)


class TestFmt(unittest.TestCase):
    def test_缺失值显示未知(self):
        from basic.plot import fmt

        self.assertEqual(fmt(None), "未知")
        self.assertEqual(fmt(25.04), "25.0")


if __name__ == "__main__":
    unittest.main()
