"""服务装配与主循环 —— **整个系统唯一"知道所有零件"的地方**。

其他模块的分工：
- ``hal/`` 定义契约；``sensors/``、``outputs/`` 实现器件；
- ``core/`` 只做业务（不认识具体器件类）；
- ``net/`` 只管对外接口；
- **本文件**负责：读配置 → 造器件 → 装配 → 跑主循环 → 处理报警。

主循环一帧做的事
----------------
1. **实体按键**：先处理按键动作（短按 = 消音、长按 = 求助，见 `_consume_button_events`）；
2. 采集所有到期的设备（``Collector.collect_due``）；
3. 汇总快照（``Collector.snapshot``，含"陈旧即空"的保护）；
4. 规则引擎判定（``RuleEngine.evaluate``）；
5. 报警下发（``AlarmDispatcher.dispatch``）、落库、记录；
6. 按最近到期时间睡眠（不空转烧 CPU）。

额外兜底：**所有传感器都读不到时**（例如一次性拔了线），
会自发一条 ``SENSOR_FAULT``，避免"系统还在跑但什么都测不到"却悄无声息。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .core.collector import Collector
from .core.config import AppConfig, load_config
from .core.dispatcher import AlarmDispatcher
from .core.rules import RuleEngine
from .core.store import Store
from .hal import Device, OutputDevice, create_device
from .hal.exceptions import ConfigError
from .hal.models import AlarmCode, AlarmEvent, ButtonAction, Severity, now_ts
from .hal.registry import get_spec
from .net.web import WebApi, start_in_thread

_LOG = logging.getLogger(__name__)

VERSION = "1.0.1"


class Runtime:
    """系统运行时：把配置里的设备装配起来并跑起来。

    Args:
        config: 应用配置。
        mock: 是否模拟模式。**在 PC 上开发必须为 True**（否则会去开真实 I2C/GPIO）。
        store_path: 历史库路径；传 ``""`` 表示不落库（演示用）。
        dispatcher_enabled: 是否真的发声/显示。
        clock: 时间源（测试注入假时钟）。
        sleep: 睡眠函数（测试注入假 sleep 以避免真的等待）。
        config_path: 配置文件路径（仅用于状态展示与报错提示）。
        device_factory: 设备工厂，签名 ``(driver, params, mock, name) -> Device``。
            默认为 :func:`health_monitor.hal.registry.create_device`；
            演示/回放模式（:mod:`health_monitor.playback`）会注入自己的工厂，
            从而在**不改动任何业务逻辑**的前提下替换数据来源。
    """

    def __init__(
        self,
        config: AppConfig,
        mock: bool = True,
        store_path: Optional[str] = "data/history.db",
        dispatcher_enabled: bool = True,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        config_path: str = "",
        device_factory: Optional[Callable[..., Device]] = None,
    ) -> None:
        self.config = config
        self.mock = bool(mock)
        self.clock = clock
        self._sleep = sleep
        self.config_path = config_path
        self.version = VERSION
        self.started_at = clock()
        self._device_factory = device_factory or create_device

        # ---- 1. 装配输入设备 ----
        self.devices: Dict[str, Device] = {}
        self.assembly_errors: Dict[str, str] = {}
        for cfg in config.enabled_devices():
            try:
                device = self._device_factory(cfg.driver, params=cfg.params, mock=mock, name=cfg.name)
            except Exception as exc:  # noqa: BLE001 - 一个器件缺实现不该让整机起不来
                self.assembly_errors[cfg.name] = f"{type(exc).__name__}: {exc}"
                _LOG.warning("设备 %s（驱动 %s）装配失败：%s", cfg.name, cfg.driver, self.assembly_errors[cfg.name])
                if not cfg.optional:
                    _LOG.warning("  该设备非可选件，将缺席本轮运行（请检查驱动是否已实现）")
                continue
            self.devices[cfg.name] = device

        # ---- 2. 装配输出设备 ----
        self.outputs: Dict[str, OutputDevice] = {}
        for name, device in self.devices.items():
            if isinstance(device, OutputDevice):
                self.outputs[name] = device
        self.inputs: Dict[str, Device] = {
            name: dev for name, dev in self.devices.items() if name not in self.outputs
        }

        # ---- 3. 存储 / 规则 / 下发 ----
        self.store: Optional[Store] = Store(store_path) if store_path else None
        self.engine = RuleEngine(config.thresholds)
        self.dispatcher = AlarmDispatcher(outputs=self.outputs, enabled=dispatcher_enabled)
        self.collector = Collector(
            config, self.inputs, store=self.store, clock=clock,
        )

        # ---- 3.2 报警"持续提醒"的状态（2026-09-26 新增，见 `_drive_persistent_alert`）----
        #: 上次重发提示的时间（0 = 还没有过）
        self._last_re_alert: float = 0.0
        #: 当前报警灯的 (颜色, 是否在闪)；用来避免每帧重复下发 GPIO
        self._alert_light: Optional[Tuple[str, bool]] = None

        # ---- 3.5 上云（可选；没配置或没装 paho 就整体降级，绝不影响本地监护） ----
        # 本项目实际使用的云平台是 **OneNET**（旧版 MQTT物联网套件，数据流-数据点）；
        # 通用 MQTT 保留作为备选（例如自建 EMQX / 巴法云）。两者**只启用一个**：
        # 若 onenet.enabled 与 mqtt.enabled 同时为真，优先 OneNET 并在日志里说明。
        self.mqtt: Optional[Any] = None
        self._mqtt_last_publish: float = 0.0
        self.cloud_platform = "none"      # none / onenet / mqtt
        self.cloud_warning = ""

        onenet_enabled = bool(config.onenet.get("enabled"))
        mqtt_enabled = bool(config.mqtt.get("enabled"))
        if onenet_enabled and mqtt_enabled:
            self.cloud_warning = "onenet.enabled 与 mqtt.enabled 同时为真，已优先使用 OneNET（请关掉其一）"
            _LOG.warning("%s", self.cloud_warning)

        if onenet_enabled:
            try:
                from .net.onenet import OneNetConfig, OneNetPublisher

                onenet_cfg = OneNetConfig.from_dict(config.onenet)
                self.mqtt = OneNetPublisher(onenet_cfg, clock=clock)
                self.cloud_platform = "onenet"
            except Exception as exc:  # noqa: BLE001 - 云配置写错不该让整机起不来
                self.cloud_warning = f"OneNET 配置解析失败（已降级为不上云）：{exc}"
                _LOG.warning("%s", self.cloud_warning)
        else:
            try:
                from .net.mqtt import MqttConfig, MqttPublisher

                mqtt_cfg = MqttConfig.from_dict(config.mqtt) if config.mqtt else MqttConfig()
            except Exception as exc:  # noqa: BLE001 - 配置写错不该让整机起不来
                self.cloud_warning = f"MQTT 配置解析失败（已忽略上云）：{exc}"
                _LOG.warning("%s", self.cloud_warning)
            else:
                publisher = MqttPublisher(mqtt_cfg)
                if mqtt_cfg.enabled:
                    self.cloud_platform = "mqtt"
                else:
                    publisher.reason = publisher.reason or "配置里 mqtt.enabled=false（未启用上云）"
                self.mqtt = publisher   # 保留对象以便 status() 里能看到"为什么没上云"
        self.mqtt_started = False

        # ---- 4. 运行状态 ----
        self._events: List[AlarmEvent] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._server: Any = None
        self._last_stale_alert: float = 0.0
        self.ticks = 0
        #: 云端（OneNET 规则引擎）推送回来的最近若干条报文（只记录，不驱动报警）
        self._cloud_pushes: List[Dict[str, Any]] = []
        self.cloud_push_keep = 20
        self.cloud_push_count = 0

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def open(self) -> Dict[str, str]:
        """打开全部设备（先输入后输出），返回失败清单（空字典=全成功）。

        ⚠️ **输出器件也必须打开**：它们被刻意排除在采集调度之外（不该被当传感器轮询），
        但如果不显式打开，`send()` 会因 ``DeviceNotReady`` 全部失败——
        表现是"报警判出来了、蜂鸣器和屏幕却毫无反应"（2026-09-21 实测踩到）。
        """
        errors = self.collector.open_all()
        for name, device in self.outputs.items():
            try:
                device.open()
            except Exception as exc:  # noqa: BLE001 - 一个输出器件坏了不该让整机起不来
                errors[name] = f"{type(exc).__name__}: {exc}"
                _LOG.warning("输出器件 %s 打开失败：%s", name, errors[name])
        self.assembly_errors.update(errors)

        # 启动提示：让屏幕/LED/音箱立刻给出"我活着"的反馈。
        # 这条容易漏，但很有用：现场演示时，一看屏幕就知道服务起来了，
        # 而不是"等了半天不知道有没有在跑"。
        try:
            startup = AlarmEvent(
                ts=self.clock(), code=AlarmCode.SYSTEM_START, severity=Severity.NOTICE,
                message="监护系统已启动", source="service",
            )
            self.dispatcher.dispatch(startup, startup.ts)
        except Exception as exc:  # noqa: BLE001 - 启动提示失败绝不该影响服务启动
            _LOG.debug("启动提示下发失败（已忽略）：%s", exc)
        return errors

    def close(self) -> None:
        """停止服务并释放资源（幂等）。"""
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        self._thread = None
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception as exc:  # noqa: BLE001
                _LOG.debug("关闭 HTTP 服务异常（已忽略）：%s", exc)
            self._server = None
        self.collector.close_all()
        for name, device in self.outputs.items():
            try:
                device.close()
            except Exception as exc:  # noqa: BLE001
                _LOG.debug("输出器件 %s 关闭异常（已忽略）：%s", name, exc)
        if self.mqtt is not None:
            try:
                self.mqtt.stop()
            except Exception as exc:  # noqa: BLE001
                _LOG.debug("MQTT 关闭异常（已忽略）：%s", exc)
            self.mqtt_started = False

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def tick(self) -> List[AlarmEvent]:
        """执行**一帧**（采集 → 判定 → 下发 → 落库），返回本帧报警事件。

        单测直接调它，配合假时钟与假 sleep 就能确定性地跑完整个链路。
        """
        now = self.clock()
        self.ticks += 1
        self.collector.collect_due()
        snap = self.collector.snapshot()

        # ---- 实体按键：先处理（"按下去"应当立刻生效，而不是等下一帧判定）----
        # ⚠️ 2026-09-26 之前这条链是断的：驱动能报 CLICK / LONG_PRESS，但业务层没人消费
        #    ⇒ 真机上按实体键毫无反应（`docs/14` 的 T3）。这里把它接上，
        #    并且**复用 `sos()` / `silence()`** —— HTTP 接口走的就是这两个动作，行为一致。
        button_alarms = self._consume_button_events(now)

        # 所有输入设备都拿不到数据时，自发一条故障报警（避免"静默失能"）
        if self.inputs and not self._has_any_reading(snap):
            if (now - self._last_stale_alert) >= self.config.thresholds.repeat_cooldown_s:
                self._last_stale_alert = now
                snap.sensor_failures.setdefault("__all__", self.config.thresholds.sensor_fault_after)
                snap.sensor_errors.setdefault("__all__", "所有传感器均无有效数据")

        events = self.engine.evaluate(snap)
        for event in events:
            self.dispatcher.dispatch(event, now)
            if self.store is not None:
                self.store.save_alarm(event)
            if self.mqtt is not None and self.mqtt_started:
                self.mqtt.publish_alarm(event)
        if events:
            self._events.extend(events)
            self._events = self._events[-500:]
            for event in events:
                _LOG.info("[报警] %s %s", event.code.value, event.message)

        # ---- 报警"持续提醒"（重发 + 灯持续闪 / 消音后常亮）----
        self._drive_persistent_alert(now)

        # 上云：按 interval_s 周期发布读数摘要（失败只计数，不影响本地）
        # ⚠️ 周期也**跑在假时钟下**：演示/单测推进时钟即可触发上报，不必真的等 30 秒。
        if self.mqtt is not None and self.mqtt_started:
            interval = float(getattr(self.mqtt.config, "interval_s", 30.0))
            if (now - self._mqtt_last_publish) >= interval:
                self._mqtt_last_publish = now
                try:
                    self.mqtt.publish_reading(snap.health_summary(), now)
                except TypeError:
                    # 通用 MQTT 发布器只接受 summary（OneNET 版多一个 ts 参数）
                    self.mqtt.publish_reading(snap.health_summary())

        # 定期清理过期历史（每小时一次足够）
        if self.store is not None and self.ticks % 3600 == 0:
            try:
                self.store.prune(now)
            except Exception as exc:  # noqa: BLE001
                _LOG.debug("清理历史失败（已忽略）：%s", exc)
        # 按键引发的报警（SOS）**已经在下发时就地处理过**，这里只是把它一起返回，
        # 让调用方（演示/日志/单测）看到"这一帧发生过什么"；不会二次下发。
        return events + button_alarms

    def _top_active_code(self, active: Dict[str, float]) -> Any:
        """从"仍在报警的项"里挑**最该被提醒**的那个（红 > 黄 > 绿，同色取最早触发）。"""
        order = {"red": 0, "yellow": 1, "green": 2}

        def rank(item: Tuple[str, float]) -> Tuple[int, float]:
            name, first_ts = item
            try:
                code: Any = AlarmCode(name)
            except ValueError:  # pragma: no cover - 理论上不会发生
                code = name
            plan = self.dispatcher.plan_for(code)
            return (order.get(str(plan.light), 3), float(first_ts))

        return min(active.items(), key=rank)[0]

    def _drive_persistent_alert(self, now: float) -> None:
        """报警"持续提醒"：**按周期重发**提示，并让报警灯**持续闪**（消音后转常亮）。

        为什么需要它（2026-09-26 真机 T3，用户实测反馈）：
        * 原来报警**只在事件发生那一帧下发一次**（`SENSOR_FAULT` 响 2 声就结束）⇒
          ① 安全性不足：老人房间里"响两声就完"等于没报警；
          ② **"消音"在真机上根本没有可观察的效果**（没有持续的声音可停）；
        * 灯原来 `blink=True` = 驱动里**阻塞**闪 3 秒后常亮 ⇒ 用户看到的其实是"灯不闪"，
          而且那 3 秒**阻塞主循环**（不采样、不响应按键）。
        现在：未消音期间每 `re_alert_interval_s` 秒重发一次（响 + 刷屏，灯保持闪）；
        用户短按消音后**不再响**、灯转**常亮**（"灯仍亮"是本项目的明确设计）。
        """
        interval = float(getattr(self.config.thresholds, "re_alert_interval_s", 0.0) or 0.0)
        active = self.engine.active_alarms()          # {报警码字符串: 首次触发时间}
        if not active:
            self._last_re_alert = 0.0
            self._alert_light = None
            return
        if interval <= 0:
            return                                    # 0 = 关闭持续提醒（回到"只提示一次"）

        silenced = self.dispatcher.is_silenced(now)
        code = self._top_active_code(active)
        plan = self.dispatcher.plan_for(code)
        want_blink = bool(plan.blink) and not silenced
        desired = (str(plan.light), want_blink)

        if self._alert_light != desired:
            # 状态变了（刚报警 / 刚消音 / 报警颜色变了）⇒ 立刻把灯切过去并重新计时
            self.dispatcher.alert_light(code, now, blink=want_blink)
            self._alert_light = desired
            self._last_re_alert = now
            return

        if not silenced and (now - self._last_re_alert) >= interval:
            self._last_re_alert = now
            self.dispatcher.re_alert(code, now)
            _LOG.info(
                "[报警] 持续提醒：%s 仍在报警，每 %.0fs 重发一次（第 %d 次）",
                getattr(code, "value", code), interval, self.dispatcher.realerts,
            )

    def _consume_button_events(self, now: float) -> List[AlarmEvent]:
        """把采集器攒下的实体按键事件变成动作：**短按消音 / 长按求助**。

        设计取舍（为什么放在这里，而不是放进规则引擎）：
        - 按键是**动作**，不是"状态"：规则引擎只判"数据是否越界"，不认"刚刚按了一下"；
        - 动作要**立刻生效**：所以在本帧的规则判定之前处理，
          这样"按下消音"能同时压住本帧即将下发的报警声；
        - **复用 `sos()` / `silence()`**：与 HTTP `/api/v1/sos`、`/api/v1/silence` 完全同一条路径
          （`sos()` 会先解除静音——求救不能被之前的静音挡住）。

        Returns:
            由按键产生的报警事件（目前只有 SOS；CLICK 只是消音，不产生事件）。
        """
        produced: List[AlarmEvent] = []
        drain = getattr(self.collector, "drain_button_events", None)
        if not callable(drain):  # pragma: no cover - 兼容老的自定义采集器
            return produced
        for event in drain():
            action = getattr(event, "action", None)
            if action is ButtonAction.LONG_PRESS:
                _LOG.info("[按键] 长按 %.1fs：触发紧急求助", getattr(event, "pressed_for_s", 0.0))
                produced.append(self.sos(now))
            elif action is ButtonAction.CLICK:
                _LOG.info("[按键] 短按：消音（灯仍亮）")
                self.silence(now)
        return produced

    @staticmethod
    def _has_any_reading(snap: Any) -> bool:
        return any(
            sample is not None
            for sample in (snap.vitals, snap.body_temp, snap.ambient, snap.motion)
        )

    def run_forever(self) -> None:
        """阻塞运行主循环，直到 :meth:`stop` 被调用。"""
        _LOG.info("监护服务已启动（mock=%s，设备 %d 个）", self.mock, len(self.devices))
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - 主循环绝不能因单帧异常退出
                _LOG.exception("主循环单帧异常（已忽略并继续）")
            wait = min(self.collector.next_due_in(), 1.0)
            self._sleep(max(0.02, wait))

    def start_background(self) -> threading.Thread:
        """在后台线程跑主循环（供 ``serve`` 命令使用）。

        同时尝试启动 MQTT 上报（没配置或没装 paho-mqtt 时**只记日志**，不影响主循环）。
        """
        if self.mqtt is not None and not self.mqtt_started:
            self.mqtt_started = bool(self.mqtt.start())
            if self.mqtt_started:
                self.mqtt.publish_status(self.status())
        self._thread = threading.Thread(target=self.run_forever, name="monitor-loop", daemon=True)
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        self._stop.set()

    def start_http(self, host: str = "0.0.0.0", port: int = 8080, token: str = "") -> Any:
        """启动 HTTP API（后台线程），返回 server 对象。"""
        api = WebApi(self, token=token)
        self._server, _ = start_in_thread(api, host=host, port=port)
        return self._server

    # ------------------------------------------------------------------
    # 业务动作（HTTP 接口与按键都走这里，保证行为一致）
    # ------------------------------------------------------------------

    def sos(self, ts: Optional[float] = None) -> AlarmEvent:
        """触发一次紧急求助（按钮按下 / 手机端点"求助"）。"""
        event = AlarmEvent(
            ts=ts if ts is not None else now_ts(),
            code=AlarmCode.SOS_PRESSED,
            severity=Severity.CRITICAL,
            message="已收到紧急求助，请立即查看",
            source="sos",
        )
        self.dispatcher.unsilence()          # 求救必须能响：先解除静音
        self.dispatcher.dispatch(event, event.ts)
        if self.store is not None:
            self.store.save_alarm(event)
        self._events.append(event)
        return event

    def silence(self, ts: Optional[float] = None) -> None:
        """消音：一段时间内只亮灯、不响铃、不播报。"""
        self.dispatcher.silence(ts if ts is not None else now_ts())

    # ------------------------------------------------------------------
    # 云云对接：接收 OneNET 规则引擎的 HTTP 推送
    # ------------------------------------------------------------------

    def record_cloud_push(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """记录一条来自云平台（OneNET 规则引擎 HTTP 推送）的报文。

        设计取舍：
        - **只记录，不改监护状态**。云端转发回来的数据是我们自己刚上报的，
          再拿它去驱动报警会形成"自己喂自己"的环（一旦云端重发就可能误报）；
        - 保留最近 ``cloud_push_keep`` 条，供状态页与 ``/api/v1/cloud/last`` 查看；
        - **只记结构不外传**：这里的内容仍属本机，不会被再次上报。

        Returns:
            规范化后的记录（含 ``ts`` / ``payload``），便于 HTTP 端点回显。
        """
        entry = {
            "ts": float(record.get("ts") or self.clock()),
            "payload": record.get("payload"),
            "query": record.get("query") or {},
        }
        self._cloud_pushes.append(entry)
        self._cloud_pushes = self._cloud_pushes[-self.cloud_push_keep:]
        self.cloud_push_count += 1
        try:
            preview = json.dumps(entry["payload"], ensure_ascii=False)[:400]
        except Exception:  # noqa: BLE001 - 记录日志不该因为序列化失败而中断
            preview = str(entry["payload"])[:400]
        _LOG.info("[云端推送] #%d %s", self.cloud_push_count, preview)
        return entry

    def recent_cloud_pushes(self, limit: int = 20) -> List[Dict[str, Any]]:
        """最近收到的云端推送（新的在后）。"""
        return self._cloud_pushes[-limit:]

    def clear_alarms(self, ts: Optional[float] = None) -> AlarmEvent:
        """人工确认：清除全部报警态，并**把输出器件复位到正常状态**。

        ⚠️ 只说"清除了报警态"是不够的：LED 若停在红色、LCD 若停在 "SOS"，
        用户会以为还在报警（2026-09-21 演示实测踩到）。
        因此这里必须**真的下发一次 ALL_CLEAR**，而不是只改内部状态。
        """
        now = ts if ts is not None else now_ts()
        self.engine.clear_active()
        event = AlarmEvent(
            ts=now, code=AlarmCode.ALL_CLEAR, severity=Severity.NORMAL,
            message="已确认，监护恢复正常", source="operator",
        )
        self.dispatcher.dispatch(event, now)
        if self.store is not None:
            self.store.save_alarm(event)
        return event

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------

    def recent_events(self, limit: int = 50) -> List[AlarmEvent]:
        return self._events[-limit:]

    def status(self) -> Dict[str, Any]:
        """整体状态（给 ``main.py status`` 与自检脚本用）。"""
        return {
            "version": self.version,
            "mock": self.mock,
            "config_path": self.config_path,
            "uptime_s": round(self.clock() - self.started_at, 1),
            "ticks": self.ticks,
            "devices": {
                "configured": [c.name for c in self.config.enabled_devices()],
                "assembled": sorted(self.devices),
                "inputs": sorted(self.inputs),
                "outputs": sorted(self.outputs),
                "errors": dict(self.assembly_errors),
            },
            "collector": self.collector.status(),
            "dispatcher": self.dispatcher.status(),
            "active_alarms": self.engine.active_alarms(),
            "mqtt": (
                self.mqtt.status() if self.mqtt is not None
                else {"enabled": False, "reason": "未创建"}
            ),
            "cloud": {
                "platform": self.cloud_platform,
                "warning": self.cloud_warning,
                "started": self.mqtt_started,
            },
            "store": self.store.stats() if self.store else None,
        }

    def device_report(self) -> Dict[str, Any]:
        """每个设备的接线与自检信息（``docs`` 生成与 ``selfcheck`` 命令用）。"""
        report: Dict[str, Any] = {}
        for name, device in sorted(self.devices.items()):
            entry: Dict[str, Any] = {"driver": getattr(device, "NAME", ""), "mock": device.mock}
            try:
                entry["describe"] = device.describe()
            except Exception as exc:  # noqa: BLE001
                entry["describe"] = {"error": f"{type(exc).__name__}: {exc}"}
            try:
                entry["self_check"] = device.self_check()
            except Exception as exc:  # noqa: BLE001
                entry["self_check"] = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
            report[name] = entry
        return report


# --------------------------------------------------------------------------
# 便捷构造
# --------------------------------------------------------------------------


def build_runtime(
    config_path: Optional[str | Path] = None,
    mock: bool = True,
    store_path: Optional[str] = "data/history.db",
    dispatcher_enabled: bool = True,
) -> Runtime:
    """按配置文件构造运行时（命令行入口用它）。"""
    config = load_config(config_path)
    path_text = str(config_path) if config_path else ""
    return Runtime(
        config,
        mock=mock,
        store_path=store_path,
        dispatcher_enabled=dispatcher_enabled,
        config_path=path_text,
    )


__all__ = ["Runtime", "build_runtime", "VERSION"]