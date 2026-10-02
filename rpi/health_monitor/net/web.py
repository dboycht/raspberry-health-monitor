"""HTTP API（纯标准库 ``http.server``，树莓派上零额外依赖）。

为什么不用 Flask/FastAPI：部署在树莓派上，"一条命令就能跑起来"比框架特性重要；
本服务的接口很少（读数据 + 几个控制指令），标准库完全够用，且不用装依赖。

接口一览（完整说明见 ``docs/05-安卓通信协议.md``）::

    GET  /api/v1/health          系统与设备健康状态（含每个设备的读取计数/失败数）
    GET  /api/v1/current         当前读数（扁平摘要，缺失值为 null）
    GET  /api/v1/history         历史曲线（?metric=ambient_temp_c&limit=120）
    GET  /api/v1/alarms          最近报警事件（?limit=50）
    GET  /api/v1/devices         设备清单与接线说明（describe() 的输出）
    GET  /api/v1/config          当前配置（全部阈值 + 每个设备的开关/周期）
    POST /api/v1/config          改配置（**部分合并**；写盘 + 立即生效，不重启）
    POST /api/v1/silence         消音（用户按消音键 / 手机端点"我知道了"）
    POST /api/v1/sos             手机端触发一次紧急求助

网页：
    GET  /                       状态页（只读，自动刷新）
    GET  /panel                  配置面板（**会改配置**，见下）

安全约定
--------
- 只用于**局域网**：不要在公网直接暴露（树莓派上请只在家庭内网使用）；
- 可选 ``token``：配置了 token 时，所有请求必须带请求头 ``X-Auth-Token``，
  否则被拒绝（**一律 401**）。**默认不带 token**（局域网演示方便），
  上公网前务必配。
- ⚠️ **配置写入默认不设防**（用户明确要求的演示取舍）：只要没配 ``--token``，
  任何能访问到本机 8080 端口的人都能改阈值与器件开关。
  **`/panel` 页面顶部有醒目提示**，别把它当"内部工具"随便暴露。
- ⚠️ **配置接口只能改"阈值 + 器件开关 / 读取周期"，不能改引脚**（传 `params` 一律 400）。
  这条是**故意的**：正因为写操作不设防，就**不能**同时给它改硬件的能力 ——
  否则同一个 Wi-Fi 下任何人都能把蜂鸣器改到别的脚上，而"该响的不响"在现场极难定位。
  **引脚请手工改文件**（改完 `validate.py` 会查撞脚）。
- ``POST /api/v1/config`` 改配置时会**先校验再落盘**（见 `core/configstore.py`）：
  校验不过一律 400，且**文件一个字节都不改**。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from ..hal.exceptions import ConfigError
from ..hal.models import AlarmCode, AlarmEvent, Severity, message_kind

_LOG = logging.getLogger(__name__)

#: 当前线程正在处理的请求体。
#: 本 API 的参数都走 query string，**只有** ``/api/v1/cloud/callback``（云推送）与
#: ``/api/v1/config``（改配置）需要读 body。用 thread-local 传递，避免把 body
#: 塞进所有处理函数的签名里（那会让其它接口的测试也要造 body）。
#:
#: 2026-10-01 补：:meth:`WebApi.handle` 现在接受显式的 ``body`` 参数并写进这里 ——
#: 单测可以直接 ``handle("POST", "/api/v1/config", {}, {}, body=b"...")``，
#: 不必去构造一个真的 HTTP 服务器。
_CURRENT_BODY = threading.local()

#: 本进程没绑定配置文件时的统一提示。
#: **说清原因**比返回一个含糊的 500 重要：这是"启动方式不对"，不是"改配置失败"。
_NO_CONFIG_STORE = (
    "本进程没有绑定可写的配置文件（启动时未解析出配置路径），配置面板不可用；"
    "请用 `python -m health_monitor --config <配置文件路径> serve` 启动"
)


def _current_body() -> bytes:
    """取当前请求的原始 body（没有就是空字节）。

    ⚠️ ``threading.local`` 实例**没有 ``.get()``**，只能 ``getattr(..., "value", ...)``。
    2026-10-01 踩到：``_cloud_callback`` 里原本写的是 ``_CURRENT_BODY.get()`` ——
    那条路径当时**没有被注册进路由**，所以这个写法错误一直没暴露；
    这次给 ``POST /api/v1/config`` 接 body 时，走真 HTTP 的测试**当场全红**。
    （教训：`sys.path` 之外的东西也一样 —— **没被走到的代码等于没写**。）
    """
    return getattr(_CURRENT_BODY, "value", b"") or b""


class WebApi:
    """把 HTTP 请求映射到业务对象（不依赖 ``http.server``，可直接单测）。

    依赖注入进来的 ``runtime`` 需要提供这些属性/方法（见 ``service.py``）::

        runtime.engine        -> RuleEngine（active_alarms / clear_active）
        runtime.collector     -> Collector（snapshot / status）
        runtime.dispatcher    -> AlarmDispatcher（dispatch / silence / recent）
        runtime.store         -> Store | None
        runtime.sos(ts)       -> 触发一次紧急求助（business 侧统一入口）
        runtime.started_at    -> 启动时间
    """

    def __init__(self, runtime: Any, token: str = "") -> None:
        self.runtime = runtime
        self.token = token or ""

    # ------------------------------------------------------------------
    # 路由
    # ------------------------------------------------------------------

    def handle(self, method: str, path: str, query: Dict[str, list], headers: Dict[str, str],
               body: bytes = b"") -> Tuple[int, Dict[str, Any]]:
        """返回 ``(状态码, JSON 可序列化对象)``。**这是可单测的纯入口。**

        Args:
            body: 请求体原始字节。只有 ``POST /api/v1/config``（改配置）与
                ``POST /api/v1/cloud/callback``（云推送）用得到它，其余接口走 query string。
                默认空字节 ⇒ 既有的四参数调用点（单测、手机端）完全不受影响。
        """
        _CURRENT_BODY.value = body or b""

        if self.token:
            supplied = headers.get("x-auth-token", "")
            if supplied != self.token:
                # **统一 401（"未认证"）**：本项目所有接口在 token 不匹配时都是 401 ——
                # 同一种失败只该有一种表达，否则手机端/脚本要写两套分支
                # （2026-09-30：配置写接口最初写成 403"拒绝执行"，review 时统一成 401）。
                # ⚠️ 加配置写接口**没有**把 token 保护改弱：拒绝就是拒绝，
                #    文件一个字节都不会动（有测试断言）。
                return 401, {
                    "ok": False,
                    "error": "缺少或错误的 X-Auth-Token（本服务启用了 --token）",
                }

        routes: Dict[Tuple[str, str], Callable[[Dict[str, list]], Tuple[int, Dict[str, Any]]]] = {
            ("GET", "/api/v1/health"): self._health,
            ("GET", "/api/v1/current"): self._current,
            ("GET", "/api/v1/history"): self._history,
            ("GET", "/api/v1/alarms"): self._alarms,
            ("GET", "/api/v1/messages"): self._messages,
            ("GET", "/api/v1/devices"): self._devices,
            ("GET", "/api/v1/config"): self._config_get,
            ("POST", "/api/v1/config"): self._config_post,
            ("GET", "/api/v1/spo2"): self._spo2_get,
            ("POST", "/api/v1/spo2/measure"): self._spo2_measure,
            ("POST", "/api/v1/spo2/decline"): self._spo2_decline,
            ("POST", "/api/v1/screen"): self._screen,
            ("POST", "/api/v1/silence"): self._silence,
            ("POST", "/api/v1/sos"): self._sos,
            # ⚠️ **回归修复（2026-10-01）**：`_cloud_callback` 从它被写出来的那一天起
            #    就**没有注册过**（既不在老的 if/elif 链里、也不在这张表里）——
            #    它是**死代码**，而 `CHANGELOG-接口.md` 与手册都写着这个端点存在
            #    ⇒ 实测 `POST /api/v1/cloud/callback` 一直返回 **404**，
            #    OneNET 规则引擎转发这条路**从来没通过**（`docs/07` 的 C7 因此"未实测"，
            #    其实是**没法测**）。
            #    没人发现的原因：**没有任何守卫比较"路由表 ↔ 契约文档"**。
            #    现已补上守卫 `tests/integration/test_api_contract_docs.py`（双向比对）。
            ("POST", "/api/v1/cloud/callback"): self._cloud_callback,
            # 把 `_cloud_callback` 记下的原件读出来（此前只记不读，等于证据不存在）
            ("GET", "/api/v1/cloud/last"): self._cloud_last,
        }
        handler = routes.get((method.upper(), path))
        if handler is None:
            return 404, {"ok": False, "error": f"未知接口 {method} {path}", "available": sorted(f"{m} {p}" for m, p in routes)}
        try:
            return handler(query)
        except Exception as exc:  # noqa: BLE001 - 任何业务异常都要变成结构化错误，而不是断连
            _LOG.exception("接口 %s %s 处理失败", method, path)
            return 500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # ------------------------------------------------------------------
    # 各接口
    # ------------------------------------------------------------------

    def _health(self, _q: Dict[str, list]) -> Tuple[int, Dict[str, Any]]:
        collector = self.runtime.collector
        payload: Dict[str, Any] = {
            "ok": True,
            "ts": time.time(),
            "uptime_s": round(time.time() - self.runtime.started_at, 1),
            "version": self.runtime.version,
            "mock": bool(self.runtime.mock),
            "active_alarms": self.runtime.engine.active_alarms(),
            "dispatcher": self.runtime.dispatcher.status(),
            "devices": collector.status()["devices"],
            #: 输出器件（LED / 两屏 / 蜂鸣器 / 音箱）的**实时状态**（2026-10-01 用户要求）。
            #: 见 :meth:`_outputs_status` 的说明：为什么以前没有、为什么它值得有。
            "outputs": self._outputs_status(),
        }
        # 上云状态（可选功能）：客户端可用它判断"云上那条链路通不通"
        mqtt = getattr(self.runtime, "mqtt", None)
        payload["mqtt"] = mqtt.status() if mqtt is not None else {"enabled": False}
        payload["cloud"] = {
            "platform": getattr(self.runtime, "cloud_platform", "none"),
            "warning": getattr(self.runtime, "cloud_warning", ""),
            "started": bool(getattr(self.runtime, "mqtt_started", False)),
        }
        return 200, payload

    def _outputs_status(self) -> Dict[str, Any]:
        """输出器件的实时状态：``{设备名: 驱动 status()}``（**只读、不碰硬件**）。

        为什么加它（2026-10-01 用户拍板；起因是一次验收的尴尬）
        --------------------------------------------------------
        `/api/v1/health` 的 `devices` 来自 `Collector.status()` ⇒ **只有输入器件**；
        `/api/v1/devices` 只给 `describe()`（接线说明）与自检。
        于是"**报警到底上屏了没 / 灯是什么颜色**"在接口上**看不到** ——
        我做 S9 替代验证时只能用调度器的下发流水（`/api/v1/alarms` 的 `live[]`）
        **间接**证明"命令发出去了"，而"屏上真的是那两行字吗"没法从外面取证。
        ⇒ 现在把每个输出器件自己的 `status()` 原样暴露出来（LED 的 `current_color`/`blinking`、
        LCD 的 `lines`/`page`、TFT 的页码与富帧摘要、蜂鸣器/音箱的计数…）。

        ⚠️ 三条纪律：

        1. **按 `dispatcher.outputs` 取，不是按配置的 `enabled`** —— 配置里写着启用的器件
           也可能装配失败（没接线是常态），那种情况下它**不该**在这里出现
           （与 :meth:`Runtime._screen_present` 同一口径：**不假装成功**）；
        2. **单个器件的 `status()` 抛异常不许把整个健康接口打崩**：错误就地转成
           ``{"error": "..."}``（`/api/v1/health` 是运维与手机端的第一入口，它必须永远能答）；
        3. **只读**：绝不调用 `read()`（那会去碰硬件；LCD/MAX30102 没有排他锁，
           见 `ERROR.md` E76 —— 健康接口不该产生副作用）。
        """
        outputs: Dict[str, Any] = {}
        for name, device in sorted(getattr(self.runtime.dispatcher, "outputs", {}).items()):
            getter = getattr(device, "status", None)
            try:
                info = getter() if callable(getter) else {"driver": type(device).__name__}
            except Exception as exc:  # noqa: BLE001 - 一个器件坏了不能让健康接口也坏
                info = {"error": f"{type(exc).__name__}: {exc}"}
            if not isinstance(info, dict):        # 驱动写错了也不能让 JSON 变味
                info = {"value": repr(info)}
            info.setdefault("kind", getattr(getattr(device, "KIND", None), "value", ""))
            outputs[name] = info
        return outputs

    def _cloud_callback(self, q: Dict[str, list]) -> Tuple[int, Dict[str, Any]]:
        """接收 OneNET **规则引擎 HTTP 推送**的数据（云云对接）。

        OneNET 规则引擎把"设备数据点消息"按规则 POST 到我们配置的 URL，
        期望收到 ``{"code": 0, "msg": "ok"}`` 表示成功（平台侧推送失败会按其策略重试）。

        本端点做三件事（**只记录，不改任何监护状态**）：
        1. 把收到的原始 JSON 记一条日志（排查"云端到底发了什么"的唯一硬证据）；
        2. 在内存里保留最近若干条，供 :meth:`_cloud_last`（``GET /api/v1/cloud/last``）查看；
        3. 原样返回 ``{"code": 0, "msg": "ok"}``（平台约定的成功应答）。

        ⚠️ **安全**：默认不校验来源（局域网/课程演示够用）。
        要暴露到公网，请同时给 ``serve --token`` 与 OneNET 的推送 URL 配上令牌
        （见 ``docs/11-OneNET云端接入与云云对接.md`` 的"安全"一节）。
        """
        raw = _current_body()
        record: Dict[str, Any] = {"ts": time.time(), "query": {k: v[0] for k, v in q.items()}}
        if raw:
            try:
                record["payload"] = json.loads(raw.decode("utf-8"))
            except Exception:  # noqa: BLE001 - 不是 JSON 也要留证
                record["payload"] = raw[:2000].decode("utf-8", "replace")
        saver = getattr(self.runtime, "record_cloud_push", None)
        if callable(saver):
            try:
                saver(record)
            except Exception as exc:  # noqa: BLE001 - 记录失败不该让平台收到错误应答
                _LOG.debug("记录云端推送失败（已忽略）：%s", exc)
        else:
            _LOG.info("收到云端推送（无记录器）：%s", json.dumps(record, ensure_ascii=False)[:500])
        return 200, {"code": 0, "msg": "ok"}

    def _cloud_last(self, q: Dict[str, list]) -> Tuple[int, Dict[str, Any]]:
        """最近收到的云端推送（新的在后）—— **把 `_cloud_callback` 记下的证据读出来**。

        为什么必须有它（2026-10-01 发现的两个洞叠在一起）：
        ① `_cloud_callback` 的 docstring 从写出来那天起就写着"供 `/api/v1/cloud/last`
           **与状态页**查看"，但**两者都不存在** —— 既没有这个路由，`webui.py` 里也
           一个"云端"字样都没有；
        ② 结果就是**推送被记进了内存、却没有任何办法读出来**，
           而它自称是"排查云云对接的唯一硬证据" ⇒ 证据等于没有。
        （注：状态页那一半**不打算做** —— "云端推送了几条"是运维/排查信息，
        不属于给老人家属看的照护数据；`GET` 这个端点才是它该在的地方。）
        """
        limit = _int_param(q, "limit", 20, low=1, high=200)
        getter = getattr(self.runtime, "recent_cloud_pushes", None)
        pushes = list(getter(limit)) if callable(getter) else []
        return 200, {
            "ok": True,
            "count": len(pushes),
            "total": int(getattr(self.runtime, "cloud_push_count", 0) or 0),
            "pushes": pushes,
        }

    def _current(self, _q: Dict[str, list]) -> Tuple[int, Dict[str, Any]]:
        snap = self.runtime.collector.snapshot()
        return 200, {
            "ok": True,
            "data": snap.health_summary(),
            "active_alarms": self.runtime.engine.active_alarms(),
        }

    def _history(self, q: Dict[str, list]) -> Tuple[int, Dict[str, Any]]:
        if self.runtime.store is None:
            return 200, {"ok": True, "data": [], "note": "当前未启用历史存储"}
        metric = _first(q, "metric", "ambient_temp_c")
        limit = _int_param(q, "limit", 120, low=1, high=2000)
        since = _float_param(q, "since", None)
        if metric == "vitals":
            rows = self.runtime.store.recent_vitals(limit=limit, since=since)
        else:
            rows = self.runtime.store.recent_readings(metric=metric, limit=limit, since=since)
        return 200, {"ok": True, "metric": metric, "count": len(rows), "data": rows}

    def _alarms(self, q: Dict[str, list]) -> Tuple[int, Dict[str, Any]]:
        limit = _int_param(q, "limit", 50, low=1, high=500)
        events = self.runtime.dispatcher.recent(limit)
        stored = self.runtime.store.recent_alarms(limit=limit) if self.runtime.store else []
        return 200, {"ok": True, "live": events, "history": stored}

    def _messages(self, q: Dict[str, list]) -> Tuple[int, Dict[str, Any]]:
        """统一"消息流"：报警 + 老人动作（消音）+ 测量记录 + 系统信息，**按时间正序**。

        与 :meth:`_alarms` 的分工：

        * ``/api/v1/alarms`` 是**报警专用**（``live`` 来自调度器、``history`` 来自历史库）；
        * ``/api/v1/messages`` 是**给人翻记录**的流，每条多带一个 ``kind``
          （``alarm`` / ``record`` / ``clear`` / ``info``，见 :func:`message_kind`），
          后台"消息页"按它分组 —— 用户 2026-10-01 要求
          "老人短按 A 关警报 / 长按 A 求救 / 测完血氧，**后台消息页都要收到指示**"。

        取数**优先用历史库**（跨重启仍在 —— 用户翻的就是"记录"）；没有库时退回内存最近事件。
        """
        limit = _int_param(q, "limit", 50, low=1, high=500)
        store = self.runtime.store
        if store is not None:
            # 历史库按时间**倒序**给（最近的在前），这里翻成正序：**新的在后**，
            # 与"消息页往下滚动看最新"的阅读习惯一致。
            raw: List[Dict[str, Any]] = list(reversed(store.recent_alarms(limit=limit)))
        else:
            raw = [event.to_dict() for event in self.runtime.recent_events(limit)]
        messages = [_message_json(row) for row in raw]
        return 200, {"ok": True, "count": len(messages), "messages": messages}

    def _devices(self, _q: Dict[str, list]) -> Tuple[int, Dict[str, Any]]:
        devices: Dict[str, Any] = {}
        for name, entry in self.runtime.collector.entries.items():
            device = entry.device
            describe = getattr(device, "describe", None)
            try:
                info = describe() if callable(describe) else {}
            except Exception as exc:  # noqa: BLE001
                info = {"error": f"describe() 失败：{exc}"}
            devices[name] = {"driver": entry.driver, "interval_s": entry.interval, "describe": info}
        return 200, {"ok": True, "devices": devices}

    # ------------------------------------------------------------------
    # 配置读写（Web 面板；落盘与校验细节见 core/configstore.py）
    # ------------------------------------------------------------------

    def _config_store(self) -> Any:
        """本进程绑定在哪个配置文件上（没绑定则返回 ``None``）。"""
        return getattr(self.runtime, "config_store", None)

    def _config_get(self, _q: Dict[str, list]) -> Tuple[int, Dict[str, Any]]:
        store = self._config_store()
        if store is None:
            return 400, {"ok": False, "error": _NO_CONFIG_STORE}
        try:
            return 200, {"ok": True, **store.snapshot()}
        except ConfigError as exc:
            return 400, {"ok": False, "error": str(exc)}

    def _config_post(self, _q: Dict[str, list]) -> Tuple[int, Dict[str, Any]]:
        """改配置：**校验 → 落盘 → 热应用**（顺序有讲究）。

        为什么"先落盘、再热应用"（而不是反过来）：**文件是真相来源** ——
        落盘成功就意味着"重启后必定是这个状态"。若先热应用再落盘，
        一旦写盘失败（磁盘满 / 只读挂载），用户看到"保存失败"，
        但**正在跑的进程其实已经改了**，而重启后又回到旧值 —— 那是最难解释的一种状态。
        """
        store = self._config_store()
        if store is None:
            return 400, {"ok": False, "error": _NO_CONFIG_STORE}
        raw = _current_body()
        if not raw:
            return 400, {"ok": False, "error": "缺少请求体：POST /api/v1/config 需要 JSON 对象"}
        try:
            patch = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return 400, {"ok": False, "error": f"请求体不是合法 JSON：{exc}"}
        try:
            result = store.apply_patch(patch)
        except ConfigError as exc:
            # ★ 校验不过 ⇒ apply_patch 是"只读"的，文件**一个字节都没动**（测试有断言）
            return 400, {"ok": False, "error": str(exc)}

        warnings: List[str] = []
        backup = ""
        if result.changed:
            try:
                backup = store.save(result.raw) or ""
            except (ConfigError, OSError) as exc:
                return 500, {
                    "ok": False,
                    "error": f"配置校验通过，但写盘失败（文件保持原样）：{type(exc).__name__}: {exc}",
                }
        else:
            # 什么都没变就不写盘：否则每点一次"保存"都多一个备份文件 + 一次全量重写
            warnings.append("各项取值与当前配置一致，未写盘")

        try:
            applied, apply_warnings = self.runtime.apply_config(result.config)
            warnings.extend(apply_warnings)
        except Exception as exc:  # noqa: BLE001 - 热应用出错不该把"已经存好了"说成失败
            _LOG.exception("配置热应用失败")
            applied = []
            warnings.append(
                f"配置已写入文件，但热应用出错（重启服务后一定生效）：{type(exc).__name__}: {exc}"
            )
        return 200, {
            "ok": True,
            "changed": result.changed,
            "applied": applied,
            "warnings": warnings,
            "backup": backup,
        }

    # ---- 按需测血氧（2026-10-01）----
    #
    # ⚠️ 三条都**不自己改状态**，只调 `Runtime` 上那三个方法 ——
    # 物理按键走的是同一套（`_consume_button_events` → `_drive_spo2` → 同名方法）。
    # 这是本项目的硬纪律："一个动作只有一条路径"，否则面板/手机端/按键迟早行为分叉。

    def _now(self) -> float:
        """血氧这几条接口统一取时间：**优先用 Runtime 的时钟**。

        为什么不能用 ``time.time()``（既有 `_silence` / `_sos` 用的是它）：
        血氧状态机里存的是**"截止时刻"**，它在 `tick()` 里与 Runtime 时钟比较。
        HTTP 若用墙上时钟，一旦两者不是同一个源（单测注入假时钟、或将来做时间回放），
        倒计时会变成负数、`measure` 的窗口永远不会到点 —— 而且**测试里根本测不出来**。
        ⇒ 凡是有"截止时刻"语义的接口，都必须与 tick 共用一个时钟。
        """
        clock = getattr(self.runtime, "clock", None)
        return clock() if callable(clock) else time.time()

    def _spo2_get(self, _q: Dict[str, list]) -> Tuple[int, Dict[str, Any]]:
        """「测血氧」当前状态（面板每 2 秒轮询它刷新那一节的状态行）。"""
        return 200, self.runtime.spo2_status(self._now())

    def _spo2_measure(self, _q: Dict[str, list]) -> Tuple[int, Dict[str, Any]]:
        """等价于**物理短按**那个「测血氧」按键。"""
        now = self._now()
        if not self.runtime.spo2_measure_now(now):
            return 409, {
                "ok": False,
                "error": "测血氧功能未启用（配置里 spo2_button.enabled = false）",
            }
        return 200, {"ok": True, "state": self.runtime.spo2_status(now)["state"]}

    def _spo2_decline(self, _q: Dict[str, list]) -> Tuple[int, Dict[str, Any]]:
        """等价于**叫人阶段长按**（= 否决本轮）。

        不在叫人阶段时返回 **409**（而不是"假装成功"）：面板据此把按钮灰掉，
        脚本也能凭状态码判断"这一下到底有没有意义"。
        """
        now = self._now()
        if not self.runtime.spo2_decline(now):
            status = self.runtime.spo2_status(now)
            if not status["enabled"]:
                return 409, {
                    "ok": False,
                    "error": "测血氧功能未启用（配置里 spo2_button.enabled = false）",
                }
            return 409, {
                "ok": False,
                "error": "当前不在「正在叫人」阶段（state=%s），没有可否决的提醒" % status["state"],
            }
        return 200, {"ok": True, "state": self.runtime.spo2_status(now)["state"]}

    # ---- 屏显控制（2026-10-01：功能面板上"点一下 ⇒ 屏上弹出对应面板"）----
    #
    # ⚠️ 与血氧那三条同一条纪律：**不自己碰屏幕**，只调 `Runtime.show_screen()`。
    # "该显示哪几行文案"由 service 层决定（那里才有 snapshot 与页文案函数），
    # HTTP 只负责参数校验与状态码。

    def _screen(self, _q: Dict[str, list]) -> Tuple[int, Dict[str, Any]]:
        """让指定屏幕**立刻**显示指定面板。

        Args:
            body: ``{"target": "lcd"|"tft", "page": "<页名>"}``（**用请求体而不是
                query string**：它与 ``/api/v1/config`` 一样是"带结构的写动作"，
                放在 body 里以后加字段不用改 URL）。

        三种结果分得很清楚（**都不假装成功**）：

        * ``400`` —— 参数不认识（``target`` 不在允许表里，或 ``page`` 不是该类允许的页）；
        * ``409`` —— 参数没问题，但**这块屏当前不在系统里**（没接线 / 装配失败 / 被关掉了）；
        * ``200`` —— 真的已经发出去了。

        为什么非法 target 要回 400 而不是"静默忽略"：功能面板上只有 4 个写死的按钮，
        能打出非法 target 的只可能是**手写请求或前端 bug** —— 静默忽略会让那类问题
        永远不暴露（与配置接口拒绝未知字段是同一条理由）。
        """
        raw = _current_body()
        if not raw:
            return 400, {
                "ok": False,
                "error": '缺少请求体：需要 JSON 对象 {"target": "lcd"|"tft", "page": "<页名>"}',
            }
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return 400, {"ok": False, "error": f"请求体不是合法 JSON：{exc}"}
        if not isinstance(payload, dict):
            return 400, {"ok": False, "error": "请求体必须是 JSON 对象"}

        drivers = getattr(self.runtime, "SCREEN_DRIVERS", {}) or {}
        pages = getattr(self.runtime, "SCREEN_PAGES", {}) or {}
        target = str(payload.get("target") or "").strip().lower()
        page = str(payload.get("page") or "").strip().lower()

        if target not in drivers:
            return 400, {
                "ok": False,
                "error": "target 只能是 %s（收到 %r）" % ("、".join(sorted(drivers)), target),
            }
        allowed = tuple(pages.get(target) or ())
        if page not in allowed:
            return 400, {
                "ok": False,
                "error": "%s 的 page 只能是 %s（收到 %r）" % (target, "、".join(allowed), page),
            }
        if not self.runtime.show_screen(target, page, self._now()):
            return 409, {
                "ok": False,
                "error": "%s 这块屏当前不在系统里（未接线 / 装配失败 / 已被关闭）" % target,
            }
        return 200, {"ok": True, "target": target, "page": page}

    def _silence(self, _q: Dict[str, list]) -> Tuple[int, Dict[str, Any]]:
        now = time.time()
        # ``by="api"``：让消息流能区分"老人按的实体键"与"后台/手机端点掉的"（见 service.silence）
        self.runtime.silence(now, by="api")
        return 200, {"ok": True, "silenced_until": self.runtime.dispatcher.status()["silenced_until"]}

    def _sos(self, _q: Dict[str, list]) -> Tuple[int, Dict[str, Any]]:
        event = self.runtime.sos(time.time())
        return 200, {"ok": True, "event": event.to_dict()}

# --------------------------------------------------------------------------
# 参数解析小工具（越界一律夹紧，而不是报 500）
# --------------------------------------------------------------------------


def _message_json(row: Dict[str, Any]) -> Dict[str, Any]:
    """把一条历史事件整理成"消息页"要的形状（补上 ``kind``）。

    历史库的行与 ``AlarmEvent.to_dict()`` 的键**基本一致**（ts/code/severity/message/
    value/unit/source/detail），这里统一成同一个形状 —— 前端不必关心数据来自哪一侧。
    """
    code = str(row.get("code") or "")
    return {
        "ts": row.get("ts"),
        "code": code,
        "kind": message_kind(code),
        "severity": int(row.get("severity") or 0),
        "message": row.get("message") or "",
        "source": row.get("source") or "",
        "value": row.get("value"),
        "unit": row.get("unit") or "",
        "detail": row.get("detail") or {},
    }


def _first(q: Dict[str, list], key: str, default: str) -> str:
    values = q.get(key)
    return values[0] if values else default


def _int_param(q: Dict[str, list], key: str, default: int, low: int, high: int) -> int:
    try:
        value = int(_first(q, key, str(default)))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def _float_param(q: Dict[str, list], key: str, default: Optional[float]) -> Optional[float]:
    raw = q.get(key)
    if not raw:
        return default
    try:
        return float(raw[0])
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------
# HTTP 服务器
# --------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    """把标准库请求转给 :class:`WebApi`。"""

    server_version = "HealthMonitor/1.0"
    api: WebApi  # 由 make_server 注入

    def _respond(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _respond_html(self, status: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - 标准库约定的方法名
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        # 读请求体：绝大多数接口只用 query string，但云端回调需要 JSON body。
        # 保留原始字节，交给 _cloud_callback 解析（读掉 body 也让连接可复用）。
        length = int(self.headers.get("Content-Length") or 0)
        _CURRENT_BODY.value = self.rfile.read(length) if length else b""
        try:
            self._dispatch("POST")
        finally:
            _CURRENT_BODY.value = b""

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        headers = {k.lower(): v for k, v in self.headers.items()}

        # 配置面板（GET /panel）：给人看的 HTML，不走 JSON 路由。
        # ⚠️ 它**会改配置**，与只读的状态页是两回事，所以单独一条分支、单独提示。
        if method == "GET" and parsed.path == "/panel":
            status_code, page = _render_panel_page(self.api)
            self._respond_html(status_code, page)
            return

        # 状态网页（GET /）与 /index.html：给人看的 HTML，不走 JSON 路由
        if method == "GET" and parsed.path in ("/", "/index.html", "/status"):
            status_code, page = _render_status_page(self.api)
            self._respond_html(status_code, page)
            return

        # 功能面板（GET /control，2026-10-01）：**会触发动作**（测血氧 / 指定屏显），
        # 与只读的状态页分开，理由与 /panel 一样。
        if method == "GET" and parsed.path in ("/control", "/control.html"):
            status_code, page = _render_control_page(self.api)
            self._respond_html(status_code, page)
            return

        # 请求体显式传给 handle()：单测可以直接调 handle(),
        # 这里只是把服务器已经读到的字节原样传下去（不再依赖"谁先读 body"的顺序）。
        body = _current_body()
        status, payload = self.api.handle(method, parsed.path, query, headers, body)
        self._respond(status, payload)

    def log_message(self, fmt: str, *args: Any) -> None:
        """默认实现会往 stderr 打日志；这里降级为 debug，避免刷屏。"""
        _LOG.debug("%s - %s", self.address_string(), fmt % args)


def _render_status_page(api: WebApi) -> Tuple[int, str]:
    """渲染状态网页；**渲染失败也必须给出可读页面**（不能让 / 直接 500 空白）。"""
    if api.token:
        return 401, (
            "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
            "<title>需要令牌</title></head><body style='font-family:sans-serif;padding:24px'>"
            "<h1>需要访问令牌</h1>"
            "<p>本服务启用了 <code>--token</code>；状态网页不提供令牌输入框（避免把口令写进浏览器历史）。</p>"
            "<p>请改用手机 App 或在请求头带 <code>X-Auth-Token</code> 访问 <code>/api/v1/current</code>。</p>"
            "</body></html>"
        )
    try:
        from .webui import render_page

        return 200, render_page(api.runtime)
    except Exception as exc:  # noqa: BLE001 - 网页渲染失败不能影响 API
        _LOG.exception("状态网页渲染失败")
        return 500, (
            "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
            "<title>页面渲染失败</title></head><body style='font-family:sans-serif;padding:24px'>"
            "<h1>状态页渲染失败</h1>"
            f"<p>原因：{esc(str(exc))}</p>"
            "<p>JSON 接口仍然可用：<code>/api/v1/current</code>、<code>/api/v1/health</code></p>"
            "</body></html>"
        )


def _render_control_page(api: WebApi) -> Tuple[int, str]:
    """渲染**功能面板**（``GET /control``，2026-10-01）。

    它与另外两页的分工（用户 2026-10-01 明确划的）：

    * ``/``        **数据展示**：图表 + 可视化（只读）；
    * ``/control`` **功能操作**：让屏上弹出面板 / 按需测一次血氧（**会触发动作**）；
    * ``/panel``   **配置**：改阈值与器件开关（**会写文件**）。

    ⚠️ 设了 ``--token`` 时本页与另两页一样不提供（同一取舍：不在浏览器里输入口令）。
    """
    if api.token:
        return 401, (
            "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
            "<title>需要令牌</title></head><body style='font-family:sans-serif;padding:24px'>"
            "<h1>需要访问令牌</h1>"
            "<p>本服务启用了 <code>--token</code>；功能面板不提供令牌输入框"
            "（与状态页同一取舍：避免把口令写进浏览器历史）。</p>"
            "<p>请在请求头带 <code>X-Auth-Token</code> 调用 "
            "<code>POST /api/v1/spo2/measure</code> 与 <code>POST /api/v1/screen</code>。</p>"
            "</body></html>"
        )
    try:
        from .webui import render_control

        return 200, render_control(api.runtime)
    except Exception as exc:  # noqa: BLE001 - 功能面板渲染失败不能影响 API
        _LOG.exception("功能面板渲染失败")
        return 500, (
            "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
            "<title>面板渲染失败</title></head><body style='font-family:sans-serif;padding:24px'>"
            "<h1>功能面板渲染失败</h1>"
            f"<p>原因：{esc(str(exc))}</p>"
            "<p>JSON 接口仍然可用：<code>/api/v1/spo2</code>、<code>POST /api/v1/screen</code></p>"
            "</body></html>"
        )


def _render_panel_page(api: WebApi) -> Tuple[int, str]:
    """渲染**配置面板**（``GET /panel``）—— 与状态页同一套"绝不白屏"纪律。

    ⚠️ 设了 ``--token`` 时本页**同样不提供**（与状态页一致的取舍：
    不在浏览器里输入口令，避免口令被写进浏览器历史/自动补全）。
    要改配置请在请求头带 ``X-Auth-Token`` 直接打 ``/api/v1/config``。
    """
    if api.token:
        return 401, (
            "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
            "<title>需要令牌</title></head><body style='font-family:sans-serif;padding:24px'>"
            "<h1>需要访问令牌</h1>"
            "<p>本服务启用了 <code>--token</code>；<strong>配置面板不提供令牌输入框</strong>"
            "（与状态页同一取舍：避免把口令写进浏览器历史）。</p>"
            "<p>请在请求头带 <code>X-Auth-Token</code> 调用 "
            "<code>GET /api/v1/config</code> 与 <code>POST /api/v1/config</code>。</p>"
            "</body></html>"
        )
    store = getattr(api.runtime, "config_store", None)
    if store is None:
        return 400, (
            "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
            "<title>配置面板不可用</title></head><body style='font-family:sans-serif;padding:24px'>"
            "<h1>配置面板不可用</h1>"
            f"<p>{esc(_NO_CONFIG_STORE)}</p>"
            "<p>JSON 接口仍然可用：<code>/api/v1/current</code>、<code>/api/v1/health</code></p>"
            "</body></html>"
        )
    try:
        from .webui import render_panel

        return 200, render_panel(api.runtime, store, secured=bool(api.token))
    except Exception as exc:  # noqa: BLE001 - 面板渲染失败不能影响 API
        _LOG.exception("配置面板渲染失败")
        return 500, (
            "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
            "<title>面板渲染失败</title></head><body style='font-family:sans-serif;padding:24px'>"
            "<h1>配置面板渲染失败</h1>"
            f"<p>原因：{esc(str(exc))}</p>"
            "<p>配置接口本身可能仍然可用：<code>GET /api/v1/config</code></p>"
            "</body></html>"
        )


def esc(text: str) -> str:
    """最小 HTML 转义（渲染失败页用，避免把异常信息里的尖括号当标签）。"""
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def probe_port(host: str = "0.0.0.0", port: int = 8080) -> Optional[str]:
    """**先探一下端口能不能绑**，返回 ``None`` 表示可以，否则返回一句人话的原因。

    为什么需要它（`ERROR.md` **E76**，2026-10-01 真机 S10 验收发现）
    ---------------------------------------------------------------
    启动顺序原来是"**先开器件、再绑端口**" ⇒ "端口被占用"这件事是在**碰完硬件之后**
    才发现的。GPIO 类器件因为排他（`GPIO busy`）会当场失败、无害；
    但 **I2C 器件（LCD1602 / MAX30102）没有排他锁** ⇒ 第二个实例**真的会去读 MAX30102 的
    FIFO**（破坏性：两进程互相抢走采样），也可能往 LCD 写过一帧。

    ⚠️ 判据：**注定要失败的操作不许产生副作用**。
    所以 `serve` 现在**先探端口**、探不过就立刻退出，连一个器件都不打开。

    ⚠️ 探针必须与 :func:`make_server` **同口径**（同一个 :func:`allow_reuse_address`），
    否则会出现"探得到、绑不上"（或反过来）的假结论 ——
    2026-10-01 实测：口径不一致时，把 `TIME_WAIT` 造成的占用误读成"服务起不来"（E77）。
    探测用的 socket 立刻关闭 —— 真正的绑定仍由 :func:`make_server` 完成
    （探针只是把"注定失败"提前，不承担互斥职责）。
    """
    import socket

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if allow_reuse_address():
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((host, int(port)))
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}（可能已有实例在运行；换端口用 --port，或先关掉旧进程）"
    finally:
        probe.close()
    return None


def allow_reuse_address() -> bool:
    """本平台是否让监听 socket 复用地址（``SO_REUSEADDR``）—— **按平台分开**。

    为什么必须分开（`ERROR.md` **E7 + E77**）
    -----------------------------------------
    同一个开关在两个平台上是**两件不同的事**：

    | 平台 | 设了 ``SO_REUSEADDR`` 会怎样 |
    | --- | --- |
    | **Windows** | **两个活着的监听者可以绑同一端口** ⇒ "我以为只有一个服务在跑，实际两个在抢请求"，手机端时而连到旧实例（**E7**，2026-09-21 实测踩到）⇒ **必须禁止** |
    | **Linux** | 只允许绑"仍有 ``TIME_WAIT`` 的端口"，**不允许**第二个活着的监听者 ⇒ **防双开不受影响**，却能把"停服务后必须等约 60 秒"（**E77**）这个坑去掉 |

    所以判据不是"要不要复用地址"，而是"**这个平台的复用语义会不会破坏防双开**"。
    Linux 上不会 ⇒ 允许；Windows 上会 ⇒ 禁止。

    ⚠️ 真机复验（2026-10-01，树莓派/Linux）：改成允许之后 ① 停服务后**立刻**能重新绑定
    （不再等 60 秒）；② 已有实例在跑时再起一个，**仍然 `rc=2` 报端口被占用**
    ⇒ 两条属性同时成立（见 `docs/07` §6.3）。
    """
    # ⚠️ `os` 在**模块级**导入（不在函数里）：否则测试没法用 `mock.patch.object(web.os, "name", ...)`
    #    去验证"取值真的跟着平台变" —— 而那正是防止这条判据被写死、变成"永远绿"的唯一办法。
    return os.name != "nt"


def make_server(api: WebApi, host: str = "0.0.0.0", port: int = 8080) -> ThreadingHTTPServer:
    """创建一个（尚未启动的）HTTP 服务器。调用方负责 ``serve_forever()`` 与 ``shutdown()``。

    ⚠️ ``allow_reuse_address`` **按平台取值**，见 :func:`allow_reuse_address`：
    标准库的 ``HTTPServer`` 默认把它设为 1，在 **Windows** 上那会让**第二个实例也能绑上
    同一个端口**（"两个服务抢请求"，E7）；而在 **Linux** 上它只影响 `TIME_WAIT`，
    还能省掉"停服务后等 60 秒"（E77）。
    ⇒ 关掉它的**理由只对 Windows 成立**，所以现在按平台给值，而不是一刀切关掉。

    两平台的**共同保障**仍然成立：第二个活着的实例都会明确抛 ``OSError``，
    调用方据此给出"端口被占用"的提示并按 E76 的顺序**一个器件都不碰**地退出。
    """
    handler = type("BoundHandler", (_Handler,), {"api": api})
    server_cls = type("ExclusiveHTTPServer", (ThreadingHTTPServer,),
                      {"allow_reuse_address": allow_reuse_address()})
    server = server_cls((host, port), handler)
    server.daemon_threads = True
    return server


def start_in_thread(api: WebApi, host: str = "0.0.0.0", port: int = 8080) -> Tuple[ThreadingHTTPServer, threading.Thread]:
    """在后台线程启动 HTTP 服务，返回 ``(server, thread)``。

    ⚠️ 端口被占用时 ``ThreadingHTTPServer`` 会抛 ``OSError``——调用方必须捕获并给出
    "换个端口或先关掉旧进程"的提示，而不是让整个服务静默退出。
    """
    server = make_server(api, host=host, port=port)
    thread = threading.Thread(target=server.serve_forever, name="http-api", daemon=True)
    thread.start()
    _LOG.info("HTTP API 已启动：http://%s:%d/api/v1/health", host, port)
    return server, thread


__all__ = ["WebApi", "allow_reuse_address", "make_server", "probe_port", "start_in_thread"]
