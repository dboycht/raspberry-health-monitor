package com.dboycht.healthmonitor.domain

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 测试组①：**报警码 → 中文文案映射**（照 `docs/05-安卓通信协议.md` §4.4 的表）。
 *
 * 这张表是手机端唯一的中文来源，写错了家属就会看到"未知报警"，
 * 所以每个码都逐条断言；另外还要保证"未知码不吞掉"。
 */
class AlarmCatalogTest {

    @Test
    fun `协议 4_4 表里的每个报警码都映射到规定的中文名`() {
        val expected = mapOf(
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
        expected.forEach { (code, label) ->
            assertEquals("报警码 $code 的中文名不对", label, AlarmCatalog.label(code))
        }
        // 表里数一数：15 个码，别漏。
        // ⚠️ 这个数字**故意写死**：新增报警码时必须同时改三处
        //    （models.py 的 AlarmCode / docs/08-报警规则表.md / 本文件），
        //    跨语言的一致性由 `rpi/scripts/check_docs.py` 兜底检查。
        assertEquals(15, AlarmCatalog.allCodes().size)
    }

    @Test
    fun `协议 4_4 表里的默认严重度一致`() {
        // 2 警告
        assertEquals(Severity.WARNING, AlarmCatalog.defaultSeverity("hr_too_high"))
        assertEquals(Severity.WARNING, AlarmCatalog.defaultSeverity("hr_too_low"))
        assertEquals(Severity.WARNING, AlarmCatalog.defaultSeverity("body_temp_high"))
        assertEquals(Severity.WARNING, AlarmCatalog.defaultSeverity("body_temp_low"))
        assertEquals(Severity.WARNING, AlarmCatalog.defaultSeverity("night_frequent_wake"))
        assertEquals(Severity.WARNING, AlarmCatalog.defaultSeverity("sensor_fault"))
        assertEquals(Severity.WARNING, AlarmCatalog.defaultSeverity("device_offline"))
        // 3 紧急
        assertEquals(Severity.CRITICAL, AlarmCatalog.defaultSeverity("spo2_too_low"))
        assertEquals(Severity.CRITICAL, AlarmCatalog.defaultSeverity("no_motion_too_long"))
        assertEquals(Severity.CRITICAL, AlarmCatalog.defaultSeverity("sos_pressed"))
        // 1 提示
        assertEquals(Severity.INFO, AlarmCatalog.defaultSeverity("ambient_temp_high"))
        assertEquals(Severity.INFO, AlarmCatalog.defaultSeverity("ambient_temp_low"))
        assertEquals(Severity.INFO, AlarmCatalog.defaultSeverity("humidity_high"))
        assertEquals(Severity.INFO, AlarmCatalog.defaultSeverity("system_start"))
        // 0 正常
        assertEquals(Severity.NORMAL, AlarmCatalog.defaultSeverity("all_clear"))
    }

    @Test
    fun `未知报警码不吞掉_带出原码方便排查`() {
        val label = AlarmCatalog.label("brand_new_alarm")
        assertTrue("未知码要标出未知：$label", label.contains(AlarmCatalog.UNKNOWN_LABEL))
        assertTrue("未知码要带出原码：$label", label.contains("brand_new_alarm"))
        // 未知码按"警告"处理：宁可高估，不能漏报。
        assertEquals(Severity.WARNING, AlarmCatalog.defaultSeverity("brand_new_alarm"))
    }

    @Test
    fun `null 与空报警码不会崩_也不显示成有效报警`() {
        assertEquals("未知报警", AlarmCatalog.label(null))
        assertEquals("未知报警", AlarmCatalog.label(""))
        assertEquals("未知报警", AlarmCatalog.label("   "))
        assertEquals(Severity.WARNING, AlarmCatalog.defaultSeverity(null))
    }

    @Test
    fun `服务器给的 severity 优先于目录默认值`() {
        // 用户在树莓派上把"室温偏高"调成了紧急，手机端必须听服务器的。
        assertEquals(Severity.CRITICAL, AlarmCatalog.severityOf("ambient_temp_high", 3))
        // 没给 severity 才退回默认值。
        assertEquals(Severity.INFO, AlarmCatalog.severityOf("ambient_temp_high", null))
        // all_clear 默认 0，但不能因为 0 被当成"没给"而误判成警告。
        assertEquals(Severity.NORMAL, AlarmCatalog.severityOf("all_clear", 0))
        assertNotEquals(Severity.WARNING, AlarmCatalog.severityOf("all_clear", 0))
    }

    @Test
    fun `severity 为 null 的历史事件用报警码默认值`() {
        val event = AlarmEvent(code = "spo2_too_low", ts = 1.0, severity = null)
        assertEquals(Severity.CRITICAL, event.effectiveSeverity)
        assertEquals("血氧过低", event.label)
    }
}
