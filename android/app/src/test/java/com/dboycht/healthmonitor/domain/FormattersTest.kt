package com.dboycht.healthmonitor.domain

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 测试组②：**null 值格式化**。
 *
 * 这是协议里最硬的一条要求（§2 与 §4.1）：
 *   > 数值缺失一律是 JSON `null`，绝不是 0；手机端必须显示"未知/--"，**不得显示 0**。
 *
 * 所以这里逐个入口断言"null 进来 → `--` 出去，且绝不出现字符 '0'"。
 */
class FormattersTest {

    private val allFormatters: List<Pair<String, (Double?) -> String>> = listOf(
        "formatNumber" to { v -> Formatters.formatNumber(v, 1) },
        "heartRate" to { v -> Formatters.heartRate(v) },
        "spo2" to { v -> Formatters.spo2(v) },
        "bodyTemp" to { v -> Formatters.bodyTemp(v) },
        "ambientTemp" to { v -> Formatters.ambientTemp(v) },
        "humidity" to { v -> Formatters.humidity(v) },
        "quality" to { v -> Formatters.quality(v) },
        "motionSilent" to { v -> Formatters.motionSilent(v) },
        "duration" to { v -> Formatters.duration(v) },
        "ageLabel" to { v -> Formatters.ageLabel(v) },
    )

    @Test
    fun `所有数值格式化函数遇到 null 都返回 -- 而不是 0`() {
        allFormatters.forEach { (name, format) ->
            val text = format(null)
            assertEquals("$name(null) 必须是 --", Formatters.UNKNOWN, text)
            assertFalse("$name(null) 不能出现 0：$text", text.contains("0"))
        }
    }

    @Test
    fun `NaN 与无穷大也当作缺失处理`() {
        assertEquals(Formatters.UNKNOWN, Formatters.formatNumber(Double.NaN, 1))
        assertEquals(Formatters.UNKNOWN, Formatters.formatNumber(Double.POSITIVE_INFINITY, 1))
        // 传感器坏掉时可能算出负数秒，也当缺失，不显示 "-3 秒前"。
        assertEquals(Formatters.UNKNOWN, Formatters.motionSilent(-1.0))
        assertEquals(Formatters.UNKNOWN, Formatters.duration(-5.0))
    }

    @Test
    fun `缺失值时不显示单位_避免出现 -- bpm`() {
        assertEquals("--", Formatters.heartRate(null))
        assertEquals("--", Formatters.valueWithUnit(null, " bpm"))
        assertEquals("--", Formatters.valueWithUnit(null, ""))
    }

    @Test
    fun `有值时单位与精度正确`() {
        assertEquals("72 bpm", Formatters.heartRate(72.4))
        assertEquals("97.9 %", Formatters.spo2(97.9))
        assertEquals("36.5 ℃", Formatters.bodyTemp(36.5))
        assertEquals("24.5 ℃", Formatters.ambientTemp(24.5))
        assertEquals("55.2 %", Formatters.humidity(55.2))
        assertEquals("95 %", Formatters.quality(0.95))
        assertEquals(Formatters.UNKNOWN, Formatters.quality(null))
        // 整数值不带小数点（协议样本里的 motion_silent_s = 0.0 应显示成 0 秒前）。
        assertEquals("0 秒前", Formatters.motionSilent(0.0))
        assertEquals("12 秒前", Formatters.motionSilent(12.0))
        assertEquals("3 分 5 秒前", Formatters.motionSilent(185.0))
        assertEquals("2 小时 1 分前", Formatters.motionSilent(7260.0))
    }

    @Test
    fun `finger_detected 为 false 时心率与血氧显示请将手指放好`() {
        // 这是协议 §4.1 渲染规则的第二条，即使服务器同时给了数值也以"未贴合"为准。
        assertEquals(Formatters.PLACE_FINGER, Formatters.heartRateDisplay(null, false))
        assertEquals(Formatters.PLACE_FINGER, Formatters.spo2Display(null, false))
        assertEquals(Formatters.PLACE_FINGER, Formatters.heartRateDisplay(80.0, false))
        assertEquals("请将手指放好", Formatters.fingerDetectedLabel(false))
        // 没贴合时不显示任何数字（更不能显示 0）。
        assertFalse(Formatters.heartRateDisplay(0.0, false).contains("0"))
    }

    @Test
    fun `finger_detected 为 true 或 null 时按数值显示`() {
        assertEquals("72 bpm", Formatters.heartRateDisplay(72.4, true))
        assertEquals("97.9 %", Formatters.spo2Display(97.9, true))
        // null 表示"该数据源不可用"，一样显示 --，不是 0。
        assertEquals("--", Formatters.heartRateDisplay(null, null))
        assertEquals("手指检测不可用", Formatters.fingerDetectedLabel(null))
        assertEquals("已检测到手指", Formatters.fingerDetectedLabel(true))
    }

    @Test
    fun `motion_state 为 unknown 时显示未知_不推断成无人`() {
        assertEquals("有人在活动", Formatters.motionStateLabel("detected"))
        assertEquals("未检测到活动", Formatters.motionStateLabel("idle"))
        assertEquals("未知", Formatters.motionStateLabel("unknown"))
        assertEquals("未知", Formatters.motionStateLabel(null))
        assertEquals("未知", Formatters.motionStateLabel(""))
        // 服务器将来加了别的取值，也只能是"未知"，不能瞎猜。
        assertEquals("未知", Formatters.motionStateLabel("sleeping"))
        // 不能把 unknown 说成"无人"（协议 §4.1 明令）。
        assertFalse(Formatters.motionStateLabel("unknown").contains("无人"))
    }

    @Test
    fun `sensor_failures 非空才生成警告文案`() {
        // 空对象 = 全部正常 → 不显示任何警告。
        assertTrue(Formatters.sensorFailureMessages(emptyMap()).isEmpty())
        assertTrue(Formatters.sensorFailureMessages(null).isEmpty())

        val messages = Formatters.sensorFailureMessages(mapOf("vitals" to 3))
        assertEquals(1, messages.size)
        assertEquals("vitals 传感器异常（连续 3 次）", messages[0])

        // 多个设备按名字排序，界面上的顺序稳定（否则每次刷新都在跳）。
        val many = Formatters.sensorFailureMessages(mapOf("vitals" to 1, "ambient" to 3))
        assertEquals(
            listOf("ambient 传感器异常（连续 3 次）", "vitals 传感器异常（连续 1 次）"),
            many,
        )
    }

    @Test
    fun `active_alarms 汇总文案按严重度倒序_空对象返回 null`() {
        assertNull(Formatters.activeAlarmSummary(emptyMap()))
        assertNull(Formatters.activeAlarmSummary(null))

        val summary = Formatters.activeAlarmSummary(
            mapOf("humidity_high" to 1.0, "sos_pressed" to 2.0, "hr_too_high" to 3.0),
        )
        assertNotNull(summary)
        // 最紧急的排最前面。
        assertTrue("最紧急的要排前面：$summary", summary!!.startsWith("紧急求助"))
        assertTrue("条目多时要折叠计数：$summary", summary.contains("等 3 项报警"))
    }

    @Test
    fun `数值格式化不依赖系统区域设置`() {
        // 用纯字符串拼接（不用 DecimalFormat），小数点永远是 '.'，不会变成逗号。
        assertEquals("36.5", Formatters.formatNumber(36.5, 1))
        assertEquals("1000.0", Formatters.formatNumber(1000.0, 1))
        assertEquals("0.0", Formatters.formatNumber(0.0, 1))
        assertEquals("-2.5", Formatters.formatNumber(-2.5, 1))
        assertEquals("73", Formatters.formatNumber(72.6, 0))
    }
}
