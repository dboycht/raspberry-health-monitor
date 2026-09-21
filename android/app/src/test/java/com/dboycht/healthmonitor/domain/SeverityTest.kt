package com.dboycht.healthmonitor.domain

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 测试组③：**严重度排序与颜色映射**。
 *
 * 目标：0=正常、1=提示、2=警告、3=紧急 可以直接比大小；
 * 颜色是稳定的一一对应（同一严重度在任何页面同色），未知值有兜底。
 */
class SeverityTest {

    @Test
    fun `严重度可以按大小比较`() {
        assertTrue(Severity.NORMAL < Severity.INFO)
        assertTrue(Severity.INFO < Severity.WARNING)
        assertTrue(Severity.WARNING < Severity.CRITICAL)
    }

    @Test
    fun `严重度中文名`() {
        assertEquals("正常", Severity.label(0))
        assertEquals("提示", Severity.label(1))
        assertEquals("警告", Severity.label(2))
        assertEquals("紧急", Severity.label(3))
        // 越界值不崩，给"未知"。
        assertEquals("未知", Severity.label(9))
        assertEquals("未知", Severity.label(-1))
    }

    @Test
    fun `颜色映射一一对应且相邻不同色`() {
        val colors = (0..3).map { Severity.colorArgb(it) }
        assertEquals("四个严重度四个颜色", 4, colors.toSet().size)
        assertNotEquals(colors[0], colors[1])
        assertNotEquals(colors[1], colors[2])
        assertNotEquals(colors[2], colors[3])
        // 底色必须比主色浅（ARGB 里低 24 位更大 = 更亮）。
        (0..3).forEach { level ->
            assertTrue(
                "严重度 $level 的容器色应比主色浅",
                (Severity.containerColorArgb(level) and 0xFFFFFF) > (Severity.colorArgb(level) and 0xFFFFFF),
            )
        }
    }

    @Test
    fun `越界严重度被夹到 0 到 3_并有兜底色`() {
        assertEquals(Severity.NORMAL, Severity.clamp(-5))
        assertEquals(Severity.CRITICAL, Severity.clamp(99))
        // 夹取后颜色与合法值一致（说明 clamp 真的用上了）。
        assertEquals(Severity.colorArgb(Severity.CRITICAL), Severity.colorArgb(Severity.clamp(99)))
        assertEquals(0xFF757575L, Severity.colorArgb(9)) // 未知 → 灰
    }

    @Test
    fun `只有警告及以上才需要引起注意`() {
        assertFalse(Severity.requiresAttention(0))
        assertFalse(Severity.requiresAttention(1))
        assertTrue(Severity.requiresAttention(2))
        assertTrue(Severity.requiresAttention(3))
    }

    @Test
    fun `事件列表按严重度降序_再按时间降序`() {
        val events = listOf(
            AlarmEvent(code = "humidity_high", ts = 500.0),          // 1 提示
            AlarmEvent(code = "hr_too_high", ts = 100.0),            // 2 警告（较早）
            AlarmEvent(code = "sos_pressed", ts = 300.0),            // 3 紧急
            AlarmEvent(code = "hr_too_high", ts = 200.0),            // 2 警告（较晚）
            AlarmEvent(code = "ambient_temp_low", ts = 600.0),       // 1 提示
        )
        val sorted = events.sortedBySeverityThenTime()

        assertEquals(
            listOf("sos_pressed", "hr_too_high", "hr_too_high", "ambient_temp_low", "humidity_high"),
            sorted.map { it.code },
        )
        // 同级之间是"最近的在前面"。
        assertEquals(200.0, sorted[1].ts!!, 0.001)
        assertEquals(100.0, sorted[2].ts!!, 0.001)
        // 时间缺失的事件排在同级最后，不能崩。
        val withNullTs = listOf(
            AlarmEvent(code = "sos_pressed", ts = null),
            AlarmEvent(code = "sos_pressed", ts = 10.0),
        ).sortedBySeverityThenTime()
        assertEquals(10.0, withNullTs.first().ts!!, 0.001)
    }

    @Test
    fun `active_alarms 的最高严重度用于横幅配色`() {
        assertEquals(Severity.NORMAL, MonitorSnapshot.EMPTY.maxActiveSeverity)
        val snapshot = MonitorSnapshot.EMPTY.copy(
            activeAlarms = mapOf("humidity_high" to 1.0, "spo2_too_low" to 2.0),
        )
        assertEquals(Severity.CRITICAL, snapshot.maxActiveSeverity)
        assertTrue(snapshot.hasActiveAlarms)

        // 未知报警码不能算成 0（否则横幅就"正常"了，等于漏报）。
        val unknownCode = MonitorSnapshot.EMPTY.copy(activeAlarms = mapOf("mystery_code" to 1.0))
        assertEquals(Severity.WARNING, unknownCode.maxActiveSeverity)
        assertNotEquals(Severity.NORMAL, unknownCode.maxActiveSeverity)
    }
}
