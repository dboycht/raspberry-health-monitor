package com.dboycht.healthmonitor.ui.cloud

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.dboycht.healthmonitor.domain.CloudReading
import com.dboycht.healthmonitor.domain.Formatters
import com.dboycht.healthmonitor.domain.Severity
import com.dboycht.healthmonitor.ui.component.InfoStrip

/**
 * 「云端」页：显示**中国移动 OneNET** 里"树莓派最后上报过的数据点"。
 *
 * ⚠️ 这一页的**头等任务是"不误导"**（比"多显示几个数字"重要得多）：
 *
 * 1. 顶部固定说明"**这是云端、是上次上报、不是实时**"，且**报警/消音/求助不在这里**；
 * 2. 每个值都带**自己的时间**（不同数据流的最后上报时间可能差很远）；
 * 3. 全部数据都超过 [CloudUiState.STALE_AFTER_SECONDS] 时给一条醒目提示
 *    —— "树莓派可能已经停了，云端只剩下旧数据"；
 * 4. 没配置时**不去请求**，只显示"去设置里填"的引导（也别显示假的空数据）。
 */
@Composable
fun CloudScreen(
    state: CloudUiState,
    onRefresh: () -> Unit,
    onOpenSettings: () -> Unit,
    modifier: Modifier = Modifier,
) {
    LazyColumn(
        modifier = modifier.fillMaxSize(),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        item {
            Column(Modifier.padding(horizontal = 14.dp)) {
                SectionCard(title = "这一页看的是什么") {
                    Text(
                        "来自**中国移动 OneNET**（手机能上网就行，**不需要**连到树莓派）。",
                        style = MaterialTheme.typography.bodyMedium,
                    )
                    Text(
                        "⚠️ 这些是树莓派**上一次上报**的值，**不是此刻的实时状态**；" +
                            "报警、消音、求助、按需测血氧都是树莓派本地功能，**云端没有**。",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
        }

        if (!state.configured) {
            item {
                Column(Modifier.padding(horizontal = 14.dp)) {
                    SectionCard(title = "还没配置云端") {
                        Text(
                            "在「设置 → 云端（可选）」里填 产品 ID / 设备名 / 密钥 三项即可。" +
                                "密钥只存在这台手机上，**没有写进 App**。",
                            style = MaterialTheme.typography.bodyMedium,
                        )
                        TextButton(onClick = onOpenSettings) { Text("去设置里填") }
                    }
                }
            }
            return@LazyColumn
        }

        state.errorMessage?.let { message ->
            item {
                Column(Modifier.padding(horizontal = 14.dp)) {
                    InfoStrip(
                        text = "拉取云端失败：$message" +
                            if (state.readings.isEmpty()) "" else "（下面仍是上次拿到的数据）",
                        severity = Severity.WARNING,
                    )
                }
            }
        }

        if (state.allStale) {
            item {
                Column(Modifier.padding(horizontal = 14.dp)) {
                    InfoStrip(
                        text = "云端最新数据也已经超过 10 分钟没更新 —— 树莓派可能停了或断网了。" +
                            "要判断「现在」是否正常，请看「监护」页（那是实时数据）。",
                        severity = Severity.WARNING,
                    )
                }
            }
        }

        item {
            Column(Modifier.padding(horizontal = 14.dp)) {
                SectionCard(title = "最新数据（共 ${state.streamCount} 条数据流）") {
                    if (state.readings.isEmpty()) {
                        Text(
                            if (state.loading) "正在读取…" else "云端还没有数据点（树莓派还没上报过？）",
                            style = MaterialTheme.typography.bodyMedium,
                        )
                    } else {
                        state.readings.forEachIndexed { index, reading ->
                            if (index > 0) HorizontalDivider()
                            ReadingRow(reading)
                        }
                    }
                    Row(
                        Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.End,
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Text(
                            "每 30 秒自动刷新",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                        TextButton(onClick = onRefresh) { Text("立即刷新") }
                    }
                }
            }
        }
    }
}

@Composable
private fun ReadingRow(reading: CloudReading) {
    Row(
        Modifier
            .fillMaxWidth()
            .padding(vertical = 4.dp),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(Modifier.weight(1f)) {
            Text(reading.label, style = MaterialTheme.typography.bodyLarge)
            val age = reading.ageSeconds
            val whenText = when {
                age != null -> "上报于 ${Formatters.duration(age.toDouble())}前"
                reading.atText.isNotBlank() -> "上报于 ${reading.atText}"
                else -> "时间未知"
            }
            Text(
                whenText,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        Text(
            reading.valueText,
            style = MaterialTheme.typography.titleMedium,
            fontWeight = FontWeight.Bold,
        )
    }
}

@Composable
private fun SectionCard(title: String, content: @Composable () -> Unit) {
    Card(
        modifier = Modifier.fillMaxWidth(),
        elevation = CardDefaults.cardElevation(defaultElevation = 1.dp),
    ) {
        Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            Text(
                title,
                style = MaterialTheme.typography.titleSmall,
                fontWeight = FontWeight.Bold,
                color = MaterialTheme.colorScheme.primary,
            )
            content()
        }
    }
}
