package com.dboycht.healthmonitor.ui.component

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.dboycht.healthmonitor.domain.ConnectionStatus
import com.dboycht.healthmonitor.domain.Formatters
import com.dboycht.healthmonitor.domain.Severity

/**
 * 顶部连接状态条。
 *
 * 协议 §5.2 的要求落在这里：请求失败**不弹框**，显示"连接中…/连接失败"，
 * 并明确标注"数据可能已过期（xx 秒前）"，同时保留上一次成功的数据。
 */
@Composable
fun ConnectionBar(
    status: ConnectionStatus,
    baseUrl: String,
    lastSuccessAgeSeconds: Double?,
    errorMessage: String?,
    staleData: Boolean,
    modifier: Modifier = Modifier,
) {
    val severity = when (status) {
        ConnectionStatus.CONNECTED -> if (staleData) Severity.WARNING else Severity.NORMAL
        ConnectionStatus.CONNECTING, ConnectionStatus.IDLE -> Severity.INFO
        ConnectionStatus.FAILED -> Severity.CRITICAL
    }
    val headline = when (status) {
        ConnectionStatus.CONNECTED -> if (staleData) "已连接（数据可能已过期）" else "已连接"
        ConnectionStatus.CONNECTING -> "连接中…"
        ConnectionStatus.IDLE -> "未连接"
        ConnectionStatus.FAILED -> "连接失败（显示的是上次数据）"
    }

    Column(
        modifier = modifier
            .fillMaxWidth()
            .background(Color(Severity.containerColorArgb(severity)))
            .padding(horizontal = 14.dp, vertical = 8.dp),
        verticalArrangement = Arrangement.spacedBy(2.dp),
    ) {
        Row(
            Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
        ) {
            Text(
                text = headline,
                style = MaterialTheme.typography.bodyMedium,
                fontWeight = FontWeight.SemiBold,
                color = Color(Severity.colorArgb(severity)),
            )
            Text(
                text = Formatters.ageLabel(lastSuccessAgeSeconds),
                style = MaterialTheme.typography.bodySmall,
                color = Color(Severity.colorArgb(severity)),
            )
        }
        Text(
            text = baseUrl,
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        if (!errorMessage.isNullOrBlank()) {
            Text(
                text = errorMessage,
                style = MaterialTheme.typography.bodySmall,
                color = Color(Severity.colorArgb(Severity.CRITICAL)),
            )
        }
    }
}

/** 一条圆角小提示条（保存成功、已发送求助等）。 */
@Composable
fun InfoStrip(text: String, severity: Int = Severity.NORMAL, modifier: Modifier = Modifier) {
    Row(
        modifier = modifier
            .fillMaxWidth()
            .background(Color(Severity.containerColorArgb(severity)), RoundedCornerShape(10.dp))
            .padding(horizontal = 12.dp, vertical = 10.dp),
    ) {
        Text(
            text = text,
            style = MaterialTheme.typography.bodyMedium,
            color = Color(Severity.colorArgb(severity)),
            fontWeight = FontWeight.Medium,
        )
    }
}
