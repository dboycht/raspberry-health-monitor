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

/** 监护端的连接设置：树莓派地址 + 可选 token + 可选的云端（OneNET）只读参数。 */
data class AppSettings(
    /** 树莓派地址原始字符串（未归一化），设置页要原样回显给用户。 */
    val baseUrl: String = DEFAULT_BASE_URL,
    /** 可选鉴权 token；服务器没用 `--token` 启动时留空。 */
    val token: String = "",
    /**
     * 云端（OneNET）只读参数：**由用户手填，绝不内置在 App 里**。
     *
     * 为什么不做成内置常量：密钥一旦写进 APK 就等于公开（反编译即可取出），
     * 而它能读整个产品的数据。交给用户填有三个好处：① 不泄露；
     * ② 用户可以只填一个**只读**密钥；③ 换设备/换密钥不用重新打包。
     * 三项都填了才算配置好（见 [cloudConfigured]）。
     */
    val cloudProductId: String = "",
    val cloudDeviceName: String = "",
    val cloudAccessKey: String = "",
) {
    /** 归一化后的 Retrofit baseUrl；地址非法时返回 null（此时界面提示去设置页改）。 */
    val normalizedBaseUrl: String? get() = UrlNormalizer.normalizeOrNull(baseUrl)

    /** 发给服务器的 token；空白视为"没配 token"。 */
    val effectiveToken: String? get() = token.trim().ifEmpty { null }

    /** 云端三项是否都填了（缺一项就当"没启用云端"）。 */
    val cloudConfigured: Boolean
        get() = cloudProductId.isNotBlank() && cloudDeviceName.isNotBlank() && cloudAccessKey.isNotBlank()

    /** 云端查询用的资源串。 */
    val cloudResource: String
        get() = "products/${cloudProductId.trim()}/devices/${cloudDeviceName.trim()}"

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

    /**
     * 单独保存**云端（OneNET）只读参数**。
     *
     * 为什么单独一个方法（而不是把 [save] 的参数摊大）：两件事的**保存时机与失败面**不同 ——
     * 地址/token 改错会导致连不上树莓派；云端参数是可选功能，填错只影响「云端」页。
     * 分开保存，用户改云端不会顺手动到"连哪个板子"。
     */
    suspend fun saveCloud(productId: String, deviceName: String, accessKey: String): AppSettings

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
            cloudProductId = prefs[KEY_CLOUD_PRODUCT_ID] ?: "",
            cloudDeviceName = prefs[KEY_CLOUD_DEVICE_NAME] ?: "",
            cloudAccessKey = prefs[KEY_CLOUD_ACCESS_KEY] ?: "",
        )
    }

    override suspend fun save(baseUrl: String, token: String): AppSettings {
        val normalized = UrlNormalizer.normalizeOrNull(baseUrl)?.trimEnd('/')
            ?: AppSettings.DEFAULT_BASE_URL
        context.dataStore.edit { prefs ->
            prefs[KEY_BASE_URL] = normalized
            prefs[KEY_TOKEN] = token.trim()
        }
        return current()
    }

    override suspend fun saveCloud(
        productId: String,
        deviceName: String,
        accessKey: String,
    ): AppSettings {
        context.dataStore.edit { prefs ->
            prefs[KEY_CLOUD_PRODUCT_ID] = productId.trim()
            prefs[KEY_CLOUD_DEVICE_NAME] = deviceName.trim()
            prefs[KEY_CLOUD_ACCESS_KEY] = accessKey.trim()
        }
        return current()
    }

    override suspend fun current(): AppSettings = settings.first()

    private companion object {
        val KEY_BASE_URL = stringPreferencesKey("base_url")
        val KEY_TOKEN = stringPreferencesKey("token")
        val KEY_CLOUD_PRODUCT_ID = stringPreferencesKey("cloud_product_id")
        val KEY_CLOUD_DEVICE_NAME = stringPreferencesKey("cloud_device_name")
        val KEY_CLOUD_ACCESS_KEY = stringPreferencesKey("cloud_access_key")
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
        state.value = state.value.copy(baseUrl = normalized, token = token.trim())
        return state.value
    }

    override suspend fun saveCloud(
        productId: String,
        deviceName: String,
        accessKey: String,
    ): AppSettings {
        state.value = state.value.copy(
            cloudProductId = productId.trim(),
            cloudDeviceName = deviceName.trim(),
            cloudAccessKey = accessKey.trim(),
        )
        return state.value
    }

    override suspend fun current(): AppSettings = state.value
}
