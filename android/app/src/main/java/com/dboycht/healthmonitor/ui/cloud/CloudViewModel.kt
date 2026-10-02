package com.dboycht.healthmonitor.ui.cloud

import androidx.lifecycle.Lifecycle
import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.repeatOnLifecycle
import androidx.lifecycle.viewModelScope
import com.dboycht.healthmonitor.data.CloudRepository
import com.dboycht.healthmonitor.data.MonitorApiResult
import com.dboycht.healthmonitor.domain.CloudSnapshot
import com.dboycht.healthmonitor.settings.SettingsRepository
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

/** 「云端」页界面状态。 */
data class CloudUiState(
    /** 用户是否已填好云端三项（没填就只显示"去设置里填"的引导）。 */
    val configured: Boolean = false,
    val snapshot: CloudSnapshot? = null,
    val loading: Boolean = false,
    /** 上一次拉取失败的原因（成功时清空）。 */
    val errorMessage: String? = null,
    /** 平台报的数据流总数（界面用来解释"为什么只显示了这几项"）。 */
    val streamCount: Int = 0,
    val nowMillis: Long = System.currentTimeMillis(),
) {
    val readings get() = snapshot?.readings.orEmpty()

    /** 有没有"很久没上报"的迹象：最新一个点也超过 [STALE_AFTER_SECONDS]。 */
    val allStale: Boolean
        get() = readings.isNotEmpty() &&
            readings.all { (it.ageSeconds ?: Long.MAX_VALUE) > STALE_AFTER_SECONDS }

    companion object {
        /** 超过这个秒数就提示"云端数据已经很久没更新了"。 */
        const val STALE_AFTER_SECONDS: Long = 600L
    }
}

/**
 * 「云端」页 ViewModel：读 **OneNET** 里"树莓派最后上报过的数据点"。
 *
 * ⚠️ 三条与"板子直连"不同的性质，界面必须说清楚（不然家属会拿旧数据当此刻状态）：
 *
 * 1. **新鲜度**：这里是"上次上报的那一刻"，不是实时；每个值都带自己的时间；
 * 2. **范围**：只有上报过的数据流；`active_alarms` / 消音 / SOS / 按需测血氧**都不在云端**；
 * 3. **网络**：只要手机能上网就能看（**不需要**连到树莓派）—— 这正是这一页存在的意义。
 *
 * 刷新节奏取 30 秒（比板子直连的 4 秒慢得多）：云端数据本来就是"分钟级"的，
 * 刷太勤既没意义也白耗流量。
 */
class CloudViewModel(
    private val cloudRepository: CloudRepository,
    private val settingsRepository: SettingsRepository,
) : ViewModel() {

    private val _uiState = MutableStateFlow(CloudUiState())
    val uiState: StateFlow<CloudUiState> = _uiState.asStateFlow()

    private var pollingEnabled = false

    /** 进页面时先读一次配置，再按 30 秒轮询。 */
    suspend fun startPolling(lifecycle: Lifecycle) {
        lifecycle.repeatOnLifecycle(Lifecycle.State.STARTED) {
            pollingEnabled = true
            while (isActive && pollingEnabled) {
                refreshOnce()
                delay(POLL_INTERVAL_MILLIS)
            }
        }
    }

    fun stopPolling() {
        pollingEnabled = false
    }

    fun refreshNow() {
        viewModelScope.launch { refreshOnce() }
    }

    private suspend fun refreshOnce() {
        val settings = settingsRepository.current()
        val configured = settings.cloudConfigured
        _uiState.value = _uiState.value.copy(
            configured = configured,
            loading = configured,
            nowMillis = System.currentTimeMillis(),
        )
        if (!configured) {
            // 没配就明确显示"未配置"，**不要**去请求（省流量，也避免无谓的失败提示）。
            _uiState.value = _uiState.value.copy(loading = false, errorMessage = null)
            return
        }

        when (
            val result = cloudRepository.loadLatest(
                productId = settings.cloudProductId,
                deviceName = settings.cloudDeviceName,
                accessKey = settings.cloudAccessKey,
            )
        ) {
            is MonitorApiResult.Success -> _uiState.value = _uiState.value.copy(
                snapshot = result.data,
                streamCount = result.data.streamCount,
                loading = false,
                errorMessage = null,
                nowMillis = System.currentTimeMillis(),
            )

            is MonitorApiResult.Failure -> _uiState.value = _uiState.value.copy(
                // 失败时**保留上一次拿到的数据**（并显示失败原因），与板子直连的约定一致。
                loading = false,
                errorMessage = result.message,
                nowMillis = System.currentTimeMillis(),
            )
        }
    }

    companion object {
        /** 云端刷新周期：30 秒（云端数据是分钟级的，不必像板子那样 4 秒）。 */
        const val POLL_INTERVAL_MILLIS: Long = 30_000L

        fun factory(container: com.dboycht.healthmonitor.data.AppContainer): ViewModelProvider.Factory =
            object : ViewModelProvider.Factory {
                @Suppress("UNCHECKED_CAST")
                override fun <T : ViewModel> create(modelClass: Class<T>): T =
                    CloudViewModel(container.cloudRepository, container.settingsRepository) as T
            }
    }
}
