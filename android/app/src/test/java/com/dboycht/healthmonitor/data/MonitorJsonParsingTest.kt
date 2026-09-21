package com.dboycht.healthmonitor.data

import com.dboycht.healthmonitor.testing.JsonSamples
import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 测试组④：**接口 JSON 解析**（用真实响应样本字符串，含 `null` 字段与空对象）。
 *
 * 全部离线：直接把字符串喂给 [HealthJson]（与 App 运行时**同一个** Json 配置），
 * 不起 HTTP 服务、不连真树莓派。
 *
 * 核心断言只有一句话：**JSON 里是 null（或字段缺失）时，DTO 里必须是 null，
 * 绝不能变成 0.0**——这正是协议禁止"显示 0 bpm"的源头。
 */
class MonitorJsonParsingTest {

    private val json: Json = HealthJson

    // --- §4.1 /current ------------------------------------------------

    @Test
    fun `解析 §4_1 完整样本_字段逐一对应`() {
        val response = json.decodeFromString<CurrentResponse>(JsonSamples.CURRENT_HEALTHY)

        assertTrue(response.ok)
        val data = response.data!!
        assertEquals(1790001600.5, data.ts!!, 0.001)
        assertEquals(72.4, data.heart_rate_bpm!!, 0.001)
        assertEquals(97.9, data.spo2_percent!!, 0.001)
        assertEquals(true, data.finger_detected)
        assertEquals(0.95, data.vitals_quality!!, 0.001)
        assertEquals(36.5, data.body_temp_c!!, 0.001)
        assertEquals(24.5, data.ambient_temp_c!!, 0.001)
        assertEquals(55.2, data.humidity_percent!!, 0.001)
        assertEquals("detected", data.motion_state)
        assertEquals(0.0, data.motion_silent_s!!, 0.001)
        // 空对象 → 空 map（不是 null，也不是"有一项失败"）。
        assertTrue(data.sensor_failures.isEmpty())
        assertTrue(response.active_alarms.isEmpty())
    }

    @Test
    fun `解析含 null 字段的样本_null 就是 null_绝不变成 0`() {
        val response = json.decodeFromString<CurrentResponse>(JsonSamples.CURRENT_FINGER_MISSING_AND_FAILURES)
        val data = response.data!!

        // 三项都是 JSON null：必须是 null，不能是 0.0。
        assertNull("heart_rate_bpm 是 null，不能解析成 0", data.heart_rate_bpm)
        assertNull("spo2_percent 是 null，不能解析成 0", data.spo2_percent)
        assertNull("vitals_quality 是 null，不能解析成 0", data.vitals_quality)
        assertNull("ambient_temp_c 是 null，不能解析成 0", data.ambient_temp_c)
        assertNull("motion_silent_s 是 null，不能解析成 0", data.motion_silent_s)
        assertNotEquals(0.0, data.heart_rate_bpm ?: -1.0, 0.0)

        // 同一份样本里"有值"的字段照常解析（不能因为别的字段是 null 就整体失败）。
        assertEquals(false, data.finger_detected)
        assertEquals(36.1, data.body_temp_c!!, 0.001)
        assertEquals(48.0, data.humidity_percent!!, 0.001)

        // sensor_failures 非空要能解析成"设备名 → 次数"。
        assertEquals(3, data.sensor_failures["ambient"])
        assertEquals(1, data.sensor_failures["vitals"])
        assertEquals("ambient", data.sensor_failures.keys.sorted().first())

        // active_alarms 非空（协议 §4.1：非空要亮横幅）。
        assertEquals(1, response.active_alarms.size)
        assertEquals(1790001500.0, response.active_alarms["spo2_too_low"]!!, 0.001)
    }

    @Test
    fun `data 整体缺失时不崩_得到空快照与空报警`() {
        val response = json.decodeFromString<CurrentResponse>(JsonSamples.CURRENT_DATA_ABSENT)
        assertTrue(response.ok)
        assertNull(response.data)
        assertTrue(response.active_alarms.isEmpty())

        val snapshot = com.dboycht.healthmonitor.domain.MonitorSnapshot.from(response)
        assertNull(snapshot.heartRateBpm)
        assertTrue(snapshot.activeAlarms.isEmpty())
    }

    @Test
    fun `未知字段被忽略_协议 §6 兼容性`() {
        // 树莓派将来加了新字段，手机端必须还能解析（协议 §6：新增字段是兼容的）。
        val futureJson = """
            {"ok": true,
             "data": {"ts": 1.0, "heart_rate_bpm": 70.0, "brand_new_field": {"a": 1},
                      "spo2_percent": null, "future_array": [1,2,3]},
             "active_alarms": {}, "server_note": "hi"}
        """.trimIndent()
        val response = json.decodeFromString<CurrentResponse>(futureJson)
        assertEquals(70.0, response.data!!.heart_rate_bpm!!, 0.001)
        assertNull(response.data!!.spo2_percent)
    }

    @Test
    fun `字段缺失与显式 null 等价_都是 null`() {
        // 服务器可能整个字段不发，效果必须和发 null 一样。
        val missing = """{"ok": true, "data": {"ts": 5.0}}"""
        val data = json.decodeFromString<CurrentResponse>(missing).data!!
        assertNull(data.heart_rate_bpm)
        assertNull(data.spo2_percent)
        assertNull(data.body_temp_c)
        assertNull(data.finger_detected)
        assertNull(data.motion_state)
        assertTrue(data.sensor_failures.isEmpty())
    }

    // --- §4.4 /alarms -------------------------------------------------

    @Test
    fun `解析 §4_4 报警样本_live 与 history`() {
        val response = json.decodeFromString<AlarmsResponse>(JsonSamples.ALARMS)
        assertTrue(response.ok)
        assertEquals(1, response.live.size)
        assertEquals("hr_too_high", response.live[0].code)
        assertEquals(2, response.live[0].severity)
        assertEquals(false, response.live[0].silenced)
        assertEquals("心率偏高，请注意休息", response.live[0].speak)

        val feed = com.dboycht.healthmonitor.domain.AlarmFeed.from(response)
        assertEquals(1, feed.live.size)
        // 样本第三条的 code 是 null：没有报警码就显示不出中文名，应被过滤掉而不是变成空报警。
        assertEquals(2, feed.history.size)
        assertEquals(listOf("hr_too_high", "sos_pressed"), feed.history.map { it.code })

        // value = null 的那条（sos_pressed）不能变成 0.0，显示时用报警名兜底。
        val sos = feed.history.first { it.code == "sos_pressed" }
        assertNull(sos.value)
        assertEquals("紧急求助", sos.label)
        assertEquals("已收到紧急求助，请立即查看", sos.displayMessage)

        // 有 value 的那条直接用服务器的 message。
        val hr = feed.history.first { it.code == "hr_too_high" }
        assertEquals(128.4, hr.value!!, 0.001)
        assertEquals("心率偏高 128.4bpm，请注意查看", hr.displayMessage)
    }

    @Test
    fun `空对象与空数组的响应不会崩`() {
        val empty = json.decodeFromString<AlarmsResponse>("""{"ok": true, "live": [], "history": []}""")
        assertTrue(empty.live.isEmpty())
        assertTrue(empty.history.isEmpty())

        val absent = json.decodeFromString<AlarmsResponse>("""{"ok": true}""")
        assertTrue(absent.live.isEmpty())
        assertTrue(absent.history.isEmpty())

        // 服务器错误格式（协议 §2：{"ok": false, "error": "..."}）。
        val failed = json.decodeFromString<AlarmsResponse>(JsonSamples.UNAUTHORIZED)
        assertEquals(false, failed.ok)
        assertEquals("invalid or missing token", failed.error)
    }

    // --- §4.2 /health 与 §4.5 /devices --------------------------------

    @Test
    fun `解析 §4_2 健康样本_版本与设备状态`() {
        val response = json.decodeFromString<HealthResponse>(JsonSamples.HEALTH)
        assertEquals("1.0.1", response.version)
        assertEquals(612.3, response.uptime_s!!, 0.001)
        assertEquals(false, response.mock)
        assertEquals(1, response.active_alarms.size)
        assertEquals(true, response.dispatcher!!.enabled)
        assertEquals(listOf("alarm_buzzer", "display", "speaker", "status_led"), response.dispatcher!!.outputs)

        val health = com.dboycht.healthmonitor.domain.SystemHealth.from(response)
        assertEquals("1.0.1", health.version)
        assertEquals(2, health.devices.size)

        val vitals = health.devices.first { it.name == "vitals" }
        assertEquals("max30102", vitals.driver)
        assertEquals(612, vitals.reads)
        assertEquals("正常", vitals.statusText)
        assertNull(vitals.lastError)

        // 有失败与陈旧标记的设备要能看出来。
        val ambient = health.devices.first { it.name == "ambient" }
        assertEquals(7, ambient.failures)
        assertEquals(true, ambient.stale)
        assertEquals("checksum error", ambient.lastError)
        assertEquals("数据陈旧", ambient.statusText)
    }

    @Test
    fun `解析 §4_5 设备清单_接线说明`() {
        val response = json.decodeFromString<DevicesResponse>(JsonSamples.DEVICES)
        val devices = com.dboycht.healthmonitor.domain.DeviceInfo.from(response)
        assertEquals(1, devices.size)
        val vitals = devices.first()
        assertEquals("vitals", vitals.name)
        assertEquals("max30102", vitals.driver)
        assertEquals("I2C-1", vitals.bus)
        assertEquals("GPIO2 / 物理脚 3", vitals.pins["sda"])
        assertEquals("I2C 0x57", vitals.notes)
        assertEquals(false, vitals.mock)
    }

    // --- 动作接口 ------------------------------------------------------

    @Test
    fun `解析 silence 与 sos 的响应`() {
        val silence = json.decodeFromString<SilenceResponse>(
            """{"ok": true, "silenced_until": 1790001900.0}""",
        )
        assertEquals(1790001900.0, silence.silenced_until!!, 0.001)

        val sos = json.decodeFromString<SosResponse>(
            """{"ok": true, "event": {"ts": 1790001560.0, "code": "sos_pressed", "severity": 3,
               "message": "已收到紧急求助，请立即查看", "value": null, "unit": "", "source": "sos", "detail": {}}}""",
        )
        assertEquals("sos_pressed", sos.event!!.code)
        assertNull(sos.event!!.value)
        assertEquals("已收到紧急求助，请立即查看", sos.event!!.message)
    }

    /**
     * 这条测试专门守住"**不要退回 Gson**"的决定：Gson 会把缺失的 double 字段
     * 留成 0.0，于是界面上就出现"心率 0 bpm"。这里用结构化断言把行为钉死。
     */
    @Test
    fun `缺失数值字段不会退化成 0_这是不用 Gson 的原因`() {
        val data = json.decodeFromString<CurrentResponse>(
            """{"ok": true, "data": {"ts": 1.0}}""",
        ).data!!
        listOf(
            data.heart_rate_bpm,
            data.spo2_percent,
            data.body_temp_c,
            data.ambient_temp_c,
            data.humidity_percent,
            data.vitals_quality,
            data.motion_silent_s,
        ).forEach { value ->
            assertNull("缺失字段必须是 null（Gson 会给 0.0，那正是协议禁止的）", value)
        }
    }
}
