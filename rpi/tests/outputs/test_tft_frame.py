"""TFT「富帧」渲染测试（2026-10-01）。

背景：用户原话「**那个 TFT 屏幕…上面平时显示的内容好少**」——
老版只画两行文字，128×160 的屏中间约 100 px 全是黑的。
于是新增 `DisplayCommand.frame`（结构化富帧）与 `TftSpi._render_frame()`。

这里钉的不是"代码跑通了"，而是**画出来会不会出问题**：

1. **绝不画出屏外**（2026-09-29 真机踩过"字被截断/右半边没了"）；
2. **走对路径**：有帧走富帧、没帧仍走老的两行文字（向后兼容）；
3. **过期必须一眼看出**（主指标转灰 + `STALE`），与网页那条约定同一口径；
4. **趋势线缺口处断开**、**全平也要画**、**只用一次 SPI 突发**
   （逐像素会有上百次 SPI 事务，而"拖住主循环"正是 **E63** 的病灶）。
"""

from __future__ import annotations

import unittest
from typing import Any, Dict, List, Optional, Sequence, Tuple

from health_monitor.hal.models import DisplayCommand
from health_monitor.outputs.tft_spi import BLACK, CYAN, GRAY, RED, TftSpi, WHITE


class _RecordingTft(TftSpi):
    """把绘制调用录下来（不碰硬件），用来断言**可见后果**。"""

    def __init__(self, **kw: Any) -> None:
        super().__init__(controller="st7735", mock=True, **kw)
        self.texts: List[Tuple[int, int, str, Any, int]] = []
        self.windows: List[Tuple[int, int, int, int]] = []
        self.pushes: List[Tuple[Tuple[int, int, int, int], int, List[int]]] = []
        self.frames: List[Dict[str, Any]] = []
        self.plain: List[Tuple[str, str]] = []

    # --- 绘制原语：只记录，不画 ---
    def text(self, x: int, y: int, s: str, rgb: Any, bg: Any = None, scale: int = 1) -> int:
        self.texts.append((x, y, s, rgb, scale))
        return x + 8 * scale * len(s)

    def fill(self, rgb: Any) -> None:                        # noqa: D102
        self.texts.append((-1, -1, "<fill>", rgb, 0))

    def hline(self, x: int, y: int, w: int, rgb: Any) -> None:   # noqa: D102
        self.windows.append((x, y, x + w - 1, y))

    def set_window(self, x0: int, y0: int, x1: int, y1: int) -> None:   # noqa: D102
        self.windows.append((x0, y0, x1, y1))

    def push_pixels(self, pixels: Sequence[int]) -> None:    # noqa: D102
        window = self.windows[-1] if self.windows else (0, 0, 0, 0)
        self.pushes.append((window, len(pixels), list(pixels)))

    # --- 两条渲染路径：记录走了哪条 ---
    def _render(self, lines: Tuple[str, str], page: int) -> None:
        self.plain.append((lines[0], lines[1]))

    def _render_frame(self, frame: Dict[str, Any], page: int) -> None:
        self.frames.append(dict(frame))
        super()._render_frame(frame, page)

    # --- 方便：真正进渲染（mock 会短路 send，所以测试里临时关掉）---
    def open_for_draw(self) -> "_RecordingTft":
        self.open()
        self.mock = False
        return self

    def visible_texts(self) -> List[Tuple[int, int, str, Any, int]]:
        return [t for t in self.texts if t[0] >= 0]


FULL_FRAME: Dict[str, Any] = {
    "label": "ROOM",
    "value": "23.4",
    "unit": "C",
    "rows": ["HUM 57%", "MOTION IDLE"],
    "clock": "14:32",
    "trend": [23.0, 23.1, 23.4, 23.2, 23.6],
    "footer": "ALARMS 0  DATA 2s",
    "stale": False,
}


class TestFrameDispatch(unittest.TestCase):
    def test_有帧走富帧路径(self) -> None:
        tft = _RecordingTft().open_for_draw()
        tft.send(DisplayCommand(lines=("ROOM 23.4 C", "HUM 57%"), page=0, frame=FULL_FRAME))
        self.assertEqual(len(tft.frames), 1, "有 frame 就该走富帧渲染")
        self.assertEqual(tft.plain, [], "不该同时再走一遍两行文字")

    def test_没帧仍走老的两行文字路径(self) -> None:
        """★ 向后兼容：LCD 与老调用方只给 lines，不许因为新功能而改变行为。"""
        tft = _RecordingTft().open_for_draw()
        tft.send(DisplayCommand(lines=("ROOM 23.4 C", "HUM 57%"), page=0))
        self.assertEqual(tft.plain, [("ROOM 23.4 C", "HUM 57%")])
        self.assertEqual(tft.frames, [], "没有 frame 就不该走富帧")

    def test_空帧字典退回两行文字(self) -> None:
        """`frame={}` 是"没给"，不是"给了一份空的" —— 别把空帧当内容渲染。"""
        tft = _RecordingTft().open_for_draw()
        tft.send(DisplayCommand(lines=("A", "B"), page=1, frame={}))
        self.assertEqual(tft.plain, [("A", "B")])

    def test_帧不影响current_lines与page(self) -> None:
        """E57：两块屏的 `status()/read()` 必须能用**同名字段**读到"现在显示什么"。"""
        tft = _RecordingTft().open_for_draw()
        tft.send(DisplayCommand(lines=("ROOM 23.4 C", "HUM 57%"), page=2, frame=FULL_FRAME))
        status = tft.read()
        self.assertEqual(status.lines, ("ROOM 23.4 C", "HUM 57%"))
        self.assertEqual(status.page, 2)


class TestFrameLayout(unittest.TestCase):
    """★ 2026-09-29 真机踩过"字被画到屏幕外"，这里把那条不变量钉死。"""

    def test_所有文字都在屏内(self) -> None:
        tft = _RecordingTft().open_for_draw()
        tft.send(DisplayCommand(lines=("x", "y"), page=0, frame=FULL_FRAME))
        width, height = tft.width, tft.height
        for x, y, s, _rgb, scale in tft.visible_texts():
            self.assertGreaterEqual(x, 0, f"{s!r} 左越界")
            self.assertLessEqual(x + 8 * scale * len(s), width, f"{s!r} 右越界（字被切了）")
            self.assertGreaterEqual(y, 0, f"{s!r} 上越界")
            self.assertLessEqual(y + 8 * scale, height, f"{s!r} 下越界")

    def test_所有绘制窗口都在屏内(self) -> None:
        tft = _RecordingTft().open_for_draw()
        tft.send(DisplayCommand(lines=("x", "y"), page=0, frame=FULL_FRAME))
        for x0, y0, x1, y1 in tft.windows:
            self.assertTrue(0 <= x0 <= x1 < tft.width, f"窗口 x 越界 {(x0, x1)}")
            self.assertTrue(0 <= y0 <= y1 < tft.height, f"窗口 y 越界 {(y0, y1)}")

    def test_主指标装不下时会降字号而不是截断(self) -> None:
        tft = _RecordingTft().open_for_draw()
        tft.send(DisplayCommand(lines=("x", "y"), page=0,
                                frame={**FULL_FRAME, "value": "1234567890"}))
        big = [t for t in tft.visible_texts() if "1234" in t[2]]
        self.assertTrue(big, "长数字也要画出来")
        self.assertEqual(big[0][4], 1, "10 个字符在 128 px 屏上只能用 scale=1")

    def test_短指标用大字(self) -> None:
        tft = _RecordingTft().open_for_draw()
        tft.send(DisplayCommand(lines=("x", "y"), page=0, frame=FULL_FRAME))
        self.assertIn(3, [t[4] for t in tft.visible_texts()], "4 个字符够用 scale=3 大字")

    def test_中文在屏上降级为问号(self) -> None:
        """屏上只有 ASCII 字库；中文必须安全降级，**绝不送乱码字节**。"""
        tft = _RecordingTft().open_for_draw()
        tft.send(DisplayCommand(lines=("x", "y"), page=0,
                                frame={**FULL_FRAME, "label": "室温", "footer": "报警 0"}))
        # 这里只验证"渲染不崩且都把字符交给 text()"；真正的降级在 text() 里做（另有测试）
        self.assertTrue(any("室温" in t[2] for t in tft.visible_texts()))


class TestFrameStale(unittest.TestCase):
    """⚠️ 与网页同一条约定：**可以显示旧值，但必须一眼看出它旧**。"""

    def test_过期时主指标转灰并标STALE(self) -> None:
        tft = _RecordingTft().open_for_draw()
        tft.send(DisplayCommand(lines=("x", "y"), page=0, frame={**FULL_FRAME, "stale": True}))
        big = [t for t in tft.visible_texts() if t[2] == "23.4"]
        self.assertEqual(big[0][3], GRAY, "过期的主指标必须是灰的")
        self.assertTrue(any(t[2] == "STALE" for t in tft.visible_texts()), "必须显式标 STALE")
        self.assertTrue(any(t[2] == "STALE" and t[3] == RED for t in tft.visible_texts()),
                        "STALE 要用红色（灰底上唯一的醒目色）")

    def test_没过期时白字且不标STALE(self) -> None:
        tft = _RecordingTft().open_for_draw()
        tft.send(DisplayCommand(lines=("x", "y"), page=0, frame=FULL_FRAME))
        big = [t for t in tft.visible_texts() if t[2] == "23.4"]
        self.assertEqual(big[0][3], WHITE)
        self.assertFalse(any(t[2] == "STALE" for t in tft.visible_texts()))

    def test_过期时趋势线也转灰(self) -> None:
        tft = _RecordingTft().open_for_draw()
        tft.send(DisplayCommand(lines=("x", "y"), page=0, frame={**FULL_FRAME, "stale": True}))
        self.assertTrue(tft.pushes, "趋势线应当有像素推送")
        _win, _count, buf = tft.pushes[-1]
        gray565 = tft.color(GRAY)
        self.assertIn(gray565, buf, "过期的趋势线要用灰色画")


class TestFrameRobustness(unittest.TestCase):
    def test_空帧不崩(self) -> None:
        tft = _RecordingTft().open_for_draw()
        tft._render_frame({}, 0)

    def test_缺字段不崩(self) -> None:
        for frame in ({"value": "1"}, {"label": "L"}, {"rows": []}, {"trend": []},
                      {"rows": None, "trend": None, "label": None, "value": None}):
            with self.subTest(frame=frame):
                _RecordingTft().open_for_draw()._render_frame(frame, 0)

    def test_页脚为空时不画那一行(self) -> None:
        tft = _RecordingTft().open_for_draw()
        tft._render_frame({**FULL_FRAME, "footer": ""}, 0)
        self.assertFalse(any(t[2] == "ALARMS 0  DATA 2s" for t in tft.visible_texts()))

    def test_页码始终在屏内(self) -> None:
        tft = _RecordingTft().open_for_draw()
        tft._render_frame(FULL_FRAME, 2)
        page_labels = [t for t in tft.visible_texts() if t[4] == 1 and t[1] >= tft.height - 14]
        self.assertTrue(page_labels, "页码必须画出来")
        for x, _y, s, _rgb, scale in page_labels:
            self.assertLessEqual(x + 8 * scale * len(s), tft.width)


class TestTrend(unittest.TestCase):
    """★ 迷你趋势线：屏小，但"跨缺口连线 = 编造中间那段数据"这条与网页同样成立。"""

    def _draw(self, values: List[Optional[float]], w: int = 11, h: int = 9) -> List[int]:
        tft = _RecordingTft().open_for_draw()
        tft._draw_trend(0, 0, w, h, values, CYAN)
        _win, _count, buf = tft.pushes[-1]
        return buf

    def _columns_with_ink(self, buf: List[int], w: int, h: int, fg: int) -> List[int]:
        return [px for px in range(w) if any(buf[y * w + px] == fg for y in range(h))]

    def test_缺口处必须断开(self) -> None:
        tft = _RecordingTft().open_for_draw()
        fg = tft.color(CYAN)
        buf = self._draw([1.0, 2.0, None, 4.0, 5.0], w=5, h=9)
        cols = self._columns_with_ink(buf, 5, 9, fg)
        self.assertIn(1, cols)
        self.assertIn(3, cols)
        self.assertNotIn(2, cols, "缺口所在的列不许有任何像素（否则就是在跨缺口连线）")

    def test_全平的一条线也要画出来(self) -> None:
        """所有值相同 ⇒ 量程为 0 ⇒ 不能因为"除以 0"就不画（那正是"很稳定"的表达）。"""
        tft = _RecordingTft().open_for_draw()
        fg = tft.color(CYAN)
        buf = self._draw([25.0] * 6, w=6, h=9)
        self.assertTrue(any(v == fg for v in buf))

    def test_只用一次SPI突发(self) -> None:
        """★ E63 的教训：逐像素会有上百次 SPI 事务、拖住主循环。整块必须一次推完。"""
        tft = _RecordingTft().open_for_draw()
        before = len(tft.pushes)
        tft._draw_trend(0, 0, 120, 30, [20.0 + (i % 5) for i in range(120)], CYAN)
        self.assertEqual(len(tft.pushes) - before, 1, "趋势线必须一次 push_pixels 推完")
        _win, count, _buf = tft.pushes[-1]
        self.assertEqual(count, 120 * 30)

    def test_点数太少或没有数据就不画(self) -> None:
        tft = _RecordingTft().open_for_draw()
        before = len(tft.pushes)
        tft._draw_trend(0, 0, 10, 10, [1.0], CYAN)
        tft._draw_trend(0, 0, 10, 10, [None, None], CYAN)
        tft._draw_trend(0, 0, 0, 10, [1.0, 2.0], CYAN)
        self.assertEqual(len(tft.pushes), before, "没内容就别推像素")

    def test_只有一个有效点也要点一下(self) -> None:
        tft = _RecordingTft().open_for_draw()
        fg = tft.color(CYAN)
        buf = self._draw([None, 5.0, None], w=3, h=5)
        self.assertTrue(any(v == fg for v in buf), "孤点必须点出来，否则看着像没数据")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
