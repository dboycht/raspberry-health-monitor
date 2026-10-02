package com.dboycht.healthmonitor.domain

/**
 * **云端（OneNET）只读**的展示模型。
 *
 * ⚠️ 与 [MonitorSnapshot] 的关系是"**两回事，别混**"：那个是**板子**给的最新快照（实时）；
 * 这个是**云端**里"树莓派最后上报过的一个点"（可能是几分钟前，也可能几小时前）。
 * 界面上必须把"这是云端数据、时间点是 X"写在显眼处 —— 否则家属会拿旧数据当此刻的状态。
 */
data class CloudReading(
    /** 数据流 id（`heart_rate` / `motion` / …）。 */
    val id: String,
    /** 中文标签；不认识的流就原样显示 id（**不假装认识**）。 */
    val label: String,
    /** 已带单位的显示文本；缺失显示 `--`（**绝不显示 0**）。 */
    val valueText: String,
    /** 平台给的时间字符串（`2026-09-30 21:38:13`）。 */
    val atText: String,
    /** 距"现在"多少秒（null = 平台没给时间戳）。 */
    val ageSeconds: Long?,
)

data class CloudSnapshot(
    val readings: List<CloudReading> = emptyList(),
    /** 本次拉取时间（毫秒）。 */
    val fetchedAtMillis: Long = 0L,
    /** 平台报的数据流总数（比 [readings] 多的话说明有些流没有数据点）。 */
    val streamCount: Int = 0,
)
