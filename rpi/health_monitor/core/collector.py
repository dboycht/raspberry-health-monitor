"""采集调度：按各设备的周期轮询，汇总成 :class:`ReadingSnapshot`。

核心设计（为什么这么写）
------------------------
1. **每个设备有自己的周期**：MAX30102 可以 1 秒读一次，DHT11 必须 ≥2 秒（硬件限制），
   PIR 想 0.5 秒一次。调度器按"下次到期时间"挑设备读，而不是统一节拍。
2. **陈旧数据必须自曝**：某个传感器连续读不到时，绝不继续拿旧值冒充当前值——
   超过 ``stale_factor × 周期`` 没读到新数据，该字段就置 ``None`` 并记为失败。
   这是健康监护系统的红线：**"不知道"必须和"正常"区分开**。
3. **单次读取失败不打断其他设备**：一个器件挂了，其它器件照常采。
4. **可注入时钟**：测试用假时钟驱动，不 sleep、不偶发失败。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..hal.device import Device
from ..hal.exceptions import DeviceError
from ..hal.models import (
    AmbientSample,
    ButtonAction,
    ButtonEvent,
    MotionSample,
    MotionState,
    PrecisionTempSample,
    RangeSample,
    Sample,
    VitalSignsSample,
    now_ts,
)
from .config import AppConfig
from .rules import ReadingSnapshot
from .store import Store

_LOG = logging.getLogger(__name__)


class _Entry:
    """一个设备的运行状态（采集器内部用）。"""

    __slots__ = (
        "name", "driver", "device", "interval", "last_sample",
        "last_ok_ts", "last_attempt_ts", "next_due_ts", "failures", "last_error", "optional",
    )

    def __init__(self, name: str, driver: str, device: Device, interval: float, optional: bool) -> None:
        self.name = name
        self.driver = driver
        self.device = device
        self.interval = interval
        self.optional = optional
        self.last_sample: Optional[Sample] = None
        self.last_ok_ts: Optional[float] = None
        self.last_attempt_ts: Optional[float] = None
        #: 下一次到期时间。用**显式时间点**而不是"间隔是否已过"，可以避免
        #: 浮点误差让刚刚尝试过的设备立刻又被判定为到期（重复读取）。
        self.next_due_ts: Optional[float] = None
        self.failures = 0
        self.last_error: Optional[str] = None

    def schedule_next(self, now: float) -> None:
        """安排在 ``now + interval`` 到期。

        ⚠️ **失败也要安排下次**：否则坏掉的设备会被无限重试打爆总线/CPU。
        """
        self.next_due_ts = now + self.interval

    def ready(self, now: float) -> bool:
        """是否到期（从未读过 → 立即到期）。"""
        return self.next_due_ts is None or now >= self.next_due_ts


class Collector:
    """采集调度器。

    Args:
        config: 应用配置（提供设备清单与周期）。
        devices: 已装配好的设备实例，键为**配置里的设备名**（如 ``"vitals"``）。
                 由 :mod:`health_monitor.service` 负责装配，本类不管怎么造出来。
        store: 历史存储（可为 ``None``，表示只跑内存不落库）。
        clock: 时间源，返回 Unix 秒（默认 :func:`time.time`；测试注入假时钟）。
        stale_factor: 超过 ``stale_factor × 周期`` 没读到新数据即认为"陈旧"。
    """

    def __init__(
        self,
        config: AppConfig,
        devices: Dict[str, Device],
        store: Optional[Store] = None,
        clock: Callable[[], float] = time.time,
        stale_factor: float = 3.0,
    ) -> None:
        self.config = config
        self.store = store
        self.clock = clock
        self.stale_factor = float(stale_factor)
        self.entries: Dict[str, _Entry] = {}
        self._faulted: set = set()          # 已经报过"故障"的设备名（防止重复记录）
        for name, device in devices.items():
            cfg = config.device(name)
            if cfg is None:
                raise KeyError(f"设备 {name} 不在配置里（装配与配置不一致）")
            self.entries[name] = _Entry(name, cfg.driver, device, cfg.read_interval_s, cfg.optional)
        self.sensor_failures: Dict[str, int] = {}
        self.sensor_errors: Dict[str, str] = {}
        self.read_counts: Dict[str, int] = {name: 0 for name in self.entries}
        #: 实体按键的**动作事件**队列（只攒 CLICK / LONG_PRESS，见 `drain_button_events`）。
        #: 为什么用队列而不是塞进快照：按键事件是"一次性动作"，而快照是"当前状态"——
        #: 放进快照会被下一帧的 NONE 覆盖掉（驱动空闲时返回 action=NONE），
        #: 于是"按一下消音"会时灵时不灵（2026-09-26 接这条链路时特意避开）。
        self._button_events: List[ButtonEvent] = []

    # ------------------------------------------------------------------
    # 主循环入口
    # ------------------------------------------------------------------

    def next_due_in(self) -> float:
        """距下一次有设备到期的秒数（主循环用它决定睡多久；已到期返回 0）。"""
        now = self.clock()
        waits = [
            max(0.0, e.next_due_ts - now)
            for e in self.entries.values()
            if e.next_due_ts is not None
        ]
        return min(waits) if waits else 0.0

    def collect_due(self) -> List[Tuple[str, Sample]]:
        """读取所有**已到期**的设备，返回 ``[(设备名, 样本), ...]``。

        - 单个设备读取失败**不影响**其它设备；
        - 返回的样本里可能带 ``ok=False``（数据不可信），业务层按需处理；
        - 无论成功失败都会重排下次到期时间（失败不重排会导致忙等）。
        """
        now = self.clock()
        results: List[Tuple[str, Sample]] = []
        for name, entry in self.entries.items():
            if not entry.ready(now):
                continue
            sample = self._read_one(entry, now)
            entry.schedule_next(now)
            results.append((name, sample))
            if self.store is not None:
                self.store.save_sample(sample)
        return results

    def _read_one(self, entry: _Entry, now: float) -> Sample:
        """读一个设备，并把"成功/失败/陈旧"如实登记。"""
        entry.last_attempt_ts = now
        try:
            sample = entry.device.read()
        except DeviceError as exc:
            return self._mark_failure(entry, now, f"{type(exc).__name__}: {exc}", exc)
        except Exception as exc:  # noqa: BLE001 - 驱动写错了也不能让采集停摆
            return self._mark_failure(entry, now, f"驱动异常 {type(exc).__name__}: {exc}", exc)

        # DHT11 这类器件有硬件限制（两次读取间隔必须 ≥2 秒）：间隔不足时驱动会
        # **返回带缓存值的样本**。各驱动作者对 ``ok`` 的取法略有差异，这里两种都认：
        #   - ``ok=False`` 且带值 + ``is_cached=True``（DHT11 驱动的选法，最诚实）
        #   - ``ok=True`` 且 ``is_cached=True``
        # 缓存**不算读取失败**（那是器件的正常行为），但也不刷新 ``last_ok_ts``，
        # 因此"连续多轮只拿到缓存"最终仍会被 stale 保护判成陈旧。
        cached = _is_cached_sample(sample)
        if not sample.ok and not cached:
            return self._mark_failure(entry, now, sample.error or "样本标记为无效", None)

        entry.failures = 0
        entry.last_error = None
        if entry.name in self._faulted:
            self._faulted.discard(entry.name)
            _LOG.info("设备 %s 已恢复正常读取", entry.name)
        entry.last_sample = sample
        if not cached:
            entry.last_ok_ts = now
        else:
            _LOG.debug("设备 %s 返回缓存值（器件正常行为，非故障）：%s", entry.name, sample.error or "")
        self._remember_button_event(entry, sample)
        self.read_counts[entry.name] = self.read_counts.get(entry.name, 0) + 1
        return sample

    def _mark_failure(self, entry: _Entry, now: float, error: str, exc: Optional[BaseException]) -> Sample:
        entry.failures += 1
        entry.last_error = error
        self.sensor_failures[entry.name] = entry.failures
        self.sensor_errors[entry.name] = error
        if entry.name not in self._faulted:
            self._faulted.add(entry.name)
            if entry.optional:
                _LOG.info("可选设备 %s 读取失败（已忽略）：%s", entry.name, error)
            else:
                _LOG.warning("设备 %s 读取失败（第 %d 次）：%s", entry.name, entry.failures, error)
        # 失败时**必须**产出一个 ok=False 的样本，让上层知道"这一轮没有数据"
        return _failed_sample(entry, now, error)

    # ------------------------------------------------------------------
    # 快照
    # ------------------------------------------------------------------

    def snapshot(self) -> ReadingSnapshot:
        """把各设备最近一次有效读数汇总成 :class:`ReadingSnapshot`。

        ⚠️ **陈旧保护**：超过 ``stale_factor × 周期`` 没读到新值的字段一律置 ``None``
        并在 ``sensor_failures`` 里体现——**不允许用旧值冒充当前状态**。
        """
        now = self.clock()
        snap = ReadingSnapshot(ts=now)
        failures: Dict[str, int] = {}
        errors: Dict[str, str] = {}

        for name, entry in self.entries.items():
            stale = self._is_stale(entry, now)
            if entry.failures > 0:
                failures[name] = entry.failures
                errors[name] = entry.last_error or "未知错误"
            elif stale:
                # 之前读到过，但已经太久没更新：同样按"数据不可用"处理
                failures[name] = self.config.thresholds.sensor_fault_after
                errors[name] = f"数据陈旧（超过 {self.stale_factor:g} × {entry.interval:g}s 未更新）"
            sample = None if stale else entry.last_sample
            self._fill(snap, name, sample)

        # PIR 的"距上次检测到人的秒数"
        motion_entry = self._find_entry_by_kind("motion")
        if motion_entry is not None:
            silent = self._motion_silent_s(motion_entry, now)
            if silent is not None:
                snap.motion_silent_s = silent

        # ---- 新鲜度（给手机端看"服务在跑但数据是不是旧的"）----
        # 阈值刻意用"所有设备都至少该更新过一轮"的尺度：设备数 × 3 × 最长周期。
        # 这样单个慢器件（如 3 秒周期的 DHT11）不会立刻把整份快照判成陈旧。
        newest = [e.last_ok_ts for e in self.entries.values() if e.last_ok_ts is not None]
        snap.data_age_s = (now - max(newest)) if newest else None
        if self.entries:
            longest = max(e.interval for e in self.entries.values())
            snap.data_stale_after_s = max(10.0, len(self.entries) * self.stale_factor * longest)

        snap.sensor_failures = failures
        snap.sensor_errors = errors
        return snap

    def _fill(self, snap: ReadingSnapshot, name: str, sample: Optional[Sample]) -> None:
        """把一个设备的样本填进快照对应字段（按样本类型分派，**不认具体驱动类**）。"""
        if sample is None:
            return
        if isinstance(sample, VitalSignsSample):
            snap.vitals = sample
        elif isinstance(sample, PrecisionTempSample):
            snap.body_temp = sample
        elif isinstance(sample, AmbientSample):
            snap.ambient = sample
        elif isinstance(sample, MotionSample):
            snap.motion = sample
        elif isinstance(sample, RangeSample):
            # 距离暂不参与报警判定，但保留在快照里供拓展功能使用
            setattr(snap, "range", sample)
        else:
            _LOG.debug("设备 %s 返回的样本类型 %s 未参与报警判定", name, type(sample).__name__)

    def _is_stale(self, entry: _Entry, now: float) -> bool:
        if entry.last_sample is None or entry.last_ok_ts is None:
            return True
        return (now - entry.last_ok_ts) > self.stale_factor * entry.interval

    def _motion_silent_s(self, entry: _Entry, now: float) -> Optional[float]:
        """计算"距上次检测到人"的秒数。

        优先用驱动自己维护的值（它知道器件延时设置），拿不到就退回"距上次样本时间"。
        返回 ``None`` 表示**无法判断**（而不是"很久没动"）。
        """
        device = entry.device
        getter = getattr(device, "seconds_since_motion", None)
        if callable(getter):
            try:
                value = getter()
                if value is not None:
                    return float(value)
            except Exception as exc:  # noqa: BLE001 - 拿不到就退回，不打断快照
                _LOG.debug("设备 %s 的 seconds_since_motion() 失败：%s", entry.name, exc)
        sample = entry.last_sample
        if isinstance(sample, MotionSample):
            if sample.state is MotionState.DETECTED:
                return 0.0
            if entry.last_ok_ts is not None:
                return max(0.0, now - entry.last_ok_ts)
        return None

    def _find_entry_by_kind(self, kind: str) -> Optional[_Entry]:
        for entry in self.entries.values():
            if getattr(entry.device, "KIND", None) is not None and entry.device.KIND.value == kind:
                return entry
        return None

    # ------------------------------------------------------------------
    # 实体按键事件（短按消音 / 长按求助）
    # ------------------------------------------------------------------

    def _remember_button_event(self, entry: _Entry, sample: Sample) -> None:
        """把"按下了"这类**动作事件**攒进队列（其余样本类型原样忽略）。"""
        if not isinstance(sample, ButtonEvent):
            return
        if sample.action not in (ButtonAction.CLICK, ButtonAction.LONG_PRESS):
            return
        self._button_events.append(sample)
        # 上限保护：万一没人消费（比如没接业务层的自定义用法），队列也不会无限长
        if len(self._button_events) > 64:
            self._button_events = self._button_events[-64:]

    def drain_button_events(self) -> List[ButtonEvent]:
        """取出并清空按键动作事件（由 :class:`~health_monitor.service.Runtime` 每帧调用）。

        Returns:
            自上次调用以来发生的 CLICK / LONG_PRESS 事件（按发生顺序）。
        """
        events = list(self._button_events)
        self._button_events.clear()
        return events

    # ------------------------------------------------------------------
    # 生命周期与诊断
    # ------------------------------------------------------------------

    def open_all(self) -> Dict[str, str]:
        """打开所有设备。返回 ``{设备名: 错误信息}``（空字典=全部成功）。

        **不做 fail-fast**：一个器件没接好不该让整机起不来；
        但失败信息必须原样返回，由服务层决定"是否继续"（可选件允许失败）。
        """
        errors: Dict[str, str] = {}
        for name, entry in self.entries.items():
            try:
                entry.device.open()
            except Exception as exc:  # noqa: BLE001
                errors[name] = f"{type(exc).__name__}: {exc}"
                _LOG.warning("设备 %s 打开失败：%s", name, errors[name])
        return errors

    def close_all(self) -> None:
        """关闭所有设备（幂等；任何单个关闭失败都不影响其它）。"""
        self._button_events.clear()          # 停机后不该再残留"没处理的按键动作"
        for entry in self.entries.values():
            try:
                entry.device.close()
            except Exception as exc:  # noqa: BLE001
                _LOG.debug("设备 %s 关闭异常（已忽略）：%s", entry.name, exc)

    def status(self) -> Dict[str, Any]:
        """运行状态（供 ``/api/v1/health`` 与日志）。"""
        now = self.clock()
        devices: Dict[str, Any] = {}
        for name, entry in self.entries.items():
            devices[name] = {
                "driver": entry.driver,
                "interval_s": entry.interval,
                "reads": self.read_counts.get(name, 0),
                "failures": entry.failures,
                "last_error": entry.last_error,
                "stale": self._is_stale(entry, now),
                "last_ok_age_s": (None if entry.last_ok_ts is None else round(now - entry.last_ok_ts, 1)),
                "optional": entry.optional,
                "device_status": entry.device.status(),
            }
        return {"ts": now, "devices": devices, "faulted": sorted(self._faulted)}


def _is_cached_sample(sample: Any) -> bool:
    """判断样本是不是"器件缓存值"（值可用，但不是刚测的）。

    认两种驱动写法：``is_cached=True``（无论 ``ok`` 取值）。
    """
    return bool(getattr(sample, "is_cached", False))


def _failed_sample(entry: _Entry, now: float, error: str) -> Sample:
    """按设备当前返回过的样本类型，造一个 ``ok=False`` 的样本。

    这样业务层拿到的类型始终一致（不会因为失败就变成"未知类型"）。
    """
    prev = entry.last_sample
    kwargs = {"ts": now, "device": entry.name, "ok": False, "error": error}
    if isinstance(prev, VitalSignsSample):
        return VitalSignsSample(**kwargs)
    if isinstance(prev, PrecisionTempSample):
        return PrecisionTempSample(**kwargs)
    if isinstance(prev, AmbientSample):
        return AmbientSample(**kwargs)
    if isinstance(prev, MotionSample):
        return MotionSample(state=MotionState.UNKNOWN, **kwargs)
    if isinstance(prev, RangeSample):
        return RangeSample(**kwargs)
    # 从未成功读过：根据驱动名猜一个合理类型（首次失败时走到这里）
    guess = {
        "max30102": VitalSignsSample,
        "tmp36": PrecisionTempSample,
        "mcp3002": PrecisionTempSample,
        "dht11": AmbientSample,
        "hc_sr501": lambda **kw: MotionSample(state=MotionState.UNKNOWN, **kw),
        "hc_sr04": RangeSample,
    }.get(entry.driver)
    if guess is None:
        return Sample(**kwargs)
    return guess(**kwargs)


__all__ = ["Collector"]
