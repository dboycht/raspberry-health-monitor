package com.dboycht.healthmonitor.ui.dashboard

import com.dboycht.healthmonitor.data.CurrentDataDto
import com.dboycht.healthmonitor.domain.Formatters
import com.dboycht.healthmonitor.domain.MonitorSnapshot
import com.dboycht.healthmonitor.domain.Severity
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 首页五张卡片的**渲染硬要求**测试（协议 §4.1 的四条规则）。
 *
 * 这些规则如果只写在 Compose 里就得靠真机/模拟器验证，课设环境下不可靠；
 * 抽成 [DashboardCards.build] 之后就能在 JVM 单测里逐条钉死。
 */
class DashboardCardsTest {

    private fun snapshot(data: CurrentDataDto): MonitorSnapshot = MonitorSnapshot.of(data)

    private fun cardOf(cards: List<DashboardCard>, title: String): DashboardCard =
        cards.first { it.title == title }

    @Test
    fun `正好五张卡片_心率 血氧 体温 室温湿度 活动状态`() {
        val cards = DashboardCards.build(MonitorSnapshot.EMPTY)
        assertEquals(5, cards.size)
        assertEquals(
            listOf("心率", "血氧", "体温", "室温 / 湿度", "活动状态"),
            cards.map { it.title },
        )
    }

    @Test
    fun `全空快照时五张卡片都不显示 0`() {
        val cards = DashboardCards.build(MonitorSnapshot.EMPTY)
        cards.forEach { card ->
            // 只允许"--"或"-- / --"这种纯未知占位，不允许出现任何数字。
            assertTrue(
                "${card.title} 的值只能是未知占位，实际是：${card.value}",
                card.value.split(" / ").all { it == Formatters.UNKNOWN || it == "未知" },
            )
            assertFalse("${card.title} 的值里不能有 0：${card.value}", card.value.contains("0"))
        }
        assertEquals(Formatters.UNKNOWN, cardOf(cards, "心率").value)
        assertEquals(Formatters.UNKNOWN, cardOf(cards, "体温").value)
        assertEquals("-- / --", cardOf(cards, "室温 / 湿度").value)
        assertEquals("未知", cardOf(cards, "活动状态").value)
        // 值缺失时不给单位（避免出现 "-- ℃"）。
        assertNull(cardOf(cards, "体温").unit)
        assertNull(cardOf(cards, "心率").unit)
    }

    @Test
    fun `心率与血氧为 null 时显示 -- 而不是 0 bpm`() {
        // 协议 §4.1 的核心场景：算法没收敛 → 数值是 null。
        val cards = DashboardCards.build(
            snapshot(CurrentDataDto(ts = 1.0, heart_rate_bpm = null, spo2_percent = null, finger_detected = true)),
        )
        val hr = cardOf(cards, "心率")
        val spo2 = cardOf(cards, "血氧")
        assertEquals(Formatters.UNKNOWN, hr.value)
        assertEquals(Formatters.UNKNOWN, spo2.value)
        assertFalse(hr.value.contains("0"))
        assertEquals("未知", hr.statusText)
        assertNull(hr.unit)
    }

    @Test
    fun `finger_detected 为 false 时心率与血氧区显示请将手指放好`() {
        // 即使服务器同时给了数值，也以"未贴合"为准（协议 §4.1 第二条）。
        val cards = DashboardCards.build(
            snapshot(
                CurrentDataDto(
                    ts = 1.0,
                    finger_detected = false,
                    heart_rate_bpm = 0.0,
                    spo2_percent = 0.0,
                ),
            ),
        )
        val hr = cardOf(cards, "心率")
        val spo2 = cardOf(cards, "血氧")
        assertEquals(Formatters.PLACE_FINGER, hr.value)
        assertEquals(Formatters.PLACE_FINGER, spo2.value)
        assertEquals("请将手指放好", hr.value)
        assertEquals("未贴合", hr.statusText)
        assertEquals("未贴合", spo2.statusText)
        assertFalse(hr.value.contains("0"))
        assertNull(hr.unit)
    }

    @Test
    fun `sensor_failures 非空时给出警告文案`() {
        val healthy = MonitorSnapshot.EMPTY
        assertNull(DashboardCards.sensorWarningText(healthy))
        assertTrue(DashboardCards.sensorWarnings(healthy).isEmpty())

        val failing = snapshot(
            CurrentDataDto(ts = 1.0, sensor_failures = mapOf("vitals" to 3, "ambient" to 1)),
        )
        assertEquals(
            "ambient 传感器异常（连续 1 次）；vitals 传感器异常（连续 3 次）",
            DashboardCards.sensorWarningText(failing),
        )
    }

    @Test
    fun `motion_state 为 unknown 时活动卡片显示未知而不是无人`() {
        val cards = DashboardCards.build(snapshot(CurrentDataDto(ts = 1.0, motion_state = "unknown")))
        val motion = cardOf(cards, "活动状态")
        assertEquals("未知", motion.value)
        assertEquals("未知", motion.statusText)
        assertEquals(Severity.INFO, motion.statusSeverity)
        assertFalse(motion.value.contains("无人"))
        // null 也一样。
        val cardsNull = DashboardCards.build(snapshot(CurrentDataDto(ts = 1.0, motion_state = null)))
        assertEquals("未知", cardOf(cardsNull, "活动状态").value)
    }

    @Test
    fun `活动状态正常与无活动的区分`() {
        val detected = cardOf(
            DashboardCards.build(
                snapshot(CurrentDataDto(ts = 1.0, motion_state = "detected", motion_silent_s = 0.0)),
            ),
            "活动状态",
        )
        assertEquals("有人在活动", detected.value)
        assertEquals("有活动", detected.statusText)

        val idleLong = cardOf(
            DashboardCards.build(
                snapshot(CurrentDataDto(ts = 1.0, motion_state = "idle", motion_silent_s = 7200.0)),
            ),
            "活动状态",
        )
        assertEquals("未检测到活动", idleLong.value)
        // 超过 10 分钟无活动给个警告色提示（真正报警由树莓派规则判定）。
        assertEquals(Severity.WARNING, idleLong.statusSeverity)
        assertTrue(idleLong.subtitle!!.contains("2 小时"))
    }

    @Test
    fun `心率与体温越界时卡片高亮`() {
        val cards = DashboardCards.build(
            snapshot(CurrentDataDto(ts = 1.0, finger_detected = true, heart_rate_bpm = 128.4, body_temp_c = 38.2)),
        )
        val hr = cardOf(cards, "心率")
        assertEquals("128", hr.value)
        assertEquals("bpm", hr.unit)
        assertEquals("异常", hr.statusText)
        assertEquals(Severity.WARNING, hr.statusSeverity)
        assertTrue(hr.alert)

        val temp = cardOf(cards, "体温")
        assertEquals("38.2", temp.value)
        assertEquals("异常", temp.statusText)
        assertTrue(temp.alert)
    }

    @Test
    fun `正常数值原样显示_室温湿度合并在同一张卡`() {        val cards = DashboardCards.build(
            snapshot(
                CurrentDataDto(
                    ts = 1.0,
                    finger_detected = true,
                    heart_rate_bpm = 72.4,
                    spo2_percent = 97.9,
                    ambient_temp_c = 24.5,
                    humidity_percent = 55.2,
                ),
            ),
        )
        assertEquals("72", cardOf(cards, "心率").value)
        assertEquals("97.9", cardOf(cards, "血氧").value)
        assertEquals("24.5 ℃ / 55.2 %", cardOf(cards, "室温 / 湿度").value)
        assertFalse(cardOf(cards, "心率").alert)
    }

    @Test
    fun `室温湿度缺失时各显示 -- 且不显示 0`() {
        val cards = DashboardCards.build(
            snapshot(CurrentDataDto(ts = 1.0, ambient_temp_c = null, humidity_percent = null)),
        )
        val room = cardOf(cards, "室温 / 湿度")
        assertEquals("-- / --", room.value)
        assertFalse(room.value.contains("0"))
        assertEquals("未知", room.statusText)
    }

    // -----------------------------------------------------------------------
    // 协议 §4.1 第四条：服务端说"数据已过期"要提示（与本机请求失败是两件事）
    // -----------------------------------------------------------------------

    @Test
    fun `data_stale 为真时给出过期提示_并带上秒数`() {
        val snap = snapshot(CurrentDataDto(ts = 1.0, data_stale = true, data_age_s = 42.0))
        val text = DashboardCards.staleWarningText(snap)
        assertNotNull(text)
        assertEquals("数据可能已过期：最近 42 秒无新数据", text)
    }

    @Test
    fun `data_stale 为真且分钟级时用分钟表述`() {
        val snap = snapshot(CurrentDataDto(ts = 1.0, data_stale = true, data_age_s = 600.0))
        assertEquals("数据可能已过期：最近 10 分钟无新数据", DashboardCards.staleWarningText(snap))
    }

    @Test
    fun `从未读到数据时给出不可用提示_而不是编个秒数`() {
        val snap = snapshot(CurrentDataDto(ts = 1.0, data_stale = true, data_age_s = null))
        assertEquals("数据不可用：树莓派尚未读到任何传感器数据", DashboardCards.staleWarningText(snap))
    }

    @Test
    fun `data_stale 为假时不提示_阈值由服务端决定不自己猜`() {
        // 即使本机看到的年龄很大，服务端没说 stale 就**不**提示——
        // 阈值（设备数 × 3 × 最长周期）在树莓派端可配，客户端不该复制一套逻辑。
        val snap = snapshot(CurrentDataDto(ts = 1.0, data_stale = false, data_age_s = 999.0))
        assertNull(DashboardCards.staleWarningText(snap))
    }

    @Test
    fun `旧版服务端没有 data_stale 字段时按不陈旧处理_且不崩`() {
        val snap = snapshot(CurrentDataDto(ts = 1.0))
        assertFalse(snap.dataStale)
        assertNull(DashboardCards.staleWarningText(snap))
    }
}
