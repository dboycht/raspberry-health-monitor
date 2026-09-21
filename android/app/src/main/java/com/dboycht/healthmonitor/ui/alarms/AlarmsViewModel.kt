package com.dboycht.healthmonitor.ui.alarms

import androidx.lifecycle.Lifecycle
import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.repeatOnLifecycle
import androidx.lifecycle.viewModelScope
import com.dboycht.healthmonitor.data.MonitorApiResult
import com.dboycht.healthmonitor.data.MonitorRepository
import com.dboycht.healthmonitor.domain.AlarmCatalog
import com.dboycht.healthmonitor.domain.AlarmFeed
import com.dboycht.healthmonitor.domain.ConnectionStatus
import com.dboycht.healthmonitor.domain.Formatters
import com.dboycht.healthmonitor.domain.Severity
import com.dboycht.healthmonitor.domain.sortedBySeverityThenTime
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

/** 报警页界面状态。 */
data class AlarmsUiState(
    /** 当前报警（来自 `/current` 的 active_alarms，用来点亮横幅）。 */
    val activeAlarms: Map<String, Double> = emptyMap(),
    val feed: AlarmFeed = AlarmFeed.EMPTY,
    val connection: ConnectionStatus = ConnectionStatus.IDLE,
    val errorMessage: String? = null,
    val nowSeconds: Double = System.currentTimeMillis() / 1000.0,
    /** 服务器返回的消音截止时间（Unix 秒）。 */
    val silencedUntil: Double? = null,
    /** 给用户看的一句反馈（消音成功 / 求助已发送 / 失败原因）。 */
    val feedback: String? = null,
    val feedbackSeverity: Int = 0,
    val sendingSos: Boolean = false,
) {
    /** 事件列表：严重的、最近的排前面。 */
    val sortedHistory get() = feed.history.sortedBySeverityThenTime()

    val isSilencedNow: Boolean
        get() = silencedUntil != null && silencedUntil > nowSeconds

    val hasActiveAlarms: Boolean get() = activeAlarms.isNotEmpty()

    /** 横幅配色用的最高严重度；无报警时是 [Severity.NORMAL]。 */
    val maxSeverity: Int
        get() = activeAlarms.keys.maxOfOrNull { AlarmCatalog.defaultSeverity(it) } ?: Severity.NORMAL
}

/**
 * 报警页 ViewModel：轮询 `/api/v1/alarms` + `/api/v1/current`（只要 `active_alarms`），
 * 并提供消音与紧急求助两个动作。
 *
 * 协议 §3 建议报警相关 5 秒刷一次；这里复用 5 秒。
 */
class AlarmsViewModel(
    private val repository: MonitorRepository,
) : ViewModel() {

    private val _uiState = MutableStateFlow(AlarmsUiState())
    val uiState: StateFlow<AlarmsUiState> = _uiState.asStateFlow()

    private var pollingEnabled = false

    /** 与首页同样的生命周期策略：进后台立即停（协议 §5.1）。 */
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
        _uiState.value = _uiState.value.copy(
            connection = ConnectionStatus.CONNECTING,
            nowSeconds = System.currentTimeMillis() / 1000.0,
        )

        val alarmsResult = repository.loadAlarms()
        val currentResult = repository.loadCurrent()

        val failures = listOfNotNull(
            (alarmsResult as? MonitorApiResult.Failure)?.message,
            (currentResult as? MonitorApiResult.Failure)?.message,
        )

        _uiState.value = _uiState.value.copy(
            feed = (alarmsResult as? MonitorApiResult.Success)?.data ?: _uiState.value.feed,
            activeAlarms = (currentResult as? MonitorApiResult.Success)?.data?.activeAlarms
                ?: _uiState.value.activeAlarms,
            connection = if (failures.isEmpty()) ConnectionStatus.CONNECTED else ConnectionStatus.FAILED,
            errorMessage = failures.firstOrNull(),
            nowSeconds = System.currentTimeMillis() / 1000.0,
        )
    }

    /** 消音（协议 §4.6）。消音 ≠ 解除报警，反馈里必须说清楚。 */
    fun silence() {
        viewModelScope.launch {
            when (val result = repository.silence()) {
                is MonitorApiResult.Success -> _uiState.value = _uiState.value.copy(
                    silencedUntil = result.data ?: _uiState.value.silencedUntil,
                    feedback = if (result.data != null) {
                        "已消音：树莓派在 ${Formatters.timestamp(result.data)} 前不响铃。报警状态仍然存在。"
                    } else {
                        "已消音。报警状态仍然存在。"
                    },
                    feedbackSeverity = com.dboycht.healthmonitor.domain.Severity.INFO,
                )

                is MonitorApiResult.Failure -> _uiState.value = _uiState.value.copy(
                    feedback = "消音失败：${result.message}",
                    feedbackSeverity = com.dboycht.healthmonitor.domain.Severity.CRITICAL,
                )
            }
        }
    }

    /** 紧急求助（协议 §4.7）。只有"长按 1.5 秒"后才允许调用本方法。 */
    fun sendSos() {
        if (_uiState.value.sendingSos) return
        viewModelScope.launch {
            _uiState.value = _uiState.value.copy(
                sendingSos = true,
                feedback = "正在发送求助…",
                feedbackSeverity = com.dboycht.healthmonitor.domain.Severity.WARNING,
            )
            when (val result = repository.sos()) {
                is MonitorApiResult.Success -> {
                    _uiState.value = _uiState.value.copy(
                        sendingSos = false,
                        feedback = "✅ 求助已发送：${result.data}（树莓派已解除静音并蜂鸣报警）",
                        feedbackSeverity = com.dboycht.healthmonitor.domain.Severity.CRITICAL,
                    )
                    refreshOnce()
                }

                is MonitorApiResult.Failure -> _uiState.value = _uiState.value.copy(
                    sendingSos = false,
                    feedback = "❌ 求助发送失败：${result.message}",
                    feedbackSeverity = com.dboycht.healthmonitor.domain.Severity.CRITICAL,
                )
            }
        }
    }

    fun clearFeedback() {
        _uiState.value = _uiState.value.copy(feedback = null)
    }

    companion object {
        /** 报警页刷新周期：5 秒（协议 §3）。 */
        const val POLL_INTERVAL_MILLIS: Long = 5_000L

        fun factory(container: com.dboycht.healthmonitor.data.AppContainer): ViewModelProvider.Factory =
            object : ViewModelProvider.Factory {
                @Suppress("UNCHECKED_CAST")
                override fun <T : ViewModel> create(modelClass: Class<T>): T =
                    AlarmsViewModel(container.monitorRepository) as T
            }
    }
}
