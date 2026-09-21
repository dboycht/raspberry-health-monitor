package com.dboycht.healthmonitor.data

import com.dboycht.healthmonitor.domain.AlarmFeed
import com.dboycht.healthmonitor.domain.DeviceInfo
import com.dboycht.healthmonitor.domain.MonitorSnapshot
import com.dboycht.healthmonitor.domain.SystemHealth
import java.net.ConnectException
import java.net.SocketTimeoutException
import java.net.UnknownHostException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.SerializationException
import retrofit2.HttpException

/**
 * 数据仓库：界面只认这一层，不直接碰 Retrofit。
 *
 * 抽成接口是为了两个现实需求：
 *  - 单元测试塞假实现（`FakeMonitorRepository`），一点网络都不碰；
 *  - 将来换 Ktor client 或加本地缓存，界面代码不用改。
 */
interface MonitorRepository {

    suspend fun loadCurrent(): MonitorApiResult<MonitorSnapshot>

    suspend fun loadAlarms(limit: Int = 50): MonitorApiResult<AlarmFeed>

    suspend fun loadHealth(): MonitorApiResult<SystemHealth>

    suspend fun loadDevices(): MonitorApiResult<List<DeviceInfo>>

    /** 消音（协议 §4.6）。返回服务器的 `silenced_until`（Unix 秒），可能为 null。 */
    suspend fun silence(): MonitorApiResult<Double?>

    /** 紧急求助（协议 §4.7）。返回服务器回显的事件描述，用于界面确认提示。 */
    suspend fun sos(): MonitorApiResult<String>
}

/**
 * Retrofit 实现。
 *
 * 每次请求都从 [settingsProvider] 现取地址与 token：用户在设置页改完地址，
 * 下一次轮询（3~5 秒内）就会打到新地址，不必重启 App。
 * Retrofit 实例按 baseUrl 缓存，避免每 3 秒重建一次。
 */
class RetrofitMonitorRepository(
    private val settingsProvider: suspend () -> com.dboycht.healthmonitor.settings.AppSettings,
    private val apiFactory: (String) -> MonitorApi = { baseUrl -> ApiClient.create(baseUrl) },
) : MonitorRepository {

    private var cachedBaseUrl: String? = null
    private var cachedApi: MonitorApi? = null
    private var cachedToken: String? = null

    /** 当前使用的地址（界面上显示"正在连接 xxx"用）。 */
    var lastBaseUrl: String? = null
        private set

    override suspend fun loadCurrent(): MonitorApiResult<MonitorSnapshot> = call { api, token ->
        MonitorSnapshot.from(api.current(token))
    }

    override suspend fun loadAlarms(limit: Int): MonitorApiResult<AlarmFeed> = call { api, token ->
        AlarmFeed.from(api.alarms(limit = limit, token = token))
    }

    override suspend fun loadHealth(): MonitorApiResult<SystemHealth> = call { api, token ->
        SystemHealth.from(api.health(token))
    }

    override suspend fun loadDevices(): MonitorApiResult<List<DeviceInfo>> = call { api, token ->
        DeviceInfo.from(api.devices(token))
    }

    override suspend fun silence(): MonitorApiResult<Double?> = call { api, token ->
        val response = api.silence(token)
        if (!response.ok) throw ApiFailure(response.error ?: "服务器拒绝了消音请求")
        response.silenced_until
    }

    override suspend fun sos(): MonitorApiResult<String> = call { api, token ->
        val response = api.sos(token)
        if (!response.ok) throw ApiFailure(response.error ?: "服务器拒绝了求助请求")
        val code = response.event?.code
        val message = response.event?.message
        message?.takeIf { it.isNotBlank() }
            ?: com.dboycht.healthmonitor.domain.AlarmCatalog.label(code)
    }

    /** 统一：解析地址 → 取/建 api → 执行 → 把异常翻译成中文提示。 */
    private suspend fun <T> call(block: suspend (MonitorApi, String?) -> T): MonitorApiResult<T> =
        withContext(Dispatchers.IO) {
            val settings = settingsProvider()
            val baseUrl = settings.normalizedBaseUrl
                ?: return@withContext MonitorApiResult.Failure(
                    "服务器地址无效，请到「设置」页填写，例如 192.168.1.20:8080",
                )
            lastBaseUrl = baseUrl
            val api = apiFor(baseUrl)
            try {
                MonitorApiResult.Success(block(api, settings.effectiveToken))
            } catch (e: Throwable) {
                MonitorApiResult.Failure(describeError(e), e)
            }
        }

    private fun apiFor(baseUrl: String): MonitorApi {
        val current = cachedApi
        if (current != null && cachedBaseUrl == baseUrl) return current
        val created = apiFactory(baseUrl)
        cachedApi = created
        cachedBaseUrl = baseUrl
        return created
    }

    /** 服务器返回 `{"ok": false, "error": ...}` 时抛这个。 */
    class ApiFailure(message: String) : Exception(message)

    private fun describeError(e: Throwable): String = when (e) {
        is ApiFailure -> e.message ?: "服务器返回错误"
        is HttpException -> when (e.code()) {
            401 -> "鉴权失败（401）：请检查设置页里的 token"
            404 -> "接口不存在（404）：树莓派上的服务版本可能太旧"
            else -> "服务器返回 HTTP ${e.code()}"
        }

        is UnknownHostException -> "找不到该地址：请检查树莓派 IP 与手机是否在同一 WiFi"
        is ConnectException -> "连接被拒绝：树莓派上的服务可能没在运行"
        is SocketTimeoutException -> "连接超时：手机和树莓派可能不在同一网络"
        is SerializationException -> "响应格式不对：树莓派返回的不是预期 JSON"
        else -> "请求失败：${e.javaClass.simpleName} ${e.message.orEmpty()}".trim()
    }
}
