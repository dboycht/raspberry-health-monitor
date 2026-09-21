package com.dboycht.healthmonitor.ui.component

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.dboycht.healthmonitor.domain.Severity

/**
 * 五个监护指标共用的卡片。
 *
 * 一条纪律：卡片**只负责排版**，所有数值都必须是调用方用
 * `domain/Formatters` 处理过的字符串——这样"null 显示为 -- 而不是 0"
 * 这条硬要求只有一个落点，不会被某个新加的卡片绕过。
 */
@Composable
fun MetricCard(
    title: String,
    value: String,
    modifier: Modifier = Modifier,
    unit: String? = null,
    subtitle: String? = null,
    statusText: String? = null,
    statusSeverity: Int = Severity.NORMAL,
    alert: Boolean = false,
    hint: String? = null,
) {
    Card(
        modifier = modifier,
        colors = CardDefaults.cardColors(
            containerColor = if (alert) {
                Color(Severity.containerColorArgb(statusSeverity))
            } else {
                MaterialTheme.colorScheme.surface
            },
        ),
        elevation = CardDefaults.cardElevation(defaultElevation = if (alert) 4.dp else 1.dp),
    ) {
        Column(Modifier.padding(14.dp)) {
            Row(
                Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(
                    text = title,
                    style = MaterialTheme.typography.titleSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                if (statusText != null) {
                    SeverityBadge(text = statusText, severity = statusSeverity)
                }
            }

            Row(
                Modifier.padding(top = 6.dp),
                verticalAlignment = Alignment.Bottom,
            ) {
                Text(
                    text = value,
                    fontSize = if (value.length > 8) 20.sp else 30.sp,
                    fontWeight = FontWeight.SemiBold,
                    color = if (alert) Color(Severity.colorArgb(statusSeverity)) else MaterialTheme.colorScheme.onSurface,
                )
                if (!unit.isNullOrEmpty() && value != com.dboycht.healthmonitor.domain.Formatters.UNKNOWN) {
                    Text(
                        text = unit,
                        modifier = Modifier.padding(start = 2.dp, bottom = 4.dp),
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }

            if (subtitle != null) {
                Text(
                    text = subtitle,
                    modifier = Modifier.padding(top = 2.dp),
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            if (hint != null) {
                Surface(
                    modifier = Modifier
                        .padding(top = 6.dp)
                        .fillMaxWidth()
                        .heightIn(min = 28.dp),
                    shape = RoundedCornerShape(6.dp),
                    color = Color(Severity.containerColorArgb(Severity.WARNING)),
                ) {
                    Text(
                        text = hint,
                        modifier = Modifier.padding(horizontal = 8.dp, vertical = 5.dp),
                        style = MaterialTheme.typography.bodySmall,
                        color = Color(Severity.colorArgb(Severity.WARNING)),
                    )
                }
            }
        }
    }
}

/** 没有数据时的占位块（例如还没连上树莓派）。 */
@Composable
fun PlaceholderCard(text: String, modifier: Modifier = Modifier) {
    Surface(
        modifier = modifier.fillMaxWidth().height(64.dp),
        shape = RoundedCornerShape(12.dp),
        color = MaterialTheme.colorScheme.surfaceVariant,
    ) {
        Column(
            Modifier.fillMaxWidth().padding(12.dp),
            verticalArrangement = Arrangement.Center,
        ) {
            Text(text = text, style = MaterialTheme.typography.bodyMedium)
        }
    }
}
