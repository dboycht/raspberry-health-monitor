"""MQTT 上云模块测试（**不需要 paho-mqtt，也不需要真的连 broker**）。

手法：把 ``MqttPublisher.start()`` 里用到的 paho 交互抽成"可注入的假客户端"——
测试先 monkeypatch ``sys.modules['paho.mqtt.client']`` 为一个假模块，
就能验证"配置校验 / 连接回调 / 入队 / 队列满丢最旧 / 发布失败只计数 / 隐私字段过滤"
这些**真正容易写错**的逻辑。

⚠️ 上云是**可选功能**，所以最重要的三条验收是：
1. 没装 paho-mqtt 时**只记日志、不影响本地**（start 返回 False 且有 reason）；
2. 发布失败**不抛异常**，只累加计数；
3. 发出去的消息里**不含本机路径/凭据**等敏感信息。
"""

from __future__ import annotations

import json
import sys
import types
import unittest
from typing import Any, Dict, List, Tuple

from health_monitor.core.config import AppConfig, DEFAULT_CONFIG
from health_monitor.net.mqtt import PASSWORD_ENV, MqttConfig, MqttPublisher


class FakePublishInfo:
    def __init__(self, rc: int = 0) -> None:
        self.rc = rc


class FakeClient:
    """假 paho 客户端：记录调用，可注入发布失败。"""

    instances: List["FakeClient"] = []
    raise_on_publish: bool = False

    def __init__(self, client_id: str = "", clean_session: bool = True) -> None:
        self.client_id = client_id
        self.clean_session = clean_session
        self.published: List[Tuple[str, str, int]] = []
        self.connected_to: Tuple[str, int, int] | None = None
        self.loop_started = False
        self.disconnected = False
        self.on_connect: Any = None
        self.on_disconnect: Any = None
        FakeClient.instances.append(self)

    def username_pw_set(self, username: str, password: str | None = None) -> None:
        self.credentials = (username, password)

    def connect_async(self, host: str, port: int, keepalive: int) -> None:
        self.connected_to = (host, port, keepalive)

    def loop_start(self) -> None:
        self.loop_started = True

    def loop_stop(self) -> None:
        self.loop_started = False

    def disconnect(self) -> None:
        self.disconnected = True

    def publish(self, topic: str, message: str, qos: int = 0) -> FakePublishInfo:
        if FakeClient.raise_on_publish:
            raise OSError("模拟 broker 断开")
        self.published.append((topic, message, qos))
        return FakePublishInfo(rc=0)


def install_fake_paho() -> None:
    """把假 paho 装进 sys.modules（仅测试用）。"""
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


class TestMqttConfig(unittest.TestCase):
    def test_默认关闭(self) -> None:
        cfg = MqttConfig()
        self.assertFalse(cfg.enabled)
        cfg.validate()   # 未启用时不校验 host

    def test_启用后必须填host(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            MqttConfig(enabled=True, host="").validate()
        self.assertIn("host", str(ctx.exception))

    def test_端口越界报错(self) -> None:
        with self.assertRaises(ValueError):
            MqttConfig(enabled=True, host="x", port=0).validate()
        with self.assertRaises(ValueError):
            MqttConfig(enabled=True, host="x", port=70000).validate()

    def test_周期必须为正(self) -> None:
        with self.assertRaises(ValueError):
            MqttConfig(enabled=True, host="x", interval_s=0).validate()

    def test_未知字段报错_避免拼错键静默失效(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            MqttConfig.from_dict({"enable": True, "host": "x"})
        self.assertIn("enable", str(ctx.exception))

    def test_密码优先取环境变量(self) -> None:
        import os

        cfg = MqttConfig(password="in-file")
        os.environ[PASSWORD_ENV] = "from-env"
        try:
            self.assertEqual(cfg.resolved_password(), "from-env")
        finally:
            os.environ.pop(PASSWORD_ENV, None)
        self.assertEqual(cfg.resolved_password(), "in-file")

    def test_默认配置里的mqtt段可解析且关闭(self) -> None:
        cfg = MqttConfig.from_dict(dict(DEFAULT_CONFIG["mqtt"]))
        self.assertFalse(cfg.enabled)
        cfg.validate()


class TestMqttPublisherWithoutPaho(unittest.TestCase):
    def test_未安装paho时只降级不报错(self) -> None:
        """上云是可选功能：没装 paho 必须只记日志，本地一切照旧。"""
        uninstall_fake_paho()
        publisher = MqttPublisher(MqttConfig(enabled=True, host="example.com"))
        started = publisher.start()
        if started:   # 本机真的装了 paho 时会走到这里，跳过该断言
            publisher.stop()
            self.skipTest("本机已安装 paho-mqtt，无法验证'缺依赖'路径")
        self.assertFalse(started)
        self.assertIn("paho", publisher.reason)

    def test_未启用时start返回False且有原因(self) -> None:
        publisher = MqttPublisher(MqttConfig(enabled=False))
        self.assertFalse(publisher.start())
        self.assertIn("enabled=false", publisher.reason)

    def test_发布接口在未启动时是安全的空操作(self) -> None:
        publisher = MqttPublisher(MqttConfig(enabled=False))
        publisher.publish_reading({"ts": 1.0, "heart_rate_bpm": 72.0})
        publisher.publish_alarm(types.SimpleNamespace(to_dict=lambda: {"code": "x"}))
        self.assertEqual(publisher.published, 0)
        self.assertEqual(publisher.dropped, 0)


class TestMqttPublisherWithFakePaho(unittest.TestCase):
    def setUp(self) -> None:
        FakeClient.instances.clear()
        FakeClient.raise_on_publish = False
        install_fake_paho()
        self.cfg = MqttConfig(
            enabled=True, host="broker.example", port=1883,
            topic_prefix="health/room1", client_id="raspi-test", interval_s=1.0,
        )
        self.publisher = MqttPublisher(self.cfg)
        self.assertTrue(self.publisher.start())

    def tearDown(self) -> None:
        self.publisher.stop()
        uninstall_fake_paho()

    def test_启动后进入已连接状态(self) -> None:
        client = FakeClient.instances[-1]
        self.assertTrue(client.loop_started)
        self.assertEqual(client.connected_to, ("broker.example", 1883, 60))
        client.on_connect(client, None, None, 0)     # 模拟 broker 回连成功
        self.assertTrue(self.publisher.connected)
        self.assertTrue(self.publisher.status()["available"])

    def test_连接被拒会记录原因(self) -> None:
        client = FakeClient.instances[-1]
        client.on_connect(client, None, None, 5)     # 5 = not authorised
        self.assertFalse(self.publisher.connected)
        self.assertIn("rc=5", self.publisher.status()["last_error"] or "")

    def test_用户名密码会传给客户端(self) -> None:
        self.publisher.stop()
        FakeClient.instances.clear()
        cfg = MqttConfig(enabled=True, host="h", username="u", password="p")
        publisher = MqttPublisher(cfg)
        self.assertTrue(publisher.start())
        try:
            self.assertEqual(FakeClient.instances[-1].credentials, ("u", "p"))
        finally:
            publisher.stop()

    def _drain(self, timeout: float = 2.0) -> List[Tuple[str, str, int]]:
        """等工作线程把队列里的消息发出去。"""
        import time

        deadline = time.time() + timeout
        client = FakeClient.instances[-1]
        while time.time() < deadline and not client.published:
            time.sleep(0.02)
        return client.published

    def test_发布读数走正确的主题(self) -> None:
        self.publisher.publish_reading(
            {"ts": 1.0, "heart_rate_bpm": 72.0, "spo2_percent": 98.0, "data_stale": False,
             "sensor_failures": {"vitals": 2}}
        )
        published = self._drain()
        self.assertTrue(published, "应当有消息被发布")
        topic, message, qos = published[0]
        self.assertEqual(topic, "health/room1/reading")
        self.assertEqual(qos, 0)
        payload = json.loads(message)
        self.assertEqual(payload["heart_rate_bpm"], 72.0)
        # sensor_failures 只发"数量"，不发设备名与错误文本
        self.assertEqual(payload["sensor_fault_count"], 1)
        self.assertNotIn("sensor_failures", payload)

    def test_发布报警只保留安全的detail字段(self) -> None:
        class FakeEvent:
            def to_dict(self) -> Dict[str, Any]:
                return {
                    "ts": 1.0, "code": "all_clear", "severity": 0, "message": "已确认",
                    "value": None, "unit": "", "source": "operator",
                    "detail": {"recovered_code": "hr_too_high",
                               "local_path": "/home/pi/secret/config.json",
                               "traceback": "Traceback ..."},
                }

        self.publisher.publish_alarm(FakeEvent())
        published = self._drain()
        payload = json.loads(published[0][1])
        self.assertEqual(published[0][0], "health/room1/alarm")
        self.assertEqual(payload["detail"], {"recovered_code": "hr_too_high"})
        self.assertNotIn("local_path", json.dumps(payload))
        self.assertNotIn("Traceback", json.dumps(payload))

    def test_发布失败只计数不抛异常(self) -> None:
        """上云失败绝不能让本地监护停摆。"""
        FakeClient.raise_on_publish = True
        self.publisher.publish_reading({"ts": 1.0})
        import time

        deadline = time.time() + 2.0
        while time.time() < deadline and self.publisher.failed == 0:
            time.sleep(0.02)
        self.assertGreaterEqual(self.publisher.failed, 1)
        self.assertIsNotNone(self.publisher.last_error)

    def test_队列满时丢最旧的并计数(self) -> None:
        publisher = MqttPublisher(self.cfg, queue_size=2)
        # 不启动工作线程：让消息堆在队列里
        publisher._client = FakeClient()   # noqa: SLF001 - 只为了走 _enqueue 的路径
        for i in range(5):
            publisher.publish_reading({"ts": float(i)})
        self.assertEqual(publisher.dropped, 3, "队列容量 2，塞 5 条应丢 3 条")
        self.assertEqual(publisher._queue.qsize(), 2)

    def test_状态快照含关键字段(self) -> None:
        status = self.publisher.status()
        for key in ("enabled", "available", "connected", "published", "dropped", "failed", "topic_prefix"):
            self.assertIn(key, status)
        self.assertEqual(status["topic_prefix"], "health/room1")

    def test_主题前缀两端斜杠会被规整(self) -> None:
        self.publisher.stop()
        FakeClient.instances.clear()
        publisher = MqttPublisher(MqttConfig(enabled=True, host="h", topic_prefix="/health/room2/"))
        self.assertTrue(publisher.start())
        try:
            publisher.publish_reading({"ts": 1.0})
            published = self._drain()
            self.assertEqual(published[0][0], "health/room2/reading")
        finally:
            publisher.stop()


class TestRuntimeMqttIntegration(unittest.TestCase):
    """运行时集成：默认配置（mqtt 关闭）时不应启动，也不应影响本地功能。"""

    def test_默认配置下mqtt关闭且状态可见(self) -> None:
        from health_monitor.core.config import AppConfig
        from health_monitor.demo import DEMO_CONFIG

        cfg = AppConfig.from_dict(DEMO_CONFIG)
        from health_monitor.playback import PlaybackRuntime

        rt = PlaybackRuntime(cfg, sleep=lambda _s: None, verbose_outputs=False)
        rt.open()
        try:
            self.assertIsNotNone(rt.mqtt, "应保留 MqttPublisher 对象以便 status() 说明原因")
            self.assertFalse(rt.mqtt_started)
            status = rt.status()
            self.assertIn("mqtt", status)
            self.assertFalse(status["mqtt"]["enabled"])
        finally:
            rt.close()

    def test_配置里未知顶层字段会报错(self) -> None:
        from health_monitor.core.config import ConfigError
        from health_monitor.demo import DEMO_CONFIG

        bad = dict(DEMO_CONFIG)
        bad["mqttt"] = {}    # 拼错
        with self.assertRaises(ConfigError) as ctx:
            AppConfig.from_dict(bad)
        self.assertIn("mqttt", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
