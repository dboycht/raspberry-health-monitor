package com.dboycht.healthmonitor.ui.about

import androidx.lifecycle.Lifecycle
import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.repeatOnLifecycle
import androidx.lifecycle.viewModelScope
import com.dboycht.healthmonitor.data.MonitorApiResult
import com.dboycht.healthmonitor.data.MonitorRepository
import com.dboycht.healthmonitor.domain.ConnectionStatus
import com.dboycht.healthmonitor.domain.DeviceInfo
import com.dboycht.healthmonitor.domain.SystemHealth
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

/** 关于/硬件页界面状态。 */
data class AboutUiState(
    /** `/api/v1/health`：版本号、在线时长、各设备状态。 */
    val health: SystemHealth = SystemHealth.EMPTY,
    /** `/api/v1/devices`：器件清单与接线说明。 */
    val devices: List<DeviceInfo> = emptyList(),
    val connection: ConnectionStatus = ConnectionStatus.IDLE,
    val errorMessage: String? = null,
    val loading: Boolean = true,
    val nowSeconds: Double = System.currentTimeMillis() / 1000.0,
) {
    /** 树莓派上的服务版本号（协议 §4.2）；null 表示还没读到。 */
    val serverVersion: String? get() = health.version

    val isMockMode: Boolean get() = health.mock == true

    /** 有问题的设备数（界面上一眼看出哪块传感器挂了）。 */
    val unhealthyDevices: List<com.dboycht.healthmonitor.domain.DeviceHealth>
        get() = health.devices.filter { it.statusText != "正常" }
}

/**
 * 关于页 ViewModel。
 *
 * 协议 §5.3 要求"首屏必须先连一次 /health"：这里在页面进入时拉一次，
 * 并且按 §3 的建议每 10 秒刷新（用来显示"树莓派是否还在线"）。
 */
class AboutViewModel(
    private val repository: MonitorRepository,
) : ViewModel() {

    private val _uiState = MutableStateFlow(AboutUiState())
    val uiState: StateFlow<AboutUiState> = _uiState.asStateFlow()

    private var pollingEnabled = false

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
        _uiState.value = _uiState.value.copy(loading = true, nowSeconds = System.currentTimeMillis() / 1000.0)

        val healthResult = repository.loadHealth()
        // 器件清单基本不变，第一次拿到后就不必每 10 秒重拉（协议 §3：进设置页拉一次）。
        val needsDevices = _uiState.value.devices.isEmpty()
        val devicesResult = if (needsDevices) repository.loadDevices() else null

        val failures = listOfNotNull(
            (healthResult as? MonitorApiResult.Failure)?.message,
            (devicesResult as? MonitorApiResult.Failure)?.message,
        )

        _uiState.value = _uiState.value.copy(
            health = (healthResult as? MonitorApiResult.Success)?.data ?: _uiState.value.health,
            devices = (devicesResult as? MonitorApiResult.Success)?.data ?: _uiState.value.devices,
            connection = if (failures.isEmpty()) ConnectionStatus.CONNECTED else ConnectionStatus.FAILED,
            errorMessage = failures.firstOrNull(),
            loading = false,
            nowSeconds = System.currentTimeMillis() / 1000.0,
        )
    }

    companion object {
        /** `/health` 刷新周期：10 秒（协议 §3）。 */
        const val POLL_INTERVAL_MILLIS: Long = 10_000L

        fun factory(container: com.dboycht.healthmonitor.data.AppContainer): ViewModelProvider.Factory =
            object : ViewModelProvider.Factory {
                @Suppress("UNCHECKED_CAST")
                override fun <T : ViewModel> create(modelClass: Class<T>): T =
                    AboutViewModel(container.monitorRepository) as T
            }
    }
}
