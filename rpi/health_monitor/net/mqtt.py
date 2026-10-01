"""MQTT 上报（可选，默认关闭）—— 把"端-边-云"补齐的那一环。

为什么要有它
------------
局域网 HTTP 只能在家里看。课程题目是"物联网"，答辩时"有没有上云"经常被问到。
本模块在**不改动任何现有代码**的前提下加一条上行链路：
树莓派把**周期读数摘要**与**报警事件**发布到 MQTT broker（巴法云 / OneNET / EMQX / 自建），
手机端或云平台订阅即可。

设计约束（刻意的）
------------------
1. **可选依赖**：`paho-mqtt` 只用**函数内延迟导入**。没装 ⇒ 记一条日志并整体降级为"不发布"，
   **绝不影响采集与本地报警**（网断了、broker 挂了都必须照常监护）；
2. **不阻塞主循环**：发布走独立后台线程 + 有界队列，队列满了**丢最旧的**并计数
   （宁可丢云端数据，也不能让本地报警卡住）；
3. **隐私最小化**：只发"数值 + 时间戳 + 报警码"，**不发任何可识别个人信息**
   （无姓名/学号/位置/设备序列号）；主题里只用 `device_id`（由用户自己起个无意义的名字）；
4. **断线自愈**：paho 的 `loop_start()` 自带重连；我们只负责"发布失败不抛异常、只计数"。

配置（`config/devices.json`）::

    "mqtt": {
      "enabled": false,
      "host": "bemfa.com",
      "port": 9501,
      "topic_prefix": "health/room1",
      "client_id": "raspi-health-01",
      "username": "",
      "password": "",
      "interval_s": 30
    }

⚠️ `password` 属于凭据：**不要提交到仓库**（用自己的 `devices.json`，它已被 .gitignore 排除）。
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

_LOG = logging.getLogger(__name__)

#: 环境变量名：允许用环境变量覆盖密码（避免把口令写进配置文件）
PASSWORD_ENV = "HEALTH_MQTT_PASSWORD"


@dataclass
class MqttConfig:
    """MQTT 上报配置。"""

    enabled: bool = False
    host: str = ""
    port: int = 1883
    topic_prefix: str = "health/monitor"
    client_id: str = "raspi-health-monitor"
    username: str = ""
    password: str = ""
    interval_s: float = 30.0
    qos: int = 0
    keepalive: int = 60

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MqttConfig":
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"mqtt 配置里有未知字段 {sorted(unknown)}；可用 {sorted(known)}")
        return cls(**data)

    def validate(self) -> None:
        if not self.enabled:
            return
        if not self.host:
            raise ValueError("mqtt.enabled=true 但没填 host")
        if not (0 < int(self.port) < 65536):
            raise ValueError(f"mqtt.port 不合法：{self.port}")
        if self.interval_s <= 0:
            raise ValueError("mqtt.interval_s 必须为正数")
        if not self.topic_prefix.strip("/"):
            raise ValueError("mqtt.topic_prefix 不能为空")

    def resolved_password(self) -> str:
        """密码优先取环境变量（这样配置文件里可以留空）。"""
        import os

        return os.environ.get(PASSWORD_ENV) or self.password


class MqttPublisher:
    """MQTT 上报器（发布摘要与报警，失败只计数不抛异常）。

    线程模型：
    - :meth:`start` 起一个**后台工作线程**，从有界队列取消息发布；
    - ``publish_*`` 只是入队（非阻塞）；队列满时丢最旧的并累加 ``dropped``。

    ⚠️ **断网即暂停入队**（用户 2026-10-01 拍板）
    ------------------------------------------------
    原来"未连接"只体现在"发布失败计数一直涨"上：断网期间我们**照旧入队**、
    照旧空转尝试、队列满了丢最旧。现在改成：

    * ``connected`` 是**三态**：``None`` = 还不知道（启动初期）/ ``True`` = 已连上 /
      ``False`` = **已知断开**；
    * **只在 `connected is False`（确定离线）时暂停入队**，并累加 ``skipped_offline``；
      恢复（``connected is True``）后从**当前值**继续上报 —— 断网期间的读数**不补传**
      （它们本来就没有意义了；本地报警与消息流仍完整保留）；
    * ``None``（还没连上过）**照旧入队**：启动瞬间的那几条状态/读数仍有机会发出去，
      而且**不依赖 paho 回调的时机**（paho 对"首次连接失败"是否回调、回调哪个函数，
      各版本行为不一致，不能拿它当判据）；
    * "已知离线"由**我们自己的发布结果**判定（``rc != 0`` / 抛异常 ⇒ 转 False，
      成功 ⇒ 转 True），所以它不依赖底层库的通知语义 —— 这也是它能被单测钉死的原因；
    * 新增指标：``offline_s``（当前这段离线多久）、``offline_episodes``（离线了几次）、
      ``offline_total_s``（累计离线时长）、``skipped_offline``（因离线而没入队的条数）。
      它们都在 ``status()`` 里，因此 ``/api/v1/health`` 直接看得到。
    """

    def __init__(self, config: MqttConfig, queue_size: int = 200,
                 clock: Callable[[], float] = time.time) -> None:
        self.config = config
        self.clock = clock
        self._queue: "queue.Queue[tuple]" = queue.Queue(maxsize=queue_size)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._client: Any = None
        #: **三态**：``None`` 还不知道 / ``True`` 已连上 / ``False`` 已知断开（见类文档）
        self.connected: Optional[bool] = None
        self.published = 0
        self.dropped = 0
        self.failed = 0
        self.last_error: Optional[str] = None
        self.available = False          # 依赖是否就绪（paho 是否装上）
        self.reason = ""                # 不可用/关闭的原因（给人看）
        #: 断网即暂停入队（用户 2026-10-01 拍板）；置 False 可回到"照旧入队"的旧行为（做 A/B 用）
        self.pause_when_offline = True
        #: 当前这段离线的起点（``None`` = 不在离线中）
        self.offline_since: Optional[float] = None
        self.offline_episodes = 0       # 离线了几次（非离线 → 离线 才算一次）
        self.offline_total_s = 0.0      # 累计离线时长（秒）
        self.skipped_offline = 0        # 因"已知离线"而没入队的条数

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self) -> bool:
        """启动上报线程。返回是否真的启动（False 时 ``reason`` 说明原因）。"""
        if not self.config.enabled:
            self.reason = "配置里 mqtt.enabled=false（未启用上云）"
            return False
        try:
            self.config.validate()
        except ValueError as exc:
            self.reason = f"配置不合法：{exc}"
            _LOG.warning("MQTT %s", self.reason)
            return False
        try:
            import paho.mqtt.client as mqtt  # type: ignore import-not-found
        except ImportError as exc:
            self.reason = f"未安装 paho-mqtt（{exc}）：pip3 install paho-mqtt"
            _LOG.warning("MQTT 未启用：%s", self.reason)
            return False

        self.available = True
        try:
            client = mqtt.Client(client_id=self.config.client_id, clean_session=True)
            if self.config.username:
                client.username_pw_set(self.config.username, self.config.resolved_password())
            client.on_connect = self._on_connect
            client.on_disconnect = self._on_disconnect
            client.connect_async(self.config.host, int(self.config.port), int(self.config.keepalive))
            client.loop_start()
            self._client = client
        except Exception as exc:  # noqa: BLE001 - 上云失败绝不影响本地监护
            self.reason = f"初始化失败：{type(exc).__name__}: {exc}"
            _LOG.warning("MQTT %s", self.reason)
            return False

        self._stop.clear()
        self._thread = threading.Thread(target=self._worker, name="mqtt-publisher", daemon=True)
        self._thread.start()
        _LOG.info("MQTT 上报已启动：%s:%s，主题前缀 %s",
                  self.config.host, self.config.port, self.config.topic_prefix)
        return True

    def stop(self, timeout: float = 3.0) -> None:
        """停止上报（幂等）。"""
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._thread = None
        # 收尾把"这一段离线"结掉，否则 offline_total_s 会从"还在离线"变成"永远差一段"
        self._close_offline_episode()
        if self._client is not None:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception as exc:  # noqa: BLE001
                _LOG.debug("MQTT 关闭异常（已忽略）：%s", exc)
            self._client = None

    # ------------------------------------------------------------------
    # 连接状态（三态）与离线指标
    # ------------------------------------------------------------------

    def _mark_offline(self, reason: str = "") -> None:
        """记录"**已知**离线"：``connected=False``，并开一段离线区间（幂等）。

        ⚠️ 只允许**我们自己的观测**来调它（发布失败 / 明确收到断开通知），
        绝不用"还没连上"来猜 —— 那会把启动初期误判成断网（见类文档的三态说明）。
        """
        if reason:
            self.last_error = reason
        already = self.connected is False
        self.connected = False
        if self.offline_since is None:
            self.offline_since = float(self.clock())
            self.offline_episodes += 1
            _LOG.warning("上云已断开：%s（断网期间**暂停上报**，恢复后从当前值继续）",
                         reason or "未说明原因")
        elif not already:
            # 理论上到不了（False 时 offline_since 必然非 None），留个兜底避免重复计数
            self.offline_episodes += 1

    def _mark_online(self) -> None:
        """记录"已连上"：``connected=True``，并结掉当前离线区间（幂等）。"""
        if self.connected is not True and self.offline_since is not None:
            gap = max(0.0, float(self.clock()) - self.offline_since)
            self.offline_total_s += gap
            self.offline_since = None
            _LOG.info("上云已恢复：本次离线 %.1f 秒（期间跳过 %d 条未入队）",
                      gap, self.skipped_offline)
        self.connected = True

    def _close_offline_episode(self) -> None:
        """把"仍在离线"的那一段计入累计时长（停机收尾用；幂等）。"""
        if self.offline_since is not None:
            self.offline_total_s += max(0.0, float(self.clock()) - self.offline_since)
            self.offline_since = None

    def offline_s(self) -> Optional[float]:
        """当前这段离线已持续多少秒（在线或"还不知道" ⇒ ``None``）。"""
        if self.connected is False and self.offline_since is not None:
            return max(0.0, float(self.clock()) - self.offline_since)
        return None

    # ------------------------------------------------------------------
    # 回调
    # ------------------------------------------------------------------

    def _on_connect(self, client: Any, userdata: Any, flags: Any, rc: int) -> None:
        if rc == 0:
            self._mark_online()
            _LOG.info("MQTT 已连接：%s:%s", self.config.host, self.config.port)
        else:
            self._mark_offline(f"连接被拒绝 rc={rc}")
            _LOG.warning("MQTT 连接被拒绝：rc=%s", rc)

    def _on_disconnect(self, client: Any, userdata: Any, rc: int) -> None:
        if rc != 0:
            _LOG.warning("MQTT 意外断开 rc=%s（paho 会自动重连）", rc)
            self._mark_offline(f"意外断开 rc={rc}")
        else:
            # 主动断开（我们调 disconnect()）：不算"离线事故"，但状态必须翻成 False
            self.connected = False
            self._close_offline_episode()

    # ------------------------------------------------------------------
    # 发布入口（都是非阻塞入队）
    # ------------------------------------------------------------------

    def publish_reading(self, summary: Dict[str, Any], ts: Optional[float] = None) -> None:
        """发布一次读数摘要（``health/<prefix>/reading``）。

        ⚠️ 只发数值与时间戳；`sensor_failures` 只发**次数**，不发错误文本
        （错误文本里可能带本机路径）。

        Args:
            summary: :meth:`ReadingSnapshot.health_summary` 的输出。
            ts: 可选时间戳。**通用 MQTT 版忽略它**；OneNET 版用它填数据点的 ``t``。
                保留这个参数是为了让运行时用同一套调用代码（见 ``Runtime.tick``）。
        """
        payload = {
            "ts": summary.get("ts"),
            "heart_rate_bpm": summary.get("heart_rate_bpm"),
            "spo2_percent": summary.get("spo2_percent"),
            "finger_detected": summary.get("finger_detected"),
            "ambient_temp_c": summary.get("ambient_temp_c"),
            "humidity_percent": summary.get("humidity_percent"),
            "motion_state": summary.get("motion_state"),
            "data_stale": summary.get("data_stale"),
            "sensor_fault_count": len(summary.get("sensor_failures") or {}),
        }
        self._enqueue("reading", payload)

    def publish_alarm(self, event: Any) -> None:
        """发布一条报警事件（``health/<prefix>/alarm``）。"""
        try:
            payload = event.to_dict()
        except AttributeError:
            payload = {"message": str(event)}
        # detail 里可能含本机路径/异常文本：只保留恢复码这类安全字段
        detail = payload.get("detail") or {}
        payload["detail"] = {k: v for k, v in detail.items() if k in ("recovered_code",)}
        self._enqueue("alarm", payload)

    def publish_status(self, status: Dict[str, Any]) -> None:
        """发布一条状态（``health/<prefix>/status``）：设备数、故障设备名、版本。"""
        devices = status.get("devices", {})
        payload = {
            "version": status.get("version"),
            "mock": status.get("mock"),
            "faulted": (devices.get("errors") or {}).keys() and list((devices.get("errors") or {}).keys()) or [],
            "device_count": len(devices.get("assembled") or []),
        }
        self._enqueue("status", payload)

    def _enqueue(self, kind: str, payload: Dict[str, Any]) -> None:
        if self._client is None:
            return
        if self.pause_when_offline and self.connected is False:
            # ★ 已知离线：**不入队**（用户 2026-10-01 拍板）。
            #   为什么不是"继续入队、失败计数"：那样断网期间会一直空转尝试，
            #   队列满了还得丢；而断网期间的读数**本来就不该补传**（时间戳已经过期）。
            #   计数 `skipped_offline` 让"跳过了多少条"仍然看得见（不静默）。
            self.skipped_offline += 1
            return
        topic = f"{self.config.topic_prefix.strip('/')}/{kind}"
        message = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        try:
            self._queue.put_nowait((topic, message))
        except queue.Full:
            # 队列满：丢最旧的（宁可丢云端数据，也不能让本地报警卡住）
            try:
                self._queue.get_nowait()
                self._queue.put_nowait((topic, message))
            except queue.Empty:  # pragma: no cover - 竞态兜底
                pass
            self.dropped += 1

    # ------------------------------------------------------------------
    # 工作线程
    # ------------------------------------------------------------------

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                topic, message = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            client = self._client
            if client is None:
                continue
            try:
                info = client.publish(topic, message, qos=int(self.config.qos))
                # paho 的 publish 是异步的：rc != 0 才算"立即失败"
                if getattr(info, "rc", 0) != 0:
                    self.failed += 1
                    # ★ rc != 0 是**我们自己的观测**：它证明"现在发不出去" ⇒ 转入已知离线
                    #   （不依赖 paho 对"首次连接失败"的回调语义），之后 `_enqueue` 就会暂停
                    self._mark_offline(f"publish rc={info.rc}")
                else:
                    self.published += 1
                    if self.connected is not True:
                        self._mark_online()
            except Exception as exc:  # noqa: BLE001 - 上云失败不影响本地
                self.failed += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                self._mark_offline(self.last_error)
                _LOG.debug("MQTT 发布失败（已忽略）：%s", self.last_error)

    # ------------------------------------------------------------------
    # 诊断
    # ------------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        return {
            "enabled": self.config.enabled,
            "available": self.available,
            #: 三态：``None`` 还不知道 / ``True`` 已连上 / ``False`` 已知断开
            "connected": self.connected,
            "published": self.published,
            "dropped": self.dropped,
            "failed": self.failed,
            "last_error": self.last_error,
            "reason": self.reason,
            "topic_prefix": self.config.topic_prefix,
            "host": self.config.host,
            # ---- 断网行为（用户 2026-10-01 拍板"断网即暂停入队"）----
            #: 现在这段离线已持续多少秒（在线 ⇒ None）
            "offline_s": (None if self.offline_s() is None else round(float(self.offline_s()), 1)),
            #: 离线了几次 / 累计离线多少秒
            "offline_episodes": self.offline_episodes,
            "offline_total_s": round(float(self.offline_total_s), 1),
            #: 因"已知离线"而**没有入队**的条数（不静默：跳过了多少条要看得见）
            "skipped_offline": self.skipped_offline,
            "pause_when_offline": bool(self.pause_when_offline),
        }


__all__ = ["MqttConfig", "MqttPublisher", "PASSWORD_ENV"]
