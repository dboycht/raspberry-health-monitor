package com.dboycht.healthmonitor.ui.dashboard

import com.dboycht.healthmonitor.domain.Formatters
import com.dboycht.healthmonitor.domain.MonitorSnapshot
import com.dboycht.healthmonitor.domain.Severity

/**
 * 首页五张卡片的**内容模型**。
 *
 * 为什么把"卡片显示什么字"抽成纯数据而不是直接写在 Compose 里：
 * 协议 §4.1 的渲染硬要求（null → `--` 而不是 0、`finger_detected=false` → "请将手指放好"、
 * `sensor_failures` 非空要警告）必须在 **JVM 单测**里能验证；Compose 的 UI 测试需要
 * 真机/模拟器，课设环境下不可靠。抽出 [DashboardCards.build] 之后，这四条规则就有单测守着，
 * 将来谁把 `?: 0.0` 写回去都会立刻挂测试。
 */
data class DashboardCard(
    /** 标题，例如"心率"。 */
    val title: String,
    /** 主数值文案，一律由 [Formatters] 产出（**绝不允许出现 "0"**）。 */
    val value: String,
    /** 单位；值缺失时应为 null（"-- bpm" 很怪）。 */
    val unit: String? = null,
    /** 副标题（正常范围、检测状态等）。 */
    val subtitle: String? = null,
    /** 右上的状态角标文案。 */
    val statusText: String? = null,
    val statusSeverity: Int = Severity.NORMAL,
    /** 是否用警告底色高亮这张卡。 */
    val alert: Boolean = false,
    /** 卡片底部的黄色提示条（传感器异常等）。 */
    val hint: String? = null,
)

object DashboardCards {

    // 正常参考范围（写在这里是为了"超出范围就标红"，不至于把医学阈值散落在界面代码里）。
    private const val HR_LOW = 50.0
    private const val HR_HIGH = 110.0
    private const val SPO2_LOW = 92.0
    private const val BODY_TEMP_LOW = 35.5
    private const val BODY_TEMP_HIGH = 37.5
    private const val HUMIDITY_LOW = 30.0
    private const val HUMIDITY_HIGH = 70.0

    /**
     * 按协议 §4.1 的渲染规则生成五张卡片：
     * 心率 / 血氧 / 体温 / 室温湿度 / 活动状态。
     */
    fun build(snapshot: MonitorSnapshot): List<DashboardCard> = listOf(
        heartRateCard(snapshot),
        spo2Card(snapshot),
        bodyTempCard(snapshot),
        roomCard(snapshot),
        motionCard(snapshot),
    )

    private fun heartRateCard(s: MonitorSnapshot): DashboardCard {
        // 协议 §4.1：finger_detected == false → 心率区显示"请将手指放好"，不要显示 0。
        val placeFinger = s.fingerDetected == false
        val value = Formatters.heartRateDisplay(s.heartRateBpm, s.fingerDetected)
        val outOfRange = s.heartRateBpm != null && (s.heartRateBpm < HR_LOW || s.heartRateBpm > HR_HIGH)
        return DashboardCard(
            title = "心率",
            // 值为缺失/未检测时，主文案自己带单位或不带，故这里 unit 只在有数值时给。
            value = if (placeFinger || s.heartRateBpm == null) value else Formatters.formatNumber(s.heartRateBpm, 0),
            unit = if (!placeFinger && s.heartRateBpm != null) "bpm" else null,
            subtitle = when {
                placeFinger -> "请将手指放在 MAX30102 上"
                s.heartRateBpm == null -> "暂无读数（算法未收敛或数据源不可用）"
                else -> "正常范围 $HR_LOW~$HR_HIGH bpm"
            },
            statusText = when {
                placeFinger -> "未贴合"
                s.heartRateBpm == null -> "未知"
                outOfRange -> "异常"
                else -> "正常"
            },
            statusSeverity = when {
                placeFinger || s.heartRateBpm == null -> Severity.INFO
                outOfRange -> Severity.WARNING
                else -> Severity.NORMAL
            },
            alert = outOfRange,
        )
    }

    private fun spo2Card(s: MonitorSnapshot): DashboardCard {
        val placeFinger = s.fingerDetected == false
        val value = Formatters.spo2Display(s.spo2Percent, s.fingerDetected)
        val low = s.spo2Percent != null && s.spo2Percent < SPO2_LOW
        val quality = Formatters.quality(s.vitalsQuality)
        return DashboardCard(
            title = "血氧",
            value = if (placeFinger || s.spo2Percent == null) value else Formatters.formatNumber(s.spo2Percent, 1),
            unit = if (!placeFinger && s.spo2Percent != null) "%" else null,
            subtitle = if (placeFinger) Formatters.PLACE_FINGER else "数据质量 $quality",
            statusText = when {
                placeFinger -> "未贴合"
                s.spo2Percent == null -> "未知"
                low -> "偏低"
                else -> "正常"
            },
            statusSeverity = when {
                placeFinger || s.spo2Percent == null -> Severity.INFO
                low -> Severity.CRITICAL
                else -> Severity.NORMAL
            },
            alert = low,
        )
    }

    private fun bodyTempCard(s: MonitorSnapshot): DashboardCard {
        val temp = s.bodyTempC
        val outOfRange = temp != null && (temp < BODY_TEMP_LOW || temp > BODY_TEMP_HIGH)
        return DashboardCard(
            title = "体温",
            value = if (temp == null) Formatters.UNKNOWN else Formatters.formatNumber(temp, 1),
            unit = if (temp == null) null else "℃",
            subtitle = "正常范围 $BODY_TEMP_LOW~$BODY_TEMP_HIGH ℃",
            statusText = when {
                temp == null -> "未知"
                outOfRange -> "异常"
                else -> "正常"
            },
            statusSeverity = when {
                temp == null -> Severity.INFO
                outOfRange -> Severity.WARNING
                else -> Severity.NORMAL
            },
            alert = outOfRange,
        )
    }

    /** 室温 + 湿度合在一张卡里（协议 §4.1 的 ambient_temp_c / humidity_percent 都是 DHT11）。 */
    private fun roomCard(s: MonitorSnapshot): DashboardCard {
        val temp = s.ambientTempC
        val humidity = s.humidityPercent
        val humidityOut = humidity != null && (humidity < HUMIDITY_LOW || humidity > HUMIDITY_HIGH)
        val valueText = "${Formatters.ambientTemp(temp)} / ${Formatters.humidity(humidity)}"
        return DashboardCard(
            title = "室温 / 湿度",
            value = valueText,
            unit = null,
            subtitle = "DHT11 采集",
            statusText = when {
                temp == null && humidity == null -> "未知"
                humidityOut -> "偏湿/偏干"
                else -> "正常"
            },
            statusSeverity = when {
                temp == null && humidity == null -> Severity.INFO
                humidityOut -> Severity.INFO
                else -> Severity.NORMAL
            },
            alert = humidityOut,
        )
    }

    private fun motionCard(s: MonitorSnapshot): DashboardCard {
        val state = s.motionState
        // 协议 §4.1：motion_state == "unknown" 必须显示"未知"，**不要**推断成"无人"。
        val unknown = state == null || state.trim().lowercase() == "unknown" || state.isBlank()
        val detected = state?.trim()?.lowercase() == "detected"
        val silent = s.motionSilentSeconds
        val subtitle = when {
            unknown -> "无法判断（传感器无数据）"
            silent != null -> "上次检测到人：${Formatters.motionSilent(silent)}"
            else -> "距上次检测到人的时间未知"
        }
        return DashboardCard(
            title = "活动状态",
            value = Formatters.motionStateLabel(state),
            unit = null,
            subtitle = subtitle,
            statusText = when {
                unknown -> "未知"
                detected -> "有活动"
                else -> "无活动"
            },
            statusSeverity = when {
                unknown -> Severity.INFO
                detected -> Severity.NORMAL
                // "长时间无活动"由树莓派报警规则判定，这里只做提示级标色。
                (silent ?: 0.0) > 600 -> Severity.WARNING
                else -> Severity.NORMAL
            },
            alert = !unknown && !detected && (silent ?: 0.0) > 600,
        )
    }

    /** `sensor_failures` 非空的警告文案（协议 §4.1 第三条）。空 map → 空列表。 */
    fun sensorWarnings(snapshot: MonitorSnapshot): List<String> =
        Formatters.sensorFailureMessages(snapshot.sensorFailures)

    /** 一整句警告，例如"vitals 传感器异常（连续 3 次）"。 */
    fun sensorWarningText(snapshot: MonitorSnapshot): String? =
        sensorWarnings(snapshot).takeIf { it.isNotEmpty() }?.joinToString("；")
}
