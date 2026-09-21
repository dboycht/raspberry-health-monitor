package com.dboycht.healthmonitor.ui.alarms

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
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
import com.dboycht.healthmonitor.domain.AlarmEvent
import com.dboycht.healthmonitor.domain.Formatters
import com.dboycht.healthmonitor.domain.Severity
import com.dboycht.healthmonitor.ui.component.AlarmBanner
import com.dboycht.healthmonitor.ui.component.ConnectionBar
import com.dboycht.healthmonitor.ui.component.EmptyAlarms
import com.dboycht.healthmonitor.ui.component.InfoStrip
import com.dboycht.healthmonitor.ui.component.SeverityLevelBadge
import com.dboycht.healthmonitor.ui.component.SosButton

/**
 * 报警页：当前报警横幅 + 事件列表 + 长按 1.5 秒的紧急求助按钮。
 *
 * 布局刻意把 SOS 按钮**固定在底部**并且要长按：老人家属慌乱时最容易误触的地方
 * 就是这里（协议 §5.5 要求防误触）。
 */
@Composable
fun AlarmsScreen(
    state: AlarmsUiState,
    onRefresh: () -> Unit,
    onSilence: () -> Unit,
    onSos: () -> Unit,
    onDismissFeedback: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Column(modifier.fillMaxSize()) {
        ConnectionBar(
            status = state.connection,
            baseUrl = "报警事件来自 /api/v1/alarms",
            lastSuccessAgeSeconds = null,
            errorMessage = state.errorMessage,
            staleData = false,
        )

        Column(
            Modifier
                .weight(1f)
                .fillMaxWidth(),
        ) {
            LazyColumn(
                modifier = Modifier.fillMaxSize(),
                verticalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                item {
                    Column(Modifier.padding(horizontal = 14.dp, vertical = 8.dp)) {
                        AlarmBanner(
                            activeAlarms = state.activeAlarms,
                            nowSeconds = state.nowSeconds,
                            silencedUntil = state.silencedUntil,
                        )
                    }
                }

                if (state.feedback != null) {
                    item {
                        Column(Modifier.padding(horizontal = 14.dp)) {
                            InfoStrip(text = state.feedback, severity = state.feedbackSeverity)
                        }
                    }
                }

                item {
                    Row(
                        Modifier
                            .fillMaxWidth()
                            .padding(horizontal = 14.dp, vertical = 4.dp),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Text(
                            text = "报警事件（${state.feed.history.size} 条）",
                            style = MaterialTheme.typography.titleMedium,
                            fontWeight = FontWeight.SemiBold,
                        )
                        Text(
                            text = "严重→轻微",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                }

                if (state.feed.history.isEmpty()) {
                    item { EmptyAlarms() }
                } else {
                    // 排序规则在 domain 层（严重度降序 → 时间降序），有单测覆盖。
                    items(state.sortedHistory, key = { event -> eventKey(event) }) { event ->
                        Column(Modifier.padding(horizontal = 14.dp)) {
                            AlarmEventRow(event)
                        }
                    }
                }

                item {
                    if (state.feed.live.isNotEmpty()) {
                        Column(Modifier.padding(horizontal = 14.dp, vertical = 8.dp)) {
                            HorizontalDivider()
                            Text(
                                text = "本次运行下发过的报警（含消音标记）",
                                modifier = Modifier.padding(top = 8.dp),
                                style = MaterialTheme.typography.titleSmall,
                            )
                            state.feed.live.forEach { live ->
                                Row(
                                    Modifier
                                        .fillMaxWidth()
                                        .padding(top = 4.dp),
                                    horizontalArrangement = Arrangement.SpaceBetween,
                                ) {
                                    Text(
                                        text = "• ${live.label}",
                                        style = MaterialTheme.typography.bodySmall,
                                    )
                                    Text(
                                        text = (if (live.silenced) "已消音" else "已下发") +
                                            " · ${Formatters.timestamp(live.ts)}",
                                        style = MaterialTheme.typography.bodySmall,
                                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                                    )
                                }
                            }
                        }
                    }
                }
            }
        }

        Column(Modifier.padding(14.dp)) {
            SosButton(onTrigger = onSos, label = "紧急求助（长按 1.5 秒）")
            if (state.feedback != null) {
                TextButton(onClick = onDismissFeedback, modifier = Modifier.fillMaxWidth()) {
                    Text("清除上面的提示")
                }
            }
            TextButton(onClick = onSilence, modifier = Modifier.fillMaxWidth()) {
                Text("消音（只让树莓派别响，不解除报警）")
            }
        }
    }
}

/** 单条报警事件。 */
@Composable
private fun AlarmEventRow(event: AlarmEvent, modifier: Modifier = Modifier) {
    val severity = event.effectiveSeverity
    Card(
        modifier = modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.surface,
        ),
        elevation = CardDefaults.cardElevation(defaultElevation = 1.dp),
    ) {
        Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Row(
                Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(
                    text = event.label,
                    style = MaterialTheme.typography.titleSmall,
                    fontWeight = FontWeight.SemiBold,
                )
                SeverityLevelBadge(severity = severity)
            }
            Text(
                // 服务器给了 message 就用原话；没给就拼"中文名 + 数值"。
                text = event.displayMessage,
                style = MaterialTheme.typography.bodyMedium,
            )
            Row(
                Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
            ) {
                Text(
                    text = "来源：${event.source ?: Formatters.UNKNOWN}",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                Text(
                    text = Formatters.timestamp(event.ts),
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
    }
}

/** 列表 key：没有唯一 id，用"时间 + 报警码"足够区分（同一秒同一码极少重复）。 */
private fun eventKey(event: AlarmEvent): String = "${event.ts ?: 0.0}#${event.code}"
