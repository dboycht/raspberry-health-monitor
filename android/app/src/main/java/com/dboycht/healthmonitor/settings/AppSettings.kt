package com.dboycht.healthmonitor.settings

import android.content.Context
import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import com.dboycht.healthmonitor.data.UrlNormalizer
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map

/** 监护端的连接设置：树莓派地址 + 可选 token。 */
data class AppSettings(
    /** 树莓派地址原始字符串（未归一化），设置页要原样回显给用户。 */
    val baseUrl: String = DEFAULT_BASE_URL,
    /** 可选鉴权 token；服务器没用 `--token` 启动时留空。 */
    val token: String = "",
) {
    /** 归一化后的 Retrofit baseUrl；地址非法时返回 null（此时界面提示去设置页改）。 */
    val normalizedBaseUrl: String? get() = UrlNormalizer.normalizeOrNull(baseUrl)

    /** 发给服务器的 token；空白视为"没配 token"。 */
    val effectiveToken: String? get() = token.trim().ifEmpty { null }

    companion object {
        /** 协议 §1 的示例地址，首次安装时填进去，用户改一个 IP 就能用。 */
        const val DEFAULT_BASE_URL: String = "http://192.168.1.20:8080"
    }
}

/**
 * 设置的读写接口。
 *
 * 抽象成接口是为了**单元测试可以塞内存实现**：JVM 单测里没有 Context，
 * 不能真的去建 DataStore 文件。
 */
interface SettingsRepository {
    val settings: Flow<AppSettings>

    /** 保存并对地址做归一化（用户只填 `192.168.1.20` 也能用）。 */
    suspend fun save(baseUrl: String, token: String): AppSettings

    /** 取一次当前值（启动时决定连哪个地址）。 */
    suspend fun current(): AppSettings
}

private val Context.dataStore: DataStore<Preferences> by preferencesDataStore(name = "health_monitor_settings")

/**
 * 基于 **Jetpack DataStore(Preferences)** 的持久化实现（对应需求"持久化"）。
 *
 * 为什么不用 SharedPreferences：DataStore 是 Flow 驱动的，地址/token 改了之后
 * ViewModel 与网络层会自动收到新值并重建 Retrofit，不必手动到处传；
 * 而且它不在主线程做磁盘 IO。
 */
class DataStoreSettingsRepository(private val context: Context) : SettingsRepository {

    override val settings: Flow<AppSettings> = context.dataStore.data.map { prefs ->
        AppSettings(
            baseUrl = prefs[KEY_BASE_URL] ?: AppSettings.DEFAULT_BASE_URL,
            token = prefs[KEY_TOKEN] ?: "",
        )
    }

    override suspend fun save(baseUrl: String, token: String): AppSettings {
        val normalized = UrlNormalizer.normalizeOrNull(baseUrl)?.trimEnd('/')
            ?: AppSettings.DEFAULT_BASE_URL
        context.dataStore.edit { prefs ->
            prefs[KEY_BASE_URL] = normalized
            prefs[KEY_TOKEN] = token.trim()
        }
        return AppSettings(baseUrl = normalized, token = token.trim())
    }

    override suspend fun current(): AppSettings = settings.first()

    private companion object {
        val KEY_BASE_URL = stringPreferencesKey("base_url")
        val KEY_TOKEN = stringPreferencesKey("token")
    }
}

/**
 * 内存实现：单元测试与 Compose 预览用，不落盘。
 */
class InMemorySettingsRepository(initial: AppSettings = AppSettings()) : SettingsRepository {

    private val state = kotlinx.coroutines.flow.MutableStateFlow(initial)

    override val settings: Flow<AppSettings> = state

    override suspend fun save(baseUrl: String, token: String): AppSettings {
        val normalized = UrlNormalizer.normalizeOrNull(baseUrl)?.trimEnd('/')
            ?: AppSettings.DEFAULT_BASE_URL
        val next = AppSettings(baseUrl = normalized, token = token.trim())
        state.value = next
        return next
    }

    override suspend fun current(): AppSettings = state.value
}
