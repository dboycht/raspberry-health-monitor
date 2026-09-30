"""屏显控制（``POST /api/v1/screen``，2026-10-01）测试。

用户 2026-10-01 的要求：「用户可以通过点击使其 **LCD 弹出相关面板**」。
这一节钉四件事：

1. 四种合法组合**真的把命令发到了那一块屏**（用假设备读回来验证，不是"调用没抛异常"）；
2. 参数不合法 ⇒ **400**、那块屏不在 ⇒ **409** —— **都不假装成功**
   （假装成功会让面板显示一个没发生过的动作，用户以为按坏了）；
3. 手动指定之后**自动轮播不会马上把它抢回去**（LCD 的调试面板每 2 秒刷一次，
   没有这个保持窗口，用户点的"环境页"2 秒就没了）；
4. 走的是**已有的两条下发路径**（``show_page`` / ``show_debug``），没有另造一套协议 ——
   顺带验证"只发给某一块屏"这条纪律没被破坏（LCD 的动作不许碰到 TFT，反之亦然）。
"""

from __future__ import annotations

import json
import unittest

from health_monitor.core.config import AppConfig
from health_monitor.net.web import WebApi
from health_monitor.playback import PlaybackRuntime
from health_monitor.service import (
    MANUAL_SCREEN_HOLD_S,
    debug_lines,
    display_page_lines,
)

DEMO = {
    "thresholds": {"repeat_cooldown_s": 0},
    "devices": {
        "vitals": {"driver": "max30102", "read_interval_s": 1.0},
        "ambient": {"driver": "dht11", "read_interval_s": 2.0},
        "display": {"driver": "lcd1602", "read_interval_s": 1.0},
        "tft": {"driver": "tft_spi", "read_interval_s": 2.0,
                "params": {"controller": "st7735", "spi_device": 1}},
        "motion": {"driver": "hc_sr501", "read_interval_s": 0.5},
    },
}


class _Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = float(t)

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += float(seconds)


class _Base(unittest.TestCase):
    #: 子类改成 False ⇒ 造一个**没有 TFT** 的运行时（验 409）
    with_tft = True

    def setUp(self) -> None:
        devices = {n: dict(c) for n, c in DEMO["devices"].items()}
        if not self.with_tft:
            devices.pop("tft")
        self.clock = _Clock()
        self.rt = PlaybackRuntime(
            AppConfig.from_dict({"thresholds": dict(DEMO["thresholds"]), "devices": devices}),
            clock=self.clock,
            sleep=lambda _s: None,
            verbose_outputs=False,
        )
        self.rt.open()
        self.addCleanup(self.rt.close)
        self.rt.set_ambient(temperature_c=23.0, humidity_percent=50.0)
        self.rt.tick()

    # ---------- 读屏 ----------

    def _lcd(self) -> tuple:
        """LCD 的两行（**去掉右侧补的空格**）。

        ⚠️ 显示器件会把每行**右补空格到 16 字符**，而 `display_page_lines()` /
        `debug_lines()` 返回的是**未补齐**的原文 —— 直接 `assertEqual` 会因为
        尾随空格而红。这是实测踩到的，不是猜的。
        """
        return tuple(str(x).rstrip() for x in self.rt.devices["display"].read().lines)

    def _tft(self) -> tuple:
        return tuple(str(x).rstrip() for x in self.rt.devices["tft"].read().lines)

    def _tick(self, advance: float = 0.0) -> None:
        if advance:
            self.clock.advance(advance)
        self.rt.tick()

    # ---------- API ----------

    def _post(self, payload) -> tuple:
        api = WebApi(self.rt)
        body = json.dumps(payload).encode("utf-8") if not isinstance(payload, bytes) else payload
        return api.handle("POST", "/api/v1/screen", {}, {}, body)


class TestShowScreen(_Base):
    """``Runtime.show_screen()``：四条合法组合 + 两条拒绝路径。"""

    def test_四种组合都把命令发到了对的那块屏(self) -> None:
        snap = self.rt.collector.snapshot()
        cases = [
            ("lcd", "env", self._lcd, display_page_lines(0, snap)),
            ("lcd", "debug", self._lcd, None),          # 调试面板内容随帧数变，只查关键词
            ("tft", "env", self._tft, display_page_lines(0, snap)),
            ("tft", "alarm", self._tft, None),          # 报警页含报警条数，同样只查关键词
        ]
        for target, page, read, expected in cases:
            with self.subTest(target=target, page=page):
                self.assertTrue(self.rt.show_screen(target, page, self.clock()),
                                f"{target}/{page} 应当成功")
                lines = read()
                self.assertEqual(len(lines), 2, "两块屏都是两行")
                if expected is not None:
                    self.assertEqual(lines, expected,
                                     f"{target}/{page} 的内容与既有页文案不一致")
                else:
                    blob = " ".join(lines)
                    self.assertTrue(blob.strip(), "屏上不该是空的")

    def test_环境页内容就是既有的那份文案(self) -> None:
        """★ 复用 `display_page_lines`（同一份文案两块屏共用），**不是**另写一套。"""
        self.rt.show_screen("lcd", "env", self.clock())
        self.assertEqual(self._lcd(), display_page_lines(0, self.rt.collector.snapshot()))

    def test_调试面板内容就是既有的那份文案(self) -> None:
        self.rt.show_screen("lcd", "debug", self.clock())
        expected = debug_lines(
            self.rt.collector.snapshot(), ticks=self.rt.ticks, failures=0, alarm_count=0,
        )
        self.assertEqual(self._lcd(), expected)
        self.assertIn("T=", expected[0], "调试面板第一行应当是环境读数")

    def test_发LCD不许碰TFT(self) -> None:
        """★ 反向钉子：`show_page` 只发彩屏、`show_debug` 只发 LCD（E57 那条纪律）。

        屏显示控制的实现若图省事直接"广播给所有显示器件"，就会把另一块屏的
        **报警文案**冲掉 —— 这正是 E57 踩过的坑。
        """
        before_tft = self._tft()
        self.rt.show_screen("lcd", "debug", self.clock())
        self.assertEqual(self._tft(), before_tft, "给 LCD 发指令不该动 TFT")

    def test_发TFT不许碰LCD(self) -> None:
        before_lcd = self._lcd()
        self.rt.show_screen("tft", "alarm", self.clock())
        self.assertEqual(self._lcd(), before_lcd, "给 TFT 发指令不该动 LCD")

    def test_页名不合法(self) -> None:
        for target, page in (("lcd", "alarm"), ("tft", "debug"), ("lcd", ""), ("tft", "nope")):
            with self.subTest(target=target, page=page):
                self.assertFalse(self.rt.show_screen(target, page, self.clock()),
                                 f"{target}/{page} 是非法页名，必须拒绝")

    def test_target不合法(self) -> None:
        for target in ("", "screen", "LCD2", "lcd2x"):
            with self.subTest(target=target):
                self.assertFalse(self.rt.show_screen(target, "env", self.clock()))

    def test_页名与目标都要小写(self) -> None:
        """面板传的是小写；大写也接受（用户手打接口时不该被大小写绊住）。"""
        self.assertTrue(self.rt.show_screen("LCD", "ENV", self.clock()))

    def test_手动指定后自动轮播不会马上抢回去(self) -> None:
        """★★ 这是"点击看起来生效了没有"的关键。

        LCD 的调试面板**每 2 秒**刷一次。若只把 `_last_debug_ts` 清零而不设保持窗口，
        用户点的"LCD 显示环境页"**2 秒后就被顶掉** —— 现象就是"点了没用"。
        """
        self.rt.show_screen("lcd", "env", self.clock())
        self.assertTrue(self._lcd()[0].startswith("ROOM"), "前置条件：先切到环境页")

        # 推进到一个"调试面板本该刷两轮"的时间点。
        # ⚠️ 这里断言的是**页面形状**（还是环境页），不是具体数值 ——
        #    回放场景里的室温会随帧变化，拿"当前快照"去比对会因为数值漂移而假红。
        self._tick(advance=5.0)
        lines = self._lcd()
        self.assertTrue(lines[0].startswith("ROOM"),
                        f"保持窗口内 LCD 必须还停在环境页，实际是 {lines!r}")
        self.assertNotIn("T=", lines[0], "被调试面板抢回去了")

    def test_保持窗口过后调试面板会回来(self) -> None:
        """反向钉子：保持是**有期限**的，不是永久霸屏（否则 LCD 就不再是调试面板了）。"""
        self.rt.show_screen("lcd", "env", self.clock())
        self._tick(advance=MANUAL_SCREEN_HOLD_S + 1.0)
        self.assertIn("T=", self._lcd()[0], "保持窗口过后应当回到调试面板")

    def test_TFT手动指定后也从该页接着轮播(self) -> None:
        """手动跳到报警页（页 2）后，轮播要从 2 往下走，而不是跳回旧页。"""
        self.rt.show_screen("tft", "alarm", self.clock())
        self.assertEqual(self.rt._page, 2)


class TestScreenApi(_Base):
    """HTTP 层：状态码要能分辨"参数不对"与"那块屏不在"。"""

    def test_合法请求返回200(self) -> None:
        status, payload = self._post({"target": "lcd", "page": "env"})
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["target"], "lcd")
        self.assertEqual(payload["page"], "env")

    def test_页名非法返回400(self) -> None:
        status, payload = self._post({"target": "lcd", "page": "alarm"})
        self.assertEqual(status, 400, payload)
        self.assertFalse(payload["ok"])
        self.assertIn("page", payload["error"])

    def test_target非法返回400(self) -> None:
        status, payload = self._post({"target": "printer", "page": "env"})
        self.assertEqual(status, 400, payload)
        self.assertIn("target", payload["error"])

    def test_缺请求体返回400(self) -> None:
        status, payload = self._post(b"")
        self.assertEqual(status, 400, payload)

    def test_请求体不是JSON返回400(self) -> None:
        status, payload = self._post(b"{not json")
        self.assertEqual(status, 400, payload)
        self.assertIn("JSON", payload["error"])

    def test_请求体不是对象返回400(self) -> None:
        status, payload = self._post(b"[1,2,3]")
        self.assertEqual(status, 400, payload)

    def test_大小写不敏感(self) -> None:
        status, _ = self._post({"target": "LCD", "page": "Env"})
        self.assertEqual(status, 200)


class TestScreenApiWithoutTft(_Base):
    """那块屏不在系统里 ⇒ **409**（不是 400：参数没问题，是硬件不在）。"""

    with_tft = False

    def test_请求TFT返回409(self) -> None:
        status, payload = self._post({"target": "tft", "page": "env"})
        self.assertEqual(status, 409, payload)
        self.assertFalse(payload["ok"])
        self.assertIn("tft", payload["error"])

    def test_LCD仍然可用(self) -> None:
        status, _ = self._post({"target": "lcd", "page": "env"})
        self.assertEqual(status, 200)

    def test_屏不在时不许假装成功(self) -> None:
        """★ 关键：返回 409 的同时，**TFT 那条路径一次都不许被调用**。

        用假运行时记录 dispatcher 的调用，确认"没发生的事"没有被报成成功。
        """
        calls = []
        real = self.rt.dispatcher.show_page

        def spy(*args, **kwargs):
            calls.append(args)
            return real(*args, **kwargs)

        self.rt.dispatcher.show_page = spy  # type: ignore[method-assign]
        status, _ = self._post({"target": "tft", "page": "env"})
        self.assertEqual(status, 409)
        self.assertEqual(calls, [], "409 的时候不该真的去给 TFT 下发")


if __name__ == "__main__":
    unittest.main(verbosity=2)
