"""端到端集成测试：**把整机的真实链路跑一遍**（不接任何硬件）。

与单元测试的分工：
- 单元测试验证"某个模块自己对不对"；
- 本文件验证"模块之间的**接线**对不对"——这是最容易出错、又最难被单测发现的一类问题
  （例如：报警判出来了但输出器件根本没打开、HTTP 接口参数解析错位、
   演示脚本改了值但驱动没生效）。

覆盖的链路：
1. 配置 → 装配 → 采集 → 快照（真实 Runtime，不是手搓的假对象）
2. 报警 → 下发 → 输出器件真的收到了指令（LED/LCD/蜂鸣器/音箱）
3. HTTP API 真的能返回当前读数（起真实 socket，走真实 HTTP 请求）
4. 消音 / 求助 控制接口对输出的影响
5. 引脚冲突与配置一致性
"""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from typing import Any, Dict, List

from health_monitor.core.config import AppConfig
from health_monitor.demo import DEMO_CONFIG
from health_monitor.hal import find_conflicts
from health_monitor.net.web import WebApi
from health_monitor.playback import PlaybackRuntime
from health_monitor.service import Runtime


def demo_runtime(**kwargs: Any) -> PlaybackRuntime:
    config = AppConfig.from_dict(DEMO_CONFIG)
    kwargs.setdefault("sleep", lambda _s: None)
    return PlaybackRuntime(config, **kwargs)


class _Clock:
    def __init__(self, start: float = 1_790_000_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> float:
        self.t += seconds
        return self.t


class TestFullPipeline(unittest.TestCase):
    """整机链路：采集 → 判定 → 下发（走真实 Runtime）。"""

    def setUp(self) -> None:
        self.clock = _Clock()
        self.rt = demo_runtime(clock=self.clock)
        self.errors = self.rt.open()
        self.assertEqual(self.errors, {}, f"演示配置下不应有装配/打开失败：{self.errors}")

    def tearDown(self) -> None:
        self.rt.close()

    def test_所有演示设备都装配成功(self) -> None:
        self.assertIn("vitals", self.rt.inputs)
        self.assertIn("motion", self.rt.inputs)
        for name in ("display", "speaker", "alarm_buzzer", "status_led"):
            self.assertIn(name, self.rt.outputs, f"输出器件 {name} 未装配")

    def test_输出器件在open后真的被打开(self) -> None:
        """★ 回归测试：曾经 Runtime 把输出器件排除在采集器之外后**忘了 open 它们**，
        结果"报警判出来了、蜂鸣器和屏幕却毫无反应"，而且只有 warning 日志。"""
        for name, device in self.rt.outputs.items():
            with self.subTest(device=name):
                self.assertTrue(device.status()["opened"], f"输出器件 {name} 没有被 open()")

    def test_正常数据不报警(self) -> None:
        self.rt.set_vitals(heart_rate=72.0, spo2=98.0)
        self.rt.set_motion(__import__("health_monitor.hal.models", fromlist=["MotionState"]).MotionState.DETECTED)
        events = self.rt.tick()
        self.assertEqual([e.code.value for e in events if e.code.value != "system_start"], [])

    def test_心率偏高会一路下发到灯屏声(self) -> None:
        self.rt.set_vitals(heart_rate=130.0, spo2=98.0)
        events = self.rt.tick()
        codes = [e.code.value for e in events]
        self.assertIn("hr_too_high", codes)

        led = self.rt.outputs["status_led"]
        lcd = self.rt.outputs["display"]
        buzzer = self.rt.outputs["alarm_buzzer"]
        speaker = self.rt.outputs["speaker"]
        self.assertNotEqual(led.current_color, "off", "报警时 LED 必须亮")
        self.assertIn("HR HIGH", " ".join(lcd.current_lines), "LCD 必须显示报警文案")
        self.assertGreater(buzzer.total_beeps, 0, "报警时蜂鸣器必须响")
        self.assertTrue(speaker.spoken, "报警时必须有语音播报")
        self.assertEqual(self.rt.dispatcher.errors, [], "下发不应有失败")

    def test_血氧过低是紧急等级(self) -> None:
        self.rt.set_vitals(heart_rate=75.0, spo2=88.0)
        events = self.rt.tick()
        spo2 = next(e for e in events if e.code.value == "spo2_too_low")
        self.assertEqual(int(spo2.severity), 3)
        self.assertEqual(self.rt.outputs["status_led"].current_color, "red")

    def test_久无活动报警(self) -> None:
        from health_monitor.hal.models import MotionState

        self.rt.set_motion(MotionState.IDLE, silent_s=120.0)
        codes = [e.code.value for e in self.rt.tick()]
        self.assertIn("no_motion_too_long", codes)

    def test_求救会先解除静音再报警(self) -> None:
        self.rt.silence(self.clock())
        self.assertTrue(self.rt.dispatcher.is_silenced(self.clock()))
        event = self.rt.sos(self.clock())
        self.assertEqual(event.code.value, "sos_pressed")
        self.assertFalse(self.rt.dispatcher.is_silenced(self.clock()), "求救必须能响，先解除静音")
        self.assertEqual(self.rt.outputs["status_led"].current_color, "red")

    def test_消音之后只亮灯不出声(self) -> None:
        self.rt.set_vitals(heart_rate=130.0, spo2=98.0)
        self.rt.silence(self.clock())
        buzzer = self.rt.outputs["alarm_buzzer"]
        speaker = self.rt.outputs["speaker"]
        before_beeps, before_spoken = buzzer.total_beeps, len(speaker.spoken)
        self.rt.tick()
        self.assertEqual(buzzer.total_beeps, before_beeps, "静音期间蜂鸣器不该响")
        self.assertEqual(len(speaker.spoken), before_spoken, "静音期间不该语音播报")
        self.assertNotEqual(self.rt.outputs["status_led"].current_color, "off", "静音期间仍要亮灯提示")

    def test_确认解除后输出器件复位(self) -> None:
        """★ 回归测试：只清内部报警态而不同步输出器件，会让 LED 永远停在红色。"""
        from health_monitor.hal.models import MotionState

        self.rt.sos(self.clock())
        self.assertEqual(self.rt.outputs["status_led"].current_color, "red")
        self.rt.set_vitals(heart_rate=72.0, spo2=98.0)
        self.rt.set_motion(MotionState.DETECTED, silent_s=0.0)
        self.rt.clear_alarms(self.clock())
        self.assertEqual(self.rt.outputs["status_led"].current_color, "green", "确认解除后必须把灯复位")
        self.assertIn("NORMAL", " ".join(self.rt.outputs["display"].current_lines))

    def test_传感器故障会报警并随后恢复(self) -> None:
        self.rt.break_sensor("vitals", "模拟掉线")
        codes: List[str] = []
        for _ in range(3):
            self.clock.advance(5.0)
            codes = [e.code.value for e in self.rt.tick()]
        self.assertIn("sensor_fault", codes)
        self.rt.fix_sensor("vitals")
        self.clock.advance(5.0)
        events = self.rt.tick()
        self.assertNotIn("sensor_fault", [e.code.value for e in events], "恢复后不该继续报故障")

    def test_状态里能看到装配与下发情况(self) -> None:
        status = self.rt.status()
        self.assertEqual(status["devices"]["errors"], {})
        self.assertIn("vitals", status["devices"]["assembled"])
        self.assertGreaterEqual(len(status["dispatcher"]["outputs"]), 4)


class Test实体按键接线(unittest.TestCase):
    """★ 回归测试（2026-09-26）：**实体按键此前没有接到业务层**。

    当时的情况：`sensors/button.py` 的去抖与 CLICK / LONG_PRESS 事件都有单测，
    但 `ReadingSnapshot` 里没有按键字段、`Runtime.tick()` 也不消费按键事件
    ⇒ 真机上按实体键**毫无反应**（`docs/14` 的 T3 就是这么发现的）。
    这里钉住"短按消音 / 长按求助"这两条链路，避免以后又被拆掉。

    为什么用配置加一个 `sos_button`：演示配置（`DEMO_CONFIG`）里本来没有按键，
    而按键的接线正是被测对象，所以这里显式造一个。
    """

    def setUp(self) -> None:
        import copy

        config = copy.deepcopy(DEMO_CONFIG)
        config["devices"]["sos_button"] = {"driver": "button", "read_interval_s": 0.2}
        self.clock = _Clock()
        self.rt = PlaybackRuntime(
            AppConfig.from_dict(config), clock=self.clock, sleep=lambda _s: None
        )
        self.errors = self.rt.open()
        self.assertEqual(self.errors, {}, f"装配不应失败：{self.errors}")
        self.button = self.rt.sim("sos_button")
        self.assertIsNotNone(self.button, "配置里的 sos_button 没有被装配出来")

    def tearDown(self) -> None:
        self.rt.close()

    def test_短按消音(self) -> None:
        from health_monitor.hal.models import ButtonAction

        self.rt.set_vitals(heart_rate=130.0, spo2=98.0)
        self.button.press(ButtonAction.CLICK)
        self.rt.tick()
        self.assertTrue(
            self.rt.dispatcher.is_silenced(self.clock()),
            "短按实体键应当消音（灯仍亮、不出声）",
        )

    def test_短按不产生求助事件(self) -> None:
        from health_monitor.hal.models import ButtonAction

        self.button.press(ButtonAction.CLICK)
        codes = [e.code.value for e in self.rt.tick()]
        self.assertNotIn("sos_pressed", codes)

    def test_长按触发求助并且先解除静音(self) -> None:
        from health_monitor.hal.models import ButtonAction

        self.rt.silence(self.clock())
        buzzer = self.rt.outputs["alarm_buzzer"]
        before = buzzer.total_beeps

        self.button.press(ButtonAction.LONG_PRESS)
        codes = [e.code.value for e in self.rt.tick()]
        self.assertIn("sos_pressed", codes, "长按必须触发 SOS 事件")
        self.assertFalse(self.rt.dispatcher.is_silenced(self.clock()), "求救必须能响：先解除静音")
        self.assertGreater(buzzer.total_beeps, before, "SOS 应当让蜂鸣器响")
        self.assertEqual(self.rt.outputs["status_led"].current_color, "red", "SOS 应当亮红灯")

    def test_没有按键设备时不影响主循环(self) -> None:
        """没装配按键的配置（例如只接了 DHT11 的 T0/T1 阶段）必须照常跑。"""
        rt = demo_runtime(clock=_Clock())
        self.assertEqual(rt.open(), {})
        try:
            self.assertIsInstance(rt.tick(), list)
        finally:
            rt.close()


class Test报警持续提醒(unittest.TestCase):
    """★ 2026-09-26 真机 T3 用户反馈后新增：报警不能"响两声就完"。

    真机现象（用户原话）：拔掉传感器 → 黄灯 + 蜂鸣 2 声 → **之后再无动静**，
    于是"短按消音"根本看不出效果；而且灯只是"闪 3 秒就常亮"（不是持续闪）。

    现在：报警仍在且**未消音**期间，每 `re_alert_interval_s` 秒**重发**一次（响 + 刷屏，
    灯持续闪）；短按消音后**不再响**、灯转**常亮**；报警解除后彻底停。
    """

    def setUp(self) -> None:
        self.clock = _Clock()
        self.rt = self._runtime(5.0)
        self.led = self.rt.outputs["status_led"]
        self.buzzer = self.rt.outputs["alarm_buzzer"]

    def tearDown(self) -> None:
        self.rt.close()

    @staticmethod
    def _config_with(interval: float) -> AppConfig:
        import copy

        config = copy.deepcopy(DEMO_CONFIG)
        thresholds = config.setdefault("thresholds", {})
        thresholds["re_alert_interval_s"] = interval
        # ⚠️ 演示配置里 `repeat_cooldown_s=0`（为了几十秒演完 11 幕）⇒ **每一帧都会重新报同一条报警**。
        #    那会污染本组测试（把"持续重发"与"规则引擎每帧重报"混在一起），所以这里把它调大。
        thresholds["repeat_cooldown_s"] = 300
        return AppConfig.from_dict(config)

    def _runtime(self, interval: float) -> PlaybackRuntime:
        rt = PlaybackRuntime(self._config_with(interval), clock=self.clock, sleep=lambda _s: None)
        self.assertEqual(rt.open(), {}, "演示配置下不该有装配失败")
        return rt

    def _trigger_alarm(self) -> None:
        self.rt.set_vitals(heart_rate=130.0, spo2=98.0)
        self.rt.tick()

    def test_报警期间灯持续闪(self) -> None:
        self._trigger_alarm()
        self.assertTrue(self.led.blink_requested, "报警期间 LED 应当处于持续闪状态")

    def test_到间隔才重响不到不响(self) -> None:
        self._trigger_alarm()
        first = self.buzzer.total_beeps
        self.clock.advance(1.0)
        self.rt.tick()
        self.assertEqual(self.buzzer.total_beeps, first, "没到间隔不该重响")
        self.clock.advance(5.0)
        self.rt.tick()
        self.assertGreater(self.buzzer.total_beeps, first, "到了间隔必须再响一次")
        self.assertGreaterEqual(self.rt.dispatcher.realerts, 1, "重发次数要能被观察")

    def test_消音后不再重响但灯仍亮(self) -> None:
        self._trigger_alarm()
        self.rt.silence(self.clock())
        self.clock.advance(0.1)
        self.rt.tick()
        self.assertFalse(self.led.blink_requested, "消音后灯应转常亮（不再闪）")
        self.assertNotEqual(self.led.current_color, "off", "消音只停声音，灯仍要亮")

        beeps = self.buzzer.total_beeps
        for _ in range(4):
            self.clock.advance(5.0)
            self.rt.tick()
        self.assertEqual(self.buzzer.total_beeps, beeps, "消音期间不许再响")

    def test_报警解除后不再重发(self) -> None:
        self._trigger_alarm()
        self.rt.set_vitals(heart_rate=72.0, spo2=98.0)
        self.clock.advance(5.0)
        events = self.rt.tick()
        self.assertIn("all_clear", [e.code.value for e in events], "恢复正常应当发 ALL_CLEAR")

        beeps = self.buzzer.total_beeps
        for _ in range(3):
            self.clock.advance(5.0)
            self.rt.tick()
        self.assertEqual(self.buzzer.total_beeps, beeps, "报警解除后不该再重发")

    def test_间隔为0时回到只提示一次(self) -> None:
        """`re_alert_interval_s=0` = 关闭持续提醒（保留旧行为，便于对照与降级）。"""
        clock = _Clock()
        rt = PlaybackRuntime(self._config_with(0.0), clock=clock, sleep=lambda _s: None)
        self.assertEqual(rt.open(), {})
        try:
            rt.set_vitals(heart_rate=130.0, spo2=98.0)
            rt.tick()
            beeps = rt.outputs["alarm_buzzer"].total_beeps
            for _ in range(3):
                clock.advance(10.0)
                rt.tick()
            self.assertEqual(rt.outputs["alarm_buzzer"].total_beeps, beeps, "关闭后不该重发")
            self.assertEqual(rt.dispatcher.realerts, 0)
        finally:
            rt.close()


class TestHttpApiEndToEnd(unittest.TestCase):
    """起真实 HTTP 服务，走真实 socket 请求（不是直接调 WebApi）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.rt = demo_runtime()
        cls.rt.open()
        cls.rt.set_vitals(heart_rate=88.0, spo2=97.0)
        cls.rt.tick()
        cls.server = cls.rt.start_http(host="127.0.0.1", port=0)   # 端口 0 = 让系统分配
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.rt.close()

    def _get(self, path: str) -> Dict[str, Any]:
        with urllib.request.urlopen(f"{self.base}{path}", timeout=5) as resp:  # noqa: S310
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("Content-Type"), "application/json; charset=utf-8")
            return json.loads(resp.read().decode("utf-8"))

    def _post(self, path: str) -> Dict[str, Any]:
        req = urllib.request.Request(f"{self.base}{path}", method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))

    def test_health_接口(self) -> None:
        payload = self._get("/api/v1/health")
        self.assertTrue(payload["ok"])
        self.assertIn("devices", payload)
        self.assertIn("version", payload)

    def test_current_接口返回扁平读数(self) -> None:
        payload = self._get("/api/v1/current")
        data = payload["data"]
        # 注意：tick() 会重新采样，模拟器/驱动给出的是"物理合理"的值，
        # 所以这里断言**量级合理**，而不是死等我们设进去的那个数（那是在测模拟器，不是在测接口）。
        self.assertIsInstance(data["heart_rate_bpm"], (int, float))
        self.assertGreater(data["heart_rate_bpm"], 30.0)
        self.assertLess(data["heart_rate_bpm"], 200.0)
        self.assertIn("ambient_temp_c", data)
        self.assertIsInstance(data["sensor_failures"], dict)

    def test_current_缺失值是null而不是0(self) -> None:
        """手机端渲染规则的契约：缺失必须是 null（App 会显示"未知"）。"""
        payload = self._get("/api/v1/current")
        data = payload["data"]
        for key in ("heart_rate_bpm", "spo2_percent", "body_temp_c", "ambient_temp_c"):
            self.assertTrue(data[key] is None or isinstance(data[key], (int, float)), key)

    def test_history_接口支持指标与条数(self) -> None:
        payload = self._get("/api/v1/history?metric=ambient_temp_c&limit=5")
        self.assertTrue(payload["ok"])
        self.assertLessEqual(len(payload["data"]), 5)

    def test_history_参数越界被夹紧而不是500(self) -> None:
        self.assertTrue(self._get("/api/v1/history?limit=999999")["ok"])
        self.assertTrue(self._get("/api/v1/history?limit=abc")["ok"])

    def test_history_指标名要先做URL编码再发(self) -> None:
        """★ 回归测试：HTTP 请求行只能是 ASCII，
        直接把中文指标名塞进 URL 会抛 ``UnicodeEncodeError``。
        非 ASCII 参数必须 ``quote()`` 之后再发（浏览器/安卓端会自动做，脚本要自己做）。
        """
        from urllib.parse import quote

        payload = self._get(f"/api/v1/history?metric={quote('不存在的指标')}")
        self.assertTrue(payload["ok"], "未知指标名应返回空数据而不是报错")

    def test_alarms_接口(self) -> None:
        self.rt.set_vitals(heart_rate=135.0, spo2=97.0)
        self.rt.tick()
        self.rt.set_vitals(heart_rate=72.0, spo2=97.0)
        payload = self._get("/api/v1/alarms?limit=10")
        self.assertTrue(payload["ok"])
        self.assertIn("live", payload)
        self.assertIn("history", payload)

    def test_devices_接口给出接线说明(self) -> None:
        payload = self._get("/api/v1/devices")
        self.assertIn("vitals", payload["devices"])
        self.assertIn("describe", payload["devices"]["vitals"])

    def test_silence与sos控制接口(self) -> None:
        self.assertTrue(self._post("/api/v1/silence")["ok"])
        self.assertTrue(self.rt.dispatcher.is_silenced(self.rt.clock()))
        event = self._post("/api/v1/sos")["event"]
        self.assertEqual(event["code"], "sos_pressed")

    def test_speak_空文本被拒绝且给出400(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/v1/speak?text=")
        self.assertEqual(ctx.exception.code, 400)

    def test_speak_超长文本被拒绝(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/v1/speak?text=" + "a" * 61)
        self.assertEqual(ctx.exception.code, 400)

    def test_未知路径返回404且列出可用接口(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/api/v1/nope")
        self.assertEqual(ctx.exception.code, 404)
        body = json.loads(ctx.exception.read().decode("utf-8"))
        self.assertIn("available", body)


class TestHttpTokenAuth(unittest.TestCase):
    def test_配置token后无token请求被拒绝(self) -> None:
        rt = demo_runtime()
        rt.open()
        api = WebApi(rt, token="secret")
        status, payload = api.handle("GET", "/api/v1/current", {}, {})
        self.assertEqual(status, 401)
        self.assertFalse(payload["ok"])
        status, payload = api.handle("GET", "/api/v1/current", {}, {"x-auth-token": "secret"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        rt.close()


class TestPortConflict(unittest.TestCase):
    def test_端口被占用时抛出OSError而不是静默失败(self) -> None:
        """两个实例抢同一个端口时，第二个必须**明确失败**（而不是假装启动成功）。"""
        rt1 = demo_runtime()
        rt1.open()
        rt2 = demo_runtime()
        rt2.open()
        server1 = rt1.start_http(host="127.0.0.1", port=0)
        port = server1.server_address[1]
        try:
            with self.assertRaises(OSError):
                rt2.start_http(host="127.0.0.1", port=port)
        finally:
            rt1.close()
            rt2.close()


class TestConfigConsistency(unittest.TestCase):
    def test_演示配置没有引脚冲突(self) -> None:
        claims = []
        for name, item in DEMO_CONFIG["devices"].items():
            params = item.get("params", {})
            for key in ("pin", "trig_pin", "echo_pin"):
                if key in params:
                    claims.append((f"{name}.{key}", int(params[key])))
        self.assertEqual(find_conflicts(claims), [])

    def test_演示配置的设备驱动都已实现(self) -> None:
        """演示配置里引用的每个驱动都必须能在注册表里找到（否则演示会退回模拟）。"""
        from health_monitor.hal import get_spec

        for name, item in DEMO_CONFIG["devices"].items():
            with self.subTest(device=name):
                spec = get_spec(item["driver"])
                spec.load()   # 驱动模块必须存在且类可导入


if __name__ == "__main__":
    unittest.main(verbosity=2)
