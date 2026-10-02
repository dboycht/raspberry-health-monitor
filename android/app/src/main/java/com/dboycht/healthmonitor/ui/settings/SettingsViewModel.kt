package com.dboycht.healthmonitor.ui.settings

import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewModelScope
import com.dboycht.healthmonitor.data.MonitorApiResult
import com.dboycht.healthmonitor.data.MonitorRepository
import com.dboycht.healthmonitor.data.RetrofitMonitorRepository
import com.dboycht.healthmonitor.data.UrlNormalizer
import com.dboycht.healthmonitor.settings.AppSettings
import com.dboycht.healthmonitor.settings.SettingsRepository
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

/** 设置页界面状态。 */
data class SettingsUiState(
    val baseUrl: String = AppSettings.DEFAULT_BASE_URL,
    val token: String = "",
    /** 地址输入框下方的实时提示（用户边打字边告诉他"最终会连到哪儿"）。 */
    val normalizedPreview: String? = null,
    val saved: Boolean = false,
    val savedMessage: String? = null,
    /** 保存后再做一次真实连通性测试的结果。 */
    val testing: Boolean = false,
    val testResult: String? = null,
    val testOk: Boolean = false,
    // ---- 云端（OneNET）只读：**可选**功能，密钥由用户手填、不进 APK ----
    val cloudProductId: String = "",
    val cloudDeviceName: String = "",
    val cloudAccessKey: String = "",
    val cloudSaved: Boolean = false,
    val cloudSavedMessage: String? = null,
) {
    val addressValid: Boolean get() = UrlNormalizer.normalizeOrNull(baseUrl) != null

    /** 云端三项是否都填了（与 `AppSettings.cloudConfigured` 同一判据）。 */
    val cloudConfigured: Boolean
        get() = cloudProductId.isNotBlank() && cloudDeviceName.isNotBlank() && cloudAccessKey.isNotBlank()
}

/**
 * 设置页 ViewModel：地址 + token 的持久化与连通性测试。
 *
 * 关于"测试连接"：实现方式是**临时**用一个指向新地址的仓库去打一次 `/health`，
 * 不修改已保存的设置——这样用户填错地址时不会把 App 卡在连不上的状态。
 */
class SettingsViewModel(
    private val settingsRepository: SettingsRepository,
    /** 用于测试连接；注入工厂便于单测替换。 */
    private val repositoryFactory: (String) -> MonitorRepository = { baseUrl ->
        RetrofitMonitorRepository(settingsProvider = { AppSettings(baseUrl = baseUrl) })
    },
) : ViewModel() {

    private val _uiState = MutableStateFlow(SettingsUiState())
    val uiState: StateFlow<SettingsUiState> = _uiState.asStateFlow()

    suspend fun load() {
        val settings = settingsRepository.current()
        _uiState.value = _uiState.value.copy(
            baseUrl = settings.baseUrl,
            token = settings.token,
            normalizedPreview = UrlNormalizer.normalizeOrNull(settings.baseUrl),
            cloudProductId = settings.cloudProductId,
            cloudDeviceName = settings.cloudDeviceName,
            cloudAccessKey = settings.cloudAccessKey,
        )
    }

    fun onCloudProductIdChange(value: String) {
        _uiState.value = _uiState.value.copy(cloudProductId = value, cloudSaved = false, cloudSavedMessage = null)
    }

    fun onCloudDeviceNameChange(value: String) {
        _uiState.value = _uiState.value.copy(cloudDeviceName = value, cloudSaved = false, cloudSavedMessage = null)
    }

    fun onCloudAccessKeyChange(value: String) {
        _uiState.value = _uiState.value.copy(cloudAccessKey = value, cloudSaved = false, cloudSavedMessage = null)
    }

    /**
     * 保存云端三项（**可选功能**：三项都空 = 关掉云端）。
     *
     * ⚠️ 只做"填没填全"的检查，**不做联网校验** —— 校验放在「云端」页的拉取里，
     * 因为那一步才真正会用到平台的签名与接口（在这里校验等于多打一次网络，且失败原因更绕）。
     */
    fun saveCloud() {
        val current = _uiState.value
        viewModelScope.launch {
            val saved = settingsRepository.saveCloud(
                productId = current.cloudProductId,
                deviceName = current.cloudDeviceName,
                accessKey = current.cloudAccessKey,
            )
            val message = if (saved.cloudConfigured) {
                "已保存云端参数（资源 ${saved.cloudResource}）；去「云端」页看数据"
            } else if (saved.cloudProductId.isBlank() && saved.cloudDeviceName.isBlank() &&
                saved.cloudAccessKey.isBlank()
            ) {
                "已清空云端参数：不再访问云端"
            } else {
                "三项要一起填才算启用（现在缺了至少一项）"
            }
            _uiState.value = _uiState.value.copy(
                cloudProductId = saved.cloudProductId,
                cloudDeviceName = saved.cloudDeviceName,
                cloudAccessKey = saved.cloudAccessKey,
                cloudSaved = true,
                cloudSavedMessage = message,
            )
        }
    }

    fun onBaseUrlChange(value: String) {
        _uiState.value = _uiState.value.copy(
            baseUrl = value,
            normalizedPreview = UrlNormalizer.normalizeOrNull(value),
            saved = false,
            savedMessage = null,
            testResult = null,
        )
    }

    fun onTokenChange(value: String) {
        _uiState.value = _uiState.value.copy(token = value, saved = false, savedMessage = null, testResult = null)
    }

    /** 保存（地址会归一化：只填 192.168.1.20 也能用）。 */
    fun save() {
        val current = _uiState.value
        val normalized = UrlNormalizer.normalizeOrNull(current.baseUrl)
        if (normalized == null) {
            _uiState.value = current.copy(
                savedMessage = UrlNormalizer.INVALID_MESSAGE,
                saved = false,
            )
            return
        }
        viewModelScope.launch {
            val saved = settingsRepository.save(normalized, current.token)
            _uiState.value = _uiState.value.copy(
                baseUrl = saved.baseUrl,
                normalizedPreview = saved.normalizedBaseUrl,
                saved = true,
                savedMessage = "已保存，将连接 ${saved.normalizedBaseUrl}",
            )
        }
    }

    /** 用当前输入框里的地址试打一次 `/health`（协议 §5.3：先确认版本与在线状态）。 */
    fun testConnection() {
        val current = _uiState.value
        val normalized = UrlNormalizer.normalizeOrNull(current.baseUrl)
        if (normalized == null) {
            _uiState.value = current.copy(testResult = UrlNormalizer.INVALID_MESSAGE, testOk = false)
            return
        }
        viewModelScope.launch {
            _uiState.value = _uiState.value.copy(testing = true, testResult = "正在连接 $normalized …", testOk = false)
            val repository = repositoryFactory(normalized)
            when (val result = repository.loadHealth()) {
                is MonitorApiResult.Success -> {
                    val health = result.data
                    _uiState.value = _uiState.value.copy(
                        testing = false,
                        testOk = true,
                        testResult = buildString {
                            append("✅ 连接成功")
                            append("：树莓派服务版本 ")
                            append(health.version ?: com.dboycht.healthmonitor.domain.Formatters.UNKNOWN)
                            if (health.mock == true) append("（当前是模拟模式，未接真实传感器）")
                        },
                    )
                }

                is MonitorApiResult.Failure -> _uiState.value = _uiState.value.copy(
                    testing = false,
                    testOk = false,
                    testResult = "❌ 连接失败：${result.message}",
                )
            }
        }
    }

    companion object {
        fun factory(container: com.dboycht.healthmonitor.data.AppContainer): ViewModelProvider.Factory =
            object : ViewModelProvider.Factory {
                @Suppress("UNCHECKED_CAST")
                override fun <T : ViewModel> create(modelClass: Class<T>): T =
                    SettingsViewModel(container.settingsRepository) as T
            }
    }
}
