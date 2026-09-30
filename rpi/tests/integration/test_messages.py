"""「按键交互收口」测试（2026-10-01）：消音 / SOS / 血氧结果都变成**后台可查询的消息记录**。

用户原话
--------
> 「当环境温度过低之类，进行蜂鸣器报警处理，**老人可以通过短按 A 键进行关闭警报，
> 然后后台相关消息页面收到相关指示**；老人**长按 A 键，触发 SOS，蜂鸣器报警若干声，
> 然后后台同时显示紧急消息**；血氧进行测量后……**后台也能看到相关记录**」

三件事里只有一件原来就是通的
----------------------------
* **长按 SOS** —— 本来就是事件（`sos()` 会建 `SOS_PRESSED`、落库、进 `_events`、下发）；
* **短按消音** —— **后台完全看不到**：`silence()` 只调 `dispatcher.silence()`，不产生事件；
* **测完血氧** —— **后台完全看不到**：结果只写进内存 `_spo2_last_result`。

★ 本组最要紧的判据是**反向钉子**：两个新事件属于 `RECORD_ONLY_CODES`（**只记录、不下发**）。
一旦误走 `dispatch()`，就会把**正在显示的活动报警刷掉**（那正好毁掉报警最要紧的几秒），
或者让蜂鸣器在不该响的时候响（血氧测量本来就有自己的 1/2/5 声暗号，再来一次是两次提示打架）。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

_RPI_DIR = Path(__file__).resolve().parents[2]
if str(_RPI_DIR) not in sys.path:
    sys.path.insert(0, str(_RPI_DIR))

from health_monitor.core.config import AppConfig  # noqa: E402
from health_monitor.hal.models import (  # noqa: E402
    AlarmCode,
    AlarmEvent,
    ButtonAction,
    RECORD_ONLY_CODES,
    Severity,
    message_kind,
)
from health_monitor.net import webui  # noqa: E402
from health_monitor.net.web import WebApi  # noqa: E402
from health_monitor.playback import PlaybackRuntime  # noqa: E402

REMIND_S = 120.0
TIMEOUT_S = 30.0
MEASURE_S = 10.0

DEMO = {
    "thresholds": {
        "no_motion_timeout_s": 60,
        "repeat_cooldown_s": 0,
        "ambient_temp_max": 30,
        "spo2_remind_interval_s": REMIND_S,
        "spo2_remind_timeout_s": TIMEOUT_S,
        "spo2_measure_s": MEASURE_S,
    },
    "devices": {
        "vitals": {"driver": "max30102", "read_interval_s": 1.0},
        "ambient": {"driver": "dht11", "read_interval_s": 2.0},
        "display": {"driver": "lcd1602", "read_interval_s": 1.0},
        "tft": {"driver": "tft_spi", "read_interval_s": 2.0,
                "params": {"controller": "st7735", "spi_device": 1}},
        "alarm_buzzer": {"driver": "buzzer", "read_interval_s": 1.0},
        "status_led": {"driver": "led", "read_interval_s": 1.0},
        "sos_button": {"driver": "button", "read_interval_s": 0.2},
        "spo2_button": {"driver": "button", "read_interval_s": 0.2},
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
    """公共装置：假时钟 + 两块屏 + 蜂鸣 + 灯 + 两个按键（可选挂一个真历史库）。"""

    #: 子类改成 True ⇒ 建一个真的临时历史库，验证 `/api/v1/messages` 的**落库**路径
    use_store = False

    def setUp(self) -> None:
        config = {"thresholds": dict(DEMO["thresholds"]),
                  "devices": {n: dict(c) for n, c in DEMO["devices"].items()}}
        kwargs: dict = {}
        if self.use_store:
            self._tmp = tempfile.TemporaryDirectory()
            self.addCleanup(self._tmp.cleanup)
            kwargs["store_path"] = str(Path(self._tmp.name) / "history.db")
        self.clock = _Clock()
        self.rt = PlaybackRuntime(
            AppConfig.from_dict(config),
            clock=self.clock,
            sleep=lambda _s: None,
            verbose_outputs=False,
            **kwargs,
        )
        self.rt.open()
        self.addCleanup(self.rt.close)

    # ---------- 读屏 / 读蜂鸣 / 读灯 / 按键 ----------

    def _lcd(self) -> tuple:
        return tuple(self.rt.devices["display"].read().lines)

    def _tft(self) -> tuple:
        return tuple(self.rt.devices["tft"].read().lines)

    def _beeps(self) -> int:
        return int(self.rt.devices["alarm_buzzer"].total_beeps)

    def _led(self) -> str:
        return str(self.rt.outputs["status_led"].current_color)

    def _tick(self, advance: float = 0.0) -> None:
        if advance:
            self.clock.advance(advance)
        self.rt.tick()

    def _press_and_tick(self, who: str, action: ButtonAction = ButtonAction.CLICK,
                        advance: float = 0.3) -> None:
        """注入一次按键再跑一帧。

        ⚠️ **必须把时钟推过按键的读取周期（0.2s）**：采集器按 `read_interval_s` 节流，
        时钟不动的话第二帧根本不会去读那个按键，事件就躺在驱动队列里。
        """
        self.rt.sim(who).press(action)
        self._tick(advance=advance)

    # ---------- 便捷断言 ----------

    def _events_of(self, code: AlarmCode) -> list:
        return [e for e in self.rt.recent_events() if e.code is code]


class TestSilenceRecorded(_Base):
    """短按 A 关警报 ⇒ **后台必须收到指示**（用户 2026-10-01 要求）。"""

    def test_有报警时消音会记下当时在报什么(self) -> None:
        self._tick()
        self.rt.set_ambient(temperature_c=35.0)          # 越过 ambient_temp_max = 30
        self._tick(advance=3.0)
        self.assertIn("ambient_temp_high", self.rt.engine.active_alarms(),
                      "前置条件没成立：得先真的在报警")

        self._press_and_tick("sos_button")               # 短按 = 消音

        recs = self._events_of(AlarmCode.ALARM_SILENCED)
        self.assertEqual(len(recs), 1, "短按消音必须留下**一条**消息")
        rec = recs[0]
        self.assertEqual(rec.detail["active_alarms"], ["ambient_temp_high"],
                         "detail 里必须写清'当时在报什么警'")
        self.assertEqual(rec.detail["by"], "button")
        self.assertIn("正在报警", rec.message)
        self.assertIs(rec.severity, Severity.NOTICE, "消音是提示级，不是紧急")

    def test_没有活动报警时消音也会记但文案不同(self) -> None:
        """按了但没东西可关 —— 也要记，**但必须与'关掉了一个真报警'分得开**。

        两句混成一句的话，后台会把"随手按了一下"误读成"老人响应了一次报警"。
        """
        self._tick()
        self._press_and_tick("sos_button")

        rec = self._events_of(AlarmCode.ALARM_SILENCED)[0]
        self.assertEqual(rec.detail["active_alarms"], [])
        self.assertIn("没有活动报警", rec.message)
        self.assertNotIn("正在报警", rec.message)

    def test_消音消息本身不发声(self) -> None:
        """★ 反向钉子：让"别再响了"的那条记录**自己不许响**。"""
        self._tick()
        self.rt.set_ambient(temperature_c=35.0)
        self._tick(advance=3.0)
        before = self._beeps()
        self._press_and_tick("sos_button")
        self.assertEqual(self._beeps(), before, "消音动作本身不该再叫一声")


class TestMeasureRecorded(_Base):
    """血氧测量结果 ⇒ **后台必须能看到记录**（用户 2026-10-01 要求）。"""

    def _measure(self) -> None:
        """主动测一次（不必等叫人），并让窗口正常结束。"""
        self.rt.tick()
        self._press_and_tick("spo2_button")
        self.rt.set_vitals(heart_rate=68.0, spo2=97.0)
        self._tick(advance=1.0)
        self._tick(advance=MEASURE_S)

    def test_成功时记下心率血氧与有效读数个数(self) -> None:
        self._measure()
        rec = self._events_of(AlarmCode.SPO2_MEASURED)[-1]
        # ⚠️ 别硬写 68.0 —— 模拟器会给读数加一点抖动。真正要钉的是**一致性**：
        #    消息里记的数必须与面板上显示的 `last_result` **逐字段相同**
        #    （两处不一致 = 后台与屏上各说一套，那比没记录更糟）。
        last = self.rt.spo2_status(self.clock())["last_result"]
        self.assertIsNotNone(last, "前置条件：测量应当已有结果")
        self.assertTrue(rec.detail["ok"])
        self.assertEqual(rec.detail["heart_rate_bpm"], last["heart_rate_bpm"])
        self.assertEqual(rec.detail["spo2_percent"], last["spo2_percent"])
        self.assertEqual(rec.value, last["spo2_percent"])
        self.assertEqual(rec.unit, "%")
        self.assertGreater(rec.detail["valid_samples"], 0, "有效读数个数要如实记下来")
        # 文案里的数要与记下的数对上（后台看到的和屏上看到的是同一个数）
        self.assertIn("%.0f" % last["spo2_percent"], rec.message)
        self.assertEqual(rec.source, "spo2")

    def test_失败也要记一条(self) -> None:
        """★ **失败也要记**：不记的话后台会一直显示上一次的成功值，
        让人以为"这次也测到了" —— 那是比"没数据"更坏的误导。"""
        self.rt.tick()
        self._press_and_tick("spo2_button")
        self.rt.set_vitals(finger=False)                  # 手指没贴好
        self._tick(advance=MEASURE_S + 1.0)

        rec = self._events_of(AlarmCode.SPO2_MEASURED)[-1]
        self.assertFalse(rec.detail["ok"])
        self.assertIsNone(rec.detail["spo2_percent"])
        self.assertIsNone(rec.value)
        self.assertEqual(rec.detail["valid_samples"], 0)
        self.assertIn("未取得有效读数", rec.message)

    def test_测量记录不会盖掉屏上的结果(self) -> None:
        """记录是"顺手记一笔"，屏上该显示的结果还得在。"""
        self._measure()
        text = " ".join(self._lcd())
        self.assertIn("68", text)
        self.assertIn("97", text)


class TestSosStillRecorded(_Base):
    """长按 A ⇒ SOS 仍然**既下发又入库**（原来就是对的，钉住别被改坏）。"""

    def test_长按A触发求救并留下事件(self) -> None:
        self._tick()
        before = self._beeps()
        self._press_and_tick("sos_button", ButtonAction.LONG_PRESS)

        self.assertIn(AlarmCode.SOS_PRESSED, [e.code for e in self.rt.recent_events()],
                      "SOS 必须在消息流里")
        # ⚠️ SOS **不走规则引擎**（`sos()` 直接下发），所以它不在 `engine.active_alarms()` 里 ——
        #    它进的是**调度器**的活动列表（手机端 `live` 那一栏看的就是它）。
        live = [e.get("code") for e in self.rt.dispatcher.recent(10)]
        self.assertIn("sos_pressed", live, "SOS 必须真的下发（进调度器的活动列表）")
        self.assertGreater(self._beeps(), before, "SOS 必须响")
        self.assertEqual(self._led(), "red", "SOS 必须亮红灯")

    def test_SOS不许被当成记录类(self) -> None:
        """★ 反向钉子：SOS 若落进 `RECORD_ONLY_CODES`，它就**不响不亮**了。"""
        self.assertNotIn(AlarmCode.SOS_PRESSED, RECORD_ONLY_CODES)
        self.assertEqual(message_kind(AlarmCode.SOS_PRESSED), "alarm")

    def test_SOS前的解除静音不算一次消音(self) -> None:
        """`sos()` 里会先 `dispatcher.unsilence()`（求救必须能响）——
        那一步**不是**消音，不许凭空多出一条 `alarm_silenced`。"""
        self._tick()
        self.rt.silence(self.clock(), by="button")        # 先消音
        before = len(self._events_of(AlarmCode.ALARM_SILENCED))
        self.rt.sos(self.clock())                          # 求救会解除静音
        self.assertEqual(len(self._events_of(AlarmCode.ALARM_SILENCED)), before,
                         "SOS 里的'解除静音'被误记成了一次消音")


class TestRecordOnlyNeverDispatches(_Base):
    """★ 本组最要紧的一条：记录类事件**绝不下发**（不点灯/不发声/不刷屏）。"""

    def test_记录事件没有任何输出侧效果(self) -> None:
        self._tick()
        dispatched: list = []
        real = self.rt.dispatcher.dispatch
        self.rt.dispatcher.dispatch = lambda *a, **k: (dispatched.append(a), real(*a, **k))[1]

        beeps0, lcd0, tft0, led0 = self._beeps(), self._lcd(), self._tft(), self._led()

        self.rt._record_event(AlarmEvent(
            ts=self.clock(), code=AlarmCode.SPO2_MEASURED, severity=Severity.NOTICE,
            message="血氧测量完成：心率 68 bpm、血氧 97 %（146 个有效读数）",
            value=97.0, unit="%", source="spo2",
        ))

        self.assertEqual(dispatched, [], "记录类事件绝不许走 dispatcher.dispatch()")
        self.assertEqual(self._beeps(), beeps0, "记录类事件不许发声")
        self.assertEqual(self._lcd(), lcd0, "记录类事件不许刷 LCD")
        self.assertEqual(self._tft(), tft0, "记录类事件不许刷 TFT")
        self.assertEqual(self._led(), led0, "记录类事件不许点灯")
        # ……但它必须**真的被记下来了**（否则"只记录"就变成"什么也没做"）
        self.assertIn(AlarmCode.SPO2_MEASURED, [e.code for e in self.rt.recent_events()])

    def test_记录事件不打断正在显示的报警(self) -> None:
        """最要命的场景：报警正占着屏时来一条记录 —— 报警必须还在屏上、灯还得是红的。"""
        self._tick()
        self.rt.set_ambient(temperature_c=35.0)
        self._tick(advance=3.0)
        lcd0, tft0, led0 = self._lcd(), self._tft(), self._led()

        self.rt._record_event(AlarmEvent(
            ts=self.clock(), code=AlarmCode.ALARM_SILENCED, severity=Severity.NOTICE,
            message="老人按下消音键（当时正在报警：ambient_temp_high）",
            source="button", detail={"by": "button", "active_alarms": ["ambient_temp_high"]},
        ))

        self.assertEqual(self._lcd(), lcd0, "记录类事件把正在显示的报警从 LCD 上刷掉了")
        self.assertEqual(self._tft(), tft0, "记录类事件把正在显示的报警从 TFT 上刷掉了")
        self.assertEqual(self._led(), led0, "记录类事件把报警的灯状态改掉了")


class TestMessagesApi(_Base):
    """`GET /api/v1/messages`：报警 + 老人动作 + 测量记录的统一流。"""

    def setUp(self) -> None:
        super().setUp()
        self.api = WebApi(self.rt)

    def _messages(self, limit=None):
        query = {} if limit is None else {"limit": [str(limit)]}
        status, payload = self.api.handle("GET", "/api/v1/messages", query, {})
        self.assertEqual(status, 200)
        return payload

    def test_消音与测量都出现在消息流里(self) -> None:
        self._tick()
        self._press_and_tick("sos_button")                 # 消音
        self.rt.tick()
        self._press_and_tick("spo2_button")                # 测一次
        self.rt.set_vitals(heart_rate=68.0, spo2=97.0)
        self._tick(advance=1.0)
        self._tick(advance=MEASURE_S)

        payload = self._messages()
        codes = [m["code"] for m in payload["messages"]]
        self.assertIn("alarm_silenced", codes)
        self.assertIn("spo2_measured", codes)
        self.assertEqual(payload["count"], len(payload["messages"]))

    def test_kind字段正确(self) -> None:
        self._tick()
        self._press_and_tick("sos_button")
        by_code = {m["code"]: m["kind"] for m in self._messages()["messages"]}
        self.assertEqual(by_code.get("alarm_silenced"), "record")

    def test_报警在消息流里的kind是alarm(self) -> None:
        self._tick()
        self.rt.set_ambient(temperature_c=35.0)
        self._tick(advance=3.0)
        by_code = {m["code"]: m["kind"] for m in self._messages()["messages"]}
        self.assertEqual(by_code.get("ambient_temp_high"), "alarm")

    def test_时间正序且limit生效(self) -> None:
        self._tick()
        for _ in range(4):
            self._press_and_tick("sos_button", advance=0.5)
        msgs = self._messages()["messages"]
        ts = [m["ts"] for m in msgs]
        self.assertEqual(ts, sorted(ts), "消息流必须按时间正序（新的在后）")

        limited = self._messages(limit=2)["messages"]
        self.assertLessEqual(len(limited), 2)
        if limited:
            self.assertEqual(limited[-1]["ts"], msgs[-1]["ts"], "limit 应当保留**最新**的那些")

    def test_字段齐全且可JSON序列化(self) -> None:
        import json
        self._tick()
        self._press_and_tick("sos_button")
        payload = self._messages()
        json.dumps(payload, ensure_ascii=False)            # 不抛就算过
        for m in payload["messages"]:
            for key in ("ts", "code", "kind", "severity", "message", "source",
                        "value", "unit", "detail"):
                self.assertIn(key, m, f"消息缺字段 {key}")

    def test_后台点消音记的是api来源(self) -> None:
        """`by` 必须能区分"老人按的实体键"与"后台/手机端点掉的"。"""
        self._tick()
        status, _ = self.api.handle("POST", "/api/v1/silence", {}, {})
        self.assertEqual(status, 200)
        rec = [m for m in self._messages()["messages"] if m["code"] == "alarm_silenced"][-1]
        self.assertEqual(rec["source"], "api")
        self.assertEqual(rec["detail"]["by"], "api")
        self.assertIn("后台", rec["message"])


    def test_消息接口受token保护(self) -> None:
        """⚠️ 鉴权在**路由分派之前**统一检查 —— 这条断言它对**新接口**同样生效。

        否则新加的接口会变成"绕过 token 的后门"：既有的读接口要令牌、新接口不要，
        而它照样能读到全部消息（含老人什么时候按了消音）。
        """
        secured = WebApi(self.rt, token="s3cret")
        status, _ = secured.handle("GET", "/api/v1/messages", {}, {})
        self.assertEqual(status, 401)
        status, payload = secured.handle("GET", "/api/v1/messages", {},
                                         {"x-auth-token": "s3cret"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])


class TestMessagesApiWithStore(TestMessagesApi):
    """同一批断言，再走一遍**真历史库**那条取数路径（`/api/v1/messages` 的落库分支）。"""

    use_store = True


class TestStatusPageMessages(_Base):
    """状态页要**能看见**这些记录（完整"消息页"UI 属于后面"三个面板"那一轮）。"""

    def test_页面有最近消息一节并显示消音记录(self) -> None:
        self._tick()
        self._press_and_tick("sos_button")
        page = webui.render_page(self.rt)
        self.assertIn("最近消息", page)
        self.assertIn("已消音", page)                       # ALARM_LABELS 的标签
        self.assertIn("老人按下消音键", page)                # 消息原文

    def test_页面显示测量记录(self) -> None:
        self.rt.tick()
        self._press_and_tick("spo2_button")
        self.rt.set_vitals(heart_rate=68.0, spo2=97.0)
        self._tick(advance=1.0)
        self._tick(advance=MEASURE_S)
        page = webui.render_page(self.rt)
        self.assertIn("血氧测量完成", page)

    def test_没有把原来的卡片与两张表弄坏(self) -> None:
        """★ 反向钉子：这一节是**加**上去的，别把原页面挤掉。"""
        self._tick()
        page = webui.render_page(self.rt)
        for must in ("当前报警态", "设备状态", "最近报警", "最近消息", "心率", "血氧"):
            self.assertIn(must, page, f"状态页少了 {must}")

    def test_消息为空时给出人话(self) -> None:
        self._tick()
        page = webui.render_page(self.rt)
        self.assertIn("最近消息", page)
        self.assertNotIn("None", page)


class TestMessagesOverRealHttp(_Base):
    """★ 再用**真实 HTTP**（真 socket，不是直接调 `WebApi.handle`）走一遍。

    为什么非要有这一层（本项目踩过）：加配置面板时，`_CURRENT_BODY` 是 `threading.local`、
    在服务器分支里被写成了 `.get()`（它**没有**这个方法）—— **直接调 `handle()` 的测试全绿**，
    只有走真 socket 的才炸。⇒ **单测的调用方式不等于真实调用方式。**
    """

    def setUp(self) -> None:
        super().setUp()
        self.server = self.rt.start_http(host="127.0.0.1", port=0)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def _get(self, path: str):
        with urllib.request.urlopen(f"{self.base}{path}", timeout=5) as resp:  # noqa: S310
            return resp.status, resp.read().decode("utf-8"), resp.headers.get("Content-Type", "")

    def test_真HTTP上消息接口返回JSON(self) -> None:
        self._tick()
        self._press_and_tick("sos_button")
        status, text, ctype = self._get("/api/v1/messages")
        self.assertEqual(status, 200)
        self.assertIn("application/json", ctype)
        payload = json.loads(text)
        self.assertTrue(payload["ok"])
        self.assertIn("alarm_silenced", [m["code"] for m in payload["messages"]])

    def test_真HTTP上limit参数生效(self) -> None:
        self._tick()
        for _ in range(3):
            self._press_and_tick("sos_button", advance=0.5)
        _, text, _ = self._get("/api/v1/messages?limit=1")
        self.assertEqual(json.loads(text)["count"], 1)

    def test_真HTTP上状态页含最近消息(self) -> None:
        status, text, ctype = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        self.assertIn("最近消息", text)

    def test_真HTTP上消息为空也不吐None(self) -> None:
        status, text, _ = self._get("/api/v1/messages")
        self.assertEqual(status, 200)
        self.assertNotIn("None", text)
