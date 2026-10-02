package com.dboycht.healthmonitor.data

import com.dboycht.healthmonitor.domain.CloudReading
import com.dboycht.healthmonitor.domain.CloudSnapshot
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull
import okhttp3.OkHttpClient
import okhttp3.Request

/**
 * **云端（OneNET）只读数据源**：手机在任意网络下都能看"树莓派最后上报了什么"。
 *
 * 与 [MonitorRepository] 的分工（这条边界很重要，界面文案也要照实说）：
 *
 * | | 板子直连（[MonitorRepository]） | 云端只读（本接口） |
 * | --- | --- | --- |
 * | 新鲜度 | **实时**（4 秒轮询） | **上次上报的那一刻**（可能几分钟甚至几小时前） |
 * | 报警/消音/SOS/按需测血氧 | ✅ | ❌（这些是板子本地功能，云端没有） |
 * | 需要手机能连到树莓派 | ✅ | ❌（只要手机能上网） |
 *
 * 为什么用 OkHttp 直接调而不是扩展现有 Retrofit：域名、鉴权头、响应形状**完全不同**
 * （`iot-api.heclouds.com` + `authorization` 签名头 + OneNET 自己的 `code`），
 * 硬塞进同一个 Retrofit 只会让"两个数据源"的边界变糊。
 */
interface CloudRepository {
    /**
     * 拉取各数据流的**最新一个数据点**。
     *
     * @param accessKey 用户在设置页手填的密钥（**不内置在 App 里**）。
     */
    suspend fun loadLatest(productId: String, deviceName: String, accessKey: String): MonitorApiResult<CloudSnapshot>
}

/** 真实实现：签 token → `GET /datapoint/history-datapoints` → 映射成界面模型。 */
class HttpCloudRepository(
    private val http: OkHttpClient = ApiClient.okHttpClient(),
    private val json: Json = HealthJson,
    /** 注入时钟便于测试（默认系统时间）。 */
    private val nowMillis: () -> Long = System::currentTimeMillis,
) : CloudRepository {

    override suspend fun loadLatest(
        productId: String,
        deviceName: String,
        accessKey: String,
    ): MonitorApiResult<CloudSnapshot> = withContext(Dispatchers.IO) {
        val pid = productId.trim()
        val name = deviceName.trim()
        val key = accessKey.trim()
        if (pid.isEmpty() || name.isEmpty() || key.isEmpty()) {
            return@withContext MonitorApiResult.Failure("云端参数不完整：需要产品 ID、设备名与密钥")
        }
        val token = try {
            OneNetToken.sign(key, OneNetToken.deviceResource(pid, name), et = expirySeconds())
        } catch (exc: IllegalArgumentException) {
            return@withContext MonitorApiResult.Failure("密钥不可用：${exc.message}")
        }
        val url = "$BASE_URL/datapoint/history-datapoints?product_id=$pid&device_name=$name"
        val request = Request.Builder()
            .url(url)
            .header("authorization", token)
            .get()
            .build()

        try {
            http.newCall(request).execute().use { response ->
                val body = response.body?.string().orEmpty()
                if (!response.isSuccessful) {
                    return@withContext MonitorApiResult.Failure(
                        "云端返回 HTTP ${response.code}${shortBody(body)}",
                    )
                }
                val parsed = try {
                    json.decodeFromString<OneNetResponse>(body)
                } catch (exc: Exception) {  // noqa: BLE001 - 解析失败要变成人话，不能崩
                    return@withContext MonitorApiResult.Failure(
                        "云端响应解析失败（${exc.javaClass.simpleName}）${shortBody(body)}",
                    )
                }
                // ⚠️ HTTP 200 也可能是业务失败（真机实测：10417 = 该产品没有物模型）
                if (parsed.code != null && parsed.code != 0) {
                    return@withContext MonitorApiResult.Failure(
                        "云端拒绝：code=${parsed.code} ${parsed.msg ?: ""}".trim(),
                    )
                }
                MonitorApiResult.Success(mapToSnapshot(parsed, nowMillis()))
            }
        } catch (exc: Exception) {  // noqa: BLE001 - 网络异常统一成人话
            MonitorApiResult.Failure("连不上云端：${exc.message ?: exc.javaClass.simpleName}")
        }
    }

    /** token 有效期：1 小时。**每次请求前现签**，避免"App 挂后台太久 token 过期"。 */
    private fun expirySeconds(): Long = nowMillis() / 1000 + TOKEN_TTL_S

    private fun shortBody(body: String): String =
        if (body.isBlank()) "" else "：${body.take(120)}"

    companion object {
        /** 新版 OneNET 的 HTTP API 域名（板子走 MQTT，不走这里）。 */
        const val BASE_URL: String = "https://iot-api.heclouds.com"

        const val TOKEN_TTL_S: Long = 3600

        /** 数据流名 → 中文标签（对不上的就原样显示 id，**不假装认识**）。 */
        val LABELS: Map<String, String> = mapOf(
            "heart_rate" to "心率",
            "spo2" to "血氧",
            "ambient_temp" to "室温",
            "humidity" to "湿度",
            "motion" to "活动",
            "alarm_level" to "报警级别",
            "alarm_value" to "报警数值",
            "version" to "固件/服务版本",
        )

        /** 有单位的流，显示时带上单位。 */
        private val UNITS: Map<String, String> = mapOf(
            "heart_rate" to " bpm",
            "spo2" to " %",
            "ambient_temp" to " ℃",
            "humidity" to " %",
        )

        /**
         * 把 OneNET 响应映射成界面模型：**每个流只取最新一个点**，按时间倒序排。
         *
         * `value` 用 [JsonElement] 接（真机里数字与字符串混着来），这里转成显示文本：
         * 数字保持原样、字符串去掉引号、`null` 显示成 `--`（**绝不显示 0**）。
         */
        fun mapToSnapshot(response: OneNetResponse, nowMillis: Long): CloudSnapshot {
            val readings = response.data?.datastreams.orEmpty().mapNotNull { stream ->
                val point = stream.datapoints.maxByOrNull { it.at_timestamp ?: Long.MIN_VALUE }
                    ?: return@mapNotNull null
                val raw = valueText(point.value)
                // ⚠️ 缺值时**不补单位**：项目既有硬规矩"值缺失时不给单位（避免出现 `-- ℃`）"，
                //    这条在面板卡片那边有单测钉着，云端这一路必须同口径。
                val text = if (raw == MISSING) raw else raw + (UNITS[stream.id] ?: "")
                CloudReading(
                    id = stream.id,
                    label = LABELS[stream.id] ?: stream.id,
                    valueText = text,
                    atText = point.at ?: "",
                    ageSeconds = point.at_timestamp?.let { (nowMillis - it) / 1000 },
                )
            }.sortedWith(
                // 同刻并列时按 id 兜底：**要的是"确定"**（同一份数据每次排出来一样），
                // 用中文标签当 tie-break 会把"显示文案"和"排序"耦在一起。
                compareBy({ it.ageSeconds ?: Long.MAX_VALUE }, { it.id }),
            )

            return CloudSnapshot(
                readings = readings,
                fetchedAtMillis = nowMillis,
                streamCount = response.data?.count ?: readings.size,
            )
        }

        /** 值缺失时的占位（与界面上其它地方一致）。 */
        const val MISSING: String = "--"

        private fun valueText(value: JsonElement?): String = when (value) {
            null, is JsonNull -> MISSING
            is JsonPrimitive -> value.contentOrNull ?: value.toString()
            is JsonObject -> value.toString()
            is JsonArray -> value.toString()
            else -> value.toString()
        }
    }
}
