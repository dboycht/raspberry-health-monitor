package com.dboycht.healthmonitor.ui.component

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
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
import com.dboycht.healthmonitor.domain.Severity

/**
 * 严重度角标：颜色**只能**来自 [Severity]，不要在别处写死颜色，
 * 否则同一报警在不同页面会变色。
 */
@Composable
fun SeverityBadge(
    text: String,
    severity: Int,
    modifier: Modifier = Modifier,
) {
    Row(
        modifier = modifier
            .background(
                color = Color(Severity.containerColorArgb(severity)),
                shape = RoundedCornerShape(6.dp),
            )
            .padding(horizontal = 8.dp, vertical = 3.dp),
        horizontalArrangement = Arrangement.Center,
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            text = text,
            style = MaterialTheme.typography.labelSmall,
            fontWeight = FontWeight.SemiBold,
            color = Color(Severity.colorArgb(severity)),
        )
    }
}

/** 用严重度数字直接渲染的角标（列表里省事）。 */
@Composable
fun SeverityLevelBadge(severity: Int, modifier: Modifier = Modifier) {
    SeverityBadge(text = Severity.label(severity), severity = severity, modifier = modifier)
}

/** 一行小提示（例如"传感器异常"）。 */
@Composable
fun WarningStrip(text: String, severity: Int = Severity.WARNING, modifier: Modifier = Modifier) {
    Row(
        modifier = modifier
            .fillMaxWidth()
            .background(Color(Severity.containerColorArgb(severity)), RoundedCornerShape(8.dp))
            .padding(horizontal = 12.dp, vertical = 8.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            text = text,
            style = MaterialTheme.typography.bodySmall,
            color = Color(Severity.colorArgb(severity)),
            fontWeight = FontWeight.Medium,
        )
    }
}
