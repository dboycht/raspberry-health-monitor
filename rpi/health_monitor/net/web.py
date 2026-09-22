"""HTTP API（纯标准库 ``http.server``，树莓派上零额外依赖）。

为什么不用 Flask/FastAPI：部署在树莓派上，"一条命令就能跑起来"比框架特性重要；
本服务的接口很少（读数据 + 几个控制指令），标准库完全够用，且不用装依赖。

接口一览（完整说明见 ``docs/05-安卓通信协议.md``）::

    GET  /api/v1/health          系统与设备健康状态（含每个设备的读取计数/失败数）
    GET  /api/v1/current         当前读数（扁平摘要，缺失值为 null）
    GET  /api/v1/history         历史曲线（?metric=ambient_temp_c&limit=120）
    GET  /api/v1/alarms          最近报警事件（?limit=50）
    GET  /api/v1/devices         设备清单与接线说明（describe() 的输出）
    POST /api/v1/silence         消音（用户按消音键 / 手机端点"我知道了"）
    POST /api/v1/sos             手机端触发一次紧急求助
    POST /api/v1/speak           让音箱说一句话（调试用）

安全约定
--------
- 只用于**局域网**：不要在公网直接暴露（树莓派上请只在家庭内网使用）；
- 可选 ``token``：配置了 token 时，所有请求必须带请求头 ``X-Auth-Token``，
  否则返回 401。**默认不带 token**（局域网演示方便），上公网前务必配。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from ..hal.models import AlarmCode, AlarmEvent, Severity, SpeakCommand

_LOG = logging.getLogger(__name__)

#: 当前线程正在处理的请求体。
#: 本 API 的参数都走 query string，**只有** ``/api/v1/cloud/callback`` 需要读 body
#: （OneNET 规则引擎推送的是 JSON 报文）。用 thread-local 传递，避免把 body
#: 塞进所有处理函数的签名里（那会让其它接口的测试也要造 body）。
_CURRENT_BODY = threading.local()


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

    def handle(self, method: str, path: str, query: Dict[str, list], headers: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
        """返回 ``(状态码, JSON 可序列化对象)``。**这是可单测的纯入口。**"""
        if self.token:
            supplied = headers.get("x-auth-token", "")
            if supplied != self.token:
                return 401, {"ok": False, "error": "缺少或错误的 X-Auth-Token"}

        routes: Dict[Tuple[str, str], Callable[[Dict[str, list]], Tuple[int, Dict[str, Any]]]] = {
            ("GET", "/api/v1/health"): self._health,
            ("GET", "/api/v1/current"): self._current,
            ("GET", "/api/v1/history"): self._history,
            ("GET", "/api/v1/alarms"): self._alarms,
            ("GET", "/api/v1/devices"): self._devices,
            ("POST", "/api/v1/silence"): self._silence,
            ("POST", "/api/v1/sos"): self._sos,
            ("POST", "/api/v1/speak"): self._speak,
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

    def _cloud_callback(self, q: Dict[str, list]) -> Tuple[int, Dict[str, Any]]:
        """接收 OneNET **规则引擎 HTTP 推送**的数据（云云对接）。

        OneNET 规则引擎把"设备数据点消息"按规则 POST 到我们配置的 URL，
        期望收到 ``{"code": 0, "msg": "ok"}`` 表示成功（平台侧推送失败会按其策略重试）。

        本端点做三件事（**只记录，不改任何监护状态**）：
        1. 把收到的原始 JSON 记一条日志（排查"云端到底发了什么"的唯一硬证据）；
        2. 在内存里保留最近若干条，供 ``/api/v1/cloud/last`` 与状态页查看；
        3. 原样返回 ``{"code": 0, "msg": "ok"}``（平台约定的成功应答）。

        ⚠️ **安全**：默认不校验来源（局域网/课程演示够用）。
        要暴露到公网，请同时给 ``serve --token`` 与 OneNET 的推送 URL 配上令牌
        （见 ``docs/11-OneNET云端接入与云云对接.md`` 的"安全"一节）。
        """
        raw = _CURRENT_BODY.get()
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

    def _silence(self, _q: Dict[str, list]) -> Tuple[int, Dict[str, Any]]:
        now = time.time()
        self.runtime.silence(now)
        return 200, {"ok": True, "silenced_until": self.runtime.dispatcher.status()["silenced_until"]}

    def _sos(self, _q: Dict[str, list]) -> Tuple[int, Dict[str, Any]]:
        event = self.runtime.sos(time.time())
        return 200, {"ok": True, "event": event.to_dict()}

    def _speak(self, q: Dict[str, list]) -> Tuple[int, Dict[str, Any]]:
        text = _first(q, "text", "").strip()
        if not text:
            return 400, {"ok": False, "error": "缺少 text 参数"}
        if len(text) > 60:
            return 400, {"ok": False, "error": "text 过长（最多 60 字），避免播报没完没了"}
        sent = 0
        for device in self.runtime.dispatcher.outputs.values():
            if getattr(device, "KIND", None) is not None and device.KIND.value == "audio" and getattr(device, "NAME", "") == "bt_speaker":
                device.send(SpeakCommand(text=text))
                sent += 1
        return 200, {"ok": True, "sent_to": sent, "text": text}


# --------------------------------------------------------------------------
# 参数解析小工具（越界一律夹紧，而不是报 500）
# --------------------------------------------------------------------------


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

        # 状态网页（GET /）与 /index.html：给人看的 HTML，不走 JSON 路由
        if method == "GET" and parsed.path in ("/", "/index.html", "/status"):
            status_code, page = _render_status_page(self.api)
            self._respond_html(status_code, page)
            return

        status, payload = self.api.handle(method, parsed.path, query, headers)
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


def esc(text: str) -> str:
    """最小 HTML 转义（渲染失败页用，避免把异常信息里的尖括号当标签）。"""
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def make_server(api: WebApi, host: str = "0.0.0.0", port: int = 8080) -> ThreadingHTTPServer:
    """创建一个（尚未启动的）HTTP 服务器。调用方负责 ``serve_forever()`` 与 ``shutdown()``。

    ⚠️ **必须关掉 ``allow_reuse_address``**：标准库的 ``HTTPServer`` 默认把它设为 1
    （即 TCP 的 ``SO_REUSEADDR``），在 Windows 上这会让**第二个实例也能绑上同一个端口**，
    于是"我以为只有一个服务在跑，实际上两个在抢请求"——手机端会时而连到旧实例，
    表现为"数据一会儿新一会儿旧"（2026-09-21 集成测试实测踩到）。
    关掉后第二个实例会明确抛 ``OSError``，调用方就能给出"端口被占用"的提示。
    """
    handler = type("BoundHandler", (_Handler,), {"api": api})
    server_cls = type("ExclusiveHTTPServer", (ThreadingHTTPServer,), {"allow_reuse_address": False})
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


__all__ = ["WebApi", "make_server", "start_in_thread"]
