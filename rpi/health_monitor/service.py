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
from .core.config import AppConfig, DeviceConfig, default_config_path, load_config
from .core.configstore import ConfigStore
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

#: 彩屏信息页每隔多少秒翻一页（E57）。0 或负数 = 不轮播。
DISPLAY_PAGE_INTERVAL_S = 6.0

#: 发出报警后，信息页**至少**让位多少秒（E57）：报警文案是"要人立刻看到"的，
#: 不能被信息页马上冲掉。规则引擎判出来的报警由 `active_alarms()` 一直挡着；
#: 这个"保持时间"管的是**一次性事件**（如用户长按 SOS —— 它不进规则引擎的 active 表）。
DISPLAY_PAGE_HOLD_AFTER_ALARM_S = 30.0

#: 各信息页停留多少个"基本周期"（2026-09-30 用户要求"TFT 主要显示环境条件"）。
#: 页 0 = **环境页**，停 2 倍时间 ⇒ 主人大部分时候看到的是室温/湿度/有没有人；
#: 其余页各 1 倍。**页数 = len(PAGE_DWELL_UNITS)**，改这里要同步改
#: :data:`health_monitor.outputs.tft_spi.PAGE_ASCII`（两者长度必须一致）。
PAGE_DWELL_UNITS: Tuple[int, ...] = (2, 1, 1)

#: LCD **调试面板**的刷新周期（秒）。0 或负数 = 不刷（LCD 就只当报警屏用）。
DEBUG_PANEL_INTERVAL_S = 2.0

# --------------------------------------------------------------------------
# 按需测血氧（2026-09-30 用户要求的「测血氧开关」）
# --------------------------------------------------------------------------

#: 配置里「测血氧」按键的设备名。**功能总开关就是它的 ``enabled``**：
#: 置 false ⇒ 连"定期叫人测血氧"一起关掉（不留半开状态）。
SPO2_BUTTON_NAME = "spo2_button"

#: 「提示 / 测量」文案的重发周期（秒）。必须重发，否则会被别的刷屏操作顶掉。
SPO2_NOTICE_INTERVAL_S = 2.0

#: 结果（成功读数 / 没测到）在屏上停留多久，再让位给信息页与调试面板。
SPO2_RESULT_HOLD_S = 8.0

#: 三段文案（每行 ≤16 字符、纯 ASCII）。
#: ⚠️ 第二段 **就是 `ERROR.md` E60 的产品化处置**：实测"按紧"会把血氧从 96.7 压到
#: 93.9（物理性伪迹、改预处理去不掉，而本项目**无真值参照**所以**不改公式**），
#: 于是改成在这里当场提示"轻贴别压" —— 在正确的时候给用户正确的提示。
#: ⚠️ 第一段（叫人）**必须把两种选择写在屏上**（2026-10-01）：老人得知道"我现在不想测"
#: 该怎么表达，否则他只能干等超时（见下面 :data:`SPO2_REQ_DECLINE` 的说明）。
SPO2_PROMPT_LINES: Tuple[str, str] = ("SPO2 CHECK NOW?", "CLICK=Y HOLD=N")
SPO2_MEASURE_LINES: Tuple[str, str] = ("FINGER ON PPG", "TOUCH LIGHTLY")
SPO2_FAIL_LINES: Tuple[str, str] = ("NO READING", "TRY AGAIN LATER")

#: 「测血氧」按键的**两种有类型的请求**。
#:
#: 为什么要"有类型"（2026-10-01 用户 review 指出的**真实设计缺陷**）：
#: 原来把 ``CLICK`` 与 ``LONG_PRESS`` **都当成"同意"** ⇒ **物理按键上不存在"拒绝"**：
#: 老人不想测时只能干等 ``spo2_remind_timeout_s``（默认 60 秒）超时，
#: 而这段时间**两块屏一直被提示占着**、他无法表达"我现在不测"。
#: 这与本项目 SOS 键的既有做法（**短按消音 / 长按求助**）不一致 ——
#: 属于**功能退化**，不是"设计取舍"。
#:
#: ⚠️ 语义边界（很容易写错，测试有双向钉子）：**``LONG_PRESS`` 只在 ``prompt`` 阶段
#: 等于"否决"**；``idle`` / ``measure`` / ``result`` 阶段它与 ``CLICK`` **完全等价**
#: （不给用户"长按了却没反应"的挫败感）。
SPO2_REQ_MEASURE = "measure"
SPO2_REQ_DECLINE = "decline"

#: 蜂鸣声数（沿用本项目的"人耳暗号"：1 声 = 注意 / 2 声 = 成功 / 5 声 = 失败）
SPO2_PROMPT_BEEPS = 2    # 叫人："该测血氧了"
SPO2_START_BEEPS = 1     # 开始测量
SPO2_DONE_BEEPS = 2      # 测到了
SPO2_FAIL_BEEPS = 5      # 没测到


def _pir_tag(motion: Any) -> str:
    """人体活动标记：``PIR Y``（检测到人）/ ``PIR N``（没人）/ ``PIR -``（没数据）。

    放在**环境页**上，是因为"家里有没有人"属于环境状态，而且它比心率血氧"长驻"
    （心率血氧要人主动伸手去测，没人测的时候屏上是空窗的）。
    """
    state = getattr(getattr(motion, "state", None), "value", None)
    if state is None:
        return "PIR -"
    return "PIR Y" if state == "detected" else "PIR N"


def display_page_lines(
    page: int,
    snap: Any,
    alarm_count: int = 0,
    last_alarm: str = "",
) -> Tuple[str, str]:
    """三个信息页的文案（**纯函数，可单测**；每行 ≤16 字符、纯 ASCII）。

    ⚠️ 两条纪律（都是本项目真机踩过的）：

    * **只能 ASCII**：彩屏是 8×8 点阵字库，中文会变成 `?`（`ERROR.md` E53 一节）；
    * **每行 ≤16 字符**：这是 LCD 与彩屏（1 倍字号）**共同的**字符上限 —— 超了就会被截断
      （E53 的另一半：`WAKE UP TOO OFTEN` 17 字符连 LCD 都放不下）。

    页序（**2026-09-30 调整**：用户要求"TFT 主要显示环境条件"）：

    0 **环境**（室温/湿度/有没有人，**主页面**，停留时间是其他页的 2 倍）/
    1 心率血氧 / 2 状态与报警记录。

    页数必须与 :data:`PAGE_DWELL_UNITS` 一致（轮播按它取模）。
    """
    vitals = getattr(snap, "vitals", None)
    ambient = getattr(snap, "ambient", None)
    motion = getattr(snap, "motion", None)
    failures = getattr(snap, "sensor_failures", None) or {}
    page = int(page) % len(PAGE_DWELL_UNITS)

    if page == 0:      # 环境页（主页面）
        if ambient is not None and getattr(ambient, "ok", False):
            line1 = "ROOM %4.1f C" % float(ambient.temperature_c)
            head = "HUM %3.0f%%" % float(ambient.humidity_percent)
        else:
            line1, head = "ROOM --", "HUM  --"
        return (line1, (head + " " + _pir_tag(motion))[:16])
    if page == 1:      # 心率血氧
        if vitals is not None and getattr(vitals, "ok", False):
            return (
                "HR %3.0f BPM" % float(vitals.heart_rate_bpm),
                "SPO2 %3.0f %%" % float(vitals.spo2_percent),
            )
        if vitals is not None and getattr(vitals, "finger_detected", False):
            return ("VITALS MEASURING", "PLEASE WAIT")
        return ("NO FINGER", "PLACE ON SENSOR")
    # 状态 / 报警记录
    if failures:
        return ("FAULT %d" % len(failures), "CHECK DEVICE")
    tail = (last_alarm or "NONE")[:10].upper()
    return (("ALARMS %d" % int(alarm_count))[:16], ("LAST " + tail)[:16])


def _has_usable_vitals(sample: Any) -> bool:
    """样本是不是**真的**能拿来报数：``ok=True`` **且**两个数值都在。

    为什么不能只看 ``ok``（2026-09-30 写测血氧时踩到，见 `ERROR.md` **E62**）：
    ``Sample.ok`` 只是个**标记**，真实驱动与模拟器都可能给出
    ``ok=True`` 但字段是 ``None`` 的样本（模拟器在"没贴手指"时就是这样）。
    只信标记 ⇒ ``float(None)`` **当场抛 TypeError**，而这段代码跑在 ``tick()``
    的调用链里 ⇒ **会把整个采集循环打断**（比报错本身严重得多）。

    ⇒ 判据要看**真值**，不能只看布尔标记。这与 `ERROR.md` E60 那条
    "没有真值参照就不许改公式"是同一族纪律：先问"这个标记到底保证了什么"。
    """
    if sample is None or not getattr(sample, "ok", False):
        return False
    return (
        getattr(sample, "heart_rate_bpm", None) is not None
        and getattr(sample, "spo2_percent", None) is not None
    )


def _median(values: List[float]) -> float:
    """中位数（**纯函数，可单测**）。调用方负责先判空。

    为什么测量结果要用中位数而不是"最后一次读数"（2026-09-30 真机教训）：
    第一次真机验收时，第二轮测量报出了 **HR 111 bpm**（恰好越过 `hr_max=110`）
    ⇒ 规则引擎**当场报了一次真的 `hr_too_high`** 并持续提醒 4 次。
    单点读数会把"手刚放上去 / 动了一下"的瞬时值当成结论。
    `scripts/vitals_check.py`（T7 的官方验收工具）一直用的是**窗口内中位数** ——
    这里与它统一口径，瞬时不稳就削掉。
    """
    ordered = sorted(float(v) for v in values)
    n = len(ordered)
    if n == 0:
        return 0.0
    mid = n // 2
    return ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0


def debug_lines(
    snap: Any,
    ticks: int = 0,
    failures: int = 0,
    alarm_count: int = 0,
) -> Tuple[str, str]:
    """LCD **调试面板**的两行文案（**纯函数，可单测**；每行 ≤16 字符、纯 ASCII）。

    为什么有它（2026-09-30 用户定调）：**TFT 彩屏是给主人看的信息面板，LCD 是开发/调试面板**。
    所以这里放"开发时要盯的量"，不是给人看的友好文案：

    * 第 1 行 ``T=23.4 H=57%`` —— 环境读数（判断 DHT11 活没活、值合不合理）；
    * 第 2 行 ``N=1234 F=0 A=0`` —— **帧数 / 故障器件数 / 活动报警数**
      （帧数在涨 = 主循环活着；``F>0`` = 有器件读不到；``A>0`` = 正在报警）。

    没有读数时用 ``--`` 而**不是 0**（E58 的同一课：**"没有读数" 与 "读数是 0" 必须能分开**）。
    """
    ambient = getattr(snap, "ambient", None)
    if ambient is not None and getattr(ambient, "ok", False):
        line1 = "T=%.1f H=%.0f%%" % (
            float(ambient.temperature_c),
            float(ambient.humidity_percent),
        )
    else:
        line1 = "T=-- H=--"
    line2 = "N=%d F=%d A=%d" % (int(ticks), int(failures), int(alarm_count))
    return (line1[:16], line2[:16])


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
        #: 配置面板的读写器：**只有本进程确实绑定了配置文件时才有**。
        #: 空字符串（例如单测直接构造 Runtime）⇒ 面板的写接口会**明确拒绝**，
        #: 而不是去猜一个路径、把仓库里的配置改掉。
        self.config_store: Optional[ConfigStore] = ConfigStore(config_path) if config_path else None
        self.version = VERSION
        self.started_at = clock()
        self._device_factory = device_factory or create_device
        # 彩屏信息页轮播（E57）：当前页 / 上次翻页时刻。**只在没有活动报警时翻页**
        #: ``-1`` = "还一页都没显示过"：第一次轮播会落到**页 0（环境页）**，
        #: 这样服务一起来，彩屏第一眼就是环境条件，而不是先闪一下别的页。
        self._page = -1
        self._last_page_ts = 0.0
        #: LCD 调试面板的上次刷新时刻（2026-09-30；**只发 LCD**，不碰彩屏）
        self._last_debug_ts = 0.0
        #: 上次"发出报警"的时刻（含一次性 SOS）：信息页要给它让够时间（E57）
        self._last_alarm_ts = -float("inf")

        # ---- 「按需测血氧」状态机（2026-09-30）----
        #: ``idle`` / ``prompt``（叫人等他按键）/ ``measure``（测量窗口）/ ``result``（结果停留）
        self._spo2_state = "idle"
        #: 当前阶段的截止时刻（提醒超时 / 测量到点 / 结果停留结束）
        self._spo2_deadline = 0.0
        #: 下一次"叫人测血氧"的时刻。**用绝对时刻排下一轮**（不用累加，避免漂移）。
        self._spo2_next_remind_ts = self.started_at + float(config.thresholds.spo2_remind_interval_s)
        #: 本帧「测血氧」按键的**有类型请求**：``None`` / ``SPO2_REQ_MEASURE`` /
        #: ``SPO2_REQ_DECLINE``（由 :meth:`_consume_button_events` 置位，:meth:`_drive_spo2` 消费）。
        #: 为什么不是一个布尔量：布尔量**表达不了"拒绝"**，那正是 2026-10-01 修掉的缺陷。
        self._spo2_request: Optional[str] = None
        #: 累计数（**只在内存里，不落库** —— 它是"运行期交互统计"，重启即归零才是对的）
        self._spo2_accepted_total = 0
        self._spo2_declined_total = 0
        #: 最近一次测量的结果（成功/失败**都记**，给面板显示"上次结果"）
        self._spo2_last_result: Optional[Dict[str, Any]] = None
        #: 测量窗口内**所有**有效读数（结尾取中位数报结果，见 :func:`_median`）
        self._spo2_samples: List[Any] = []
        #: 上次喂提示的时刻（提示要周期性重发）
        self._spo2_last_notice_ts = 0.0

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
    # 配置热应用（2026-10-01，Web 配置面板）
    # ------------------------------------------------------------------

    def apply_config(self, config: AppConfig) -> Tuple[List[str], List[str]]:
        """把一份新配置**热应用**到正在运行的实例上，返回 ``(applied, warnings)``。

        这是面板能改的全部三件事：**阈值 / 读取周期 / 器件开关**。

        设计取舍（为什么这么做）：

        * **阈值**：规则引擎每一帧都读 ``self.engine.th``，直接换掉那个对象即可 ——
          下一帧就用新值判定，不必重启、也不必通知任何人；
        * **周期**：只改 ``_Entry.interval``，并且**只在必要时提前**下次到期时间
          （``min(原值, now + 新周期)``）。刻意**不无条件重排**：把周期从 1 秒改成 60 秒时，
          若无条件重排，本来 0.2 秒后就该读的那次会被推后一分钟，现场看起来像"卡死了"；
        * **开关**：关掉 = ``close()`` + 从**三张表**（runtime.devices / collector.entries /
          dispatcher.outputs）里摘干净；打开 = 装配 → ``open()`` → 接进这三张表。

        ⚠️ **单个器件打不开不算失败**：没接线是常态，错误进 ``warnings``，
        该器件记进 ``assembly_errors``（= "已启用但装配失败"），HTTP 照样 200。
        理由：**配置本身是好的**，只是这台机器上没接 —— 不该让整次保存都失败，
        否则用户改个阈值会被"另一个器件没接"连带挡住。
        """
        applied: List[str] = []
        warnings: List[str] = []
        now = self.clock()
        old = self.config

        # ---- ① 阈值 ----
        if old.thresholds != config.thresholds:
            applied.append("报警阈值已更新（规则引擎下一帧起按新值判定）")
        self.engine.th = config.thresholds
        # ⚠️ 改了"叫人间隔"必须**顺带重排**已经排定的下一轮（`ERROR.md` **E67**）：
        #    `_spo2_next_remind_ts` 是**绝对时刻**，它是在"上一轮结束时"按**当时的**间隔
        #    算出来的。只换阈值对象**不会**重排 ⇒ 用户把间隔从 600 秒改成 15 秒，
        #    蜂鸣器**仍要等到原来那个 10 分钟后的时刻**才叫 ——
        #    现场看起来就是"改了没用 / 坏了"（2026-09-30 用户实测撞到，还以为是面板没生效）。
        if old.thresholds.spo2_remind_interval_s != config.thresholds.spo2_remind_interval_s:
            self._spo2_reschedule(now, float(config.thresholds.spo2_remind_interval_s))
            applied.append("测血氧叫人间隔已重排")

        # ---- ② 读取周期 ----
        for cfg in config.devices:
            entry = self.collector.entries.get(cfg.name)
            if entry is None or entry.interval == cfg.read_interval_s:
                continue
            entry.interval = float(cfg.read_interval_s)
            due = now + entry.interval
            if entry.next_due_ts is None or entry.next_due_ts > due:
                entry.next_due_ts = due
            applied.append(f"{cfg.name}.read_interval_s 改为 {entry.interval:g} 秒")

        # ---- ③ 器件开关 ----
        #
        # ⚠️ 这里刻意按**实际运行状态**（`self.devices`）对齐，而不是按"上一份配置里的 enabled"
        #    对齐：启动时装配失败的器件**从来没进过 `self.devices`**，若按配置差异判断，
        #    它在面板上会永远显示"已启用"、却永远接不上 —— 只能重启服务才能再试一次。
        #    按实际状态对齐之后，**修好接线再点一次保存**就把它接回来了（有测试钉住）。
        should_on = {d.name for d in config.enabled_devices()}
        is_on = set(self.devices)
        for name in sorted(is_on - should_on):
            self._detach_device(name)
            applied.append(f"已关闭并移除设备 {name}")
        for name in sorted(should_on - is_on):
            problem = self._attach_device(config.device(name))
            if problem:
                warnings.append(f"设备 {name} 已按配置启用，但本次没能打开：{problem}")
            else:
                applied.append(f"已启用设备 {name}")

        # ---- ④ 让采集器与规则引擎看到同一份配置 ----
        # 采集器会读 config.thresholds.sensor_fault_after（判定"连续失败几次算故障"），
        # 所以它的 config 引用也必须换掉，否则面板改的那个值在这条路径上不生效。
        self.config = config
        self.collector.config = config
        return applied, warnings

    def _attach_device(self, cfg: Optional[DeviceConfig]) -> str:
        """装配 + 打开 + 接进三张表。**失败返回错误文本，绝不抛异常。**"""
        if cfg is None:
            return "配置里找不到该设备"
        try:
            device = self._device_factory(
                cfg.driver, params=cfg.params, mock=self.mock, name=cfg.name,
            )
        except Exception as exc:  # noqa: BLE001 - 驱动缺失/参数错都算"这台机器上开不起来"
            self.assembly_errors[cfg.name] = f"{type(exc).__name__}: {exc}"
            _LOG.warning("启用设备 %s（驱动 %s）装配失败：%s", cfg.name, cfg.driver, exc)
            return f"装配失败（{type(exc).__name__}: {exc}）"
        try:
            device.open()
        except Exception as exc:  # noqa: BLE001
            # 打不开就**别留半个设备在表里**：半开状态会让 status() 里它显示"正常"，
            # 却永远读不到数据 —— 那种"看起来在跑其实没跑"最难查。
            self.assembly_errors[cfg.name] = f"{type(exc).__name__}: {exc}"
            _LOG.warning("启用设备 %s 打开失败：%s", cfg.name, exc)
            try:
                device.close()
            except Exception:  # noqa: BLE001
                pass
            return f"打开失败（{type(exc).__name__}: {exc}）"

        self.devices[cfg.name] = device
        if isinstance(device, OutputDevice):
            self.outputs[cfg.name] = device
            self.dispatcher.add_output(cfg.name, device)
        else:
            self.inputs[cfg.name] = device
        self.collector.add_entry(cfg, device)
        self.assembly_errors.pop(cfg.name, None)
        _LOG.info("已启用设备 %s（驱动 %s）", cfg.name, cfg.driver)
        return ""

    def _detach_device(self, name: str) -> None:
        """关闭并摘下器件：**三张表都要摘干净**，否则会留下"幽灵器件"。"""
        device = self.devices.pop(name, None)
        self.inputs.pop(name, None)
        if self.outputs.pop(name, None) is not None:
            self.dispatcher.remove_output(name)
        self.collector.remove_entry(name)
        self.assembly_errors.pop(name, None)
        if device is None:
            return
        try:
            device.close()
        except Exception as exc:  # noqa: BLE001 - 关不掉也不该影响"它已经被移出系统"这个事实
            _LOG.debug("关闭设备 %s 异常（已忽略）：%s", name, exc)

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
            self._last_alarm_ts = now          # E57：信息页要给报警文案让时间
            for event in events:
                _LOG.info("[报警] %s %s", event.code.value, event.message)

        # ---- 报警"持续提醒"（重发 + 灯持续闪 / 消音后常亮）----
        self._drive_persistent_alert(now)

        # ---- 按需测血氧（叫人 → 按键 → 测量 → 报结果；**先于刷屏**，提示优先）----
        self._drive_spo2(now, snap)

        # ---- 彩屏信息页轮播（只在**没有活动报警**时翻页，见 E57）----
        self._rotate_display_page(now, snap)

        # ---- LCD 调试面板刷新（同样是"报警优先"；**只发 LCD**，绝不碰彩屏）----
        self._refresh_debug_panel(now, snap)

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

    def _rotate_display_page(self, now: float, snap: Any) -> None:
        """彩屏信息页轮播（E57）。

        规则（都有理由）：

        * **只在没有活动报警时翻页**：报警文案是"要人立刻看到"的，不能被信息页冲掉
          （LCD 与彩屏同属 ``DeviceKind.DISPLAY``，所以轮播**只发给彩屏驱动**，见
          :meth:`AlarmDispatcher.show_page`）；
        * 按 :data:`DISPLAY_PAGE_INTERVAL_S` × **当前页的停留倍数**计时
          （:data:`PAGE_DWELL_UNITS`；页 0 = 环境页停 2 倍，让"环境条件"成为主人
          最常看到的一页 —— 2026-09-30 用户要求）；
        * 页序：0 环境 → 1 心率血氧 → 2 状态/报警 → 回到 0。
        """
        if DISPLAY_PAGE_INTERVAL_S <= 0:
            return
        if self._spo2_busy():                  # 测血氧提示优先：别把它冲掉
            return
        if self.engine.active_alarms():        # 报警优先：有活动报警就不翻页
            return
        if (now - self._last_alarm_ts) < DISPLAY_PAGE_HOLD_AFTER_ALARM_S:
            return                             # 刚发过报警（含一次性 SOS）：先让报警文案待够时间
        total = len(PAGE_DWELL_UNITS)
        if self._page >= 0:
            dwell = DISPLAY_PAGE_INTERVAL_S * PAGE_DWELL_UNITS[int(self._page) % total]
            if (now - self._last_page_ts) < dwell:
                return                         # 当前页还没停够（环境页停得更久）
        self._last_page_ts = now
        self._page = (int(self._page) + 1) % total
        last = self._events[-1].code.value if self._events else ""
        lines = display_page_lines(self._page, snap, alarm_count=len(self._events), last_alarm=last)
        self.dispatcher.show_page(lines, self._page, now)

    def _refresh_debug_panel(self, now: float, snap: Any) -> None:
        """LCD 调试面板刷新（2026-09-30：**LCD 专职当开发/调试面板**）。

        与信息页轮播**共用同一套"让位"规则**（刻意对称，好记也好测）：

        * 有活动报警 ⇒ 不刷 —— 报警文案要留在屏上；
        * 刚发过报警 ⇒ :data:`DISPLAY_PAGE_HOLD_AFTER_ALARM_S` 秒内不刷（同上）；
        * 否则每 :data:`DEBUG_PANEL_INTERVAL_S` 秒刷一次。

        ⚠️ **只发给 LCD**（见 :meth:`AlarmDispatcher.show_debug`）：调试信息若广播出去，
        会把彩屏正在显示的信息页冲掉 —— 那正是 E57 的镜像问题。
        """
        if DEBUG_PANEL_INTERVAL_S <= 0:
            return
        if self._spo2_busy():                  # 测血氧提示优先（与信息页轮播对称）
            return
        if self.engine.active_alarms():
            return
        if (now - self._last_alarm_ts) < DISPLAY_PAGE_HOLD_AFTER_ALARM_S:
            return
        if (now - self._last_debug_ts) < DEBUG_PANEL_INTERVAL_S:
            return
        self._last_debug_ts = now
        lines = debug_lines(
            snap,
            ticks=self.ticks,
            failures=len(getattr(snap, "sensor_failures", None) or {}),
            alarm_count=len(self.engine.active_alarms()),
        )
        self.dispatcher.show_debug(lines, now)

    # ------------------------------------------------------------------
    # 按需测血氧（2026-09-30：蜂鸣器叫人 → 按键 → 测量 → 报结果）
    # ------------------------------------------------------------------

    def _spo2_enabled(self) -> bool:
        """「测血氧」功能是否开启。

        **总开关 = 配置里 ``spo2_button`` 的 ``enabled``**（用户 2026-09-30 定的口径）：
        关掉它就把"专用按键"和"定期叫人"**一起**关掉，不留半开状态。
        """
        cfg = self.config.device(SPO2_BUTTON_NAME)
        if cfg is None or not getattr(cfg, "enabled", False):
            return False
        return SPO2_BUTTON_NAME in self.devices

    def _spo2_busy(self) -> bool:
        """是否正处在"提示 / 测量 / 结果"阶段 —— 这三段时间要让开两块屏。"""
        return self._spo2_state != "idle"

    def _spo2_show(self, lines: Tuple[str, str], now: float, force: bool = False) -> None:
        """喂一条提示。**报警优先**：有活动报警时绝不覆盖报警文案。"""
        if self.engine.active_alarms():
            return
        if not force and (now - self._spo2_last_notice_ts) < SPO2_NOTICE_INTERVAL_S:
            return
        self._spo2_last_notice_ts = now
        self.dispatcher.show_notice(lines, now)

    def _spo2_schedule_next(self, now: float) -> None:
        """排下一轮"叫人"。``spo2_remind_interval_s <= 0`` ⇒ 不再定期叫人（只留主动测）。"""
        interval = float(self.config.thresholds.spo2_remind_interval_s)
        self._spo2_next_remind_ts = (now + interval) if interval > 0 else float("inf")

    def _spo2_reschedule(self, now: float, interval: float) -> None:
        """**配置里改了叫人间隔**时重排下一轮（`ERROR.md` **E67**）。

        两条规矩，都是为了"不让人意外"：

        * **只提前、不推后**（``min(原值, now + 新间隔)``）—— 与 `apply_config` 里
          "改读取周期"的处理完全一致。反例：把间隔从 15 秒调成 600 秒时若无条件重排，
          本该 5 秒后就到的那一轮会被推到 10 分钟后，现场像"卡死"；
        * **只在待机时重排** —— 正在叫人/测量/出结果的那一轮**不许被打断**
          （用户正看着屏，改了配置不该把它搅掉）。
        """
        if self._spo2_state != "idle":
            return
        if interval <= 0:
            self._spo2_next_remind_ts = float("inf")
            return
        self._spo2_next_remind_ts = min(self._spo2_next_remind_ts, now + float(interval))

    def _spo2_begin_prompt(self, now: float) -> None:
        """叫人"该测血氧了"（蜂鸣 + 屏上提示），然后等他按键。"""
        th = self.config.thresholds
        self._spo2_state = "prompt"
        self._spo2_deadline = now + float(th.spo2_remind_timeout_s)
        self._spo2_last_notice_ts = 0.0
        self.dispatcher.notice_beep(SPO2_PROMPT_BEEPS, now)
        _LOG.info("[测血氧] 该测血氧了：蜂鸣 %d 声 + 屏上提示，等按键（%.0f 秒超时）",
                  SPO2_PROMPT_BEEPS, float(th.spo2_remind_timeout_s))
        self._spo2_show(SPO2_PROMPT_LINES, now, force=True)

    def _spo2_begin_measure(self, now: float) -> None:
        """开始测量窗口（用户按键了）。屏上那句提示**就是 E60 的产品化处置**。"""
        th = self.config.thresholds
        self._spo2_state = "measure"
        self._spo2_deadline = now + float(th.spo2_measure_s)
        self._spo2_samples = []
        self._spo2_last_notice_ts = 0.0
        self.dispatcher.notice_beep(SPO2_START_BEEPS, now)
        _LOG.info("[测血氧] 开始测量（%.0f 秒）：请把食指指腹轻贴 MAX30102、"
                  "**别用力压**（按紧会让血氧偏低，见 ERROR.md E60）", float(th.spo2_measure_s))
        self._spo2_show(SPO2_MEASURE_LINES, now, force=True)

    def _spo2_finish_measure(self, now: float) -> None:
        """测量结束：有有效读数就报**窗口中位数**，没有就如实说"没测到"（**不报警**）。"""
        usable = [s for s in self._spo2_samples if _has_usable_vitals(s)]
        hr: Optional[float] = None
        spo2: Optional[float] = None
        if usable:
            hr = _median([float(s.heart_rate_bpm) for s in usable])
            spo2 = _median([float(s.spo2_percent) for s in usable])
            lines = ("HR %3.0f BPM" % hr, "SPO2 %3.0f %%" % spo2)
            beeps = SPO2_DONE_BEEPS
            _LOG.info("[测血氧] 测量完成：心率 %.0f bpm、血氧 %.0f %%（%d 个有效读数取中位数）",
                      hr, spo2, len(usable))
        else:
            lines = SPO2_FAIL_LINES
            beeps = SPO2_FAIL_BEEPS
            _LOG.info("[测血氧] 测量结束但没拿到有效读数（手指没贴好 / 中途移开）")
        # 记下"最近一次结果"给面板看：**失败也要记**（不然面板会一直显示上一次的成功值，
        # 让用户以为这次也测到了）。
        self._spo2_last_result = {
            "ok": bool(usable),
            "heart_rate_bpm": (round(hr, 1) if hr is not None else None),
            "spo2_percent": (round(spo2, 1) if spo2 is not None else None),
            "ts": now,
        }
        self._spo2_state = "result"
        self._spo2_deadline = now + SPO2_RESULT_HOLD_S
        self._spo2_last_notice_ts = 0.0
        self.dispatcher.notice_beep(beeps, now)
        self._spo2_show(lines, now, force=True)
        self._spo2_schedule_next(now)

    def _spo2_give_up(self, now: float) -> None:
        """本轮没测成（叫人后没人按 / 用户否决）⇒ 回到待机并排下一轮。**不报警。**"""
        self._spo2_state = "idle"
        self._spo2_samples = []
        self._spo2_request = None
        # ★ **把两块屏立刻交还**：本方法跑在 `_rotate_display_page` / `_refresh_debug_panel`
        #   **之前**，所以把两个"上次刷新时刻"清零 ⇒ **同一帧内**信息页与调试面板就会回来。
        #   为什么必须这样：用户说"我现在不测"之后，屏上还挂着"要不要测"是**没有反馈的反馈** ——
        #   他会怀疑"到底按没按上"。清零比"等最多 12 秒自然刷新"好得多，而且零成本。
        self._last_page_ts = 0.0
        self._last_debug_ts = 0.0
        self._spo2_schedule_next(now)

    # ---- 对外动作（物理按键与 HTTP **共用这三个方法**）----

    def spo2_measure_now(self, now: float) -> bool:
        """请求**现在测一次**。物理短按与 ``POST /api/v1/spo2/measure`` 走同一条路径。

        Returns:
            ``True`` = 接受；``False`` = 功能没开（调用方据此返回 409）。

        各状态下的含义（与物理按键表完全一致）：

        * ``prompt``  ⇒ **接受叫人**，开始测量；
        * ``idle``    ⇒ 主动测（不必等叫人）；
        * ``measure`` ⇒ **提前结束**（不等满 ``spo2_measure_s``）；
        * ``result``  ⇒ 再测一次。

        ⚠️ 为什么做成 Runtime 的公开方法、而不是让 HTTP 自己改状态：
        **物理按键与 HTTP 必须是同一条路径** —— 这是本项目反复吃过的亏
        （`sos()` / `silence()` 的复用、E57 的屏分工都是同一条纪律）。
        """
        if not self._spo2_enabled():
            return False
        if self._spo2_state == "measure":
            self._spo2_finish_measure(now)      # 提前结束**不算**一次新的"接受"
            return True
        self._spo2_accepted_total += 1
        self._spo2_begin_measure(now)
        return True

    def spo2_decline(self, now: float) -> bool:
        """**否决这一轮提醒**（"我现在不测"）。只在 ``prompt`` 阶段有意义。

        Returns:
            ``True`` = 确实否决掉了这一轮；
            ``False`` = 功能没开，**或当前不在叫人阶段** —— 调用方据此返回 **409**，
            而不是"假装成功"（假装成功会让面板显示一个没发生过的动作）。
        """
        if not self._spo2_enabled():
            return False
        if self._spo2_state != "prompt":
            return False
        self._spo2_declined_total += 1
        _LOG.info("[测血氧] 用户**暂不检测**：本轮叫人被否决 ⇒ 放弃本轮（**不报警**），"
                  "按 %.0f 秒间隔排下一轮",
                  float(self.config.thresholds.spo2_remind_interval_s))
        self._spo2_give_up(now)
        return True

    def spo2_status(self, now: float) -> Dict[str, Any]:
        """「测血氧」当前状态（面板 2 秒轮询 + ``GET /api/v1/spo2`` 共用）。**纯读，不改状态。**"""
        left: Optional[float] = None
        if self._spo2_state in ("prompt", "measure"):
            left = round(max(0.0, self._spo2_deadline - now), 1)
        return {
            "ok": True,
            "enabled": self._spo2_enabled(),
            "state": self._spo2_state,
            "prompt_left_s": left if self._spo2_state == "prompt" else None,
            "measure_left_s": left if self._spo2_state == "measure" else None,
            "accepted_total": self._spo2_accepted_total,
            "declined_total": self._spo2_declined_total,
            "last_result": dict(self._spo2_last_result) if self._spo2_last_result else None,
        }

    def _drive_spo2(self, now: float, snap: Any) -> None:
        """推进「按需测血氧」状态机（每帧调一次）。

        状态流：``idle`` --(到点叫人)--> ``prompt`` --(短按接受)--> ``measure``
        --> ``result`` --(停留够)--> ``idle``；
        ``prompt`` 阶段**长按 = 否决** ⇒ 直接回 ``idle``（也是"本轮结束"的一种）。

        任一步都可能"没成"，而且**都不报警**：
        ``prompt`` 超时 ⇒ 直接回 ``idle``；``measure`` 没读到 ⇒ 屏上说"没测到"。
        （设计取舍：这是"请老人配合量一下"的**提示**，不是"出事了"的**报警**；
        混进报警流水只会污染 `/api/v1/alarms` 与报警历史。）

        ⚠️ 与报警的关系：**报警优先** —— 有活动报警时不覆盖报警文案，
        但状态机本身继续走（不会因为"正好报了个警"就把这次测量废掉）。

        ⚠️ 本方法只负责**计时器与消费按键请求**；"用户动作"一律走
        :meth:`spo2_measure_now` / :meth:`spo2_decline`，HTTP 也是调那两个 ——
        所以两条入口的行为**必然一致**，不会各写一套。
        """
        if not self._spo2_enabled():
            self._spo2_state = "idle"
            self._spo2_request = None
            return

        request = self._spo2_request
        self._spo2_request = None
        th = self.config.thresholds

        if self._spo2_state == "idle":
            if request == SPO2_REQ_MEASURE:
                self.spo2_measure_now(now)          # 主动测：不必等叫人
            elif float(th.spo2_remind_interval_s) > 0 and now >= self._spo2_next_remind_ts:
                self._spo2_begin_prompt(now)
            return

        if self._spo2_state == "prompt":
            if request == SPO2_REQ_DECLINE:
                self.spo2_decline(now)              # 否决：本轮结束
            elif request == SPO2_REQ_MEASURE:
                self.spo2_measure_now(now)          # 接受：开始测
            elif now >= self._spo2_deadline:
                _LOG.info("[测血氧] 叫人后 %.0f 秒内既没接受也没否决，本轮放弃（**不报警**）",
                          float(th.spo2_remind_timeout_s))
                self._spo2_give_up(now)
            else:
                self._spo2_show(SPO2_PROMPT_LINES, now)
            return

        if self._spo2_state == "measure":
            vitals = getattr(snap, "vitals", None)
            if _has_usable_vitals(vitals):
                self._spo2_samples.append(vitals)    # 攒窗口内的样本，结尾取中位数
            if request == SPO2_REQ_MEASURE:
                self.spo2_measure_now(now)           # 再按一下 = 提前结束
            elif now >= self._spo2_deadline:
                self._spo2_finish_measure(now)
            else:
                self._spo2_show(SPO2_MEASURE_LINES, now)
            return

        # state == "result"：让结果在屏上停够时间，再交给信息页 / 调试面板
        if request == SPO2_REQ_MEASURE:
            self.spo2_measure_now(now)               # 结果还没消失就又按了 = 马上再测
        elif now >= self._spo2_deadline:
            self._spo2_state = "idle"

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
            who = getattr(event, "device", "") or ""
            if who == SPO2_BUTTON_NAME:
                # 「测血氧」按键（2026-10-01 起是**双语义**，与 SOS 键的"短按消音/长按求助"一致）：
                #   正在叫人时：短按 = **接受**（开始测） / 长按 = **否决**（本轮不测）
                #   其它阶段　：短按与长按**完全等价**，都说"我要测"
                #              （长按不给"按了却没反应"的挫败感 —— 测试有反向钉子）
                if action is ButtonAction.LONG_PRESS and self._spo2_state == "prompt":
                    self._spo2_request = SPO2_REQ_DECLINE
                elif action in (ButtonAction.CLICK, ButtonAction.LONG_PRESS):
                    self._spo2_request = SPO2_REQ_MEASURE
                else:
                    continue          # RELEASE 之类不表达意图，别把它当成一次请求
                _LOG.info("[按键] 「测血氧」按键 %s（当前阶段 %s）⇒ 请求=%s",
                          getattr(action, "value", action), self._spo2_state,
                          self._spo2_request)
                # ⚠️ **必须 continue**：绝不能让这个按键落到下面的"消音 / 求救"分支里去
                #    （否则"想测血氧"会变成"消音"甚至"SOS"）。这就是 E61 的教训。
                continue
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
        self._last_alarm_ts = event.ts       # E57：SOS 也要把彩屏"占住"一段时间
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
    # 记录**生效的**路径：Web 配置面板要往这个文件里写。
    # 为 None 时用默认路径（与 load_config 的解析一致）—— 否则会变成
    # "面板能打开、一保存就说没有配置文件"，纯属自找的坑。
    path_text = str(config_path) if config_path else str(default_config_path())
    return Runtime(
        config,
        mock=mock,
        store_path=store_path,
        dispatcher_enabled=dispatcher_enabled,
        config_path=path_text,
    )


__all__ = ["Runtime", "build_runtime", "VERSION"]