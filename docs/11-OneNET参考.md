# 11 · OneNET 平台补充资料（对照表 / 算法细则 / 未实测清单）

> 这是 [`04-OneNET云端接入.md`](04-OneNET云端接入.md) 的**技术补充**：
> 把平台口径、字段含义、两套产品线的差异集中在一处，方便写报告与排查。
>
> **证据等级标注**（本项目纪律：区分实测与推断）：
> - `[文档]` = 来自 OneNET 官方文档（2026-09-22 查阅，链接见文末），**未在真实平台上实测**；
> - `[实测]` = 本项目代码里已用**可复算的测试**验证过（例如 token 算法的手算对照）；
> - `[推断]` = 我们的设计选择，未见官方明确说明。

---

## 1. 两套产品线对照（**最容易踩的坑**）

OneNET 目前同时存在两套接入方式，**鉴权算法一样、topic 与 payload 完全不同**：

| 维度 | **MQTT物联网套件 / 多协议接入（旧版）** ← 本项目用这套 | **OneNET Studio（新版，物模型）** |
| --- | --- | --- |
| 数据模型 | **数据流 - 数据点**（不需要预先定义） | **物模型**（属性/事件/服务，需先在控制台定义） |
| 上报主题 | `$sys/{pid}/{device}/dp/post/json` | 属性/事件/服务各自的 `$sys/...` 主题 [文档] |
| 上报报文 | `{"id":1,"dp":{"heart_rate":[{"v":72,"t":…}]}}` | OneJSON（`{"id":…,"version":"1.0","params":{"heart_rate":{"value":72}}}`）[文档] |
| 控制台入口 | 旧版控制台 → 多协议接入 | OneNET Studio |
| 适合 | 快速上手、数据流自由 | 规范化产品、需要平台侧物模型展示 |

> **判据（怎么一眼分辨自己在哪套平台上）**：
> **建产品时有没有让你"定义物模型/属性"** —— 有 ⇒ Studio（新版）；没有、直接建设备 ⇒ 旧版。

本项目实现的是**旧版**（见 `rpi/health_monitor/net/onenet.py` 顶部说明）。
若你们实际在 Studio 上，需要另写一个适配器（鉴权函数可直接复用 `sign_token`）。

---

## 2. 连接与鉴权参数（`[文档]`）

| 项 | 值 |
| --- | --- |
| 服务地址（非加密） | `mqtts.heclouds.com` : `1883` |
| 服务地址（TLS） | `mqttstls.heclouds.com` : `8883`（证书见官方文档下载） |
| IPv6（非加密） | `2409:8060:8ea:601::13:7c64` : `1883` |
| IPv6（TLS） | `2409:8060:8ea:601::13:7dc8` : `8883` |
| `clientId` | **设备名称** |
| `username` | **产品 ID** |
| `password` | 用**设备密钥**算出的 token |
| keepalive | **10 ~ 1800 秒**；平台在 1.5×keepalive 内没收到上行数据会断开 |

### 2.1 token 字段含义

| 参数 | 是否必须 | 说明 |
| --- | --- | --- |
| `version` | 是 | 参数组版本号，日期格式，目前仅 `2018-10-31` |
| `res` | 是 | 访问资源：`products/{pid}`（产品级）或 `products/{pid}/devices/{设备名}`（**设备连接必须用这个**） |
| `et` | 是 | 过期时间（unix 秒）；小于当前时间则平台拒绝 |
| `method` | 是 | 签名方法：`md5` / `sha1` / `sha256` |
| `sign` | 是 | 签名结果（base64） |

### 2.2 签名算式（`[文档]`，本项目 `[实测]` 双向核对）

```
sign = base64( hmac_<method>( base64decode(access_key), StringForSignature ) )
StringForSignature = et + "\n" + method + "\n" + res + "\n" + version
```
- 参与签名的字符串**只含 value**（不含 `key=`），参数按**名称字符串排序**（et、method、res、version）；
- `sign` 与 `res` 作为 token 的 value 需要 **URL 编码**（`/`→`%2F`、`=`→`%3D`、`+`→`%2B` 等）。

**本项目的实现与验证**：
- 代码：`health_monitor/net/onenet.py::sign_token`
- 双向核对测试：`tests/integration/test_onenet.py`（md5/sha1/sha256 三种方法都用手算结果比对）
- 离线自检工具：`python3 scripts/onenet_token.py --selftest`

> **可复用判据**：token 生成的验证方式不是"跑通就行"，而是**独立手算一遍逐字符比对**——
> 跑通只能证明"平台收下了"，手算才证明"算法是对的"。

---

## 3. 上报格式（数据点，`[文档]`）

**主题**：`$sys/{pid}/{device-name}/dp/post/json`（QoS 0/1）
**回执主题**：`$sys/{pid}/{device-name}/dp/post/json/accepted` 与 `.../rejected`

**报文**：

```json
{
  "id": 123,
  "dp": {
    "heart_rate": [{ "v": 72, "t": 1552289676 }],
    "body_temp":  [{ "v": 36.5, "t": 1552289676 }],
    "motion":     [{ "v": "detected", "t": 1552289677 }]
  }
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | int | 是 | 消息 ID，大于 0 的整数（4 字节有符号范围） |
| `dp` | object | 是 | `{数据流名: [{v, t}, …]}` |
| `v` | int/float/string/object | 是 | 数据点值 |
| `t` | int | 否 | unix 秒；不传则用平台到达时间 |

**失败回执**：`{"id":123,"err_code":98,"err_msg":"Illegal Data"}`（98 = payload 格式有误）[文档]

### 3.1 本项目上报的数据流

| 数据流 | 来源 | 说明 |
| --- | --- | --- |
| `heart_rate` | `heart_rate_bpm` | 心率 bpm |
| `spo2` | `spo2_percent` | 血氧 % |
| `body_temp` | `body_temp_c` | 精密体温 ℃ |
| `ambient_temp` | `ambient_temp_c` | 室温 ℃ |
| `humidity` | `humidity_percent` | 湿度 % |
| `motion` | `motion_state` | `detected`/`idle`/`unknown` |
| `data_age_s` | 快照新鲜度 | 距最近一次成功采集的秒数 |
| `alarm_code` / `alarm_level` / `alarm_value` | 报警事件 | 发生时才上报 |
| `version` / `online` / `device_count` / `device_error_count` | 启动时 | 设备状态自述 |

映射表在 `config/devices.json` 的 `onenet.stream_map` 里可改。

> **设计取舍**：值为 `None` 的字段**整条跳过**，不上报 0。
> 这与项目"数据缺失绝不当成正常"的红线一致——云端看到的"没有这个点"和"值为 0"是两回事。

---

## 4. 规则引擎：消息源格式（`[文档]`，**未实测**）

若要让云把数据转发回来，规则引擎会按固定结构把消息 POST 到你的 URL：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `sysProperty.messageType` | string | 固定 `deviceDatapoint`（设备数据点） |
| `sysProperty.productId` | string | 产品 ID |
| `appProperty.deviceId` | string | 设备 ID |
| `appProperty.dataTimestamp` | int | 数据点时间戳（**毫秒**） |
| `appProperty.datastream` | string | 数据流名称 |
| `body` | object/string/number | 数据点内容 |

示例（数值型）[文档]：

```json
{
  "sysProperty": { "messageType": "deviceDatapoint", "productId": "90273" },
  "appProperty": { "deviceId": "102839", "dataTimestamp": 15980987429000, "datastream": "temperature" },
  "body": 10
}
```

生命周期事件（上线/离线）的 `messageType` 是 `deviceLifeCycle`，`body.event` 取 `online`/`offline`[文档]。

> ⚠️ **诚实标注（重要）**：以上结构**来自官方文档，本项目尚未在真实平台/真实规则上收到过推送**。
> 因此我们的接收端点 `POST /api/v1/cloud/callback` **故意做成"宽容接收"**：
> 不校验字段、先原样记录（内存保留最近 20 条）+ 记日志，并**始终应答** `{"code":0,"msg":"ok"}`
> （平台约定的成功应答）。这样第一次联调**不会因为格式差异而失败**，
> 你只要看日志里"到底收到了什么"，再按实际结构写解析即可。

---

## 5. 安全清单（要暴露到公网时必须做）

| 风险 | 对策 |
| --- | --- |
| 设备密钥泄露 | 只放环境变量 `HEALTH_ONENET_KEY` 或本地 `devices.json`（已被 `.gitignore` 排除）；**绝不入库** |
| token 泄露 | 本项目日志**只打印 token 长度**，不打印内容；`status()` 里也不含 token |
| 回调端点被伪造 | ① `serve --token <长口令>`；② 推送 URL 带只有你知道的路径/查询串并在服务端校验 |
| 明文 MQTT | 需要加密就用 `tls: true` + 8883（要装平台证书） |
| 令牌过期导致断连 | 启动时现签（默认 24h）；长期运行请调大 `token_ttl_s` 或定时重启服务 |

---

## 6. 未实测清单（**报告里请如实这样写**）

| 项 | 状态 | 说明 |
| --- | --- | --- |
| token 算法 | ✅ **已实测**（手算对照，三种签名） | `tests/integration/test_onenet.py` |
| 数据点报文构造 | ✅ **已实测**（纯函数单测） | 结构/时间戳/None 跳过 |
| MQTT 连接三要素拼装 | ✅ **已实测**（假 paho 注入） | clientId/username/password、keepalive、订阅回执 |
| 真实连上 `mqtts.heclouds.com` | ⬜ **未实测** | 需要真实账号与设备密钥 |
| 控制台数据流出现数值 | ⬜ **未实测** | 同上 |
| 规则引擎推送、回调解析 | ⬜ **未实测** | 需要公网可达的回调 URL |
| TLS（8883）连接 | ⬜ **未实测** | 需要平台证书 |
| token 长期有效期与自动重签 | ⚠️ 部分 | 现签逻辑已测；"到期前自动重签"**尚未实现**（临时办法：调大 ttl 或定时重启） |

> 报告建议写法举例：
> "本系统的 OneNET 上报链路已按官方文档实现并完成**离线验证**（token 算法双向核对、
> 报文结构单测、连接参数拼装测试）；受限于课程环境（无公网回调地址），
> **真机连云与规则引擎转发尚未实测**，已预留宽容接收与日志留证以便现场联调。"

---

## 7. 官方文档来源

| 主题 | 链接 |
| --- | --- |
| token 算法 | <https://open.iot.10086.cn/doc/mqtt/book/manual/auth/token.html> |
| token 生成示例（Python） | <https://open.iot.10086.cn/doc/mqtt/book/manual/auth/python.html> |
| 设备开发指南（服务地址 / 三要素 / keepalive） | <https://open.iot.10086.cn/doc/mqtt/book/device-develop/manual.html> |
| 数据点 topic 簇 | <https://open.iot.10086.cn/doc/mqtt/book/device-develop/topics/dp-topics.html> |
| 规则引擎基础消息格式 | <https://open.iot.10086.cn/doc/mqtt/book/manual/rule-engine/dataFormat.html> |
| 设备命令 topic 簇 | <https://open.iot.10086.cn/doc/mqtt/book/device-develop/topics/cmd-topics.html> |
| OneNET Studio（新版，另一套） | <https://open.iot.10086.cn/doc/iot_platform/book/device-connect&manager/MQTT/mqtt-device-development.html> |
