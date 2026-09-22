# 04 · OneNET 云端接入 + 规则引擎转发（照着点，一步一步）

> **目标**：树莓派把数据上报到 **OneNET（中国移动物联网平台）**，再由 OneNET **规则引擎**把数据
> 转发回我们自己的服务/App（"云云对接"），形成 **端 → 边 → 云 → 应用** 的完整链路。
> **预计 60 分钟**（主要在控制台上点）。
> **做完的判据**：第 8 节 7 条勾完。

> ⚠️ **先说清平台版本**：OneNET 目前有两套并存的产品线，**鉴权算法相同、上报格式完全不同**。
> 本项目用的是 **[旧版] MQTT物联网套件 / 多协议接入（数据流-数据点）**：
>
> | | 本项目（旧版，数据流-数据点） | OneNET Studio（新版，物模型 OneJSON） |
> | --- | --- | --- |
> | 上报内容 | `{"id":1,"dp":{"heart_rate":[{"v":72,"t":…}]}}` | 物模型属性/事件/服务 |
> | 控制台位置 | 旧版控制台 → 多协议接入 | OneNET Studio |
> | 是否需先建物模型 | **不需要** | 需要（先定义属性） |
>
> 如果你打开的控制台界面与本手册截图/描述**不一样**（比如让你先"定义物模型"），
> 说明你在 Studio 上——那需要另写一个适配器，请告诉负责同学（`net/onenet.py` 顶部注明了差异）。

---

## 1. 注册与实名（5 分钟）

1. 打开 <https://open.iot.10086.cn/> → 注册账号（手机号 + 验证码）。
2. 登录后按提示做**实名认证**（个人认证即可；不认证无法创建产品）。
3. 进入**旧版控制台**：页面上方/右上角找「控制台」→ 选择 **多协议接入**（或"MQTT物联网套件"）。

> 找不到旧版入口：官方论坛有「找旧版入口」的说明帖；实在找不到就换一个浏览器再登录试试。

## 2. 创建产品（5 分钟）

| 字段 | 填什么 | 说明 |
| --- | --- | --- |
| 产品名称 | `居家老人健康监护` | 随便起，中文可以 |
| 行业 | 其他 / 健康 | 影响不大 |
| 设备接入方式 | **设备接入** | 不是"网关接入" |
| 设备接入协议 | **MQTT** | 本项目走 MQTT |
| 联网方式 | WiFi / 以太网 | 树莓派是有线或 WiFi |
| 数据格式 | **JSON** | 本项目上报 JSON |
| 其他 | 默认 | |

创建成功后，在**产品详情页**记下两个值：

- **产品 ID**（纯数字，例如 `123123`）→ 配置里的 `product_id`
- **产品 APIKey**（一长串 base64，末尾常有 `=`)→ 产品级密钥（**设备连接不用它**，见第 3 步）

## 3. 创建设备并拿设备密钥（5 分钟）

1. 产品详情 → **设备列表** → **添加设备**
2. 设备名称：**同一产品内唯一**，建议用能对上的名字，例如 `living-room-pi`
   （官方建议用 SN / MAC / IMEI；本项目用固定名字，方便配置）
3. 鉴权信息：可留空（保留默认）
4. 创建成功后，在设备详情页找到 **设备密钥**（也可能标为 `key` / `APIKey`）——**这一串才是设备连接要用的密钥**
5. 记录：

| 配置项 | 从哪来 | 例子 |
| --- | --- | --- |
| `product_id` | 产品详情页 | `123123` |
| `device_name` | 你刚才起的 | `living-room-pi` |
| `access_key` | **设备详情页的"设备密钥"** | `KuF3NT/jUBJ62LNBB/A8XZA9CqS3Cu79B/ABmfA1UCw=` |

> ⚠️ **别搞混**：产品 APIKey 与设备密钥长得一样，但 token 的 `res` 不同、**不能混用**。
> 用产品密钥去连接设备会被平台拒（错误码 `rc=5` 未授权）。

## 4. 生成 token 并确认参数（5 分钟）

项目自带工具（**不需要联网**）：

```bash
cd ~/raspberry-health-monitor/rpi
python3 scripts/onenet_token.py \
    --pid 123123 \
    --device living-room-pi \
    --key "KuF3NT/jUBJ62LNBB/A8XZA9CqS3Cu79B/ABmfA1UCw=" \
    --method sha256 --ttl 86400
```

输出会直接给出**要填进配置/调试工具的四个值**：

```
服务地址（非加密）：mqtts.heclouds.com : 1883
服务地址（TLS）   ：mqttstls.heclouds.com : 8883
clientId（=设备名）：living-room-pi
username（=产品ID）：123123
password（=token） ：version=2018-10-31&res=products%2F123123%2Fdevices%2Fliving-room-pi&et=…&method=sha256&sign=…
```

先自检一下算法实现没问题（可离线跑）：

```bash
python3 scripts/onenet_token.py --selftest
```
预期：`✅ 自检通过：md5 / sha1 / sha256 三种签名的手算结果与实现一致；非法入参会明确报错`

> 💡 这个算法已按官方文档公式**独立手算核对**（见 `tests/integration/test_onenet.py`），
> 所以"token 算错"基本可以排除；若连接被拒，优先查**密钥是不是设备级的**。

## 5. 填进项目配置（5 分钟）

编辑 `rpi/config/devices.json`，把 `onenet` 段改成（**密钥不要写进仓库**，用环境变量）：

```json
"onenet": {
  "enabled": true,
  "product_id": "123123",
  "device_name": "living-room-pi",
  "access_key": "",
  "method": "sha256",
  "token_ttl_s": 86400,
  "host": "",
  "port": 0,
  "tls": false,
  "keepalive": 120,
  "interval_s": 30.0,
  "topic_template": "$sys/{pid}/{device}/dp/post/json",
  "subscribe_result": true,
  "qos": 1,
  "publish_alarm": true,
  "publish_status": true
}
```

然后在**启动服务的那个终端**里设置密钥环境变量（这样它不会进仓库）：

```bash
export HEALTH_ONENET_KEY='KuF3NT/jUBJ62LNBB/A8XZA9CqS3Cu79B/ABmfA1UCw='
```

启动（**先看日志，别急着配规则引擎**）：

```bash
python3 -m health_monitor serve --real --port 8080
```

**判据（日志里必须出现这些）**：

```
OneNET 上报已启动：mqtts.heclouds.com:1883（产品 123123 / 设备 living-room-pi，token 长度 128，有效期 86400s）
OneNET 已连接：mqtts.heclouds.com:1883
OneNET 已接收数据点：{'id': 1}
```

- 只有"上报已启动"、没有"已连接" ⇒ 网络出不去（换网络/开加速器/查防火墙）
- `OneNET 连接被拒绝：rc=4 用户名或密码（token）不对` ⇒ 产品 ID 或 key/`res` 不匹配
- `rc=5 未授权：检查 access_key 与 res 是否配套` ⇒ **十有八九用了产品密钥而不是设备密钥**
- `OneNET 拒绝了本次数据点：{'id': 3, 'err_code': 98, 'err_msg': 'Illegal Data'}` ⇒ payload 格式问题（本项目已按文档格式，若出现请把日志发给负责同学）

> 没有"已接收数据点"也别慌：**平台回执需要设备订阅 accepted topic**，
> 本项目默认 `subscribe_result: true` 会订阅；若仍然看不到回执，去控制台看**设备数据流**是否已出现数值。

## 6. 在 OneNET 控制台确认数据上去了（5 分钟）

**这是"上云成功"的第一手证据**：

1. 控制台 → 产品 → 设备列表 → 点你的设备
2. 看 **数据流**（数据点）列表：应出现 `heart_rate`、`spo2`、`body_temp`、`ambient_temp`、
   `humidity`、`motion`、`data_age_s` 等数据流，并有最近时间戳
3. 点某个数据流能看 **最新数据 / 历史曲线**

> 本项目上报的**数据流名**（在 `config` 的 `onenet.stream_map` 里可改）：

| 数据流 | 含义 | 单位 |
| --- | --- | --- |
| `heart_rate` | 心率 | bpm |
| `spo2` | 血氧 | % |
| `body_temp` | 精密体温 | ℃ |
| `ambient_temp` | 室温 | ℃ |
| `humidity` | 湿度 | % |
| `motion` | 活动状态（`detected`/`idle`/`unknown`） | 字符串 |
| `data_age_s` | 距最近一次成功采集的秒数 | 秒 |
| `alarm_code` / `alarm_level` / `alarm_value` | 报警（发生时才有） | —— |
| `version` / `online` / `device_count` / `device_error_count` | 服务启动时上报一次 | —— |

## 7. 配置规则引擎：把数据转发回我们自己的应用（云云对接，15 分钟）

这一步就是"云→应用"那一段。目标是：**OneNET 收到数据点后，用 HTTP POST 主动推到我们的服务**。

### 7.1 第一步：让我们的接收端点能被云访问

树莓派在家庭局域网里，OneNET 在公网 ⇒ 二选一：

| 方案 | 做法 | 适合 |
| --- | --- | --- |
| **A. 内网穿透（推荐）** | 用 frp / ZeroTier / Tailscale / ngrok 把树莓派的 `8080` 映射出一个公网 URL | 有电脑可常开、想真联调 |
| **B. 已有公网服务器** | 在自己服务器上加一个转发脚本，收到就存库/展示 | 组里有服务器 |
| **C. 只看云端** | **跳过本节**，直接看 OneNET 控制台的数据与曲线（很多课设到这步就够了） | 时间紧 |

> 本项目的接收端点是 **`POST /api/v1/cloud/callback`**（已实现，见 `net/web.py`），
> 它会把收到的 JSON 记日志、留最近 20 条，并按 OneNET 约定返回 `{"code": 0, "msg": "ok"}`。
>
> 用方案 A 时，推送 URL 形如：`http://<穿透域名>/api/v1/cloud/callback`
> （若服务带了 `--token`，把令牌作为查询参数或请求头带上，见 7.4 的安全说明）。

自测接收端点（在树莓派上，不需要真的配 OneNET）：

```bash
curl -s -X POST http://127.0.0.1:8080/api/v1/cloud/callback \
     -H 'Content-Type: application/json' \
     -d '{"sysProperty":{"messageType":"deviceDatapoint","productId":"123123"},
          "appProperty":{"deviceId":"102839","dataTimestamp":15980987429000,"datastream":"temperature"},
          "body":30}'
```
预期：返回 `{"code":0,"msg":"ok",...}`，并且服务日志里出现 `[云端推送] #1 {...}`。

### 7.2 在控制台建规则

旧版控制台 → 产品（或"应用"）→ **规则引擎** → **创建规则**：

1. **规则名称**：`转发健康数据到本机`
2. **消息源**：选 **设备数据点消息**（messageType = `deviceDatapoint`）
3. **筛选（SQL）**：先不筛，全转发（后面可按需筛）。
   若控制台要写 SQL，形如：

   ```sql
   SELECT * FROM deviceDatapoint
   ```
   （不同版本界面措辞不同：有的是"全部数据"，有的是 `SELECT *`。**以界面为准**。）

4. **消息目的地**：选 **HTTP 推送**（有的版本叫"HTTP 转发"）
   - URL：`http://<你的穿透域名>/api/v1/cloud/callback`
   - 方法：`POST`
   - 内容类型：`application/json`
5. 保存 → **启用规则**（别忘这一步，很多"没数据"都是因为规则没启用）

### 7.3 验证转发成功（关键判据）

1. 等一个上报周期（`interval_s` 默认 30 秒）
2. 树莓派日志里应出现：

```
[云端推送] #1 {"sysProperty":{"messageType":"deviceDatapoint","productId":"123123"},"appProperty":{"deviceId":"…","datastream":"heart_rate",…},"body":72}
```

3. 用接口回看最近收到的推送：

```bash
curl -s http://127.0.0.1:8080/api/v1/health | python3 -m json.tool | head -40
```

> ⚠️ **诚实标注**：规则引擎推送的报文结构（`sysProperty` / `appProperty` / `body`）来自
> OneNET 官方《基础消息格式》，**本项目尚未在真实平台上实测**（因为没有账号与穿透环境）。
> 我们的接收端点**故意设计成"宽容接收"**：不管云端发什么 JSON 都先记下来、原样应答成功，
> 所以**第一次联调不会因为格式差异而失败**——你只要看日志里到底收到了什么，
> 再按实际结构写解析（在 `Runtime.record_cloud_push` 里加字段映射即可）。

### 7.4 安全（要暴露到公网就必须看）

- 接收端点默认**不校验来源**（局域网/课程演示够用）。
- 暴露到公网时要加两道：
  1. `serve --token <长口令>`（所有请求需带 `X-Auth-Token`）；
  2. 在 OneNET 的推送 URL 里带一个只有你知道的路径或查询串（例如
     `/api/v1/cloud/callback?k=<随机串>`），并在服务端校验它。
- OneNET 侧同样**不要把设备密钥写进任何公开文档**；本项目用环境变量 `HEALTH_ONENET_KEY`。

---

## 8. 这一步的"完成标志"

- [ ] `python3 scripts/onenet_token.py --selftest` 通过（算法正确）
- [ ] 配置里 `onenet.enabled = true`，且 `python3 -m health_monitor selfcheck --real` 仍无 FAIL
- [ ] 启动日志出现 **`OneNET 已连接`**
- [ ] OneNET 控制台的设备**数据流里能看到真实数值**（心率/体温等）
- [ ] （做云云对接的话）启用了规则引擎，且树莓派日志出现 **`[云端推送] #N`**
- [ ] `curl http://127.0.0.1:8080/api/v1/health` 的 `mqtt`/`cloud` 字段显示 `connected: true`
- [ ] 断网测试：拔网线/关 WiFi 后，**本地报警仍然工作**（蜂鸣器+LED+LCD 照旧），
      恢复网络后日志出现自动重连

## 9. 常见问题速查

| 现象 | 原因 / 处理 |
| --- | --- |
| 启动日志没有 OneNET 相关行 | `onenet.enabled` 还是 false，或没装 `paho-mqtt`（看 `mqtt.reason` 字段） |
| `reason: token 生成失败` | key 不是合法 base64（复制时漏了末尾 `=`）或为空 |
| `rc=4` | 用户名/密码不对：`product_id` 填错，或 token 过期（重启服务会重新签） |
| `rc=5` | 权限不足：**八成用了产品密钥**；必须用设备密钥 |
| 连上但控制台看不到数据 | 看有没有 `rejected` 回执（`Illegal Data`）；确认 `dp` 字段名与数据流名没问题 |
| 规则引擎配了但收不到推送 | 规则**没启用** / URL 打不通（先在树莓派上 `curl` 自己那条 URL 试） |
| 数据流名看不懂 | `config` 里 `onenet.stream_map` 可改映射（`heart_rate_bpm` → `heart_rate`） |
| 断网后一直重连失败 | 正常，paho 会自动重连；本地监护不依赖它 |

> **token 过期**：本项目在**启动时现签**（有效期 `token_ttl_s`，默认 24 小时）。
> 长时间运行（>1 天）会因 token 过期被平台断开 ⇒ 计划实现"到期前自动重签"，
> 目前临时办法是**定时重启服务**（`install-service.sh` 的 systemd 可加 `RuntimeMaxSec`），
> 或把 `token_ttl_s` 设大一些（例如 30 天 = 2592000）。

下一步：出问题查 [`05-排错手册.md`](05-排错手册.md)；
要准备答辩看 [`06-答辩准备.md`](06-答辩准备.md)。
