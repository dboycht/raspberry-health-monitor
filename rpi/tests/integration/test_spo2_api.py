"""「测血氧」HTTP 接口测试：`GET /api/v1/spo2` + `POST …/measure` + `POST …/decline`。

为什么单独一组
--------------
2026-10-01 用户 review 指出：T14 原来把 `CLICK` 与 `LONG_PRESS` **都当成"同意"**，
于是**物理按键上不存在"拒绝"**，老人只能干等超时、而两块屏一直被占着。
修法是"有类型的请求 + 否决"，并且**面板/手机端也走同一条路径** —— 那就必须钉住：

1. **HTTP 与物理按键行为一致**（不是"两套代码碰巧看起来一样"）；
2. **不在叫人阶段时 `decline` 必须 409**（不能"假装成功"，否则面板会显示没发生过的动作）；
3. **功能关掉时两个 POST 都 409**、`GET` 里 `enabled=false`；
4. **面板那一节不能整页刷新**（整页刷新会抹掉用户没保存的阈值输入）。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

_RPI_DIR = Path(__file__).resolve().parents[2]
if str(_RPI_DIR) not in sys.path:
    sys.path.insert(0, str(_RPI_DIR))

from health_monitor.core.config import AppConfig  # noqa: E402
from health_monitor.core.configstore import ConfigStore  # noqa: E402
from health_monitor.hal.models import ButtonAction  # noqa: E402
from health_monitor.net import webui  # noqa: E402
from health_monitor.net.web import WebApi  # noqa: E402
from health_monitor.playback import PlaybackRuntime  # noqa: E402
from health_monitor.service import (  # noqa: E402
    SPO2_FAIL_LINES,
    SPO2_MEASURE_LINES,
    SPO2_PROMPT_LINES,
)

REMIND_S = 120.0
TIMEOUT_S = 30.0
MEASURE_S = 10.0

DEMO = {
    "thresholds": {
        "no_motion_timeout_s": 60,
        "repeat_cooldown_s": 0,
        "spo2_remind_interval_s": REMIND_S,
        "spo2_remind_timeout_s": TIMEOUT_S,
        "spo2_measure_s": MEASURE_S,
    },
    "devices": {
        "vitals": {"driver": "max30102", "read_interval_s": 1.0},
        "display": {"driver": "lcd1602", "read_interval_s": 1.0},
        "tft": {"driver": "tft_spi", "read_interval_s": 2.0,
                "params": {"controller": "st7735", "spi_device": 1}},
        "alarm_buzzer": {"driver": "buzzer", "read_interval_s": 1.0},
        "spo2_button": {"driver": "button", "read_interval_s": 0.2},
    },
    "mqtt": {"enabled": False, "host": "keep-me"},
}


class _Clock:
    """假时钟。

    ⚠️ 血氧这几条接口**必须**用 Runtime 的时钟（`WebApi._now()`）——
    若它们用 `time.time()`，本文件的倒计时断言在假时钟下会全错
    （这正是"截止时刻必须与 tick 共用一个时间源"那条注释的由来）。
    """

    def __init__(self, t: float = 1_790_000_000.0) -> None:
        self.t = float(t)

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += float(seconds)


class _ApiCase(unittest.TestCase):
    #: 子类改成 False ⇒ 关掉「测血氧」功能（验证总开关）
    spo2_enabled = True

    def setUp(self) -> None:
        devices = {name: dict(cfg) for name, cfg in DEMO["devices"].items()}
        if not self.spo2_enabled:
            devices["spo2_button"] = dict(devices["spo2_button"], enabled=False)
        config = {"thresholds": dict(DEMO["thresholds"]), "devices": devices}

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "devices.json"
        self.path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")

        self.clock = _Clock()
        self.rt = PlaybackRuntime(
            AppConfig.from_dict(config),
            clock=self.clock,
            sleep=lambda _s: None,
            verbose_outputs=False,
            config_path=str(self.path),
        )
        # use_local=False：断言不能随"这台机器上有没有 devices.local.json"而变（E56 那类坑）
        self.rt.config_store = ConfigStore(self.path, use_local=False, clock=self.clock)
        self.rt.open()
        self.addCleanup(self.rt.close)
        self.api = WebApi(self.rt)

    # ---------- 便捷调用 ----------

    def get(self):
        return self.api.handle("GET", "/api/v1/spo2", {}, {})

    def measure(self):
        return self.api.handle("POST", "/api/v1/spo2/measure", {}, {})

    def decline(self):
        return self.api.handle("POST", "/api/v1/spo2/decline", {}, {})

    def tick(self, advance: float = 0.0):
        if advance:
            self.clock.advance(advance)
        self.rt.tick()

    def reach_prompt(self):
        """走到"正在叫人"这一步（用假时钟推进，与接口共用同一个时间源）。"""
        self.rt.tick()
        self.clock.advance(REMIND_S)
        self.rt.tick()

    def panel_html(self) -> str:
        return webui.render_panel(self.rt, self.rt.config_store)

    def control_html(self) -> str:
        """功能面板（``GET /control``，2026-10-01）——【血氧检测】一节 2026-10-01 搬到了这里。"""
        return webui.render_control(self.rt)


class TestStatus(_ApiCase):
    def test_待机时的字段(self) -> None:
        self.tick()
        status, payload = self.get()
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["enabled"])
        self.assertEqual(payload["state"], "idle")
        self.assertIsNone(payload["prompt_left_s"])
        self.assertIsNone(payload["measure_left_s"])
        self.assertEqual(payload["accepted_total"], 0)
        self.assertEqual(payload["declined_total"], 0)
        self.assertIsNone(payload["last_result"])

    def test_叫人时prompt_left_s递减(self) -> None:
        self.reach_prompt()
        _, first = self.get()
        self.assertEqual(first["state"], "prompt")
        self.assertIsNotNone(first["prompt_left_s"])
        self.tick(advance=5.0)
        _, second = self.get()
        self.assertLess(second["prompt_left_s"], first["prompt_left_s"], "倒计时没有递减")

    def test_测量时measure_left_s递减(self) -> None:
        self.reach_prompt()
        status, _ = self.measure()
        self.assertEqual(status, 200)
        _, first = self.get()
        self.assertEqual(first["state"], "measure")
        self.assertIsNotNone(first["measure_left_s"])
        self.tick(advance=3.0)
        _, second = self.get()
        self.assertLess(second["measure_left_s"], first["measure_left_s"], "倒计时没有递减")

    def test_出结果后last_result能读到(self) -> None:
        self.reach_prompt()
        self.measure()
        self.rt.set_vitals(heart_rate=68.0, spo2=97.0)
        self.tick(advance=MEASURE_S + 1.0)
        _, payload = self.get()
        result = payload["last_result"]
        self.assertIsNotNone(result)
        self.assertTrue(result["ok"])
        self.assertAlmostEqual(result["heart_rate_bpm"], 68.0, delta=2.0)

    def test_没测到时last_result也记(self) -> None:
        self.reach_prompt()
        self.measure()
        self.rt.set_vitals(finger=False)
        self.tick(advance=MEASURE_S + 1.0)
        _, payload = self.get()
        self.assertFalse(payload["last_result"]["ok"])


class TestMeasure(_ApiCase):
    def test_待机时也能开始(self) -> None:
        self.tick()
        status, payload = self.measure()
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["state"], "measure")

    def test_叫人时接受(self) -> None:
        self.reach_prompt()
        status, payload = self.measure()
        self.assertEqual(status, 200)
        self.assertEqual(payload["state"], "measure")
        _, st = self.get()
        self.assertEqual(st["accepted_total"], 1)

    def test_走的是同一条路径(self) -> None:
        """★★ 核心判据：HTTP 与**物理按键**必须产生**完全一致**的行为。

        判据取"两块屏上的文案 + 状态 + 计数"三样 —— 只比状态的话，
        "两套各写一半、碰巧都进了 measure"这种分叉会被漏掉。
        """
        # A 组：物理按键
        self.reach_prompt()
        self.rt.sim("spo2_button").press(ButtonAction.CLICK)
        self.clock.advance(0.3)
        self.rt.tick()
        physical = (
            tuple(self.rt.devices["display"].read().lines),
            tuple(self.rt.devices["tft"].read().lines),
            self.rt.spo2_status(self.clock())["state"],
            self.rt.spo2_status(self.clock())["accepted_total"],
        )
        self.rt.close()

        # B 组：同样的配置与前置状态，改用 HTTP
        self.setUp()
        self.reach_prompt()
        status, _ = self.measure()
        self.assertEqual(status, 200)
        http = (
            tuple(self.rt.devices["display"].read().lines),
            tuple(self.rt.devices["tft"].read().lines),
            self.rt.spo2_status(self.clock())["state"],
            self.rt.spo2_status(self.clock())["accepted_total"],
        )
        self.assertEqual(physical, http, "HTTP 与物理按键的行为不一致（有两条路径）")
        self.assertIn(SPO2_MEASURE_LINES[0], physical[0][0])


class TestDecline(_ApiCase):
    def test_叫人时否决生效(self) -> None:
        self.reach_prompt()
        status, payload = self.decline()
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["state"], "idle")
        _, st = self.get()
        self.assertEqual(st["declined_total"], 1)
        self.assertEqual(st["accepted_total"], 0)
        self.assertNotIn(SPO2_MEASURE_LINES[0], tuple(self.rt.devices["display"].read().lines)[0])

    def test_待机时否决返回409(self) -> None:
        """没有"正在叫人"，就没有可否决的东西 —— 必须 409，不能假装成功。"""
        self.tick()
        status, payload = self.decline()
        self.assertEqual(status, 409)
        self.assertFalse(payload["ok"])
        self.assertIn("叫人", payload["error"])
        _, st = self.get()
        self.assertEqual(st["declined_total"], 0, "被拒的操作不该改动计数")

    def test_测量时否决返回409(self) -> None:
        self.reach_prompt()
        self.measure()
        status, payload = self.decline()
        self.assertEqual(status, 409)
        self.assertIn("measure", payload["error"])

    def test_否决后还能再被叫(self) -> None:
        self.reach_prompt()
        self.decline()
        self.tick(advance=REMIND_S)
        _, st = self.get()
        self.assertEqual(st["state"], "prompt", "否决之后没有按间隔重新排队")


class TestDisabled(_ApiCase):
    """功能总开关：`spo2_button.enabled=false`。"""

    spo2_enabled = False

    def test_get里enabled为假(self) -> None:
        self.tick()
        status, payload = self.get()
        self.assertEqual(status, 200)
        self.assertFalse(payload["enabled"])
        self.assertEqual(payload["state"], "idle")

    def test_measure返回409(self) -> None:
        status, payload = self.measure()
        self.assertEqual(status, 409)
        self.assertIn("未启用", payload["error"])

    def test_decline返回409(self) -> None:
        status, payload = self.decline()
        self.assertEqual(status, 409)
        self.assertIn("未启用", payload["error"])


class TestPanelSection(_ApiCase):
    """【血氧检测】一节（2026-10-01 从 ``/panel`` **搬到功能面板** ``/control``）。

    搬家的理由（用户 2026-10-01 划的三面板分工）：``/panel`` 只管**改数字**，
    ``/control`` 才管**触发动作**。血氧检测是"触发一次测量"⇒ 归功能面板。
    ⚠️ 所以这里既有**正向钉子**（``/control`` 必须有），也有**反向钉子**
    （``/panel`` 必须**没有**）—— 只钉一头的话，两边都留一份也不会被发现。
    """

    def test_有两个按钮(self) -> None:
        html = self.control_html()
        self.assertIn("【血氧检测】", html)
        self.assertIn(">血氧检测<", html)
        self.assertIn(">暂不检测<", html)
        self.assertIn("spo2Act('measure')", html)
        self.assertIn("spo2Act('decline')", html)

    def test_配置面板上不再有血氧检测一节(self) -> None:
        """★ 反向钉子：搬完就必须**搬干净**。

        一个动作只能有一个入口 —— 两页各留一份，迟早"一边改了、另一边忘了"
        （本项目"一个动作只有一条路径"的纪律，血氧那条路径本身就吃过类似的亏）。
        """
        html = self.panel_html()
        self.assertNotIn("【血氧检测】", html)
        self.assertNotIn("spo2Act", html)
        self.assertNotIn("spo2state", html)

    def test_轮询只刷自己那一节_绝不整页刷新(self) -> None:
        """★★ 轮询**只更新血氧那一节自己的 DOM**，绝不整页刷新。

        功能面板上有"屏显控制"的结果要留着给用户看、配置面板上还有没保存的输入 ——
        顺手写一句 ``location.reload`` 就会把它们全抹掉。所以要有机器判据钉死。
        """
        for html in (self.control_html(), self.panel_html()):
            self.assertNotIn("location.reload", html)
            self.assertNotIn("location.href", html)
            self.assertNotIn('http-equiv="refresh"', html)
        html = self.control_html()
        # 只更新这一节自己的 DOM：状态行 + 两个按钮
        self.assertIn("spo2state", html)
        self.assertIn("setInterval(spo2Poll, 2000)", html)

    def test_暂不检测默认是灰的(self) -> None:
        """非"正在叫人"时按钮必须灰掉 —— 不让用户点了才拿到 409。"""
        html = self.control_html()
        marker = 'id="spo2no"'
        self.assertIn(marker, html)
        self.assertIn("disabled", html.split(marker)[1][:80])

class TestPromptText(_ApiCase):
    def test_叫人文案把两种选择都写在屏上(self) -> None:
        self.assertEqual(SPO2_PROMPT_LINES, ("SPO2 CHECK NOW?", "CLICK=Y HOLD=N"))
        for line in SPO2_PROMPT_LINES + SPO2_MEASURE_LINES + SPO2_FAIL_LINES:
            self.assertTrue(line.isascii(), line)
            self.assertLessEqual(len(line), 16, line)


class TestSpo2OverRealHttp(_ApiCase):
    """★ 再用**真实 HTTP**（真 socket，不是直接调 ``WebApi.handle``）走一遍。

    为什么非要有这一层：加配置面板时踩到过 —— ``_CURRENT_BODY`` 是 ``threading.local``，
    服务器分支里被写成 ``.get()``（它**没有**这个方法），**直接调 ``handle()`` 的测试全绿**，
    只有走真 socket 的才炸。⇒ 单测的调用方式**不等于**真实调用方式。
    """

    def setUp(self) -> None:
        super().setUp()
        self.server = self.rt.start_http(host="127.0.0.1", port=0)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def _get(self, path: str):
        with urllib.request.urlopen(f"{self.base}{path}", timeout=5) as resp:  # noqa: S310
            return resp.status, resp.read().decode("utf-8"), resp.headers.get("Content-Type", "")

    def _post(self, path: str):
        req = urllib.request.Request(f"{self.base}{path}", data=b"", method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_真HTTP上GET状态返回JSON(self) -> None:
        self.tick()
        status, text, ctype = self._get("/api/v1/spo2")
        self.assertEqual(status, 200)
        self.assertIn("application/json", ctype)
        self.assertEqual(json.loads(text)["state"], "idle")

    def test_真HTTP上measure进入测量(self) -> None:
        self.reach_prompt()
        status, body = self._post("/api/v1/spo2/measure")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["state"], "measure")

    def test_真HTTP上待机时decline是409(self) -> None:
        self.tick()
        status, body = self._post("/api/v1/spo2/decline")
        self.assertEqual(status, 409)
        self.assertFalse(body["ok"])
        self.assertIn("叫人", body["error"])

    def test_真HTTP上叫人时decline生效(self) -> None:
        self.reach_prompt()
        status, body = self._post("/api/v1/spo2/decline")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["state"], "idle")
        _, text, _ = self._get("/api/v1/spo2")
        self.assertEqual(json.loads(text)["declined_total"], 1)

    def test_真HTTP上功能面板含血氧检测一节(self) -> None:
        """走真 socket 验证路由：``/control`` 是新的功能面板。"""
        status, text, ctype = self._get("/control")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        self.assertIn("【血氧检测】", text)
        self.assertIn(">暂不检测<", text)

    def test_真HTTP上配置面板已不含血氧检测(self) -> None:
        """反向钉子（真 socket 版）：搬完必须搬干净。"""
        status, text, ctype = self._get("/panel")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        self.assertNotIn("【血氧检测】", text)
        self.assertNotIn("spo2Act", text)

    def test_真HTTP上三个面板都在且都是HTML(self) -> None:
        """三个面板（数据 / 功能 / 配置）都能打开、都是 HTML、都带同一套导航。"""
        for path in ("/", "/control", "/panel"):
            status, text, ctype = self._get(path)
            self.assertEqual(status, 200, path)
            self.assertIn("text/html", ctype, path)
            self.assertIn('nav class="tabs"', text, path)
            self.assertIn('href="/control"', text, path)
            self.assertIn('href="/panel"', text, path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
