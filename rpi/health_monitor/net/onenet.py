"""OneNET（中国移动物联网平台）接入 —— **旧版「MQTT物联网套件 / 多协议接入」数据流-数据点模式**。

本项目采用的上云方式是（与用户确认）：
**树莓派 → OneNET 上报数据点 → OneNET 规则引擎转发（HTTP 推送）→ 我们自己的服务器/App**，
即题目里的"云云对接"。

⚠️ 平台有**两套并存**的产品线，鉴权算法相同但 topic 与 payload 不同：

===============  ============================================  ==========================
                 OneNET Studio（新版，物模型 OneJSON）           MQTT物联网套件（旧版，数据点）
===============  ============================================  ==========================
本模块是否支持    否（如需请另写适配器）                          ✅ **本模块就是这一套**
上报内容         属性/事件/服务（物模型）                        数据流-数据点 {id, dp:{流:[{v,t}]}}
===============  ============================================  ==========================

接入事实（均为官方文档口径，**标 [文档] 者未在真机/真平台上实测**）
----------------------------------------------------------------
- 服务地址 [文档]：非加密 ``mqtts.heclouds.com:1883``；加密 ``mqttstls.heclouds.com:8883``
  （本模块默认走 1883；用 TLS 时把 ``tls=True`` 且端口改 8883）；
- 连接三要素 [文档]：``clientId = 设备名称``、``username = 产品ID``、``password = token``；
- keepalive [文档]：允许 10~1800 秒；平台在 1.5×keepalive 内没收到上行数据会断开；
- token [文档]：``version=2018-10-31&res=<res>&et=<过期unix秒>&method=<md5|sha1|sha256>&sign=<...>``，
  其中 ``sign = base64(hmac_<method>(base64decode(access_key), "et\nmethod\nres\nversion"))``，
  且 ``sign`` 与 ``res`` 作为 value 需要 URL 编码；
- ``res`` 取值 [文档]：访问产品 API 用 ``products/{pid}``；
  **设备连接必须用设备级密钥**，``res = products/{pid}/devices/{device_name}``；
- 数据点 topic [文档]：发布 ``$sys/{pid}/{device-name}/dp/post/json``，
  订阅结果通知 ``.../dp/post/json/accepted`` 与 ``.../dp/post/json/rejected``；
- 数据点 payload [文档]：``{"id": <int>, "dp": {"<数据流名>": [{"v": <值>, "t": <unix秒>}]}}``；
- 结果通知 [文档]：成功回 ``{"id": ...}``；失败回 ``{"id": ..., "err_code": 98, "err_msg": "Illegal Data"}`。

设计纪律（与本项目其它模块一致）
--------------------------------
1. **可选依赖**：``paho-mqtt`` 函数内延迟导入；没装只记日志并降级，**绝不影响本地监护**；
2. **不阻塞主循环**：入队由父类 :class:`~health_monitor.net.mqtt.MqttPublisher` 的后台线程完成；
3. **隐私最小化**：只发数值/时间戳/报警码，不发本机路径与异常栈；
4. **token 不打印**：日志里只出现前缀与长度（避免口令进日志）。

官方文档来源（2026-09-22 查阅）
------------------------------
- token 算法：https://open.iot.10086.cn/doc/mqtt/book/manual/auth/token.html
- token 生成示例（python）：https://open.iot.10086.cn/doc/mqtt/book/manual/auth/python.html
- 设备开发指南（地址/三要素/keepalive）：https://open.iot.10086.cn/doc/mqtt/book/device-develop/manual.html
- 数据点 topic 簇：https://open.iot.10086.cn/doc/mqtt/book/device-develop/topics/dp-topics.html
- 规则引擎消息格式：https://open.iot.10086.cn/doc/mqtt/book/manual/rule-engine/dataFormat.html
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote

from .mqtt import MqttConfig, MqttPublisher

_LOG = logging.getLogger(__name__)

#: token 算法固定的版本号（平台规定，目前仅支持这一个取值）[文档]
TOKEN_VERSION = "2018-10-31"

#: 本适配器支持的产品线（**已与需求方确认 = legacy**）。
#: 加这个开关的目的：一旦有人把 Studio（新版物模型）的参数填进来，
#: 能**立刻**报错并说清原因，而不是"连上了但数据流一个都不出现"（那种最难查）。
PLATFORM_LEGACY = "legacy"     # 旧版 MQTT物联网套件 / 多协议接入（数据流-数据点）← 本项目用这套
PLATFORM_STUDIO = "studio"     # OneNET Studio（新版物模型 OneJSON）—— 需要另写适配器
SUPPORTED_PLATFORMS = (PLATFORM_LEGACY,)

#: 平台默认地址 [文档]
DEFAULT_HOST_PLAIN = "mqtts.heclouds.com"
DEFAULT_HOST_TLS = "mqttstls.heclouds.com"
DEFAULT_PORT_PLAIN = 1883
DEFAULT_PORT_TLS = 8883

#: 一条数据点的默认数据流名（树莓派健康监护）
DEFAULT_STREAM_MAP: Dict[str, str] = {
    "heart_rate_bpm": "heart_rate",
    "spo2_percent": "spo2",
    "body_temp_c": "body_temp",
    "ambient_temp_c": "ambient_temp",
    "humidity_percent": "humidity",
    "motion_state": "motion",
}


class OneNetError(Exception):
    """OneNET 配置/上报相关的错误（调用方通常只记日志，不打断主流程）。"""


# ==========================================================================
# 一、token 生成（纯函数，最好单测的部分）
# ==========================================================================


def sign_token(
    access_key: str,
    res: str,
    et: Optional[int] = None,
    method: str = "sha256",
    version: str = TOKEN_VERSION,
    now: Optional[int] = None,
    ttl_s: int = 3600,
) -> str:
    """按 OneNET 官方算法生成访问 token。

    Args:
        access_key: 平台分配的密钥（**base64 字符串**；产品级或设备级）。
        res: 访问资源，例如 ``products/123123`` 或 ``products/123123/devices/mydev``。
        et: 过期时间（unix 秒）。为 ``None`` 时用 ``now + ttl_s``。
        method: 签名方法，支持 ``md5`` / ``sha1`` / ``sha256``。
        version: 算法版本，固定 ``2018-10-31``。
        now: 当前时间（测试可注入；默认取系统时间）。
        ttl_s: token 有效期（秒），默认 1 小时。

    Returns:
        token 字符串（含 ``version=&res=&et=&method=&sign=``，value 已 URL 编码）。

    Raises:
        OneNetError: ``access_key`` 不是合法 base64，或 ``method`` 不支持。
    """
    method = (method or "sha256").lower()
    if method not in ("md5", "sha1", "sha256"):
        raise OneNetError(f"不支持的签名方法 {method!r}：只支持 md5 / sha1 / sha256")
    if not access_key:
        raise OneNetError("access_key 为空：请填平台分配的密钥（产品级或设备级）")
    if not res:
        raise OneNetError("res 为空：至少形如 products/{产品ID}")

    try:
        key = base64.b64decode(access_key)
    except Exception as exc:  # noqa: BLE001 - base64 解码失败要给出可读原因
        raise OneNetError(f"access_key 不是合法 base64（平台的密钥末尾常带 =）：{exc}") from exc
    if not key:
        raise OneNetError("access_key 解码后为空，请核对是否复制完整")

    now = int(now if now is not None else time.time())
    et = int(et if et is not None else now + int(ttl_s))

    # 平台规定的签名原文：et、method、res、version 按参数名字符串排序后用 \n 连接，
    # 且**只取 value**（不含 key=）[文档]
    org = f"{et}\n{method}\n{res}\n{version}"
    digest = hmac.new(key=key, msg=org.encode("utf-8"), digestmod=method).digest()
    sign = quote(base64.b64encode(digest).decode(), safe="")
    res_encoded = quote(res, safe="")
    return f"version={version}&res={res_encoded}&et={et}&method={method}&sign={sign}"


def device_resource(product_id: str, device_name: str) -> str:
    """设备连接用的 ``res``（**必须配设备级密钥**）[文档]。"""
    return f"products/{product_id}/devices/{device_name}"


def product_resource(product_id: str) -> str:
    """产品级 ``res``（用于调用平台 API，不用于设备连接）。"""
    return f"products/{product_id}"


# ==========================================================================
# 二、配置
# ==========================================================================


@dataclass
class OneNetConfig:
    """OneNET 接入配置（``config/devices.json`` 的 ``onenet`` 段）。

    ⚠️ ``access_key`` 属于凭据：**不要提交到仓库**。
    推荐用环境变量 ``HEALTH_ONENET_KEY``，或写在已被 .gitignore 排除的 ``devices.json`` 里。
    """

    enabled: bool = False
    #: 产品线版本：``legacy`` = 旧版 MQTT物联网套件（数据流-数据点，本项目用这套）；
    #: ``studio`` = OneNET Studio（物模型 OneJSON）——**本适配器不支持**，填了会明确报错。
    #: 判据（怎么知道自己是哪套）：建产品时**有没有让你"定义物模型/属性"** —— 有 = studio。
    platform: str = PLATFORM_LEGACY
    #: 产品 ID（控制台里那串数字）
    product_id: str = ""
    #: 设备名称（同一产品内唯一；推荐用设备的 MAC / SN）
    device_name: str = ""
    #: 设备密钥（base64 字符串）。留空则读环境变量 HEALTH_ONENET_KEY
    access_key: str = ""
    #: 签名方法：md5 / sha1 / sha256
    method: str = "sha256"
    #: token 有效期（秒）。**注意**：token 过期后连接会被拒，需要重连时重新签发
    token_ttl_s: int = 86400
    #: 服务地址（默认取官方非加密地址）
    host: str = ""
    port: int = 0
    #: 是否用 TLS（True 时默认 8883 + mqttstls）
    tls: bool = False
    #: keepalive 秒（平台允许 10~1800）[文档]
    keepalive: int = 120
    #: 上报周期（秒），与本地采集解耦
    interval_s: float = 30.0
    #: 数据点 topic 模板：`$sys/{pid}/{device}/dp/post/json`
    topic_template: str = "$sys/{pid}/{device}/dp/post/json"
    #: 结果通知：是否订阅 accepted / rejected（订阅后能在日志里看到平台回执）
    subscribe_result: bool = True
    qos: int = 1
    #: 本地量名 → OneNET 数据流名
    stream_map: Dict[str, str] = field(default_factory=lambda: dict(DEFAULT_STREAM_MAP))
    #: 报警与状态是否也上报（报警走独立数据流 alarm_code / alarm_level）
    publish_alarm: bool = True
    publish_status: bool = True
    #: 环境变量名（access_key 的优先级：环境变量 > 配置文件）
    key_env: str = "HEALTH_ONENET_KEY"

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OneNetConfig":
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        unknown = set(data) - known
        if unknown:
            raise OneNetError(f"onenet 配置里有未知字段 {sorted(unknown)}；可用 {sorted(known)}")
        obj = cls(**data)
        obj.validate()
        return obj

    def validate(self) -> None:
        if not self.enabled:
            # 即使未启用，也拦住"平台版本填错"这种会在联调时浪费半天的错误
            if self.platform not in SUPPORTED_PLATFORMS:
                raise OneNetError(self._platform_hint())
            return
        if self.platform not in SUPPORTED_PLATFORMS:
            raise OneNetError(self._platform_hint())
        missing = [n for n, v in (("product_id", self.product_id), ("device_name", self.device_name)) if not v]
        if missing:
            raise OneNetError(f"onenet.enabled=true 但缺少必填项：{', '.join(missing)}")
        if not self.resolved_key():
            raise OneNetError(
                "onenet.access_key 为空，且环境变量 "
                f"{self.key_env} 也没设：请在 OneNET 控制台复制设备密钥，"
                "或设置该环境变量（不要把密钥写进仓库）"
            )
        if self.method.lower() not in ("md5", "sha1", "sha256"):
            raise OneNetError(f"onenet.method 不支持：{self.method}")
        if not (10 <= int(self.keepalive) <= 1800):
            raise OneNetError(f"onenet.keepalive 必须在 10~1800 之间（平台限制），当前 {self.keepalive}")
        if self.interval_s <= 0:
            raise OneNetError("onenet.interval_s 必须为正数")
        if int(self.token_ttl_s) <= 0:
            raise OneNetError("onenet.token_ttl_s 必须为正数")
        if "{pid}" not in self.topic_template or "{device}" not in self.topic_template:
            raise OneNetError(
                "onenet.topic_template 必须包含 {pid} 与 {device}，"
                "例如 $sys/{pid}/{device}/dp/post/json"
            )
        # ⚠️ 必须以 $sys/ 开头（2026-09-22 实测踩到）：少写前缀时**发布仍会成功**
        #    （平台侧用别的路径收到了），但 `_topic()` 拼出来的**订阅主题是错的**，
        #    于是 accepted/rejected 回执一条都收不到 —— 等于把"数据到底上没上去"
        #    这个唯一的硬证据静默丢掉，排查时只能靠猜。所以这里直接拒绝。
        if not self.topic_template.startswith("$sys/"):
            raise OneNetError(
                f"onenet.topic_template 必须以 $sys/ 开头（当前 {self.topic_template!r}）。"
                "旧版 MQTT物联网套件的数据点上报主题是 "
                "$sys/{pid}/{device}/dp/post/json —— 少了 $sys/ 会**发得出去但收不到回执**，"
                "让人误以为没上报成功。"
            )

    def resolved_key(self) -> str:
        """密钥优先取环境变量（配置文件里可以留空）。"""
        import os

        return os.environ.get(self.key_env) or self.access_key

    @staticmethod
    def _platform_hint() -> str:
        """平台版本填错时的提示（把"两套产品线的差异"讲清楚，别让人猜）。"""
        return (
            f"onenet.platform 只支持 {SUPPORTED_PLATFORMS}（= 旧版 MQTT物联网套件 / 多协议接入，"
            "数据流-数据点），当前填的是 studio。\n"
            "  → OneNET Studio 用的是**物模型 OneJSON**（`params.xxx.value`），"
            "topic 与 payload 都与本适配器不同，不能直接混用。\n"
            "  → 怎么判断自己在哪套平台：建产品时**有没有让你'定义物模型/属性'** —— "
            "有 = Studio；没有、直接建设备 = 旧版（本适配器）。\n"
            "  → 如果你们确实在 Studio 上：把平台切到旧版（新建一个'多协议接入'产品），"
            "或让负责同学按 OneJSON 另写一个适配器（鉴权函数 sign_token 可以直接复用）。"
        )

    def resolved_host(self) -> str:
        if self.host:
            return self.host
        return DEFAULT_HOST_TLS if self.tls else DEFAULT_HOST_PLAIN

    def resolved_port(self) -> int:
        if self.port:
            return int(self.port)
        return DEFAULT_PORT_TLS if self.tls else DEFAULT_PORT_PLAIN

    def make_token(self, now: Optional[int] = None, res: Optional[str] = None) -> str:
        """生成当前可用的 token（设备连接用设备级 res）。"""
        resource = res or device_resource(self.product_id, self.device_name)
        return sign_token(
            self.resolved_key(), resource, method=self.method,
            now=now, ttl_s=int(self.token_ttl_s),
        )


# ==========================================================================
# 三、数据点上报器
# ==========================================================================


class OneNetPublisher(MqttPublisher):
    """把本地读数/报警按 OneNET「数据点」格式上报。

    继承 :class:`~health_monitor.net.mqtt.MqttPublisher` 复用"后台线程 + 有界队列 +
    失败只计数"的机制；这里只覆盖三件事：
    ① 连接参数（clientId/username/password = 设备名/产品ID/token）；
    ② topic 与 payload 的 OneNET 数据点格式；
    ③ 订阅平台的 accepted/rejected 回执（便于排查"数据到底上没上去"）。

    ⚠️ **不打印 token**：日志里只出现前缀与长度。
    """

    def __init__(self, config: OneNetConfig, queue_size: int = 200,
                 clock: Callable[[], float] = time.time) -> None:
        self.onenet = config
        # 复用父类的 MQTT 配置：把 OneNET 参数映射进去
        base = MqttConfig(
            enabled=config.enabled,
            host=config.resolved_host(),
            port=config.resolved_port(),
            topic_prefix=config.topic_template.split("/")[0] if config.topic_template else "",
            client_id=config.device_name or "raspberry-health-monitor",
            username=config.product_id,
            password="",           # token 在 start() 时现签（可能过期，需要刷新）
            interval_s=config.interval_s,
            qos=config.qos,
            keepalive=config.keepalive,
        )
        super().__init__(base, queue_size=queue_size)
        self.clock = clock
        self._msg_id = 0
        self.received_results: List[Dict[str, Any]] = []   # 平台回执（accepted/rejected）
        self.token_issued_at: float = 0.0
        #: "本轮没有任何可上报字段"的次数 —— 静默不发是缺陷，必须能被看见
        self.skipped_empty = 0

    # -- 连接参数 --------------------------------------------------------

    def _token(self) -> str:
        token = self.onenet.make_token(now=int(self.clock()))
        self.token_issued_at = self.clock()
        return token

    def start(self) -> bool:
        """启动上报（覆盖父类：改用 OneNET 的 clientId/username/password）。"""
        if not self.onenet.enabled:
            self.reason = "配置里 onenet.enabled=false（未启用上云）"
            return False
        try:
            import paho.mqtt.client as mqtt  # type: ignore import-not-found
        except ImportError as exc:
            self.reason = f"未安装 paho-mqtt（{exc}）：pip3 install paho-mqtt"
            _LOG.warning("OneNET 未启用：%s", self.reason)
            return False

        try:
            token = self._token()
        except OneNetError as exc:
            self.reason = f"token 生成失败：{exc}"
            _LOG.warning("OneNET 未启用：%s", self.reason)
            return False

        self.available = True
        try:
            client = mqtt.Client(client_id=self.onenet.device_name, clean_session=True)
            # ⚠️ 平台要求 username=产品ID、password=token [文档]
            client.username_pw_set(self.onenet.product_id, token)
            client.on_connect = self._on_connect
            client.on_disconnect = self._on_disconnect
            if self.onenet.subscribe_result:
                client.on_message = self._on_message
            if self.onenet.tls:
                try:
                    client.tls_set()          # 用系统 CA；平台证书见官方文档
                except Exception as exc:  # noqa: BLE001 - TLS 配置失败要能看见原因
                    self.reason = f"TLS 配置失败：{type(exc).__name__}: {exc}"
                    _LOG.warning("OneNET %s", self.reason)
                    return False
            client.connect_async(self.onenet.resolved_host(), self.onenet.resolved_port(),
                                 int(self.onenet.keepalive))
            client.loop_start()
            self._client = client
        except Exception as exc:  # noqa: BLE001 - 上云失败绝不影响本地
            self.reason = f"初始化失败：{type(exc).__name__}: {exc}"
            _LOG.warning("OneNET %s", self.reason)
            return False

        self._stop.clear()
        import threading

        self._thread = threading.Thread(target=self._worker, name="onenet-publisher", daemon=True)
        self._thread.start()
        _LOG.info(
            "OneNET 上报已启动：%s:%s（产品 %s / 设备 %s，token 长度 %d，有效期 %ds）",
            self.onenet.resolved_host(), self.onenet.resolved_port(),
            self.onenet.product_id, self.onenet.device_name, len(token), self.onenet.token_ttl_s,
        )
        return True

    def _on_connect(self, client: Any, userdata: Any, flags: Any, rc: int) -> None:
        self.connected = (rc == 0)
        if rc == 0:
            _LOG.info("OneNET 已连接：%s:%s", self.onenet.resolved_host(), self.onenet.resolved_port())
            if self.onenet.subscribe_result:
                base = self._topic()
                for suffix in ("accepted", "rejected"):
                    try:
                        client.subscribe(f"{base}/{suffix}", qos=0)
                    except Exception as exc:  # noqa: BLE001 - 订阅失败不影响上报
                        _LOG.debug("订阅 %s 失败（已忽略）：%s", suffix, exc)
        else:
            # 常见 rc：4=用户名密码错（token/product_id 不对）、5=未授权（key 或 res 不匹配）
            hint = {4: "用户名或密码（token）不对", 5: "未授权：检查 access_key 与 res 是否配套"}.get(rc, "")
            self.last_error = f"连接被拒绝 rc={rc} {hint}".strip()
            _LOG.warning("OneNET 连接被拒绝：rc=%s %s", rc, hint)

    def _on_message(self, client: Any, userdata: Any, message: Any) -> None:
        """收到平台回执（accepted / rejected）——"数据到底上没上去"的唯一硬证据。"""
        try:
            payload = json.loads(message.payload.decode("utf-8"))
        except Exception:  # noqa: BLE001
            payload = {"raw": message.payload[:200].decode("utf-8", "replace")}
        record = {"topic": message.topic, "payload": payload}
        self.received_results.append(record)
        self.received_results = self.received_results[-50:]
        if message.topic.endswith("rejected"):
            self.failed += 1
            self.last_error = f"平台拒绝：{payload}"
            _LOG.warning("OneNET 拒绝了本次数据点：%s", payload)
        else:
            _LOG.info("OneNET 已接收数据点：%s", payload)

    # -- 上报内容 --------------------------------------------------------

    def _topic(self) -> str:
        return self.onenet.topic_template.format(
            pid=self.onenet.product_id, device=self.onenet.device_name
        )

    def _next_id(self) -> int:
        self._msg_id = (self._msg_id + 1) % 2_000_000_000 or 1
        return self._msg_id

    @staticmethod
    def build_datapoint(message_id: int, values: Dict[str, Any], ts: Optional[float] = None) -> Dict[str, Any]:
        """构造 OneNET 数据点报文（**纯函数，便于单测**）。

        格式 [文档]：``{"id": <int>, "dp": {"<流名>": [{"v": <值>, "t": <unix秒>}]}}``

        - 值为 ``None`` 的字段**整条跳过**（不上报"未知"为 0，与本项目"缺失不当正常"一致）；
        - ``t`` 取整秒。
        """
        ts_int = int(ts if ts is not None else time.time())
        dp: Dict[str, List[Dict[str, Any]]] = {}
        for name, value in values.items():
            if value is None:
                continue
            dp[name] = [{"v": value, "t": ts_int}]
        return {"id": int(message_id), "dp": dp}

    def _enqueue_datapoint(self, values: Dict[str, Any], ts: Optional[float] = None) -> None:
        """把一组读数按数据点格式入队（父类的队列与线程负责发送）。"""
        if self._client is None or not values:
            return
        payload = self.build_datapoint(self._next_id(), values, ts)
        if not payload["dp"]:
            return      # 全为 None：这一轮没有可上报的数据，别发空包
        message = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        try:
            self._queue.put_nowait((self._topic(), message))
        except Exception:  # noqa: BLE001 - 队列满：丢最旧并计数（与父类一致）
            try:
                self._queue.get_nowait()
                self._queue.put_nowait((self._topic(), message))
            except Exception:  # noqa: BLE001
                pass
            self.dropped += 1

    # -- 对外接口（与 Runtime 对接） --------------------------------------

    def publish_reading(self, summary: Dict[str, Any], ts: Optional[float] = None) -> None:
        """上报一次读数摘要（按 stream_map 映射成本地量 → 数据流名）。

        ⚠️ **只上报本地量名**（``heart_rate_bpm`` / ``body_temp_c`` …，见 ``stream_map``）。
        传进来的其它键会被忽略；若一个字段都没映射上，本调用**什么都不会发**，
        并且记一条 warning + 累加 ``skipped_empty`` ——
        **不允许"静默不发"**（2026-09-22 实测踩到：用自造键 ``probe_temp`` 调用，
        数据点一条都没发出去却毫无提示，白白排查了半天）。
        """
        values: Dict[str, Any] = {}
        for local_key, stream in self.onenet.stream_map.items():
            value = summary.get(local_key)
            if value is None:
                continue
            # 活动状态是字符串（detected/idle/unknown），OneNET 数据点支持 string [文档]
            values[stream] = value
        # 顺带把"数据是否过期"也报到云端：云端据此判断树莓派是不是采集停了
        if summary.get("data_age_s") is not None:
            values["data_age_s"] = round(float(summary["data_age_s"]), 1)

        if not values:
            self.skipped_empty += 1
            _LOG.warning(
                "OneNET 本轮没有任何可上报字段（入参键：%s；已配置数据流：%s）"
                "—— 请用 stream_map 里的本地量名，或把该量加进 onenet.stream_map",
                sorted(summary)[:8], sorted(self.onenet.stream_map),
            )
            return
        self._enqueue_datapoint(values, ts)

    def publish_alarm(self, event: Any, ts: Optional[float] = None) -> None:
        """上报一条报警（独立数据流 alarm_code / alarm_level）。"""
        if not self.onenet.publish_alarm:
            return
        try:
            code = event.code.value
        except AttributeError:
            code = str(event)
        values = {"alarm_code": code}
        severity = getattr(event, "severity", None)
        if severity is not None:
            values["alarm_level"] = int(severity)
        value = getattr(event, "value", None)
        if value is not None:
            values["alarm_value"] = value
        self._enqueue_datapoint(values, ts if ts is not None else getattr(event, "ts", None))

    def publish_status(self, status: Dict[str, Any], ts: Optional[float] = None) -> None:
        """上报一条设备状态（版本/设备数/故障设备数）。"""
        if not self.onenet.publish_status:
            return
        devices = status.get("devices", {}) or {}
        values = {
            "version": str(status.get("version") or ""),
            "online": 1,
            "device_count": len(devices.get("assembled") or []),
            "device_error_count": len(devices.get("errors") or {}),
        }
        self._enqueue_datapoint(values, ts)

    # -- 诊断 ------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        info = super().status()
        info.update({
            "platform": "onenet-mqtt-suite",
            "product_id": self.onenet.product_id,
            "device_name": self.onenet.device_name,
            "topic": self._topic(),
            "token_age_s": (round(self.clock() - self.token_issued_at, 1) if self.token_issued_at else None),
            "token_ttl_s": self.onenet.token_ttl_s,
            "results": self.received_results[-5:],
            "skipped_empty": self.skipped_empty,
            "note": "token 只在日志里出现长度，不打印内容",
        })
        return info


__all__ = [
    "OneNetConfig",
    "OneNetPublisher",
    "OneNetError",
    "sign_token",
    "device_resource",
    "product_resource",
    "DEFAULT_STREAM_MAP",
    "TOKEN_VERSION",
]
