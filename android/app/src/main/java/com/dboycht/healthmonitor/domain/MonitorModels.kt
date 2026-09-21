package com.dboycht.healthmonitor.domain

import com.dboycht.healthmonitor.data.AlarmEventDto
import com.dboycht.healthmonitor.data.AlarmsResponse
import com.dboycht.healthmonitor.data.CurrentDataDto
import com.dboycht.healthmonitor.data.CurrentResponse
import com.dboycht.healthmonitor.data.DeviceDescribeDto
import com.dboycht.healthmonitor.data.DeviceHealthDto
import com.dboycht.healthmonitor.data.DeviceInfoDto
import com.dboycht.healthmonitor.data.DevicesResponse
import com.dboycht.healthmonitor.data.HealthResponse
import com.dboycht.healthmonitor.data.LiveAlarmDto

/**
 * 界面直接使用的模型（DTO → 领域模型的映射全部集中在本文件）。
 *
 * 这样做的两个好处：
 *  1. 界面层看不到 JSON 字段名，改接口只改这一处；
 *  2. 映射是**纯 Kotlin 纯函数**，可以在 JVM 单测里直接喂"真实响应样本字符串"来验证
 *     （测试组④），不需要启动 Android、也不需要网络。
 */

/**
 * 与树莓派的连接状态。协议 §5.2：失败时不要弹崩溃框，显示"连接中…"并保留上次数据。
 */
enum class ConnectionStatus {
    /** 还没请求过。 */
    IDLE,

    /** 请求进行中。 */
    CONNECTING,

    /** 最近一次请求成功。 */
    CONNECTED,

    /** 最近一次请求失败（界面保留上次数据并标注"可能已过期"）。 */
    FAILED,
    ;

    val label: String
        get() = when (this) {
            IDLE -> "未连接"
            CONNECTING -> "连接中…"
            CONNECTED -> "已连接"
            FAILED -> "连接失败"
        }
}

// ---------------------------------------------------------------------------
// 当前读数
// ---------------------------------------------------------------------------

data class MonitorSnapshot(
    /** 快照时间（Unix 秒）。 */
    val ts: Double? = null,
    val heartRateBpm: Double? = null,
    val spo2Percent: Double? = null,
    val fingerDetected: Boolean? = null,
    val vitalsQuality: Double? = null,
    val bodyTempC: Double? = null,
    val ambientTempC: Double? = null,
    val humidityPercent: Double? = null,
    val motionState: String? = null,
    val motionSilentSeconds: Double? = null,
    /** 设备名 → 连续失败次数。 */
    val sensorFailures: Map<String, Int> = emptyMap(),
    /** 距最近一次成功读到新数据的秒数；null = 从未读到过。 */
    val dataAgeSeconds: Double? = null,
    /** 快照是否已不值得相信（树莓派那边判定的，**不是**本机请求失败）。 */
    val dataStale: Boolean = false,
    /** 报警码 → 首次触发时间。 */
    val activeAlarms: Map<String, Double> = emptyMap(),
) {
    val hasActiveAlarms: Boolean get() = activeAlarms.isNotEmpty()

    val hasSensorFailures: Boolean get() = sensorFailures.isNotEmpty()

    /** 最高严重度（用于横幅配色）；无报警时是 [Severity.NORMAL]。 */
    val maxActiveSeverity: Int
        get() = activeAlarms.keys.maxOfOrNull { AlarmCatalog.defaultSeverity(it) } ?: Severity.NORMAL

    companion object {
        /** 空快照：还没成功请求过时用，界面上全是 `--`（**不是 0**）。 */
        val EMPTY: MonitorSnapshot = MonitorSnapshot()

        /** `GET /api/v1/current` 的响应 → 快照。`data` 缺失时返回 [EMPTY]。 */
        fun from(response: CurrentResponse): MonitorSnapshot {
            val data: CurrentDataDto = response.data ?: return EMPTY.copy(activeAlarms = response.active_alarms)
            return MonitorSnapshot(
                ts = data.ts,
                heartRateBpm = data.heart_rate_bpm,
                spo2Percent = data.spo2_percent,
                fingerDetected = data.finger_detected,
                vitalsQuality = data.vitals_quality,
                bodyTempC = data.body_temp_c,
                ambientTempC = data.ambient_temp_c,
                humidityPercent = data.humidity_percent,
                motionState = data.motion_state,
                motionSilentSeconds = data.motion_silent_s,
                sensorFailures = data.sensor_failures,
                // 树莓派没给这个字段（旧版本）时，保守地按"只有真的没数据才算陈旧"处理：
                // 服务端说得明确就听服务端的，没说不许自己瞎猜。
                dataAgeSeconds = data.data_age_s,
                dataStale = data.data_stale ?: false,
                activeAlarms = response.active_alarms,
            )
        }

        /** 由 DTO 直接构造（单元测试里构造假数据用）。 */
        fun of(data: CurrentDataDto, activeAlarms: Map<String, Double> = emptyMap()): MonitorSnapshot =
            from(CurrentResponse(ok = true, data = data, active_alarms = activeAlarms))
    }
}

// ---------------------------------------------------------------------------
// 报警
// ---------------------------------------------------------------------------

/** 报警事件（历史记录或本次实时事件统一成这个模型）。 */
data class AlarmEvent(
    val ts: Double? = null,
    val code: String,
    /** 服务器给的严重度；null 表示没给，界面用 [AlarmCatalog.defaultSeverity]。 */
    val severity: Int? = null,
    /** 服务器给的中文描述（历史记录里有）；实时事件可能没有。 */
    val message: String? = null,
    val value: Double? = null,
    val unit: String = "",
    val source: String? = null,
) {
    /** 实际用于上屏的严重度：服务器优先，其次目录默认值。 */
    val effectiveSeverity: Int get() = AlarmCatalog.severityOf(code, severity)

    /** 中文报警名。 */
    val label: String get() = AlarmCatalog.label(code)

    /** 一句完整描述：优先用服务器 message，否则拼"中文名 + 数值"。 */
    val displayMessage: String
        get() {
            val server = message?.trim()
            if (!server.isNullOrEmpty()) return server
            val valueText = Formatters.valueWithUnit(value, unit)
            return if (valueText == Formatters.UNKNOWN) label else "$label $valueText"
        }
}

/** 本次运行中实际下发过的报警（含消音标记）。 */
data class LiveAlarm(
    val ts: Double? = null,
    val code: String,
    val severity: Int? = null,
    val speak: String? = null,
    val beepTimes: Int? = null,
    val silenced: Boolean = false,
) {
    val effectiveSeverity: Int get() = AlarmCatalog.severityOf(code, severity)
    val label: String get() = AlarmCatalog.label(code)
}

data class AlarmFeed(
    val live: List<LiveAlarm> = emptyList(),
    val history: List<AlarmEvent> = emptyList(),
) {
    companion object {
        val EMPTY: AlarmFeed = AlarmFeed()

        fun from(response: AlarmsResponse): AlarmFeed = AlarmFeed(
            live = response.live.mapNotNull { it.toDomainOrNull() },
            history = response.history.mapNotNull { it.toDomainOrNull() },
        )
    }
}

/** 没有 code 的事件没法显示中文名，直接丢掉（而不是显示一条空白报警）。 */
fun LiveAlarmDto.toDomainOrNull(): LiveAlarm? {
    val code = code?.trim().orEmpty()
    if (code.isEmpty()) return null
    return LiveAlarm(
        ts = ts,
        code = code,
        severity = severity,
        speak = speak,
        beepTimes = beep_times,
        silenced = silenced ?: false,
    )
}

fun AlarmEventDto.toDomainOrNull(): AlarmEvent? {
    val code = code?.trim().orEmpty()
    if (code.isEmpty()) return null
    return AlarmEvent(
        ts = ts,
        code = code,
        severity = severity,
        message = message,
        value = value,
        unit = unit.orEmpty(),
        source = source,
    )
}

/**
 * 历史事件列表的排序：**先按严重度降序，再按时间降序**。
 * 家属打开报警页时最该先看到的是"最严重的、最近的"。
 */
fun List<AlarmEvent>.sortedBySeverityThenTime(): List<AlarmEvent> =
    sortedWith(
        compareByDescending<AlarmEvent> { Severity.sortKey(it.effectiveSeverity) }
            .thenByDescending { it.ts ?: 0.0 },
    )

// ---------------------------------------------------------------------------
// 系统健康
// ---------------------------------------------------------------------------

data class DeviceHealth(
    val name: String,
    val driver: String? = null,
    val reads: Int? = null,
    val failures: Int? = null,
    val stale: Boolean? = null,
    val lastError: String? = null,
    val opened: Boolean? = null,
    /** 该设备是否只是模拟设备（树莓派没接真传感器时）。 */
    val mock: Boolean? = null,
) {
    /** 状态文案：离线/异常/正常。 */
    val statusText: String
        get() = when {
            opened == false -> "未打开"
            stale == true -> "数据陈旧"
            (failures ?: 0) > 0 -> "有失败（$failures 次）"
            else -> "正常"
        }
}

data class SystemHealth(
    val ts: Double? = null,
    val uptimeSeconds: Double? = null,
    val version: String? = null,
    val mock: Boolean? = null,
    val activeAlarms: Map<String, Double> = emptyMap(),
    val dispatcherEnabled: Boolean? = null,
    val dispatcherOutputs: List<String> = emptyList(),
    val dispatcherDispatched: Int? = null,
    val dispatcherSilencedUntil: Double? = null,
    val devices: List<DeviceHealth> = emptyList(),
) {
    companion object {
        val EMPTY: SystemHealth = SystemHealth()

        fun from(response: HealthResponse): SystemHealth = SystemHealth(
            ts = response.ts,
            uptimeSeconds = response.uptime_s,
            version = response.version,
            mock = response.mock,
            activeAlarms = response.active_alarms,
            dispatcherEnabled = response.dispatcher?.enabled,
            dispatcherOutputs = response.dispatcher?.outputs.orEmpty(),
            dispatcherDispatched = response.dispatcher?.dispatched,
            dispatcherSilencedUntil = response.dispatcher?.silenced_until,
            devices = response.devices.map { (name, dto) -> dto.toDomain(name) },
        )
    }
}

fun DeviceHealthDto.toDomain(name: String): DeviceHealth = DeviceHealth(
    name = device_status?.name ?: name,
    driver = driver,
    reads = reads,
    failures = failures,
    stale = stale,
    lastError = last_error,
    opened = device_status?.opened,
    mock = device_status?.mock,
)

// ---------------------------------------------------------------------------
// 设备清单 / 接线说明
// ---------------------------------------------------------------------------

data class DeviceInfo(
    val name: String,
    val driver: String? = null,
    val kind: String? = null,
    val bus: String? = null,
    val pins: Map<String, String> = emptyMap(),
    val notes: String? = null,
    val mock: Boolean? = null,
) {
    companion object {
        fun from(response: DevicesResponse): List<DeviceInfo> =
            response.devices.map { (key, dto) -> dto.toDomain(key) }
    }
}

fun DeviceInfoDto.toDomain(key: String): DeviceInfo {
    val describe: DeviceDescribeDto? = describe
    return DeviceInfo(
        name = describe?.name ?: key,
        driver = driver,
        kind = describe?.kind,
        bus = describe?.bus,
        pins = describe?.pins.orEmpty(),
        notes = describe?.notes,
        mock = describe?.mock,
    )
}
