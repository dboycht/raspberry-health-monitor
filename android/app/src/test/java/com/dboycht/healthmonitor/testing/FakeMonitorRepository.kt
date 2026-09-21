package com.dboycht.healthmonitor.testing

import com.dboycht.healthmonitor.data.MonitorApiResult
import com.dboycht.healthmonitor.data.MonitorRepository
import com.dboycht.healthmonitor.domain.AlarmEvent
import com.dboycht.healthmonitor.domain.AlarmFeed
import com.dboycht.healthmonitor.domain.DeviceInfo
import com.dboycht.healthmonitor.domain.MonitorSnapshot
import com.dboycht.healthmonitor.domain.SystemHealth

/**
 * 假仓库：单元测试与 Compose 预览用，**完全不碰网络**。
 *
 * 每个方法都可以单独设定"成功（返回什么）/ 失败（报什么错）"，
 * 这样就能测"请求失败时界面保留上次数据并标注过期"这类逻辑。
 */
class FakeMonitorRepository(
    var current: MonitorApiResult<MonitorSnapshot> = MonitorApiResult.Success(MonitorSnapshot.EMPTY),
    var alarms: MonitorApiResult<AlarmFeed> = MonitorApiResult.Success(AlarmFeed.EMPTY),
    var health: MonitorApiResult<SystemHealth> = MonitorApiResult.Success(SystemHealth.EMPTY),
    var devices: MonitorApiResult<List<DeviceInfo>> = MonitorApiResult.Success(emptyList()),
    var silenceResult: MonitorApiResult<Double?> = MonitorApiResult.Success(null),
    var sosResult: MonitorApiResult<String> = MonitorApiResult.Success("已收到紧急求助，请立即查看"),
) : MonitorRepository {

    /** 调用次数，便于断言"轮询真的只跑了一次"。 */
    var currentCalls: Int = 0
        private set
    var silenceCalls: Int = 0
        private set
    var sosCalls: Int = 0
        private set

    override suspend fun loadCurrent(): MonitorApiResult<MonitorSnapshot> {
        currentCalls++
        return current
    }

    override suspend fun loadAlarms(limit: Int): MonitorApiResult<AlarmFeed> = alarms

    override suspend fun loadHealth(): MonitorApiResult<SystemHealth> = health

    override suspend fun loadDevices(): MonitorApiResult<List<DeviceInfo>> = devices

    override suspend fun silence(): MonitorApiResult<Double?> {
        silenceCalls++
        return silenceResult
    }

    override suspend fun sos(): MonitorApiResult<String> {
        sosCalls++
        return sosResult
    }

    companion object {
        /** 常见的失败样本（手机不在家里 WiFi 时最常见的那类）。 */
        fun failure(message: String = "连接超时：手机和树莓派可能不在同一网络"): MonitorApiResult.Failure =
            MonitorApiResult.Failure(message)

        fun event(
            code: String,
            ts: Double?,
            severity: Int? = null,
            message: String? = null,
        ): AlarmEvent = AlarmEvent(
            ts = ts,
            code = code,
            severity = severity,
            message = message,
            value = null,
            unit = "",
            source = "test",
        )
    }
}
