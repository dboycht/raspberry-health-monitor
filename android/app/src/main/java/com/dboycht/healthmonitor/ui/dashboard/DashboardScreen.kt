package com.dboycht.healthmonitor.ui.dashboard

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyListScope
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.dboycht.healthmonitor.domain.Formatters
import com.dboycht.healthmonitor.domain.Severity
import com.dboycht.healthmonitor.ui.component.AlarmBanner
import com.dboycht.healthmonitor.ui.component.ConnectionBar
import com.dboycht.healthmonitor.ui.component.InfoStrip
import com.dboycht.healthmonitor.ui.component.MetricCard
import com.dboycht.healthmonitor.ui.component.WarningStrip

/**
 * 首页（监护面板）：五张卡片 + 当前报警横幅 + 消音按钮。
 *
 * 数据以 4 秒一轮的节奏从 `/api/v1/current` 刷新（轮询循环在 [DashboardViewModel] 里，
 * 随生命周期自动停）。
 */
@Composable
fun DashboardScreen(
    state: DashboardUiState,
    onRefresh: () -> Unit,
    onSilence: () -> Unit,
    onDismissSilenceFeedback: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val cards = DashboardCards.build(state.snapshot)
    val sensorWarning = DashboardCards.sensorWarningText(state.snapshot)
    val serverStaleWarning = state.serverStaleWarning

    LazyColumn(
        modifier = modifier.fillMaxSize(),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        item {
            ConnectionBar(
                status = state.connection,
                baseUrl = state.baseUrl,
                lastSuccessAgeSeconds = state.lastSuccessAgeSeconds,
                errorMessage = state.errorMessage,
                staleData = state.staleData,
            )
        }

        item {
            Column(Modifier.padding(horizontal = 14.dp)) {
                AlarmBanner(
                    activeAlarms = state.snapshot.activeAlarms,
                    nowSeconds = state.nowSeconds,
                )
            }
        }

        if (sensorWarning != null) {
            // 协议 §4.1：sensor_failures 非空 → 以警告色列出"X 传感器异常（连续 N 次）"。
            item {
                Column(Modifier.padding(horizontal = 14.dp)) {
                    WarningStrip(text = "⚠ $sensorWarning", severity = Severity.WARNING)
                }
            }
        }

        if (serverStaleWarning != null) {
            // 协议 §4.1 第四条：服务端说"数据已过期"就提示（与本机请求失败是两件事）。
            item {
                Column(Modifier.padding(horizontal = 14.dp)) {
                    WarningStrip(text = "⚠ $serverStaleWarning", severity = Severity.WARNING)
                }
            }
        }

        if (state.silenceFeedback != null) {
            item {
                Column(Modifier.padding(horizontal = 14.dp)) {
                    Column {
                        InfoStrip(text = state.silenceFeedback, severity = Severity.INFO)
                        TextButton(onClick = onDismissSilenceFeedback) { Text("知道了") }
                    }
                }
            }
        }

        item {
            Column(Modifier.padding(horizontal = 14.dp)) {
                CardGrid(cards = cards)
            }
        }

        item {
            Column(Modifier.padding(horizontal = 14.dp)) {
                Row(
                    Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Text(
                        text = "快照时间：${Formatters.timestamp(state.snapshot.ts)}",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                    Button(onClick = onSilence) { Text("消音（不解除报警）") }
                }
            }
        }

        if (state.pollingStopped) {
            item {
                Column(Modifier.padding(horizontal = 14.dp)) {
                    Column {
                        InfoStrip(
                            text = "已连续 ${state.consecutiveFailures} 次连接失败，自动暂停刷新以省电。",
                            severity = Severity.WARNING,
                        )
                        TextButton(onClick = onRefresh) { Text("重新开始刷新") }
                    }
                }
            }
        }

        item { Spacer(Modifier.height(16.dp)) }
    }
}

/** 五张卡片：两列排布，落单的那张（活动状态）通栏。 */
@Composable
private fun CardGrid(cards: List<DashboardCard>, modifier: Modifier = Modifier) {
    Column(modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(10.dp)) {
        var index = 0
        while (index < cards.size) {
            val left = cards[index]
            val right = cards.getOrNull(index + 1)
            if (right == null) {
                CardView(left, Modifier.fillMaxWidth())
            } else {
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                    CardView(left, Modifier.weight(1f))
                    CardView(right, Modifier.weight(1f))
                }
            }
            index += 2
        }
    }
}

@Composable
private fun CardView(card: DashboardCard, modifier: Modifier = Modifier) {
    MetricCard(
        title = card.title,
        value = card.value,
        modifier = modifier,
        unit = card.unit,
        subtitle = card.subtitle,
        statusText = card.statusText,
        statusSeverity = card.statusSeverity,
        alert = card.alert,
        hint = card.hint,
    )
}

/** 未连接时的引导块：告诉用户去哪儿填地址。 */
@Composable
fun EmptyConnectionNotice(onOpenSettings: () -> Unit, modifier: Modifier = Modifier) {
    Surface(
        modifier = modifier.fillMaxWidth(),
        shape = RoundedCornerShape(12.dp),
        color = MaterialTheme.colorScheme.surfaceVariant,
    ) {
        Column(Modifier.padding(16.dp)) {
            Text("还没有连接树莓派", fontWeight = FontWeight.SemiBold)
            Text(
                text = "请到「设置」页填写树莓派的局域网地址，例如 192.168.1.20:8080",
                style = MaterialTheme.typography.bodySmall,
            )
            TextButton(onClick = onOpenSettings) { Text("去设置") }
        }
    }
}

/** 给 LazyColumn 用的空列表占位（保持与其他页面风格一致）。 */
@Composable
fun NoMetricData(modifier: Modifier = Modifier) {
    Text(
        text = "暂无数据：${Formatters.UNKNOWN} 表示树莓派还没上报该项",
        modifier = modifier,
        style = MaterialTheme.typography.bodySmall,
        color = MaterialTheme.colorScheme.onSurfaceVariant,
    )
}
