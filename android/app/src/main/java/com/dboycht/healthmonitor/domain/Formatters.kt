package com.dboycht.healthmonitor.domain

import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import kotlin.math.abs
import kotlin.math.roundToLong

/**
 * 数值 / 时间的**显示层格式化**，全部是纯函数，方便单元测试。
 *
 * 这里是本项目最重要的一条硬要求的落点（协议 §2 与 §4.1）：
 *
 * > 数值缺失一律是 JSON `null`，绝不是 0；手机端必须显示"未知/--"，**不得显示 0**。
 *
 * 所以**任何**数值上屏前都必须经过本文件的函数，界面代码里不允许出现
 * `"${it.heart_rate_bpm}"` 这种写法——那样 null 会印成 "null"，
 * 更糟的是有人会写成 `?: 0.0` 从而显示"0 bpm"，把用户吓一跳。
 */
object Formatters {

    /** 缺失值统一占位符。 */
    const val UNKNOWN: String = "--"

    /** "请将手指放好"：`finger_detected == false` 时心率/血氧区域的专用文案。 */
    const val PLACE_FINGER: String = "请将手指放好"

    /** 单位后缀：值缺失时**连单位一起省掉**，只留 `--`（"-- bpm" 很怪）。 */
    fun valueWithUnit(value: Double?, unit: String, decimals: Int = 1): String {
        val text = formatNumber(value, decimals)
        if (text == UNKNOWN || unit.isEmpty()) return text
        return "$text$unit"
    }

    /** 数值 → 字符串；`null` → [UNKNOWN]。整数默认不带小数点。 */
    fun formatNumber(value: Double?, decimals: Int = 1): String {
        if (value == null) return UNKNOWN
        if (value.isNaN() || value.isInfinite()) return UNKNOWN
        return if (decimals <= 0) {
            value.roundToLong().toString()
        } else {
            val factor = pow10(decimals)
            val scaled = (value * factor).roundToLong()
            val sign = if (scaled < 0) "-" else ""
            val abs = abs(scaled)
            val intPart = abs / factor
            val fracPart = abs % factor
            val frac = fracPart.toString().padStart(decimals, '0')
            "$sign$intPart.$frac"
        }
    }

    /** 整数（失败次数、条数等）；`null` → [UNKNOWN]。 */
    fun formatInt(value: Int?): String = value?.toString() ?: UNKNOWN

    // --- 各指标专用入口（单位/精度集中在这里，界面不要自己拼） ---

    fun heartRate(bpm: Double?): String = valueWithUnit(bpm, " bpm", decimals = 0)

    fun spo2(percent: Double?): String = valueWithUnit(percent, " %", decimals = 1)

    fun bodyTemp(celsius: Double?): String = valueWithUnit(celsius, " ℃", decimals = 1)

    fun ambientTemp(celsius: Double?): String = valueWithUnit(celsius, " ℃", decimals = 1)

    fun humidity(percent: Double?): String = valueWithUnit(percent, " %", decimals = 1)

    /**
     * 心率卡片的主文案：手指没放好时**优先说"请将手指放好"**，
     * 而不是显示 --（协议 §4.1 渲染规则第二条）。
     */
    fun heartRateDisplay(bpm: Double?, fingerDetected: Boolean?): String =
        if (fingerDetected == false) PLACE_FINGER else heartRate(bpm)

    fun spo2Display(percent: Double?, fingerDetected: Boolean?): String =
        if (fingerDetected == false) PLACE_FINGER else spo2(percent)

    fun fingerDetectedLabel(fingerDetected: Boolean?): String = when (fingerDetected) {
        true -> "已检测到手指"
        false -> PLACE_FINGER
        null -> "手指检测不可用"
    }

    /** 数据质量 0~1 → 百分比。 */
    fun quality(quality: Double?): String =
        if (quality == null) UNKNOWN else "${formatNumber(quality * 100, 0)} %"

    // --- 活动状态 ---

    fun motionStateLabel(state: String?): String = when (state?.trim()?.lowercase(Locale.ROOT)) {
        "detected" -> "有人在活动"
        "idle" -> "未检测到活动"
        // 协议 §4.1：unknown 必须显示"未知"，**不要**推断成"无人"。
        "unknown" -> "未知"
        null, "" -> "未知"
        else -> "未知"
    }

    /** 距上次检测到人的秒数 → "12 秒前" / "3 分 5 秒前"；null = 无法判断。 */
    fun motionSilent(silentSeconds: Double?): String {
        if (silentSeconds == null || silentSeconds.isNaN() || silentSeconds < 0) return UNKNOWN
        val total = silentSeconds.roundToLong()
        if (total < 60) return "$total 秒前"
        val minutes = total / 60
        val seconds = total % 60
        if (minutes < 60) return "$minutes 分 $seconds 秒前"
        val hours = minutes / 60
        val restMinutes = minutes % 60
        return "$hours 小时 $restMinutes 分前"
    }

    /** 时长（秒）→ "10 分 12 秒"；服务器从启动就开始计时，界面上是"在线时长"。 */
    fun duration(seconds: Double?): String {
        if (seconds == null || seconds.isNaN() || seconds < 0) return UNKNOWN
        val total = seconds.roundToLong()
        val hours = total / 3600
        val minutes = (total % 3600) / 60
        val secs = total % 60
        return when {
            hours > 0 -> "$hours 小时 $minutes 分"
            minutes > 0 -> "$minutes 分 $secs 秒"
            else -> "$secs 秒"
        }
    }

    // --- 时间戳（Unix 秒，UTC；协议 §2 说明由手机端转本地时间）---

    private val TIME_FORMAT = object : ThreadLocal<SimpleDateFormat>() {
        override fun initialValue(): SimpleDateFormat =
            SimpleDateFormat("MM-dd HH:mm:ss", Locale.getDefault())
    }

    fun timestamp(unixSeconds: Double?): String {
        if (unixSeconds == null || unixSeconds <= 0) return UNKNOWN
        return TIME_FORMAT.get()!!.format(Date((unixSeconds * 1000).toLong()))
    }

    /**
     * 数据新鲜度："刚刚 / 12 秒前 / 3 分钟前"。
     * 轮询失败时界面要显示"数据可能已过期"，就靠它算出来的秒数（协议 §5.2）。
     */
    fun ageLabel(ageSeconds: Double?): String {
        if (ageSeconds == null || ageSeconds.isNaN() || ageSeconds < 0) return UNKNOWN
        val total = ageSeconds.roundToLong()
        return when {
            total < 3 -> "刚刚"
            total < 60 -> "$total 秒前"
            total < 3600 -> "${total / 60} 分钟前"
            total < 86400 -> "${total / 3600} 小时前"
            else -> "${total / 86400} 天前"
        }
    }

    // --- 传感器异常（协议 §4.1 第三条：sensor_failures 非空要以警告色列出） ---

    /**
     * `sensor_failures` → 警告文案列表，例如 `"vitals 传感器异常（连续 3 次）"`。
     * 空 map 返回空列表（界面据此决定不显示任何警告）。
     */
    fun sensorFailureMessages(failures: Map<String, Int>?): List<String> {
        if (failures.isNullOrEmpty()) return emptyList()
        return failures.entries
            .sortedBy { it.key }
            .map { (device, count) -> "$device 传感器异常（连续 $count 次）" }
    }

    /** `active_alarms` 非空 → 横幅标题里列出报警中文名（最多 [max] 条，其余折叠）。 */
    fun activeAlarmSummary(activeAlarms: Map<String, Double>?, max: Int = 2): String? {
        if (activeAlarms.isNullOrEmpty()) return null
        val codes = activeAlarms.keys.sortedBy { AlarmCatalog.severityOf(it, null) }.reversed()
        val shown = codes.take(max).joinToString("、") { AlarmCatalog.label(it) }
        return if (codes.size > max) "$shown 等 ${codes.size} 项报警" else shown
    }

    private fun pow10(decimals: Int): Long {
        var result = 1L
        repeat(decimals) { result *= 10 }
        return result
    }
}
