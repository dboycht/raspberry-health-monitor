"""Web 配置面板的 API 测试：``GET/POST /api/v1/config`` + **热应用** + ``/panel`` 页面。

为什么这组测试非有不可
----------------------
这个功能能在**运行中**改掉报警阈值与器件开关，是本项目里唯一"一边跑一边改自己"的入口。
四条最容易出事、也最难靠肉眼发现的路径，各自都要有机器判据：

1. **改完必须当场生效**（而不是"等重启"）—— 判据是"规则引擎用新阈值报出了警"；
2. **校验不过必须一个字节都不写**（否则会把服务改到起不来）；
3. **关掉的器件必须真的停下来**（只是把它标成 False 而还在采集，是最阴的一种失败）；
4. **打不开的器件不能让整次保存失败**（没接线是常态），但也不能**假装成功**。
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

from health_monitor.core.config import AppConfig, Thresholds  # noqa: E402
from health_monitor.core.configstore import ConfigStore  # noqa: E402
from health_monitor.hal.models import AlarmCode  # noqa: E402
from health_monitor.net.web import WebApi  # noqa: E402
from health_monitor.net.webui import THRESHOLD_GROUPS, render_panel  # noqa: E402
from health_monitor.playback import PlaybackRuntime  # noqa: E402

DEMO = {
    "thresholds": {
        "hr_min": 50, "hr_max": 110, "spo2_min": 93,
        "no_motion_timeout_s": 1800, "repeat_cooldown_s": 0,
    },
    "devices": {
        "vitals": {"driver": "max30102", "read_interval_s": 1.0},
        "ambient": {"driver": "dht11", "read_interval_s": 3.0, "params": {"pin": 4}},
        "display": {"driver": "lcd1602", "read_interval_s": 1.0},
        "status_led": {
            "driver": "led", "read_interval_s": 1.0,
            "params": {"pins": {"green": 22, "yellow": 23, "red": 12}},
        },
        "spo2_button": {"driver": "button", "read_interval_s": 0.2, "params": {"pin": 13}},
        "sos_button": {"driver": "button", "read_interval_s": 0.2, "params": {"pin": 27}},
        # 驱动名不存在、且**默认关闭**：用来验证"启用后打不开 ⇒ warnings + 仍然 200"
        "ghost": {"driver": "definitely_not_a_driver", "enabled": False, "read_interval_s": 1.0},
    },
    "mqtt": {"enabled": False, "host": "keep-me"},
}


class _Clock:
    def __init__(self, t: float = 1_790_000_000.0) -> None:
        self.t = float(t)

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += float(seconds)


class _ApiCase(unittest.TestCase):
    """公共装置：一份真临时配置文件 + 回放运行时 + 注入了 ConfigStore 的 WebApi。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "devices.json"
        self.path.write_text(json.dumps(DEMO, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        self.clock = _Clock()
        self.rt = PlaybackRuntime(
            AppConfig.from_dict(DEMO),
            clock=self.clock,
            sleep=lambda _s: None,
            verbose_outputs=False,
            config_path=str(self.path),
        )
        # ⚠️ 注入 use_local=False 的 store：测试必须能控制自己的环境，
        # 否则断言会随"本机有没有 config/devices.local.json"而变（E56 那一类坑）。
        self.rt.config_store = ConfigStore(self.path, use_local=False, clock=self.clock)
        self.rt.open()
        self.addCleanup(self.rt.close)
        self.api = WebApi(self.rt)

    # ---------- 便捷调用 ----------

    def post(self, patch: object, api: WebApi | None = None):
        body = patch if isinstance(patch, bytes) else json.dumps(patch).encode("utf-8")
        return (api or self.api).handle("POST", "/api/v1/config", {}, {}, body=body)

    def get(self, api: WebApi | None = None):
        return (api or self.api).handle("GET", "/api/v1/config", {}, {})

    def raw(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))


class TestReadConfig(_ApiCase):
    def test_读出全部阈值与设备(self) -> None:
        status, payload = self.get()
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        for name in Thresholds.__dataclass_fields__:
            self.assertIn(name, payload["thresholds"], f"少返回了阈值 {name}")
        self.assertEqual(payload["thresholds"]["spo2_min"], 93)
        self.assertEqual(payload["devices"]["ambient"]["driver"], "dht11")
        self.assertEqual(payload["devices"]["ambient"]["read_interval_s"], 3.0)
        self.assertFalse(payload["devices"]["ghost"]["enabled"])

    def test_没有绑定配置文件时明确拒绝(self) -> None:
        """没绑定配置文件（启动方式不对）要说清原因，而不是含糊地 500。"""
        self.rt.config_store = None
        status, payload = self.get()
        self.assertEqual(status, 400)
        self.assertIn("配置文件", payload["error"])


class TestThresholdHotApply(_ApiCase):
    def test_改阈值后规则引擎当场用新值(self) -> None:
        """★ 核心判据：**不重启**，改完下一次判定就用新阈值。"""
        self.rt.set_vitals(heart_rate=72.0, spo2=97.0)
        self.rt.tick()
        self.assertEqual(self.rt.engine.active_alarms(), {}, "下限 93 时 97 不该报警（A 组）")

        status, payload = self.post({"thresholds": {"spo2_min": 99}})
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["changed"], ["thresholds.spo2_min: 93 → 99"])

        self.rt.tick()
        self.assertIn(
            AlarmCode.SPO2_TOO_LOW.value, self.rt.engine.active_alarms(),
            "阈值改完必须**当场**生效（B 组：同一样本，这次该报警）",
        )

    def test_热应用后采集器与规则引擎看到同一份配置(self) -> None:
        """采集器会读 `config.thresholds.sensor_fault_after`：它的引用也必须换掉。"""
        self.post({"thresholds": {"sensor_fault_after": 7}})
        self.assertIs(self.rt.collector.config, self.rt.config)
        self.assertEqual(self.rt.collector.config.thresholds.sensor_fault_after, 7)


class TestRejectsWithoutWriting(_ApiCase):
    """★ 校验失败 ⇒ 400 **且文件一个字节都不改**。"""

    def _reject(self, patch: object, needle: str = "") -> str:
        before = self.path.read_bytes()
        status, payload = self.post(patch)
        self.assertEqual(status, 400, payload)
        self.assertFalse(payload["ok"])
        self.assertEqual(self.path.read_bytes(), before, "校验失败绝不能碰文件")
        self.assertEqual(list(self.path.parent.glob("*.bak-*")), [], "失败时不该留下备份文件")
        if needle:
            self.assertIn(needle, payload["error"])
        return str(payload["error"])

    def test_阈值不自洽被拒绝(self) -> None:
        self._reject({"thresholds": {"hr_min": 120}}, "hr_min")

    def test_血氧下限越界被拒绝(self) -> None:
        self._reject({"thresholds": {"spo2_min": 0}})

    def test_未知阈值字段被拒绝(self) -> None:
        self._reject({"thresholds": {"nope": 1}}, "不认识的字段")

    def test_未知设备被拒绝(self) -> None:
        self._reject({"devices": {"nope": {"enabled": False}}}, "没有设备")

    def test_引脚参数不允许通过接口改(self) -> None:
        """★ 接口不提供改引脚的能力。

        理由：① 用户选定的范围是"阈值 + 开关"（"引脚等底层项"那一项没选）；
        ② 用户同时选了"写操作不设防" —— 两者放一起意味着**同一个 Wi-Fi 下任何人都能改硬件接线**。
        **"不设防"与"能改硬件"不能同时成立。**
        """
        self._reject({"devices": {"spo2_button": {"params": {"pin": 4}}}}, "不支持的字段")

    def test_请求体不是JSON被拒绝(self) -> None:
        status, payload = self.post(b"{not json")
        self.assertEqual(status, 400)
        self.assertIn("JSON", payload["error"])

    def test_缺少请求体被拒绝(self) -> None:
        status, payload = self.api.handle("POST", "/api/v1/config", {}, {})
        self.assertEqual(status, 400)
        self.assertIn("请求体", payload["error"])


class TestDeviceToggle(_ApiCase):
    def test_关掉器件后不再被采集(self) -> None:
        self.assertIn("ambient", self.rt.collector.entries)
        status, payload = self.post({"devices": {"ambient": {"enabled": False}}})
        self.assertEqual(status, 200, payload)

        self.assertNotIn("ambient", self.rt.collector.entries, "必须从采集调度里摘掉")
        self.assertNotIn("ambient", self.rt.devices, "必须从设备表里摘掉")
        self.assertNotIn("ambient", self.rt.collector.read_counts)

        # 推着时钟跑几帧：别的设备照常被读，但 **ambient 不能再出现**
        # （只把它标成 False、却还在采集，是这类功能最阴的一种失败）
        self.clock.advance(10.0)
        self.rt.tick()
        self.assertNotIn("ambient", self.rt.collector.read_counts, "关掉后不该再被采集")

    def test_重新打开后会被采集(self) -> None:
        self.post({"devices": {"ambient": {"enabled": False}}})
        status, payload = self.post({"devices": {"ambient": {"enabled": True}}})
        self.assertEqual(status, 200, payload)

        self.assertIn("ambient", self.rt.collector.entries)
        self.assertIn("ambient", self.rt.devices)
        self.clock.advance(10.0)
        self.rt.tick()
        self.assertGreater(self.rt.collector.read_counts.get("ambient", 0), 0, "打开后要真的读它")

    def test_已启用但没跑起来的器件在下次保存时会重试(self) -> None:
        """★ 装配失败的器件**不在 `self.devices` 里**。

        如果开关判断按"上一份配置的 enabled 差异"来做，它在面板上会永远显示"已启用"、
        却永远接不上 —— 只能重启服务才能再试一次。按**实际运行状态**对齐就没这个问题：
        **修好接线再点一次保存**就把它接回来了。
        """
        from health_monitor.hal.models import Sample

        class _OkDevice:
            def __init__(self, **kw: object) -> None:
                self.name = kw.get("name", "ghost")
                self.mock = True

            def open(self) -> None: ...
            def close(self) -> None: ...
            def read(self) -> Sample:
                return Sample(device=self.name)

            def status(self) -> dict:
                return {"name": self.name}

        # ① 按配置启用它：驱动名根本不存在 ⇒ 装配失败（正是要覆盖的场景）
        status, payload = self.post({"devices": {"ghost": {"enabled": True}}})
        self.assertEqual(status, 200, payload)
        self.assertIn("ghost", self.rt.assembly_errors)
        self.assertNotIn("ghost", self.rt.devices)

        # ② "接线修好了" ⇒ 下一次保存（哪怕只改了别的）就该把它接上
        real = self.rt._device_factory  # noqa: SLF001

        def factory(driver: str, params=None, mock: bool = True, name: str = ""):
            if name == "ghost":
                return _OkDevice(name=name)
            return real(driver, params=params, mock=mock, name=name)

        self.rt._device_factory = factory  # noqa: SLF001
        status, payload = self.post({"thresholds": {"spo2_min": 92}})
        self.assertEqual(status, 200, payload)
        self.assertIn("ghost", self.rt.devices, "修好后一次保存就该接回来")
        self.assertNotIn("ghost", self.rt.assembly_errors)

    def test_关掉输出器件会从下发名单里摘掉(self) -> None:
        """★ 只改 runtime.outputs 而漏了 dispatcher，会出现"面板关了屏、报警却还在写屏"。"""
        self.assertIn("display", self.rt.dispatcher.outputs)
        status, _ = self.post({"devices": {"display": {"enabled": False}}})
        self.assertEqual(status, 200)
        self.assertNotIn("display", self.rt.outputs)
        self.assertNotIn("display", self.rt.dispatcher.outputs)

    def test_输出器件也能重新打开并接回下发名单(self) -> None:
        self.post({"devices": {"display": {"enabled": False}}})
        self.post({"devices": {"display": {"enabled": True}}})
        self.assertIn("display", self.rt.dispatcher.outputs)

    def test_改读取周期会更新采集条目(self) -> None:
        status, payload = self.post({"devices": {"ambient": {"read_interval_s": 5.0}}})
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.rt.collector.entries["ambient"].interval, 5.0)
        self.assertEqual(self.rt.config.device("ambient").read_interval_s, 5.0)

    def test_调节周期不会把下次读取推后(self) -> None:
        """把周期从 3 秒改成 5 秒时，已经排好的"马上到期"不能被推到 5 秒之后。"""
        entry = self.rt.collector.entries["ambient"]
        entry.next_due_ts = self.clock() + 0.1
        self.post({"devices": {"ambient": {"read_interval_s": 5.0}}})
        self.assertLessEqual(entry.next_due_ts - self.clock(), 0.1 + 1e-9)


class TestDeviceOpenFailure(_ApiCase):
    def test_打不开只给warnings且仍然200(self) -> None:
        """★ 没接线是常态：**配置照样保存**，问题如实放进 warnings。"""
        status, payload = self.post({"devices": {"ghost": {"enabled": True}}})
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["warnings"], "打不开必须给出 warning，不能假装成功")
        self.assertIn("ghost", " ".join(payload["warnings"]))
        self.assertIn("ghost", self.rt.assembly_errors, "要标成'已启用但装配失败'")
        self.assertNotIn("ghost", self.rt.collector.entries, "打不开的器件不该进采集调度")
        self.assertNotIn("ghost", self.rt.devices)
        self.assertTrue(self.raw()["devices"]["ghost"]["enabled"], "配置本身是合法的，应当写盘")

    def test_打开失败时会把它关掉不留半个设备(self) -> None:
        closed: list = []

        class _Exploding:
            def __init__(self, **_kw: object) -> None:
                self.name = "ghost"

            def open(self) -> None:
                raise RuntimeError("没接线")

            def close(self) -> None:
                closed.append("ghost")

        real_factory = self.rt._device_factory  # noqa: SLF001 - 这里正是在测工厂失败的分支

        def factory(driver: str, params=None, mock: bool = True, name: str = ""):
            if name == "ghost":
                return _Exploding()
            return real_factory(driver, params=params, mock=mock, name=name)

        self.rt._device_factory = factory  # noqa: SLF001
        status, payload = self.post({"devices": {"ghost": {"enabled": True}}})

        self.assertEqual(status, 200)
        self.assertTrue(payload["warnings"])
        self.assertEqual(closed, ["ghost"], "打开失败也必须 close()，不能留半个设备在里面")


class TestPersistence(_ApiCase):
    def test_落盘是原子的且带备份(self) -> None:
        before = self.path.read_text(encoding="utf-8")
        status, payload = self.post({"thresholds": {"spo2_min": 92}})
        self.assertEqual(status, 200, payload)

        self.assertEqual(self.raw()["thresholds"]["spo2_min"], 92, "新值要落盘")
        backup = payload["backup"]
        self.assertTrue(backup and Path(backup).exists(), "必须留下备份")
        self.assertEqual(Path(backup).read_text(encoding="utf-8"), before, "备份是改动前的原文")
        self.assertEqual(list(self.path.parent.glob("*.tmp-*")), [], "不能留临时文件")

    def test_没有变化时不写盘(self) -> None:
        status, payload = self.post({"thresholds": {"spo2_min": 93}})
        self.assertEqual(status, 200)
        self.assertEqual(payload["changed"], [])
        self.assertEqual(payload["backup"], "")
        self.assertEqual(list(self.path.parent.glob("*.bak-*")), [], "没变化不该产生备份")

    def test_把面板原样提交回来不写盘(self) -> None:
        """★ 面板"什么都没动就点保存"（它会提交所有阈值与所有设备的开关/周期）必须是 no-op。

        设备配置里没写的键等于默认值；若拿"文件里有没有写"去比，
        每次保存都会给每个设备补上 `enabled: true` / `read_interval_s: 1.0`。
        """
        before = self.path.read_bytes()
        snap = self.get()[1]
        patch = {
            "thresholds": snap["thresholds"],
            "devices": {
                name: {"enabled": info["enabled"], "read_interval_s": info["read_interval_s"]}
                for name, info in snap["devices"].items()
            },
        }
        status, payload = self.post(patch)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["changed"], [], "原样提交不该报出改动")
        self.assertEqual(payload["backup"], "", "原样提交不该写盘")
        self.assertEqual(self.path.read_bytes(), before, "文件内容必须一个字节都没变")

    def test_不改动本机覆盖文件(self) -> None:
        """面板只写基础配置；`devices.local.json`（密钥/本机引脚）**绝不能**被碰。"""
        local = Path(_RPI_DIR) / "config" / "devices.local.json"
        before = local.read_bytes() if local.exists() else None
        self.post({"thresholds": {"spo2_min": 92}})
        after = local.read_bytes() if local.exists() else None
        self.assertEqual(before, after, "本机覆盖文件被改动了 —— 那里面有密钥")


class TestTokenAuth(_ApiCase):
    def test_写配置不带token被拒绝且文件不变(self) -> None:
        before = self.path.read_bytes()
        api = WebApi(self.rt, token="secret")
        status, payload = self.post({"thresholds": {"spo2_min": 92}}, api=api)
        self.assertEqual(status, 401, "写操作在启用 token 时必须被拒（与其它接口统一用 401）")
        self.assertFalse(payload["ok"])
        self.assertEqual(self.path.read_bytes(), before, "被拒时绝不能改文件")

    def test_写配置带token可以成功(self) -> None:
        api = WebApi(self.rt, token="secret")
        body = json.dumps({"thresholds": {"spo2_min": 92}}).encode("utf-8")
        status, payload = api.handle(
            "POST", "/api/v1/config", {}, {"x-auth-token": "secret"}, body=body,
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.raw()["thresholds"]["spo2_min"], 92)

    def test_读接口的既有行为不变(self) -> None:
        """新增写接口**没有**把 token 保护改弱，也没改读接口的语义（仍是 401）。"""
        api = WebApi(self.rt, token="secret")
        status, _ = api.handle("GET", "/api/v1/config", {}, {})
        self.assertEqual(status, 401)
        status, _ = api.handle("GET", "/api/v1/current", {}, {})
        self.assertEqual(status, 401)


class TestPanelPage(_ApiCase):
    def test_阈值分组覆盖了每一个阈值字段(self) -> None:
        """★ 漏一个字段 ⇒ 面板上永远改不了它，而且**从界面上看不出少了什么**。"""
        covered = [field for _title, fields in THRESHOLD_GROUPS for field, _label, _unit in fields]
        self.assertEqual(sorted(covered), sorted(Thresholds.__dataclass_fields__))
        self.assertEqual(len(covered), len(set(covered)), "同一个字段不能在面板上出现两次")

    def test_面板渲染出未设防提示与全部控件(self) -> None:
        html = render_panel(self.rt, self.rt.config_store, secured=False)
        self.assertIn("未设防", html)
        self.assertIn("data-th=\"spo2_min\"", html)
        self.assertIn("data-th=\"spo2_remind_interval_s\"", html)
        self.assertIn("data-dev=\"ambient\"", html)
        self.assertIn("data-field=\"enabled\"", html)
        self.assertIn("/api/v1/config", html)
        self.assertNotIn("http://cdn", html, "不能依赖外部 CDN（现场可能没有外网）")

    def test_面板不暴露引脚参数(self) -> None:
        html = render_panel(self.rt, self.rt.config_store, secured=False)
        self.assertNotIn("data-th=\"pin\"", html)
        self.assertNotIn("params", html)

    def test_设了token时面板自身也要求令牌(self) -> None:
        from health_monitor.net.web import _render_panel_page  # noqa: PLC0415

        api = WebApi(self.rt, token="secret")
        status, html = _render_panel_page(api)
        self.assertEqual(status, 401)
        self.assertIn("需要访问令牌", html)

    def test_状态页给出面板入口(self) -> None:
        from health_monitor.net.webui import render_page  # noqa: PLC0415

        self.assertIn('href="/panel"', render_page(self.rt))


class TestPanelOverRealHttp(_ApiCase):
    """★ 再用**真实 HTTP**（真 socket，不是直接调 ``WebApi.handle``）走一遍。

    为什么非要有这一层：加这个功能时就踩到了 —— ``_CURRENT_BODY`` 是个
    ``threading.local``，而服务器分支里被写成了 ``.get()``（它**没有**这个方法）。
    **直接调 ``handle()`` 的那些测试全绿**，只有走真 socket 的测试才炸。
    ⇒ 教训：单测的调用方式**不等于**真实调用方式；协议边界上要各测一遍。
    """

    def setUp(self) -> None:
        super().setUp()
        self.server = self.rt.start_http(host="127.0.0.1", port=0)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def _http_get(self, path: str):
        with urllib.request.urlopen(f"{self.base}{path}", timeout=5) as resp:  # noqa: S310
            return resp.status, resp.read().decode("utf-8"), resp.headers.get("Content-Type", "")

    def _http_post_json(self, path: str, payload: object):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base}{path}", data=data, method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_真HTTP上POST的请求体确实传到了处理函数(self) -> None:
        status, body = self._http_post_json("/api/v1/config", {"thresholds": {"spo2_min": 92}})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"])
        self.assertEqual(self.raw()["thresholds"]["spo2_min"], 92)

    def test_真HTTP上GET配置返回JSON(self) -> None:
        status, text, ctype = self._http_get("/api/v1/config")
        self.assertEqual(status, 200)
        self.assertIn("application/json", ctype)
        self.assertEqual(json.loads(text)["thresholds"]["spo2_min"], 93)

    def test_真HTTP上非法配置仍然400且不写盘(self) -> None:
        before = self.path.read_bytes()
        status, body = self._http_post_json("/api/v1/config", {"thresholds": {"spo2_min": 0}})
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_真HTTP上panel返回HTML(self) -> None:
        status, text, ctype = self._http_get("/panel")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        self.assertIn("配置面板", text)
        self.assertIn("未设防", text)

    def test_真HTTP上状态页给出面板入口(self) -> None:
        status, text, _ctype = self._http_get("/")
        self.assertEqual(status, 200)
        self.assertIn('href="/panel"', text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
