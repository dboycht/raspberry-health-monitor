# 11 · OneNET 平台补充资料（对照表 / 算法细则 / 未实测清单）

> 这是 [`04-OneNET云端接入.md`](04-OneNET云端接入.md) 的**技术补充**：
> 把平台口径、字段含义、两套产品线的差异集中在一处，方便写报告与排查。
>
> ✅ **本项目确定使用**：**旧版「MQTT物联网套件 / 多协议接入」——数据流-数据点**
> （2026-09-22 与需求方确认；不是 OneNET Studio 物模型那套）。
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
    "spo2":       [{ "v": 98, "t": 1552289676 }],
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

> ⚠️ **验收口径的权威表在 `docs/07-测试验收记录.md` §5**（C1–C9，逐项带证据）；
> 下表只保留"文档/算法层面"的口径，2026-10-02 已按真机结果更新。

| 项 | 状态 | 说明 |
| --- | --- | --- |
| token 算法 | ✅ **已实测**（手算对照，三种签名） | `tests/integration/test_onenet.py`；App 侧另有跨语言对照 `OneNetTokenTest` |
| 数据点报文构造 | ✅ **已实测**（纯函数单测） | 结构/时间戳/None 跳过 |
| MQTT 连接三要素拼装 | ✅ **已实测**（假 paho 注入） | clientId/username/password、keepalive、订阅回执 |
| 真实连上 `mqtts.heclouds.com` | ✅ **已实测**（2026-09-26） | 见 `docs/07` C3：`MQTT connected=True`。⚠️ 2026-10-02 中午板子再次连不上（校园网侧），断网策略正确接管（见下） |
| 控制台数据流出现数值 | ✅ **已实测**（2026-09-26） | 见 `docs/07` C4 |
| 平台回执（accepted / rejected） | ✅ **已实测** | C5 回执 `{"id":1}`；C6 故意发非法报文 ⇒ `err_code 98 illegal data` |
| 规则引擎推送、回调解析 | ⚠️ **本机侧已验通；平台侧受网络拓扑限制做不到** | 本机回调端点已验 200 + 留证；**规则引擎要 POST 到公网可达 URL**，而本项目只在内网 ⇒ 见 `docs/07` C7 行（含以后要验的两条路） |
| TLS（8883）连接 | ⬜ **未实测** | 需要平台证书 |
| token 长期有效期与自动重签 | ⚠️ 部分 | 上报侧现签逻辑已测；"到期前自动重签"**尚未实现**（临时办法：调大 ttl 或定时重启）。⚠️ **App 侧是每次请求前现签**（见 §6.1），所以没有这个问题 |

> 报告建议写法举例：
> "本系统的 OneNET 上报链路已按官方文档实现并完成**离线验证**（token 算法双向核对、
> 报文结构单测、连接参数拼装测试），并已在真机上完成**平台接收**（含回执与拒绝报文的实测）；
> 受限于课程环境（无公网回调地址），**平台侧规则引擎转发与真断网恢复尚未实测**，
> 已预留宽容接收与日志留证以便现场联调。"

---

## 6.1 手机端**直接读云端数据**（2026-10-02 实测可行，App 已实现）

### 它能解决什么

树莓派在校园网/内网里，手机在外（**移动数据**）时**reach 不到树莓派**。
但只要手机能上网，就能从 **OneNET** 读到"树莓派最后上报过什么" —— 这是本项目
"端 → 边 → **云** → 应用"那一条链路的**应用侧闭环**。

### 正确的接口与鉴权（**踩过坑才写对**）

```http
GET https://iot-api.heclouds.com/datapoint/history-datapoints?product_id=<pid>&device_name=<name>
authorization: version=2018-10-31&res=products%2F<pid>%2Fdevices%2F<name>&et=<unix秒>&method=sha256&sign=<...>
```

| 坑 | 现象 | 正解 |
| --- | --- | --- |
| 用**裸密钥**当 `authorization` | `{"code":10403,"msg":"authentication failed:invalid authorization"}` | 必须是**签名 token**（算法与 MQTT 相同，见 §2.2；`res` 用**设备级** `products/{pid}/devices/{name}`） |
| 查**物模型**接口 | `{"code":10417,"msg":"产品物模型未找到"}` | 我们的是**数据流**产品 ⇒ 用 `datapoint/history-datapoints` |
| 以为 `value` 一定是数字 | — | 真机上**数字与字符串混着来**（`89`、`"unknown"`、`"1.0.1"`）⇒ 客户端按"开放类型"解析 |
| `at` 只有一个字段 | — | 真机同时给 `at`（可读字符串）与 `at_timestamp`（毫秒）⇒ 用后者算"多久以前" |

### 网络可达性（三方实测对照，**这是"为什么值得做"的硬证据**）

| 谁 | 到 `iot-api.heclouds.com` | 到树莓派 |
| --- | --- | --- |
| **手机（移动数据）** | ✅ ping 0% 丢包 / 57 ms | ❌ 不可达（不同网段） |
| 电脑 | ✅ 443 通 | ✅ 以太网直连 + 校园网 |
| **树莓派** | ❌ **超时**（校园网挡该域名） | — |

⇒ 所以：**"手机读云端"不是为了替代板子直连**，而是"手机不在家时也能看到老人最后的状态"。
⚠️ 板子本身**不需要**能访问 `iot-api`（它只走 MQTT 上报）。

### App 侧的实现要点（`android/`）

* **密钥不内置**：产品 ID / 设备名 / 密钥三项由用户在**「设置 → 云端（可选）」手填**，
  只存本机 DataStore。理由：密钥写进 APK 就等于公开；交给用户填还能让他换成**只读**密钥。
* **每次请求前现签 token**（1 小时有效），避免"App 挂后台太久 token 过期"。
* **界面上不误导**（这一页最重要的事）：顶部固定写明"这是**上次上报**、不是实时；
  报警/消音/求助/按需测血氧**云端没有**"；每个值带自己的时间；全部超过 10 分钟就提示
  "树莓派可能停了或断网了，要看此刻状态请去「监护」页"。
* **两套数据源边界清楚**：板子直连（`MonitorRepository`，4 秒轮询、能控制）
  vs 云端只读（`CloudRepository`，30 秒轮询、只读）。

### 真机验收（2026-10-02）

* 手机自带 `curl` 直接打上面的接口 ⇒ `code=0`、**18 条数据流**（心率 89、室温 22.7、版本 1.0.1…）；
* App「云端」页渲染出同一批数据：**室温 23 ℃ / 湿度 56 % / 共 18 条数据流**，
  每条带"上报于 13 小时 18 分前"，并**自动弹出**"超过 10 分钟没更新"的提示；
* ⚠️ 当天中午板子**连不上 MQTT broker**（校园网侧），所以云端数据是旧的 ——
  而这恰好**顺带真实验证了"断网即暂停入队"**：`skipped_offline=78`、无失败堆积、
  `connected=False` 如实上报，恢复后会自动从当前值继续。

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
