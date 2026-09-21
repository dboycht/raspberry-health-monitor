package com.dboycht.healthmonitor.ui.component

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.dboycht.healthmonitor.domain.AlarmCatalog
import com.dboycht.healthmonitor.domain.Formatters
import com.dboycht.healthmonitor.domain.Severity

/**
 * 当前报警横幅（协议 §4.1：`active_alarms` 非空时高亮）。
 *
 * 注意文案：**消音 ≠ 解除报警**（协议 §4.6）。横幅只描述"现在有哪些报警"，
 * 不会因为按了消音就消失。
 */
@Composable
fun AlarmBanner(
    activeAlarms: Map<String, Double>,
    modifier: Modifier = Modifier,
    silencedUntil: Double? = null,
    nowSeconds: Double = System.currentTimeMillis() / 1000.0,
) {
    val severity = activeAlarms.keys
        .maxOfOrNull { AlarmCatalog.defaultSeverity(it) }
        ?: Severity.NORMAL

    if (activeAlarms.isEmpty()) {
        Row(
            modifier = modifier
                .fillMaxWidth()
                .background(Color(Severity.containerColorArgb(Severity.NORMAL)), RoundedCornerShape(12.dp))
                .padding(horizontal = 14.dp, vertical = 12.dp),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                text = "当前无报警",
                style = MaterialTheme.typography.titleSmall,
                fontWeight = FontWeight.SemiBold,
                color = Color(Severity.colorArgb(Severity.NORMAL)),
            )
            Text(
                text = "一切正常",
                style = MaterialTheme.typography.bodySmall,
                color = Color(Severity.colorArgb(Severity.NORMAL)),
            )
        }
        return
    }

    Column(
        modifier = modifier
            .fillMaxWidth()
            .background(Color(Severity.containerColorArgb(severity)), RoundedCornerShape(12.dp))
            .padding(horizontal = 14.dp, vertical = 12.dp),
        verticalArrangement = Arrangement.spacedBy(4.dp),
    ) {
        Row(
            Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                text = "⚠ 正在报警",
                fontSize = 18.sp,
                fontWeight = FontWeight.Bold,
                color = Color(Severity.colorArgb(severity)),
            )
            SeverityLevelBadge(severity = severity)
        }

        activeAlarms.entries
            .sortedByDescending { AlarmCatalog.defaultSeverity(it.key) }
            .forEach { (code, since) ->
                Row(
                    Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                ) {
                    Text(
                        text = "• ${AlarmCatalog.label(code)}",
                        style = MaterialTheme.typography.bodyMedium,
                        fontWeight = FontWeight.Medium,
                        color = Color(Severity.colorArgb(AlarmCatalog.defaultSeverity(code))),
                    )
                    Text(
                        text = "自 ${Formatters.timestamp(since)}",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }

        val silenced = silencedUntil != null && silencedUntil > nowSeconds
        Text(
            text = if (silenced) {
                "已消音：树莓派只亮灯/更新显示，报警状态仍然存在"
            } else {
                "提示：按「消音」只让树莓派别响，不会解除报警"
            },
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}

/** 空态：暂时没有报警事件。 */
@Composable
fun EmptyAlarms(modifier: Modifier = Modifier) {
    Box(
        modifier = modifier
            .fillMaxWidth()
            .height(120.dp),
        contentAlignment = Alignment.Center,
    ) {
        Text(
            text = "暂无报警记录",
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}
