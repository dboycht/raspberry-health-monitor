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
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull

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
    /** 连续失败次数：达 [DashboardViewModel.MAX_CONSECUTIVE_FAILURES] 后**降速**重试（不是停）。 */
    val consecutiveFailures: Int = 0,
    /**
     * 是否处于"降速重试"（连续失败太多，改为 [DashboardViewModel.RETRY_INTERVAL_MILLIS] 一次）。
     *
     * ⚠️ 原字段名是 `pollingStopped`（"已停止轮询"）—— **2026-10-02 真机验收证明那个名字是假的**：
     * 循环确实退出了，而"刷新"按钮又拉不起来它 ⇒ 界面显示"已连接/刚刚"却永远不再刷新。
     * 现在语义是"**慢下来了，但没停**"，名字也照实说。
     */
    val slowRetry: Boolean = false,
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
 *  - 连续失败达上限后**不退出循环**，而是**降速重试**（[RETRY_INTERVAL_MILLIS]），
 *    一旦恢复（或用户点刷新）立刻回到 4 秒。
 *
 * ⚠️ **2026-10-02 真机验收抓到的严重缺陷（已修，见 [slowRetry] 的注释）**：
 * 原实现"连续 3 次失败就 `pollingEnabled = false` 退出 while 循环"，
 * 而 `refreshNow()` 只能把开关置回 true **并刷一次** —— **它无法让已经结束的循环重新跑起来**。
 * 表现：断一下网再恢复后，界面显示"已连接 / 刚刚"，**却永远不再刷新**（横幅一直停在旧报警上）。
 * 对一个"看老人有没有事"的 App 来说，这比直接报错更危险 —— **它看起来是活的**。
 */
class DashboardViewModel(
    private val repository: MonitorRepository,
    private val settingsProvider: suspend () -> AppSettings,
) : ViewModel() {

    private val _uiState = MutableStateFlow(DashboardUiState())
    val uiState: StateFlow<DashboardUiState> = _uiState.asStateFlow()

    /**
     * 轮询是否还该继续。**只由 [stopPolling]（onStop）置 false** ——
     * 失败**不再**让它变 false（那正是上面那个"静默冻住"的根因）。
     */
    private var pollingEnabled = true

    /**
     * "立刻刷新一次"的唤醒信号（CONFLATED：连点多次只算一次）。
     *
     * 为什么需要它：用户在降速重试期间点「刷新」，期望**马上**看到结果，
     * 而不是再等 20 秒。循环在 `delay` 期间用 [kotlinx.coroutines.withTimeoutOrNull]
     * 等这个信号 ⇒ 要么等到超时（正常节奏），要么被立刻唤醒。
     */
    private val wakeUp = Channel<Unit>(Channel.CONFLATED)

    suspend fun loadInitialSettings() {
        val settings = settingsProvider()
        _uiState.value = _uiState.value.copy(baseUrl = settings.baseUrl)
    }

    /**
     * 生命周期感知的轮询：`CURRENT/STARTED` 期间跑，退到 `CREATED`（onStop）立即取消。
     *
     * ⚠️ 这里**只做生命周期接线**，真正的循环在 [pollLoop] —— 那样它才能在**纯 JVM 单测**里跑
     * （`LifecycleRegistry` 在单测里要 `Looper`，会 NPE；而"循环会不会停住"恰恰是必须测的那件事）。
     */
    suspend fun startPolling(lifecycle: Lifecycle) {
        lifecycle.repeatOnLifecycle(Lifecycle.State.STARTED) { pollLoop() }
    }

    /**
     * 轮询循环本体（**不依赖 Android 生命周期，可直接单测**）。
     *
     * 两条不变量：
     *  1. **只在离开前台时结束**（由调用方的生命周期取消），**失败次数绝不结束它**；
     *  2. 节奏由 [DashboardUiState.slowRetry] 决定：正常 4 秒，连续失败后 20 秒。
     */
    internal suspend fun pollLoop() {
        pollingEnabled = true
        _uiState.value = _uiState.value.copy(slowRetry = false, consecutiveFailures = 0)
        while (currentCoroutineContext().isActive && pollingEnabled) {
            refreshOnce()
            val waitMillis =
                if (_uiState.value.slowRetry) RETRY_INTERVAL_MILLIS else POLL_INTERVAL_MILLIS
            // 等一个周期，或被 refreshNow() 提前唤醒（点「刷新」不用再等满一个周期）。
            withTimeoutOrNull(waitMillis) { wakeUp.receive() }
        }
    }

    /** onStop 时显式停止轮询（协议 §5.1 的硬要求）。 */
    fun stopPolling() {
        pollingEnabled = false
        _uiState.value = _uiState.value.copy(connection = ConnectionStatus.IDLE)
    }

    /**
     * 手动刷新：**立刻**刷一次，同时解除降速、叫醒循环（失败计数在**成功**时才清零）。
     *
     * 为什么既"刷一次"又"叫醒"：
     *  - **刷一次**：保证用户点下去马上有反馈 —— 即使循环因为某种原因没在跑（旧实现的坑），
     *    这一下也不会白点；
     *  - **叫醒**（[wakeUp]）：让**循环**立刻继续（否则要等满 20 秒的降速周期），
     *    并把节奏交回循环统一管理。最坏情况是这一次多点了一个 GET —— 相对"界面冻住"，
     *    这点代价可以接受（判据：**宁可多问一次，也不能看起来活着其实不动**）。
     */
    fun refreshNow() {
        _uiState.value = _uiState.value.copy(slowRetry = false)
        wakeUp.trySend(Unit)
        viewModelScope.launch { refreshOnce() }
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
                // ⚠️ 必须一起清掉降速，否则**恢复之后节奏会永远停在 20 秒**
                //    （这是本轮修法自己踩的坑，被"恢复后应退出降速"那条单测当场抓住）。
                slowRetry = false,
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
                    // 达上限后**降速**，而不是停掉。
                    slowRetry = failures >= MAX_CONSECUTIVE_FAILURES,
                )
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

        /**
         * 连续失败达 [MAX_CONSECUTIVE_FAILURES] 后的**降速重试**周期：20 秒。
         *
         * 取值理由：① 比 4 秒慢 5 倍，足以满足"别让手机和树莓派互相刷屏"的原意；
         * ② 又足够快 —— 树莓派服务重启/路由器重启通常 10~30 秒内恢复，
         * 家属几乎立刻能看到界面自己活过来，**不必自己去点刷新**。
         */
        const val RETRY_INTERVAL_MILLIS: Long = 20_000L

        /** 连续失败多少次后**降速**（注意：不是"停止"）。 */
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
