package com.dboycht.healthmonitor.domain

/**
 * 严重度：`0=正常`、`1=提示`、`2=警告`、`3=紧急`（协议 §4.4，可直接比大小）。
 *
 * 界面用的颜色也定义在这里，保证"同一严重度在任何页面颜色一致"。
 * 颜色值是 ARGB 的 `Long`，`ui/component/SeverityBadge.kt` 里的
 * `Color(severityColorArgb(x))` 负责转成 Compose 的 `Color`；
 * 这样严重度→颜色的映射可以脱离 Android 框架做单元测试。
 */
object Severity {

    const val NORMAL = 0
    const val INFO = 1
    const val WARNING = 2
    const val CRITICAL = 3

    /** 中文名，用于界面角标与无障碍朗读。 */
    fun label(severity: Int): String = when (severity) {
        NORMAL -> "正常"
        INFO -> "提示"
        WARNING -> "警告"
        CRITICAL -> "紧急"
        else -> "未知"
    }

    /** 把任意数字（含服务器给越界值时）夹到 0..3。 */
    fun clamp(severity: Int): Int = when {
        severity < NORMAL -> NORMAL
        severity > CRITICAL -> CRITICAL
        else -> severity
    }

    // 颜色（ARGB）：
    private const val ARGB_NORMAL = 0xFF2E7D32L      // 绿
    private const val ARGB_INFO = 0xFF0288D1L        // 蓝
    private const val ARGB_WARNING = 0xFFEF6C00L     // 橙
    private const val ARGB_CRITICAL = 0xFFC62828L    // 红
    private const val ARGB_UNKNOWN = 0xFF757575L     // 灰

    fun colorArgb(severity: Int): Long = when (severity) {
        NORMAL -> ARGB_NORMAL
        INFO -> ARGB_INFO
        WARNING -> ARGB_WARNING
        CRITICAL -> ARGB_CRITICAL
        else -> ARGB_UNKNOWN
    }

    /** 底色（浅色版），用于横幅背景。 */
    fun containerColorArgb(severity: Int): Long = when (severity) {
        NORMAL -> 0xFFE8F5E9L
        INFO -> 0xFFE1F5FEL
        WARNING -> 0xFFFFF3E0L
        CRITICAL -> 0xFFFFEBEEL
        else -> 0xFFF5F5F5L
    }

    /** 紧急度是否高到需要"横幅高亮 + 闪烁提示"。 */
    fun requiresAttention(severity: Int): Boolean = clamp(severity) >= WARNING

    /** 排序键：数字越大越紧急（列表排序时降序用）。 */
    fun sortKey(severity: Int): Int = clamp(severity)
}
