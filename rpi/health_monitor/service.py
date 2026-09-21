"""服务装配与主循环 —— **整个系统唯一"知道所有零件"的地方**。

其他模块的分工：
- ``hal/`` 定义契约；``sensors/``、``outputs/`` 实现器件；
- ``core/`` 只做业务（不认识具体器件类）；
- ``net/`` 只管对外接口；
- **本文件**负责：读配置 → 造器件 → 装配 → 跑主循环 → 处理报警。

主循环一帧做的事
----------------
1. 采集所有到期的设备（``Collector.collect_due``）；
2. 汇总快照（``Collector.snapshot``，含"陈旧即空"的保护）；
3. 规则引擎判定（``RuleEngine.evaluate``）；
4. 报警下发（``AlarmDispatcher.dispatch``）、落库、记录；
5. 按最近到期时间睡眠（不空转烧 CPU）。

额外兜底：**所有传感器都读不到时**（例如一次性拔了线），
会自发一条 ``SENSOR_FAULT``，避免"系统还在跑但什么都测不到"却悄无声息。
"""

from __future__ import annotations

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
from .hal.models import AlarmCode, AlarmEvent, Severity, now_ts
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

        # ---- 4. 运行状态 ----
        self._events: List[AlarmEvent] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._server: Any = None
        self._last_stale_alert: float = 0.0
        self.ticks = 0

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
        if events:
            self._events.extend(events)
            self._events = self._events[-500:]
            for event in events:
                _LOG.info("[报警] %s %s", event.code.value, event.message)

        # 定期清理过期历史（每小时一次足够）
        if self.store is not None and self.ticks % 3600 == 0:
            try:
                self.store.prune(now)
            except Exception as exc:  # noqa: BLE001
                _LOG.debug("清理历史失败（已忽略）：%s", exc)
        return events

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
        """在后台线程跑主循环（供 ``serve`` 命令使用）。"""
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