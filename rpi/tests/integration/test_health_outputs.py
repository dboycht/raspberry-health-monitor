"""`/api/v1/health` 的 `outputs` 段 + `allow_reuse_address()` 的平台语义（2026-10-01 用户拍板）。

为什么单独一组
--------------
1. **`outputs` 段**：起因是一次验收的尴尬 —— `/api/v1/health` 的 `devices` 只有**输入器件**，
   `/api/v1/devices` 只有接线说明 ⇒ "报警到底上屏了没、灯是什么颜色"在接口上**看不到**，
   只能靠调度器的下发流水**间接**证明。加它就是为了让这类结论能**直接取证**。
2. **`allow_reuse_address()`**：同一个 `SO_REUSEADDR` 在 Windows 与 Linux 上是**两件事** ——
   Windows 上它破坏"防双开"（ERROR.md E7），Linux 上它只是放开 `TIME_WAIT`
   （E77 的"停服务后等 60 秒"）。这条守卫钉住"**按平台**"，防止谁一刀切回 False 或 True。
"""

from __future__ import annotations

import unittest
from unittest import mock

from health_monitor.demo import DEMO_CONFIG
from health_monitor.core.config import AppConfig
from health_monitor.net import web as web_module
from health_monitor.net.web import WebApi, allow_reuse_address


class TestAllowReuseAddressByPlatform(unittest.TestCase):
    """`SO_REUSEADDR` 的取值必须**按平台**，不是一刀切。"""

    def test_windows上禁止(self) -> None:
        """Windows 上 `SO_REUSEADDR` 允许两个**活着的**监听者绑同一端口 ⇒ 必须禁止。

        （这是 ERROR.md **E7** 的原场景：两个服务抢请求，手机端时而连到旧实例。）
        """
        with mock.patch.object(web_module.os, "name", "nt"):
            self.assertFalse(allow_reuse_address(), "Windows 上必须禁止地址复用")

    def test_linux上允许(self) -> None:
        """Linux 上 `SO_REUSEADDR` 只放开 `TIME_WAIT`，**不允许**两个活着的监听者
        ⇒ 允许它，同时去掉"停服务后约 60 秒"的等待（E77）。"""
        with mock.patch.object(web_module.os, "name", "posix"):
            self.assertTrue(allow_reuse_address(), "Linux 上应当允许地址复用")

    def test_平台判断真的被读了(self) -> None:
        """★ 注入自证：如果实现是写死的常量，上面两条不可能同时成立。

        这条是防"守卫永远绿"的：把平台改名两次，取值必须跟着变。
        """
        values = []
        for name in ("nt", "posix", "nt"):
            with mock.patch.object(web_module.os, "name", name):
                values.append(allow_reuse_address())
        self.assertEqual(values, [False, True, False], "取值没有跟着平台变 ⇒ 多半写死了")

    def test_探针与服务器同口径(self) -> None:
        """★ E77 的直接教训：探针与 `make_server` **口径不一致**时，
        会把 `TIME_WAIT` 造成的占用误读成"服务起不来"。

        判据：两边都走**同一个** `allow_reuse_address()`，而不是各写一份常量。
        """
        import inspect

        probe_src = inspect.getsource(web_module.probe_port)
        server_src = inspect.getsource(web_module.make_server)
        self.assertIn("allow_reuse_address()", probe_src, "probe_port 必须调用同一个口径函数")
        self.assertIn("allow_reuse_address()", server_src, "make_server 必须调用同一个口径函数")


class TestHealthOutputsSection(unittest.TestCase):
    """`/api/v1/health` 必须能直接回答"输出器件现在是什么状态"。"""

    def _api(self) -> WebApi:
        from health_monitor.playback import PlaybackRuntime

        rt = PlaybackRuntime(AppConfig.from_dict(DEMO_CONFIG), sleep=lambda _s: None,
                             verbose_outputs=False)
        rt.open()
        self.addCleanup(rt.close)
        self.rt = rt
        return WebApi(rt)

    def test_health里有outputs段且含灯与屏(self) -> None:
        api = self._api()
        status, payload = api.handle("GET", "/api/v1/health", {}, {})
        self.assertEqual(status, 200)
        outputs = payload.get("outputs")
        self.assertIsInstance(outputs, dict, "health 必须带 outputs 段")
        self.assertIn("status_led", outputs, "LED 的实时状态应当看得到")
        self.assertIn("current_color", outputs["status_led"], "LED 要暴露当前颜色")
        self.assertIn("display", outputs, "LCD 的实时状态应当看得到")
        self.assertIn("lines", outputs["display"], "LCD 要暴露屏上两行字")

    def test_报警后能从outputs直接取证(self) -> None:
        """★ 这才是加它的理由：报一次警，然后**只看接口**就能说清
        "灯变黄了、屏上是 ROOM TEMP HIGH 那两行"。"""
        api = self._api()
        from health_monitor.hal.models import AlarmCode, AlarmEvent, Severity

        event = AlarmEvent(ts=self.rt.clock(), code=AlarmCode.AMBIENT_TEMP_HIGH,
                           severity=Severity.NOTICE, message="室温偏高", source="dht11")
        self.rt.dispatcher.dispatch(event, self.rt.clock())
        _, payload = api.handle("GET", "/api/v1/health", {}, {})
        outputs = payload["outputs"]
        self.assertNotEqual(outputs["status_led"]["current_color"], "green",
                            "报警后灯不该还是绿的")
        self.assertIn("ROOM TEMP HIGH", " ".join(outputs["display"]["lines"]),
                      "看接口就该知道报警上屏了")

    def test_单个器件status抛异常不会打崩健康接口(self) -> None:
        """健康接口是运维/手机端第一入口 —— **它必须永远能答**。"""
        api = self._api()
        broken = mock.Mock()
        broken.status.side_effect = RuntimeError("驱动炸了")
        broken.KIND = None
        self.rt.dispatcher.outputs["broken_output"] = broken
        status, payload = api.handle("GET", "/api/v1/health", {}, {})
        self.assertEqual(status, 200)
        self.assertIn("error", payload["outputs"]["broken_output"])

    def test_只读_不调用read碰硬件(self) -> None:
        """`outputs` 只许读 `status()`，**绝不调 `read()`** ——
        LCD/MAX30102 没有排他锁（E76），健康接口不该产生副作用。"""
        api = self._api()
        spy = mock.Mock(wraps=self.rt.dispatcher.outputs["display"])
        self.rt.dispatcher.outputs["display"] = spy
        api.handle("GET", "/api/v1/health", {}, {})
        self.assertTrue(spy.status.called, "应当调用 status()")
        self.assertFalse(spy.read.called, "绝不该调用 read()")

    def test_只列真的装配上的输出器件(self) -> None:
        """按 `dispatcher.outputs` 取，不按配置的 `enabled` —— 装配失败的器件
        不该在接口里假装存在（与 `_screen_present` 同一口径）。"""
        api = self._api()
        _, payload = api.handle("GET", "/api/v1/health", {}, {})
        self.assertEqual(sorted(payload["outputs"]), sorted(self.rt.dispatcher.outputs))

    def test_模拟输出器件也满足同形契约(self) -> None:
        """★ 回放/演示器件（ConsoleLcd/ConsoleLed）也必须带"当前状态"。

        真实驱动侧早有"每个 DISPLAY 驱动都必须带当前屏幕内容"的对称守卫
        （见 `test_tft_spi.py`），但**模拟器件**当时漏了 ⇒ 演示模式与面板测试里
        `outputs` 段会是空的。这条把同一个契约钉在模拟器件上。
        """
        from health_monitor.playback import ConsoleLed, ConsoleLcd

        lcd = ConsoleLcd(); lcd.open()
        led = ConsoleLed(); led.open()
        self.assertIn("lines", lcd.status(), "模拟 LCD 必须带 lines")
        self.assertIn("page", lcd.status(), "模拟 LCD 必须带 page")
        self.assertIn("current_color", led.status(), "模拟 LED 必须带 current_color")
        self.assertIn("blinking", led.status(), "模拟 LED 必须带 blinking")


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
