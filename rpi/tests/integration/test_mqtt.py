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
    #: ``publish`` 返回的 rc（0 = 成功；非 0 = "现在发不出去"，例如未连接）
    publish_rc: int = 0

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
        return FakePublishInfo(rc=FakeClient.publish_rc)


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


class TestOfflinePausePolicy(unittest.TestCase):
    """★ **断网即暂停入队**（用户 2026-10-01 拍板）。

    三条口径（本组把它们钉死，防止以后有人把三态"优化"成布尔量）：

    | ``connected`` | 含义 | 入队吗 |
    | --- | --- | --- |
    | ``None`` | 还不知道（刚启动，没连上也没失败过） | **入队**（启动初期那几条仍有机会发出去） |
    | ``True`` | 已连上 | 入队 |
    | ``False`` | **已知**离线 | **不入队**，只累加 ``skipped_offline`` |

    ⚠️ 为什么必须是三态而不是 `bool`：paho 对"**首次**连接失败"是否回调、
    回调 `on_connect` 还是 `on_disconnect`，各版本行为不一致 —— 若拿"还没连上"当"断网"，
    启动初期会被误判成离线（把本该发出去的启动状态/读数全丢掉）。
    "已知离线"只由**我们自己的观测**产生：发布返回 ``rc != 0``、或明确收到断开通知。
    """

    def setUp(self) -> None:
        FakeClient.instances.clear()
        FakeClient.raise_on_publish = False
        FakeClient.publish_rc = 0
        install_fake_paho()
        self.cfg = MqttConfig(
            enabled=True, host="broker.example", port=1883,
            topic_prefix="health/room1", client_id="raspi-test", interval_s=1.0,
        )
        self.publisher = MqttPublisher(self.cfg)
        self.assertTrue(self.publisher.start())
        self.client = FakeClient.instances[-1]

    def tearDown(self) -> None:
        self.publisher.stop()
        uninstall_fake_paho()

    @staticmethod
    def _wait(pred, timeout: float = 2.0) -> bool:
        import time

        deadline = time.time() + timeout
        while time.time() < deadline:
            if pred():
                return True
            time.sleep(0.02)
        return bool(pred())

    # ---------- 三态语义 ----------

    def test_还不知道连接状态时照旧入队(self) -> None:
        """启动初期（`connected is None`）**不许**被当成断网 —— 否则状态/读数全丢。"""
        self.assertIsNone(self.publisher.connected, "刚 start() 时应当是'还不知道'")
        self.publisher.publish_reading({"ts": 1.0, "ambient_temp_c": 23.0})
        self.assertTrue(self._wait(lambda: self.client.published), "应当照旧入队并被发布")

    def test_发布成功会把状态确定为已连上(self) -> None:
        """反向钉子：三态里的 `None` 必须能被"一次成功的发布"确定下来。"""
        self.publisher.publish_reading({"ts": 1.0})
        self.assertTrue(self._wait(lambda: self.publisher.connected is True))
        self.assertEqual(self.publisher.offline_episodes, 0, "从没失败过，不算离线过")

    # ---------- 暂停入队 ----------

    def test_已知离线时不入队且计数(self) -> None:
        self.client.on_disconnect(self.client, None, 1)      # broker 断了
        self.assertIs(self.publisher.connected, False)
        before = len(self.client.published)
        for _ in range(3):
            self.publisher.publish_reading({"ts": 1.0})
        self.assertEqual(self.publisher.skipped_offline, 3, "断网期间不该入队，但要计数")
        self.assertEqual(self.publisher._queue.qsize(), 0, "队列里不许攒东西")
        self.assertEqual(len(self.client.published), before, "断网期间不该发出任何消息")

    def test_断网不清空已有计数口径(self) -> None:
        """断网期间**不再累加 failed**（那是"试了发不出去"的计数），改由 skipped_offline 表达。

        为什么要分开：`failed` 涨说明"我们在空转地重试"，而暂停之后我们**根本不再尝试** ——
        两个数字混在一起，就分不清"断网多久"与"重试了多少次"。
        """
        self.client.on_disconnect(self.client, None, 1)
        failed_before = self.publisher.failed
        for _ in range(5):
            self.publisher.publish_reading({"ts": 1.0})
        self.assertEqual(self.publisher.failed, failed_before, "暂停入队后不该再有发布失败")
        self.assertEqual(self.publisher.skipped_offline, 5)

    # ---------- 恢复 ----------

    def test_恢复后继续上报并记下离线时长与次数(self) -> None:
        class Clock:
            t = 1000.0

            def __call__(self) -> float:
                return self.t

        clock = Clock()
        pub = MqttPublisher(self.cfg, clock=clock)
        FakeClient.instances.clear()
        self.assertTrue(pub.start())
        client = FakeClient.instances[-1]
        try:
            client.on_disconnect(client, None, 1)         # t=1000 断
            pub.publish_reading({"ts": 1.0})
            self.assertEqual(pub.skipped_offline, 1)
            self.assertAlmostEqual(pub.offline_s() or 0.0, 0.0, places=1)

            clock.t = 1015.0                              # 离线 15 秒
            self.assertAlmostEqual(pub.offline_s() or 0.0, 15.0, places=1)
            client.on_connect(client, None, None, 0)      # 恢复
            self.assertIs(pub.connected, True)
            self.assertIsNone(pub.offline_s(), "恢复后不该还显示'离线段'")
            self.assertAlmostEqual(pub.offline_total_s, 15.0, places=1)
            self.assertEqual(pub.offline_episodes, 1)

            pub.publish_reading({"ts": 2.0})              # 恢复后照常上报
            self.assertTrue(self._wait(lambda: client.published), "恢复后必须继续上报")
        finally:
            pub.stop()

    def test_发布失败会自动转入已知离线(self) -> None:
        """★ 这条是"断网"的**主要入口**：broker 不可达时 paho 的回调语义因版本而异，
        所以我们用**自己的发布结果**判定 —— `rc != 0` ⇒ 已知离线 ⇒ 后续不再入队。"""
        FakeClient.publish_rc = 4                             # 4 = 发不出去
        self.publisher.publish_reading({"ts": 1.0})
        self.assertTrue(self._wait(lambda: self.publisher.connected is False),
                        "发布返回非 0 后应当转入'已知离线'")
        self.assertGreaterEqual(self.publisher.failed, 1)
        self.publisher.publish_reading({"ts": 2.0})
        self.assertEqual(self.publisher.skipped_offline, 1, "之后就不该再入队了")
        self.assertEqual(self.publisher.offline_episodes, 1)

    def test_发布抛异常也算已知离线(self) -> None:
        FakeClient.raise_on_publish = True
        self.publisher.publish_reading({"ts": 1.0})
        self.assertTrue(self._wait(lambda: self.publisher.connected is False))

    # ---------- A/B 与边界 ----------

    def test_关掉开关就回到旧行为(self) -> None:
        """A/B 对照：`pause_when_offline=False` ⇒ 断网仍入队（旧行为）。

        留着它是为了**能对照**：万一将来有人说"暂停入队把数据搞丢了"，
        这条能立刻证明"开关关掉就是以前那样"。
        """
        self.publisher.pause_when_offline = False
        self.client.on_disconnect(self.client, None, 1)
        self.publisher.publish_reading({"ts": 1.0})
        self.assertEqual(self.publisher.skipped_offline, 0, "关掉开关后不该跳")
        self.assertEqual(self.publisher._queue.qsize(), 1, "旧行为：照旧入队（等着重试/失败）")

    def test_主动断开不算一次离线事故(self) -> None:
        """我们自己调 `disconnect()`（正常停机）不该被算成"断网了一次"。"""
        self.client.on_disconnect(self.client, None, 0)
        self.assertIs(self.publisher.connected, False)
        self.assertEqual(self.publisher.offline_episodes, 0)
        self.assertIsNone(self.publisher.offline_s())

    def test_停机时把仍在离线的那一段结掉(self) -> None:
        """否则 `offline_total_s` 会永远少最后一段（诊断数字对不上现场）。"""
        class Clock:
            t = 500.0

            def __call__(self) -> float:
                return self.t

        clock = Clock()
        pub = MqttPublisher(self.cfg, clock=clock)
        FakeClient.instances.clear()
        self.assertTrue(pub.start())
        client = FakeClient.instances[-1]
        client.on_disconnect(client, None, 1)
        clock.t = 512.0
        pub.stop()
        self.assertAlmostEqual(pub.offline_total_s, 12.0, places=1)
        self.assertIsNone(pub.offline_s(), "停机后不该还留着'正在离线'")

    # ---------- 可见性 ----------

    def test_状态快照暴露离线指标(self) -> None:
        self.client.on_disconnect(self.client, None, 1)
        status = self.publisher.status()
        for key in ("offline_s", "offline_episodes", "offline_total_s",
                    "skipped_offline", "pause_when_offline"):
            self.assertIn(key, status, f"离线指标 {key} 必须能被 /api/v1/health 看到")
        self.assertEqual(status["offline_episodes"], 1)
        self.assertTrue(status["pause_when_offline"])


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
