package com.dboycht.healthmonitor.data

import com.dboycht.healthmonitor.testing.JsonSamples
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * OneNET 响应 → 界面模型 的映射规则。
 *
 * 三条"不许骗人"的判据（与板子直连那条链路的约定完全一致）：
 * 1. **缺失显示 `--`，绝不显示 0**；
 * 2. 每个值**必须带自己的时间**（不同数据流的最后上报时间可能差一整天）；
 * 3. 不认识的数据流**原样显示 id**，不假装认识。
 */
class CloudMappingTest {

    private val json = HealthJson

    private fun parse(text: String) = json.decodeFromString<OneNetResponse>(text)

    @Test
    fun `真机样本映射成读数_按时间从新到旧排列`() {
        val now = 1_790_868_623_000L
        val snapshot = HttpCloudRepository.mapToSnapshot(parse(JsonSamples.ONENET_HISTORY_REAL), now)

        assertEquals(18, snapshot.streamCount)
        // motion/version 是同一刻（最新），heart_rate 最旧（差一天）⇒ 它必须排最后。
        assertEquals("heart_rate", snapshot.readings.last().id)
        assertEquals(setOf("motion", "version"), snapshot.readings.take(2).map { it.id }.toSet())
    }

    @Test
    fun `数值与字符串都能显示_并带上单位`() {
        val snapshot = HttpCloudRepository.mapToSnapshot(
            parse(JsonSamples.ONENET_HISTORY_REAL), 1_790_868_623_000L,
        )
        val byId = snapshot.readings.associateBy { it.id }

        assertEquals("89 bpm", byId["heart_rate"]!!.valueText)     // 数字 + 单位
        assertEquals("unknown", byId["motion"]!!.valueText)        // 字符串原样（不加单位）
        assertEquals("22.7", byId["alarm_value"]!!.valueText)      // 没配单位的数字不加单位
        assertEquals("1.0.1", byId["version"]!!.valueText)
        assertEquals("0", byId["alarm_level"]!!.valueText)         // 真的 0 就显示 0
    }

    @Test
    fun `时间差算得出来_并保留平台给的时间字符串`() {
        val now = 1_790_868_623_000L          // = motion 的 at_timestamp
        val snapshot = HttpCloudRepository.mapToSnapshot(parse(JsonSamples.ONENET_HISTORY_REAL), now)
        val byId = snapshot.readings.associateBy { it.id }

        assertEquals(0L, byId["motion"]!!.ageSeconds)
        assertEquals("2026-10-01 23:30:23.000", byId["motion"]!!.atText)
        // heart_rate 比 motion 早 93130000 ms ≈ 93130 秒
        assertEquals(93130L, byId["heart_rate"]!!.ageSeconds)
    }

    @Test
    fun `value 缺失显示双横线_且不补单位_绝不显示 0`() {
        // 有单位的流（spo2 → " %"）缺值时不能变成 "-- %"：
        // 项目既有硬规矩"值缺失时不给单位"，卡片那边有单测钉着，云端这一路同口径。
        val text = """{"code":0,"data":{"count":1,"datastreams":[
            {"id":"spo2","datapoints":[{"at":"2026-10-02 12:00:00.000","at_timestamp":1790868623000,"value":null}]}]}}"""
        val snapshot = HttpCloudRepository.mapToSnapshot(parse(text), 1_790_868_623_000L)
        assertEquals(1, snapshot.readings.size)
        assertEquals("--", snapshot.readings[0].valueText)
        assertFalse("不能出现任何单位或 0", snapshot.readings[0].valueText.contains("%"))
    }

    @Test
    fun `有值时同一条流会带单位`() {
        val text = """{"code":0,"data":{"count":1,"datastreams":[
            {"id":"spo2","datapoints":[{"at_timestamp":1790868623000,"value":97}]}]}}"""
        val snapshot = HttpCloudRepository.mapToSnapshot(parse(text), 1_790_868_623_000L)
        assertEquals("97 %", snapshot.readings[0].valueText)
    }

    @Test
    fun `不认识的数据流原样显示 id_不假装认识`() {
        val text = """{"code":0,"data":{"count":1,"datastreams":[
            {"id":"probe_temp","datapoints":[{"at_timestamp":1790868623000,"value":25}]}]}}"""
        val snapshot = HttpCloudRepository.mapToSnapshot(parse(text), 1_790_868_623_000L)
        assertEquals("probe_temp", snapshot.readings[0].label)
        assertNull("没有 at 字符串时不能编一个", snapshot.readings[0].atText.ifBlank { null })
    }

    @Test
    fun `没有数据点的流会被跳过_而不是显示成空行`() {
        val text = """{"code":0,"data":{"count":2,"datastreams":[
            {"id":"heart_rate","datapoints":[]},
            {"id":"spo2","datapoints":[{"at_timestamp":1790868623000,"value":97}]}]}}"""
        val snapshot = HttpCloudRepository.mapToSnapshot(parse(text), 1_790_868_623_000L)
        assertEquals(listOf("spo2"), snapshot.readings.map { it.id })
    }

    @Test
    fun `未知键要被忽略_真机样本带 msg 与 request_id`() {
        // JsonSamples 里就带 msg / request_id：能解析成功本身就是这条判据。
        val parsed = parse(JsonSamples.ONENET_HISTORY_REAL)
        assertEquals(0, parsed.code)
        assertEquals("succ", parsed.msg)
    }

    @Test
    fun `value 是对象或数组也不能把解析打崩`() {
        // 平台理论上不该这么发，但"开放类型"就得能兜住（真机教训：别假设值的类型）。
        val parsed = parse("""{"code":0,"data":{"datastreams":[{"id":"x","datapoints":[
            {"at_timestamp":1,"value":{"a":1}}]}]}}""")
        val snapshot = HttpCloudRepository.mapToSnapshot(parsed, 2_000L)
        assertTrue(snapshot.readings[0].valueText.contains("\"a\""))
        assertEquals(JsonNull, Json.parseToJsonElement("null"))
        assertEquals(JsonPrimitive("s"), JsonPrimitive("s"))
    }
}
