"""状态网页（``GET /``）测试。

为什么值得测：这个页面是**答辩现场与排查时第一眼看到的东西**，
但它最容易悄悄坏掉（模板字符串里少个花括号、数值为 None 时格式化抛异常），
而且坏了只在"人打开浏览器"时才被发现。所以把关键渲染规则钉进单测：

1. 页面能渲染出来，且是 HTML（不是 JSON）；
2. **缺失值显示"未知"，绝不显示 0**（与协议同一条硬要求）；
3. 报警态与非报警态有不同的横幅；
4. 有设备装配失败时要把失败原因显示出来（不能静默）；
5. 渲染函数对"什么都没有"的空快照也不能崩。
"""

from __future__ import annotations

import unittest

from health_monitor.core.config import AppConfig
from health_monitor.net.webui import ALARM_LABELS, render_page
from health_monitor.playback import PlaybackRuntime

DEMO = {
    "thresholds": {"no_motion_timeout_s": 60, "repeat_cooldown_s": 0},
    "devices": {
        "vitals": {"driver": "max30102", "read_interval_s": 1.0},
        "body_temp": {"driver": "tmp36", "read_interval_s": 1.0},
        "ambient": {"driver": "dht11", "read_interval_s": 3.0},
        "motion": {"driver": "hc_sr501", "read_interval_s": 0.5},
        "display": {"driver": "lcd1602", "read_interval_s": 1.0},
        "status_led": {"driver": "led", "read_interval_s": 1.0},
    },
}


class _Clock:
    def __init__(self, t: float = 1_790_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def make_runtime(clock: _Clock | None = None, **kwargs: object) -> PlaybackRuntime:
    """构造一个回放运行时（默认自带一个不前进的假时钟，按需传入可控时钟）。"""
    cfg = AppConfig.from_dict(DEMO)
    return PlaybackRuntime(
        cfg,
        clock=clock or _Clock(),
        sleep=lambda _s: None,
        verbose_outputs=False,
        **kwargs,  # type: ignore[arg-type]
    )


class TestStatusPage(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = _Clock()
        self.rt = make_runtime(clock=self.clock)
        self.rt.open()
        self.rt.set_vitals(heart_rate=72.0, spo2=98.0)
        self.rt.tick()

    def tearDown(self) -> None:
        self.rt.close()

    def _tick(self, seconds: float = 5.0) -> None:
        """推进假时钟后再采一帧。

        ⚠️ **必须推进时钟**：采集器按 `next_due_ts` 判到期，时钟不动时
        第二次 tick 不会重新读取设备，于是快照里还是上一轮的旧样本
        （实测踩到：设了 `finger=False` 页面却仍显示"已贴合"）。
        """
        self.clock.advance(seconds)
        self.rt.tick()

    def test_渲染出完整HTML(self) -> None:
        page = render_page(self.rt)
        self.assertTrue(page.startswith("<!DOCTYPE html>"))
        self.assertIn("</html>", page)
        self.assertIn("charset=\"utf-8\"", page)
        # 无外链依赖（树莓派可能没外网，答辩现场更不能白屏）
        self.assertNotIn("http://cdn", page)
        self.assertNotIn("https://cdn", page)
        self.assertNotIn("<script src", page)

    def test_六张指标卡都在(self) -> None:
        page = render_page(self.rt)
        for title in ("心率", "血氧", "体温（精密）", "室温 / 湿度", "活动状态", "数据年龄"):
            self.assertIn(title, page, f"缺少指标卡：{title}")

    def test_正常状态显示正常横幅(self) -> None:
        page = render_page(self.rt)
        self.assertIn("状态正常，监护中", page)
        self.assertNotIn("正在报警", page)

    def test_报警时横幅列出报警名(self) -> None:
        self.rt.set_vitals(heart_rate=135.0, spo2=97.0)
        self._tick()
        page = render_page(self.rt)
        self.assertIn("正在报警", page)
        self.assertIn(ALARM_LABELS["hr_too_high"], page)

    def test_缺失值显示未知而不是0(self) -> None:
        """与协议同一条硬要求：读不到就是"未知"，显示 0 会吓人。"""
        rt = make_runtime()
        rt.open()
        page = render_page(rt)
        self.assertIn("未知", page)
        # 心率/血氧都没数据时，卡片里不应出现 "0 bpm"
        self.assertNotIn(">0 bpm<", page)
        self.assertNotIn(">0 %<", page)
        rt.close()

    def test_未贴合手指时提示请将手指放好(self) -> None:
        self.rt.set_vitals(heart_rate=None, spo2=None, finger=False)
        self._tick()
        page = render_page(self.rt)
        self.assertIn("请将手指放好", page)

    def test_设备状态表列出每个设备(self) -> None:
        page = render_page(self.rt)
        for name in ("vitals", "body_temp", "ambient", "motion"):
            self.assertIn(name, page)
        self.assertIn("设备状态", page)
        self.assertIn("连续失败", page)

    def test_装配失败会显示在页面上(self) -> None:
        """有设备打不开时必须让人看见（否则用户以为一切正常）。"""
        self.rt.assembly_errors["fake_device"] = "DeviceInitError: 模拟失败"
        page = render_page(self.rt)
        self.assertIn("装配/打开失败", page)
        self.assertIn("fake_device", page)

    def test_没有报警时给出明确文案(self) -> None:
        page = render_page(self.rt)
        self.assertIn("本次运行还没有报警记录", page)
        self.assertIn("当前没有处于报警状态的项目", page)

    def test_报警码全部有中文名(self) -> None:
        """网页文案表要与枚举对齐（跨语言一致性另有 check_docs.py 兜底）。"""
        from health_monitor.hal.models import AlarmCode

        for code in AlarmCode:
            self.assertIn(code.value, ALARM_LABELS, f"网页缺报警码文案：{code.value}")

    def test_页面文本不含markdown标记(self) -> None:
        """界面文本里出现 markdown 会原样露出（项目排错记录里记过这一类）。"""
        page = render_page(self.rt)
        body = page.split("<body>", 1)[1]
        self.assertNotIn("**", body)


class TestStatusPageViaHttp(unittest.TestCase):
    """走真实 HTTP 请求验证（含 Content-Type 与路由）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.rt = make_runtime()
        cls.rt.open()
        cls.rt.set_vitals(heart_rate=80.0, spo2=97.0)
        cls.rt.tick()
        cls.server = cls.rt.start_http(host="127.0.0.1", port=0)
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.rt.close()

    def _raw(self, path: str) -> tuple:
        import urllib.request

        with urllib.request.urlopen(f"{self.base}{path}", timeout=5) as resp:  # noqa: S310
            return resp.status, resp.headers.get("Content-Type"), resp.read().decode("utf-8")

    def test_根路径返回HTML(self) -> None:
        status, ctype, body = self._raw("/")
        self.assertEqual(status, 200)
        self.assertEqual(ctype, "text/html; charset=utf-8")
        self.assertIn("<html", body)

    def test_status与index_html同页(self) -> None:
        for path in ("/index.html", "/status"):
            status, _, body = self._raw(path)
            self.assertEqual(status, 200, path)
            self.assertIn("心脏", body.replace("心率", "心脏"))

    def test_API仍然返回JSON(self) -> None:
        status, ctype, body = self._raw("/api/v1/current")
        self.assertEqual(status, 200)
        self.assertEqual(ctype, "application/json; charset=utf-8")
        self.assertTrue(body.startswith("{"))

    def test_未知路径仍是404(self) -> None:
        import urllib.error

        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._raw("/nope")
        self.assertEqual(ctx.exception.code, 404)

    def test_启用token时状态页不泄露数据(self) -> None:
        """带 token 时不渲染页面（也不给输入框——避免口令进浏览器历史）。"""
        rt = make_runtime()
        rt.open()
        server = rt.start_http(host="127.0.0.1", port=0, token="secret")
        port = server.server_address[1]
        import urllib.error
        import urllib.request

        try:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5)  # noqa: S310
            self.assertEqual(ctx.exception.code, 401)
            body = ctx.exception.read().decode("utf-8")
            self.assertIn("需要访问令牌", body)
            self.assertNotIn("心率", body)
        finally:
            rt.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
