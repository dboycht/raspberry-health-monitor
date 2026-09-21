package com.dboycht.healthmonitor.ui.dashboard

import androidx.lifecycle.Lifecycle
import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.repeatOnLifecycle
import androidx.lifecycle.viewModelScope
import com.dboycht.healthmonitor.data.MonitorApiResult
import com.dboycht.healthmonitor.data.MonitorRepository
import com.dboycht.healthmonitor.domain.ConnectionStatus
import com.dboycht.healthmonitor.domain.MonitorSnapshot
import com.dboycht.healthmonitor.settings.AppSettings
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

/** 首页（监护面板）界面状态。 */
data class DashboardUiState(
    val snapshot: MonitorSnapshot = MonitorSnapshot.EMPTY,
    val connection: ConnectionStatus = ConnectionStatus.IDLE,
    val baseUrl: String = AppSettings.DEFAULT_BASE_URL,
    /** 上次成功刷新的时间（Unix 秒）；页面据此算"数据可能已过期"。 */
    val lastSuccessAt: Double? = null,
    val nowSeconds: Double = System.currentTimeMillis() / 1000.0,
    val errorMessage: String? = null,
    val refreshing: Boolean = false,
    /** 连续失败次数：≥3 次就停掉轮询，别让手机和树莓派互相刷屏。 */
    val consecutiveFailures: Int = 0,
    val pollingStopped: Boolean = false,
    val silenceFeedback: String? = null,
) {
    /** 距上次成功刷新过了多少秒。 */
    val lastSuccessAgeSeconds: Double?
        get() = lastSuccessAt?.let { (nowSeconds - it).coerceAtLeast(0.0) }

    /** 数据是否已经过期（超过 3 个轮询周期）。 */
    val staleData: Boolean
        get() = lastSuccessAgeSeconds?.let { it > STALE_AFTER_SECONDS } ?: false

    /**
     * "树莓派那边数据已过期"的提示文案（协议 §4.1 第四条）。
     *
     * 与 [staleData] 的区别：那个是**本机**视角（多久没成功请求过），
     * 这个是**服务端**视角（请求成功，但树莓派的传感器早就没数据了）。
     * 两种情况都要让用户看见——"服务活着但测不到人"是最危险的状态。
     */
    val serverStaleWarning: String?
        get() = DashboardCards.staleWarningText(snapshot)

    companion object {
        /** 超过这个秒数就提示"数据可能已过期"。 */
        const val STALE_AFTER_SECONDS: Double = 15.0
    }
}

/**
 * 首页 ViewModel：轮询 `/api/v1/current`。
 *
 * 轮询节奏与生命周期（协议 §5.1、§5.2）：
 *  - 每 [POLL_INTERVAL_MILLIS]（4 秒，落在协议要求的 3~5 秒内）拉一次；
 *  - 必须由界面调用 [startPolling] / [stopPolling] 控制：`startPolling` 内部用
 *    `repeatOnLifecycle(STARTED)`，**App 一进后台（onStop）循环立刻取消**，省电；
 *  - 连续 3 次失败后自动停（[DashboardUiState.pollingStopped]），用户点"刷新"再开。
 */
class DashboardViewModel(
    private val repository: MonitorRepository,
    private val settingsProvider: suspend () -> AppSettings,
) : ViewModel() {

    private val _uiState = MutableStateFlow(DashboardUiState())
    val uiState: StateFlow<DashboardUiState> = _uiState.asStateFlow()

    /** 轮询是否还该继续（连续失败太多会置 false）。 */
    private var pollingEnabled = true

    suspend fun loadInitialSettings() {
        val settings = settingsProvider()
        _uiState.value = _uiState.value.copy(baseUrl = settings.baseUrl)
    }

    /**
     * 生命周期感知的轮询循环：`CURRENT/STARTED` 期间跑，退到 `CREATED`（onStop）
     * 立即取消。直接在主线程 `repeatOnLifecycle` 里 suspend，不用自己管线程。
     */
    suspend fun startPolling(lifecycle: Lifecycle) {
        lifecycle.repeatOnLifecycle(Lifecycle.State.STARTED) {
            pollingEnabled = true
            _uiState.value = _uiState.value.copy(pollingStopped = false, consecutiveFailures = 0)
            while (isActive && pollingEnabled) {
                refreshOnce()
                delay(POLL_INTERVAL_MILLIS)
            }
        }
    }

    /** onStop 时显式停止轮询（协议 §5.1 的硬要求）。 */
    fun stopPolling() {
        pollingEnabled = false
        _uiState.value = _uiState.value.copy(connection = ConnectionStatus.IDLE)
    }

    /** 手动刷新：重新打开轮询开关（连续失败计数在**成功**时才会清零）。 */
    fun refreshNow() {
        viewModelScope.launch {
            pollingEnabled = true
            _uiState.value = _uiState.value.copy(pollingStopped = false)
            refreshOnce()
        }
    }

    private suspend fun refreshOnce() {
        _uiState.value = _uiState.value.copy(
            connection = ConnectionStatus.CONNECTING,
            refreshing = true,
            nowSeconds = System.currentTimeMillis() / 1000.0,
        )
        when (val result = repository.loadCurrent()) {
            is MonitorApiResult.Success -> _uiState.value = _uiState.value.copy(
                snapshot = result.data,
                connection = ConnectionStatus.CONNECTED,
                lastSuccessAt = System.currentTimeMillis() / 1000.0,
                nowSeconds = System.currentTimeMillis() / 1000.0,
                errorMessage = null,
                refreshing = false,
                consecutiveFailures = 0,
            )

            is MonitorApiResult.Failure -> {
                val failures = _uiState.value.consecutiveFailures + 1
                _uiState.value = _uiState.value.copy(
                    // 保留上一次成功的数据（协议 §5.2），只改状态与提示。
                    connection = ConnectionStatus.FAILED,
                    refreshing = false,
                    errorMessage = result.message,
                    consecutiveFailures = failures,
                    nowSeconds = System.currentTimeMillis() / 1000.0,
                    pollingStopped = failures >= MAX_CONSECUTIVE_FAILURES,
                )
                if (failures >= MAX_CONSECUTIVE_FAILURES) {
                    pollingEnabled = false
                }
            }
        }
    }

    /** 消音：只让树莓派别响，报警状态不变（协议 §4.6）。 */
    fun silence() {
        viewModelScope.launch {
            when (val result = repository.silence()) {
                is MonitorApiResult.Success -> {
                    val until = result.data
                    _uiState.value = _uiState.value.copy(
                        silenceFeedback = if (until != null) {
                            "已发送消音，树莓派将在 ${formatUntil(until)} 前不响铃（报警未解除）"
                        } else {
                            "已发送消音（报警未解除）"
                        },
                    )
                }

                is MonitorApiResult.Failure -> _uiState.value = _uiState.value.copy(
                    silenceFeedback = "消音失败：${result.message}",
                )
            }
        }
    }

    fun clearSilenceFeedback() {
        _uiState.value = _uiState.value.copy(silenceFeedback = null)
    }

    companion object {
        /** 轮询周期：4 秒（协议 §3 建议 /current 3~5 秒一次，§5.1 不要用长连接）。 */
        const val POLL_INTERVAL_MILLIS: Long = 4_000L

        /** 连续失败多少次后停轮询。 */
        const val MAX_CONSECUTIVE_FAILURES: Int = 3

        private fun formatUntil(unixSeconds: Double): String =
            com.dboycht.healthmonitor.domain.Formatters.timestamp(unixSeconds)

        fun factory(container: com.dboycht.healthmonitor.data.AppContainer): ViewModelProvider.Factory =
            object : ViewModelProvider.Factory {
                @Suppress("UNCHECKED_CAST")
                override fun <T : ViewModel> create(modelClass: Class<T>): T =
                    DashboardViewModel(
                        repository = container.monitorRepository,
                        settingsProvider = { container.settingsRepository.current() },
                    ) as T
            }
    }
}
