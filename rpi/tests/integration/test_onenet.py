"""OneNET 接入测试（**不需要 paho-mqtt，也不需要真的连云**）。

覆盖四层：
1. **token 算法**：md5/sha1/sha256 手算对照、URL 编码、资源格式、非法入参报错；
2. **配置校验**：必填项、keepalive 平台限制（10~1800）、topic 模板必须含 {pid}/{device}、
   密钥优先取环境变量；
3. **数据点报文**：``{"id":…, "dp":{流:[{v,t}]}}`` 结构、``None`` 字段整条跳过、
   时间戳取整、数据流名映射；
4. **上报链路**：用假 paho 注入，断言 topic 与 payload 真的发到了，
   以及"平台拒绝(rejected)"能被记录成失败（这是"数据上没上去"的唯一硬证据）。

口径来源见 ``net/onenet.py`` 顶部（官方文档链接与"未实测"标注）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import types
import unittest
import urllib.parse
from typing import Any, Dict, List, Tuple

from health_monitor.net.onenet import (
    DEFAULT_HOST_PLAIN,
    DEFAULT_PORT_PLAIN,
    DEFAULT_STREAM_MAP,
    OneNetConfig,
    OneNetError,
    OneNetPublisher,
    device_resource,
    product_resource,
    sign_token,
)

# 官方文档《token生成示例 - python》里给出的样例密钥与参数（用于对照）
DOC_KEY = "KuF3NT/jUBJ62LNBB/A8XZA9CqS3Cu79B/ABmfA1UCw="
DOC_PID = "123123"
DOC_ET = 1537255523


# ---------------------------------------------------------------------------
# 假 paho（与 tests/integration/test_mqtt.py 同一手法：不依赖真 broker）
# ---------------------------------------------------------------------------


class FakePublishInfo:
    def __init__(self, rc: int = 0) -> None:
        self.rc = rc


class FakeClient:
    instances: List["FakeClient"] = []

    def __init__(self, client_id: str = "", clean_session: bool = True) -> None:
        self.client_id = client_id
        self.published: List[Tuple[str, str, int]] = []
        self.subscribed: List[Tuple[str, int]] = []
        self.credentials: Tuple[str, str] = ("", "")
        self.on_connect: Any = None
        self.on_disconnect: Any = None
        self.on_message: Any = None
        self.connected_to: Tuple[str, int, int] | None = None
        self.loop_started = False
        FakeClient.instances.append(self)

    def username_pw_set(self, username: str, password: str | None = None) -> None:
        self.credentials = (username, password or "")

    def connect_async(self, host: str, port: int, keepalive: int) -> None:
        self.connected_to = (host, port, keepalive)

    def loop_start(self) -> None:
        self.loop_started = True

    def loop_stop(self) -> None:
        self.loop_started = False

    def disconnect(self) -> None:
        pass

    def tls_set(self, *args: Any, **kwargs: Any) -> None:
        self.tls_used = True

    def subscribe(self, topic: str, qos: int = 0) -> None:
        self.subscribed.append((topic, qos))

    def publish(self, topic: str, message: str, qos: int = 0) -> FakePublishInfo:
        self.published.append((topic, message, qos))
        return FakePublishInfo(rc=0)


class FakeMessage:
    def __init__(self, topic: str, payload: Dict[str, Any]) -> None:
        self.topic = topic
        self.payload = json.dumps(payload).encode("utf-8")


def install_fake_paho() -> None:
    module = types.ModuleType("paho.mqtt.client")
    module.Client = FakeClient  # type: ignore[attr-defined]
    package = types.ModuleType("paho")
    package.mqtt = types.ModuleType("paho.mqtt")  # type: ignore[attr-defined]
    sys.modules["paho"] = package
    sys.modules["paho.mqtt"] = package.mqtt
    sys.modules["paho.mqtt.client"] = module


def uninstall_fake_paho() -> None:
    for name in ("paho.mqtt.client", "paho.mqtt", "paho"):
        sys.modules.pop(name, None)


def make_config(**overrides: Any) -> OneNetConfig:
    data = {
        "enabled": True,
        "product_id": DOC_PID,
        "device_name": "living-room-pi",
        "access_key": DOC_KEY,
        "method": "sha256",
    }
    data.update(overrides)
    return OneNetConfig.from_dict(data)


# ---------------------------------------------------------------------------
# 1. token 算法
# ---------------------------------------------------------------------------


class TestTokenAlgorithm(unittest.TestCase):
    def _manual_sign(self, res: str, et: int, method: str, version: str = "2018-10-31") -> str:
        """照官方文档公式**独立**手算一遍（不复用被测实现的任何逻辑）。"""
        org = f"{et}\n{method}\n{res}\n{version}"
        digest = hmac.new(base64.b64decode(DOC_KEY), org.encode(), digestmod=method).digest()
        return base64.b64encode(digest).decode()

    def test_三种签名方法与手算一致(self) -> None:
        res = product_resource(DOC_PID)
        for method in ("md5", "sha1", "sha256"):
            with self.subTest(method=method):
                token = sign_token(DOC_KEY, res, et=DOC_ET, method=method)
                fields = dict(part.split("=", 1) for part in token.split("&"))
                self.assertEqual(urllib.parse.unquote(fields["sign"]), self._manual_sign(res, DOC_ET, method))
                self.assertEqual(fields["et"], str(DOC_ET))
                self.assertEqual(fields["method"], method)
                self.assertEqual(fields["version"], "2018-10-31")
                self.assertEqual(urllib.parse.unquote(fields["res"]), res)

    def test_res与sign都做了URL编码(self) -> None:
        """斜杠与 base64 的 `=`、`+` 必须编码，否则 token 在 HTTP/MQTT 参数里会被截断。"""
        token = sign_token(DOC_KEY, device_resource(DOC_PID, "dev"), et=DOC_ET, method="sha256")
        self.assertIn("res=products%2F", token, "res 里的 / 必须编码成 %2F")
        self.assertIn("%3D", token, "sign 末尾的 = 必须编码成 %3D")
        self.assertNotIn("res=products/", token)

    def test_设备级res格式(self) -> None:
        self.assertEqual(device_resource("123", "abc"), "products/123/devices/abc")
        self.assertEqual(product_resource("123"), "products/123")

    def test_有效期默认一小时(self) -> None:
        token = sign_token(DOC_KEY, product_resource(DOC_PID), now=1000, method="sha1")
        fields = dict(part.split("=", 1) for part in token.split("&"))
        self.assertEqual(fields["et"], str(1000 + 3600))

    def test_非法method报错(self) -> None:
        with self.assertRaises(OneNetError) as ctx:
            sign_token(DOC_KEY, "products/1", method="sha512")
        self.assertIn("sha512", str(ctx.exception))

    def test_空密钥与空res报错(self) -> None:
        with self.assertRaises(OneNetError):
            sign_token("", "products/1")
        with self.assertRaises(OneNetError):
            sign_token(DOC_KEY, "")

    def test_非法base64密钥报错并给出提示(self) -> None:
        with self.assertRaises(OneNetError) as ctx:
            sign_token("这不是 base64!!!", "products/1")
        self.assertIn("base64", str(ctx.exception))


# ---------------------------------------------------------------------------
# 2. 配置校验
# ---------------------------------------------------------------------------


class TestOneNetConfig(unittest.TestCase):
    def test_默认关闭且不校验(self) -> None:
        cfg = OneNetConfig()
        self.assertFalse(cfg.enabled)
        cfg.validate()          # 未启用时不要求 product_id / key

    def test_启用后必填项缺失要报错(self) -> None:
        for missing, field in (({"product_id": ""}, "product_id"), ({"device_name": ""}, "device_name")):
            with self.subTest(field=field):
                data = {
                    "enabled": True, "product_id": DOC_PID, "device_name": "dev",
                    "access_key": DOC_KEY,
                }
                data.update(missing)
                cfg = OneNetConfig(**data)
                with self.assertRaises(OneNetError) as ctx:
                    cfg.validate()
                self.assertIn(field, str(ctx.exception))

    def test_缺密钥时提示用环境变量(self) -> None:
        cfg = OneNetConfig(enabled=True, product_id="1", device_name="d", access_key="")
        os.environ.pop("HEALTH_ONENET_KEY", None)
        with self.assertRaises(OneNetError) as ctx:
            cfg.validate()
        self.assertIn("HEALTH_ONENET_KEY", str(ctx.exception))

    def test_密钥优先取环境变量(self) -> None:
        cfg = OneNetConfig(enabled=True, product_id="1", device_name="d", access_key=DOC_KEY)
        os.environ["HEALTH_ONENET_KEY"] = "ZW52LWtleQ=="
        try:
            self.assertEqual(cfg.resolved_key(), "ZW52LWtleQ==")
        finally:
            os.environ.pop("HEALTH_ONENET_KEY", None)
        self.assertEqual(cfg.resolved_key(), DOC_KEY)

    def test_keepalive受平台限制(self) -> None:
        """官方文档：keepalive 允许 10~1800 秒。

        注意：``OneNetConfig.from_dict`` 会**立即**校验（这与"配置错误要在启动时炸掉"
        的项目纪律一致），所以异常在构造时就抛，而不是等到显式 ``validate()``。
        """
        for bad in (5, 1801):
            with self.subTest(keepalive=bad):
                with self.assertRaises(OneNetError) as ctx:
                    make_config(keepalive=bad)
                self.assertIn("10~1800", str(ctx.exception))
        # 边界值本身要合法
        self.assertEqual(make_config(keepalive=10).keepalive, 10)
        self.assertEqual(make_config(keepalive=1800).keepalive, 1800)

    def test_topic模板必须含占位符(self) -> None:
        with self.assertRaises(OneNetError) as ctx:
            make_config(topic_template="sys/foo/bar").validate()
        self.assertIn("{pid}", str(ctx.exception))

    def test_topic模板必须以sys开头(self) -> None:
        """★ 2026-09-22 真机踩到：少了 `$sys/` 前缀时**发布照样成功**，
        但 `_topic()` 拼出的**订阅主题是错的** → accepted/rejected 回执一条都收不到，
        等于把"数据到底上没上去"的唯一硬证据静默丢掉（排查时只能靠猜）。

        所以这里必须**直接拒绝**，而不是"让它跑起来"。
        """
        for bad in ("/{pid}/{device}/dp/post/json", "sys/{pid}/{device}/dp/post/json",
                    "{pid}/{device}/dp/post/json"):
            with self.subTest(template=bad):
                with self.assertRaises(OneNetError) as ctx:
                    make_config(topic_template=bad).validate()
                self.assertIn("$sys/", str(ctx.exception))
                self.assertIn("回执", str(ctx.exception), "错误信息要说清后果，不能只说'格式不对'")
        # 正确写法要能通过
        make_config(topic_template="$sys/{pid}/{device}/dp/post/json").validate()

    def test_默认地址与TLS地址(self) -> None:
        plain = make_config()
        self.assertEqual(plain.resolved_host(), DEFAULT_HOST_PLAIN)
        self.assertEqual(plain.resolved_port(), DEFAULT_PORT_PLAIN)
        tls = make_config(tls=True)
        self.assertEqual(tls.resolved_host(), "mqttstls.heclouds.com")
        self.assertEqual(tls.resolved_port(), 8883)

    def test_未知字段报错(self) -> None:
        with self.assertRaises(OneNetError) as ctx:
            OneNetConfig.from_dict({"enable": True})
        self.assertIn("enable", str(ctx.exception))

    def test_默认平台是legacy(self) -> None:
        """本项目已与需求方确认用**旧版 MQTT物联网套件**（数据流-数据点）。

        默认值必须是 legacy —— 免得有人照文档填了配置却因为版本不对而"连上了但没数据"。
        """
        from health_monitor.net.onenet import PLATFORM_LEGACY

        self.assertEqual(OneNetConfig().platform, PLATFORM_LEGACY)
        self.assertEqual(make_config().platform, PLATFORM_LEGACY)

    def test_填了studio要明确报错并说明差异(self) -> None:
        """★ 防"配错版本默默不上数据"：Studio 是另一套上报格式，必须**大声报错**。

        判据：异常消息里要同时出现
        ① 不支持的原因、② 两套的差异（OneJSON vs 数据点）、③ 怎么判断自己在哪套平台。
        """
        from health_monitor.net.onenet import PLATFORM_STUDIO

        with self.assertRaises(OneNetError) as ctx:
            make_config(platform=PLATFORM_STUDIO)
        message = str(ctx.exception)
        self.assertIn("studio", message)
        self.assertIn("OneJSON", message, "要说清 Studio 用的是物模型 OneJSON")
        self.assertIn("物模型", message, "要给出判断自己平台版本的方法")
        self.assertIn("数据流", message, "要说明本适配器支持的是数据流-数据点")

    def test_未启用时也会拦住版本填错(self) -> None:
        """版本错误属于配置错误，**未启用也要拦**（否则联调时才发现，白费半天）。"""
        from health_monitor.net.onenet import PLATFORM_STUDIO

        with self.assertRaises(OneNetError):
            OneNetConfig.from_dict({"enabled": False, "platform": PLATFORM_STUDIO})

    def test_token由配置生成且含设备res(self) -> None:
        cfg = make_config()
        token = cfg.make_token(now=DOC_ET)
        self.assertIn("res=products%2F123123%2Fdevices%2Fliving-room-pi", token)


# ---------------------------------------------------------------------------
# 3. 数据点报文（纯函数）
# ---------------------------------------------------------------------------


class TestDatapointPayload(unittest.TestCase):
    def test_结构符合平台要求(self) -> None:
        payload = OneNetPublisher.build_datapoint(7, {"temperature": 30, "power": 4.5}, ts=1552289676)
        self.assertEqual(payload["id"], 7)
        self.assertEqual(payload["dp"]["temperature"], [{"v": 30, "t": 1552289676}])
        self.assertEqual(payload["dp"]["power"], [{"v": 4.5, "t": 1552289676}])

    def test_缺失字段整条跳过(self) -> None:
        """★ 与本项目"数据缺失绝不当成正常"一致：不上报 0，而是**不上报**。"""
        payload = OneNetPublisher.build_datapoint(1, {"hr": 72, "spo2": None, "temp": None}, ts=1)
        self.assertIn("hr", payload["dp"])
        self.assertNotIn("spo2", payload["dp"])
        self.assertNotIn("temp", payload["dp"])

    def test_时间戳取整(self) -> None:
        payload = OneNetPublisher.build_datapoint(1, {"x": 1}, ts=1552289676.987)
        self.assertEqual(payload["dp"]["x"][0]["t"], 1552289676)
        self.assertIsInstance(payload["dp"]["x"][0]["t"], int)

    def test_字符串值与对象值都可上报(self) -> None:
        """官方文档：``v`` 可以是 int/float/string/object。"""
        payload = OneNetPublisher.build_datapoint(1, {"motion": "detected", "extra": {"a": 1}}, ts=1)
        self.assertEqual(payload["dp"]["motion"][0]["v"], "detected")
        self.assertEqual(payload["dp"]["extra"][0]["v"], {"a": 1})

    def test_默认数据流映射覆盖本地量(self) -> None:
        for local_key in ("heart_rate_bpm", "spo2_percent", "body_temp_c",
                          "ambient_temp_c", "humidity_percent", "motion_state"):
            self.assertIn(local_key, DEFAULT_STREAM_MAP)

    def test_报文可JSON序列化且无多余空白(self) -> None:
        payload = OneNetPublisher.build_datapoint(1, {"x": 1}, ts=1)
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        self.assertNotIn(" ", text)
        self.assertEqual(json.loads(text)["id"], 1)


# ---------------------------------------------------------------------------
# 4. 上报链路（假 paho）
# ---------------------------------------------------------------------------


class TestOneNetPublisher(unittest.TestCase):
    def setUp(self) -> None:
        FakeClient.instances.clear()
        install_fake_paho()
        self.cfg = make_config(interval_s=1.0, subscribe_result=True)
        self.pub = OneNetPublisher(self.cfg)
        self.assertTrue(self.pub.start(), self.pub.reason)

    def tearDown(self) -> None:
        self.pub.stop()
        uninstall_fake_paho()

    def _drain(self, timeout: float = 2.0) -> List[Tuple[str, str, int]]:
        import time

        client = FakeClient.instances[-1]
        deadline = time.time() + timeout
        while time.time() < deadline and not client.published:
            time.sleep(0.02)
        return client.published

    def test_连接三要素按平台要求(self) -> None:
        """clientId=设备名、username=产品ID、password=token [文档]。"""
        client = FakeClient.instances[-1]
        self.assertEqual(client.client_id, "living-room-pi")
        username, password = client.credentials
        self.assertEqual(username, DOC_PID)
        self.assertTrue(password.startswith("version=2018-10-31&res="))
        self.assertIn("devices%2Fliving-room-pi", password)
        self.assertEqual(client.connected_to, (DEFAULT_HOST_PLAIN, DEFAULT_PORT_PLAIN, 120))

    def test_连上后订阅平台回执(self) -> None:
        client = FakeClient.instances[-1]
        client.on_connect(client, None, None, 0)
        topics = [t for t, _ in client.subscribed]
        self.assertIn("$sys/123123/living-room-pi/dp/post/json/accepted", topics)
        self.assertIn("$sys/123123/living-room-pi/dp/post/json/rejected", topics)

    def test_没有可上报字段时必须留痕(self) -> None:
        """★ 静默不发是一级缺陷（2026-09-22 真机踩到）。

        当时用自造键（``probe_temp``）调用 ``publish_reading`` —— 该键不在 stream_map 里，
        映射后一个字段都没有，于是**什么都没发、也没有任何提示**，白排查半天。
        现在的约定：这种情况必须 ① 累加 ``skipped_empty`` ② 记 warning ③ 不入队。
        """
        before = self.pub.skipped_empty
        with self.assertLogs("health_monitor.net.onenet", level="WARNING") as captured:
            self.pub.publish_reading({"probe_temp": 25.0, "unknown_key": 1})
        self.assertEqual(self.pub.skipped_empty, before + 1)
        self.assertTrue(any("没有任何可上报字段" in line for line in captured.output))
        self.assertEqual(self._drain(timeout=0.4), [], "不该把空数据点发上去")

    def test_按映射上报真实字段(self) -> None:
        """用 stream_map 里的本地量名 → 应该正常入队并发出。"""
        self.pub.publish_reading({"heart_rate_bpm": 72.0, "spo2_percent": 98.0,
                                  "ambient_temp_c": 25.5, "data_age_s": 1.0})
        published = self._drain()
        self.assertEqual(len(published), 1)
        topic, payload, _qos = published[0]
        self.assertEqual(topic, "$sys/123123/living-room-pi/dp/post/json")
        data = json.loads(payload)
        streams = data["dp"]
        self.assertIn("heart_rate", streams)
        self.assertEqual(streams["heart_rate"][0]["v"], 72.0)
        self.assertEqual(self.pub.skipped_empty, 0)

    def test_连接被拒时给出排查提示(self) -> None:
        client = FakeClient.instances[-1]
        client.on_connect(client, None, None, 5)
        self.assertFalse(self.pub.connected)
        self.assertIn("access_key", self.pub.status()["last_error"])

    def test_上报读数走数据点topic(self) -> None:
        summary = {
            "ts": 1700000000.0, "heart_rate_bpm": 72.4, "spo2_percent": 97.9,
            "finger_detected": True, "body_temp_c": 36.5, "ambient_temp_c": 24.5,
            "humidity_percent": 55.0, "motion_state": "detected",
            "data_age_s": 1.5, "data_stale": False, "sensor_failures": {},
        }
        self.pub.publish_reading(summary, ts=1700000000.0)
        published = self._drain()
        self.assertTrue(published, "应当有数据点被发布")
        topic, message, _ = published[0]
        self.assertEqual(topic, "$sys/123123/living-room-pi/dp/post/json")
        payload = json.loads(message)
        self.assertIn("dp", payload)
        self.assertGreater(payload["id"], 0)
        dp = payload["dp"]
        self.assertEqual(dp["heart_rate"][0]["v"], 72.4)   # 本地量名 → 数据流名
        self.assertEqual(dp["spo2"][0]["v"], 97.9)
        self.assertEqual(dp["motion"][0]["v"], "detected")
        self.assertEqual(dp["data_age_s"][0]["v"], 1.5)
        self.assertNotIn("sensor_failures", dp)

    def test_全为None时不发空包(self) -> None:
        self.pub.publish_reading({"heart_rate_bpm": None, "spo2_percent": None}, ts=1.0)
        client = FakeClient.instances[-1]
        import time

        time.sleep(0.3)
        self.assertEqual(client.published, [], "没有任何有效数据时不应发空数据点")

    def test_报警上报成独立数据流(self) -> None:
        class FakeEvent:
            code = types.SimpleNamespace(value="hr_too_high")
            severity = 2
            value = 128.0
            ts = 1700000000.0

        self.pub.publish_alarm(FakeEvent())
        published = self._drain()
        dp = json.loads(published[0][1])["dp"]
        self.assertEqual(dp["alarm_code"][0]["v"], "hr_too_high")
        self.assertEqual(dp["alarm_level"][0]["v"], 2)
        self.assertEqual(dp["alarm_value"][0]["v"], 128.0)

    def test_状态上报(self) -> None:
        self.pub.publish_status({"version": "1.0.1", "devices": {"assembled": ["vitals", "motion"], "errors": {}}})
        published = self._drain()
        dp = json.loads(published[0][1])["dp"]
        self.assertEqual(dp["version"][0]["v"], "1.0.1")
        self.assertEqual(dp["online"][0]["v"], 1)
        self.assertEqual(dp["device_count"][0]["v"], 2)

    def test_平台拒绝回执被记为失败(self) -> None:
        """★ 这是"数据到底上没上去"的唯一硬证据。"""
        client = FakeClient.instances[-1]
        client.on_connect(client, None, None, 0)
        before = self.pub.failed
        client.on_message(
            client, None,
            FakeMessage("$sys/123123/living-room-pi/dp/post/json/rejected",
                        {"id": 3, "err_code": 98, "err_msg": "Illegal Data"}),
        )
        self.assertGreater(self.pub.failed, before)
        self.assertIn("平台拒绝", self.pub.status()["last_error"] or "")
        self.assertTrue(self.pub.status()["results"])

    def test_平台接收回执被记录(self) -> None:
        client = FakeClient.instances[-1]
        client.on_connect(client, None, None, 0)
        client.on_message(
            client, None,
            FakeMessage("$sys/123123/living-room-pi/dp/post/json/accepted", {"id": 9}),
        )
        results = self.pub.status()["results"]
        self.assertEqual(results[-1]["payload"]["id"], 9)

    def test_状态里不含token内容(self) -> None:
        text = json.dumps(self.pub.status(), ensure_ascii=False)
        self.assertNotIn("version=2018-10-31", text, "status() 里不许出现 token 内容")

    def test_未启用时start返回False并说明原因(self) -> None:
        pub = OneNetPublisher(make_config(enabled=False))
        self.assertFalse(pub.start())
        self.assertIn("enabled=false", pub.reason)

    def test_缺依赖时只降级不报错(self) -> None:
        uninstall_fake_paho()
        pub = OneNetPublisher(make_config())
        started = pub.start()
        if started:      # 本机真装了 paho 时跳过该断言
            pub.stop()
            self.skipTest("本机已安装 paho-mqtt，无法验证'缺依赖'路径")
        self.assertFalse(started)
        self.assertIn("paho", pub.reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
