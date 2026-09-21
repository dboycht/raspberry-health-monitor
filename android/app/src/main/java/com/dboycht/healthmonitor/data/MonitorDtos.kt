package com.dboycht.healthmonitor.data

import kotlinx.serialization.Serializable

/**
 * 树莓派接口的线上数据模型（DTO）。
 *
 * 设计要点（对应协议 §2「数值缺失一律是 JSON null，绝不是 0」）：
 *  - **所有可能缺失的字段都声明为可空并给出默认值 null**：JSON 里字段缺失、
 *    显式 null、或服务端将来删字段，都会被解析成 null，而不会解析失败，
 *    更不会被当成 0；
 *  - 字段名保持与协议一致（下划线命名），所以不需要 `@SerialName` 逐个改，
 *    一眼就能和 `docs/05-安卓通信协议.md` 对照；
 *  - 未知字段（协议 §6 说明新增字段是兼容的）由 ignoreUnknownKeys 忽略。
 *
 * 这些 DTO 只负责"解析"，界面显示的文案/格式化统一走 domain 层。
 */

/** 请求是否成功的统一包装：`{"ok": true, ...}`；失败时是 `{"ok": false, "error": "..."}`。 */
@Serializable
data class ApiEnvelope(
    val ok: Boolean = false,
    val error: String? = null,
)

/** `POST /api/v1/silence` 的响应。 */
@Serializable
data class SilenceResponse(
    val ok: Boolean = false,
    val silenced_until: Double? = null,
    val error: String? = null,
)

/** `POST /api/v1/sos` 的响应（`event` 是被触发的报警事件）。 */
@Serializable
data class SosResponse(
    val ok: Boolean = false,
    val event: AlarmEventDto? = null,
    val error: String? = null,
)

// ---------------------------------------------------------------------------
// GET /api/v1/current
// ---------------------------------------------------------------------------

@Serializable
data class CurrentResponse(
    val ok: Boolean = false,
    val data: CurrentDataDto? = null,
    val active_alarms: Map<String, Double> = emptyMap(),
    val error: String? = null,
)

/** 当前读数。任何一项都可能是 null（传感器没数据/算法未收敛）。 */
@Serializable
data class CurrentDataDto(
    val ts: Double? = null,
    val heart_rate_bpm: Double? = null,
    val spo2_percent: Double? = null,
    val finger_detected: Boolean? = null,
    val vitals_quality: Double? = null,
    val body_temp_c: Double? = null,
    val ambient_temp_c: Double? = null,
    val humidity_percent: Double? = null,
    val motion_state: String? = null,
    val motion_silent_s: Double? = null,
    /** `{设备名: 连续失败次数}`，空对象 = 全部正常。 */
    val sensor_failures: Map<String, Int> = emptyMap(),
    /**
     * 距"最近一次成功读到新数据"的秒数；null = 从未读到过（协议 §4.1）。
     *
     * 与"请求失败"是**两件事**：请求可能成功（服务活着），但树莓派那边的传感器早就没数据了。
     */
    val data_age_s: Double? = null,
    /** 快照是否已经不值得相信（超过阈值或从未读到数据）。 */
    val data_stale: Boolean? = null,
)

// ---------------------------------------------------------------------------
// GET /api/v1/alarms
// ---------------------------------------------------------------------------

@Serializable
data class AlarmsResponse(
    val ok: Boolean = false,
    /** 本次运行中实际下发过的报警（含 silenced 标记）。 */
    val live: List<LiveAlarmDto> = emptyList(),
    /** 落库的历史报警记录。 */
    val history: List<AlarmEventDto> = emptyList(),
    val error: String? = null,
)

@Serializable
data class LiveAlarmDto(
    val ts: Double? = null,
    val code: String? = null,
    val severity: Int? = null,
    val light: String? = null,
    val blink: Boolean? = null,
    val speak: String? = null,
    val beep_times: Int? = null,
    val silenced: Boolean? = null,
)

@Serializable
data class AlarmEventDto(
    val ts: Double? = null,
    val code: String? = null,
    val severity: Int? = null,
    val message: String? = null,
    val value: Double? = null,
    val unit: String? = null,
    val source: String? = null,
    val detail: Map<String, String> = emptyMap(),
    val pushed: Int? = null,
)

// ---------------------------------------------------------------------------
// GET /api/v1/health
// ---------------------------------------------------------------------------

@Serializable
data class HealthResponse(
    val ok: Boolean = false,
    val ts: Double? = null,
    val uptime_s: Double? = null,
    val version: String? = null,
    val mock: Boolean? = null,
    val active_alarms: Map<String, Double> = emptyMap(),
    val dispatcher: DispatcherDto? = null,
    val devices: Map<String, DeviceHealthDto> = emptyMap(),
    val error: String? = null,
)

@Serializable
data class DispatcherDto(
    val enabled: Boolean? = null,
    val outputs: List<String> = emptyList(),
    val dispatched: Int? = null,
    val errors: List<String> = emptyList(),
    val silenced_until: Double? = null,
)

@Serializable
data class DeviceHealthDto(
    val driver: String? = null,
    val interval_s: Double? = null,
    val reads: Int? = null,
    val failures: Int? = null,
    val last_error: String? = null,
    val stale: Boolean? = null,
    val last_ok_age_s: Double? = null,
    val optional: Boolean? = null,
    val device_status: DeviceStatusDto? = null,
)

@Serializable
data class DeviceStatusDto(
    val name: String? = null,
    val kind: String? = null,
    val opened: Boolean? = null,
    val mock: Boolean? = null,
    val read_count: Int? = null,
    val fault_count: Int? = null,
    val last_error: String? = null,
)

// ---------------------------------------------------------------------------
// GET /api/v1/devices
// ---------------------------------------------------------------------------

@Serializable
data class DevicesResponse(
    val ok: Boolean = false,
    val devices: Map<String, DeviceInfoDto> = emptyMap(),
    val error: String? = null,
)

@Serializable
data class DeviceInfoDto(
    val driver: String? = null,
    val interval_s: Double? = null,
    val describe: DeviceDescribeDto? = null,
)

@Serializable
data class DeviceDescribeDto(
    val name: String? = null,
    val kind: String? = null,
    val mock: Boolean? = null,
    val bus: String? = null,
    /** 接线：`{"sda": "GPIO2 / 物理脚 3", ...}`。 */
    val pins: Map<String, String> = emptyMap(),
    val notes: String? = null,
)

// ---------------------------------------------------------------------------
// GET /api/v1/history
// ---------------------------------------------------------------------------

@Serializable
data class HistoryResponse(
    val ok: Boolean = false,
    val metric: String? = null,
    val count: Int? = null,
    val data: List<HistoryPointDto> = emptyList(),
    val error: String? = null,
)

@Serializable
data class HistoryPointDto(
    val ts: Double? = null,
    val value: Double? = null,
    val ok: Int? = null,
    val device: String? = null,
    val heart_rate_bpm: Double? = null,
    val spo2_percent: Double? = null,
    val finger_detected: Int? = null,
    val quality: Double? = null,
)
