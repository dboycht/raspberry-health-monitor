package com.dboycht.healthmonitor.domain

/**
 * 报警码目录：codes → 中文名 / 默认严重度。
 *
 * 这张表**逐行照抄** `docs/05-安卓通信协议.md` §4.4 的对照表，两边必须同步。
 * 界面显示报警时一律走这里，不要在别的文件里再写一遍字符串。
 */
object AlarmCatalog {

    /** 找不到的报警码统一显示这个，绝不吞掉（方便发现"树莓派加了新码、手机端没跟上"）。 */
    const val UNKNOWN_LABEL: String = "未知报警"

    /** code → 中文名 */
    private val LABELS: Map<String, String> = linkedMapOf(
        "hr_too_high" to "心率过高",
        "hr_too_low" to "心率过低",
        "spo2_too_low" to "血氧过低",
        "body_temp_high" to "体温偏高",
        "body_temp_low" to "体温偏低",
        "ambient_temp_high" to "室温偏高",
        "ambient_temp_low" to "室温偏低",
        "humidity_high" to "湿度过高",
        "no_motion_too_long" to "长时间无活动（疑似跌倒）",
        "night_frequent_wake" to "夜间频繁起夜",
        "sos_pressed" to "紧急求助",
        "sensor_fault" to "传感器故障",
        "device_offline" to "设备离线",
        "system_start" to "系统已启动",
        "all_clear" to "已恢复正常",
    )

    /** code → 默认严重度（0=正常 1=提示 2=警告 3=紧急） */
    private val DEFAULT_SEVERITIES: Map<String, Int> = linkedMapOf(
        "hr_too_high" to Severity.WARNING,
        "hr_too_low" to Severity.WARNING,
        "spo2_too_low" to Severity.CRITICAL,
        "body_temp_high" to Severity.WARNING,
        "body_temp_low" to Severity.WARNING,
        "ambient_temp_high" to Severity.INFO,
        "ambient_temp_low" to Severity.INFO,
        "humidity_high" to Severity.INFO,
        "no_motion_too_long" to Severity.CRITICAL,
        "night_frequent_wake" to Severity.WARNING,
        "sos_pressed" to Severity.CRITICAL,
        "sensor_fault" to Severity.WARNING,
        "device_offline" to Severity.WARNING,
        "system_start" to Severity.INFO,
        "all_clear" to Severity.NORMAL,
    )

    /** 报警码 → 中文名；未知码返回 [UNKNOWN_LABEL] + 原码，便于排查。 */
    fun label(code: String?): String {
        val key = code?.trim().orEmpty()
        if (key.isEmpty()) return "未知报警"
        return LABELS[key] ?: "$UNKNOWN_LABEL（$key）"
    }

    /** 报警码 → 默认严重度；未知码按"警告"处理（宁可高估，不可漏报）。 */
    fun defaultSeverity(code: String?): Int {
        val key = code?.trim().orEmpty()
        return DEFAULT_SEVERITIES[key] ?: Severity.WARNING
    }

    /**
     * 事件实际严重度：服务器给了 `severity` 就用服务器的（可能被用户配置改过），
     * 没给（null）才退回目录里的默认值。**不能用 0 当默认值**——0 是"已恢复正常"。
     */
    fun severityOf(code: String?, severity: Int?): Int =
        severity ?: defaultSeverity(code)

    /** 目录里登记过的全部报警码（按协议表顺序）。 */
    fun allCodes(): List<String> = LABELS.keys.toList()
}
