package com.dboycht.healthmonitor.data

import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonElement

/**
 * OneNET **新版 HTTP API** 的响应模型（`GET /datapoint/history-datapoints`）。
 *
 * 这份形状是**2026-10-02 从真平台抓下来的**（不是照文档手写的 —— 见 `ERROR.md` E79 的教训）：
 *
 * ```json
 * {"code":0,"msg":"succ","request_id":"…","data":{"count":18,"datastreams":[
 *    {"id":"heart_rate","datapoints":[{"at":"2026-09-30 21:38:13","at_timestamp":1790775493000,"value":89}]},
 *    {"id":"motion","datapoints":[{"at":"2026-10-01 23:30:23","at_timestamp":1790868623000,"value":"unknown"}]}]}}
 * ```
 *
 * ⚠️ **`value` 的类型是开放的**：真机上既有数字（`89`、`22.7`）也有字符串（`"unknown"`、`"1.0.1"`）
 * ⇒ 用 [JsonElement] 接（同一个教训：**值的类型不由我定，就别写死**）。
 */
@Serializable
data class OneNetResponse(
    /** 平台业务码：`0` = 成功（HTTP 200 也可能是非 0，例如 10417 产品物模型未找到）。 */
    val code: Int? = null,
    val msg: String? = null,
    val data: OneNetData? = null,
)

@Serializable
data class OneNetData(
    val count: Int? = null,
    val datastreams: List<OneNetStream> = emptyList(),
)

@Serializable
data class OneNetStream(
    /** 数据流名（= 树莓派上报时的 metric），例如 `heart_rate` / `ambient_temp` / `motion`。 */
    val id: String = "",
    val datapoints: List<OneNetPoint> = emptyList(),
)

@Serializable
data class OneNetPoint(
    /** 平台给的可读时间，例如 `"2026-09-30 21:38:13"`（**平台本地时间字符串**）。 */
    val at: String? = null,
    /** 同上的毫秒时间戳（用它算"多久以前"，字符串只用来显示）。 */
    val at_timestamp: Long? = null,
    val value: JsonElement? = null,
)
