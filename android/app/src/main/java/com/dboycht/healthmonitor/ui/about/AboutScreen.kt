package com.dboycht.healthmonitor.ui.about

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.item
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
import com.dboycht.healthmonitor.BuildConfig
import com.dboycht.healthmonitor.domain.DeviceInfo
import com.dboycht.healthmonitor.domain.Formatters
import com.dboycht.healthmonitor.domain.Severity
import com.dboycht.healthmonitor.domain.SystemHealth
import com.dboycht.healthmonitor.ui.component.ConnectionBar
import com.dboycht.healthmonitor.ui.component.InfoStrip
import com.dboycht.healthmonitor.ui.component.SeverityBadge

/**
 * 关于 / 硬件页：显示 `/api/v1/health` 的版本号与设备状态、`/api/v1/devices` 的接线说明。
 *
 * 这一页也是答辩时的"系统自述"：一眼能看到树莓派版本、跑了多久、哪个传感器异常、
 * 每个器件接在哪个物理脚上。
 */
@Composable
fun AboutScreen(
    state: AboutUiState,
    onRefresh: () -> Unit,
    modifier: Modifier = Modifier,
) {
    LazyColumn(
        modifier = modifier.fillMaxSize(),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        item {
            ConnectionBar(
                status = state.connection,
                baseUrl = "系统信息来自 /api/v1/health",
                lastSuccessAgeSeconds = null,
                errorMessage = state.errorMessage,
                staleData = false,
            )
        }

        item {
            Column(Modifier.padding(horizontal = 14.dp)) {
                SectionCard(title = "树莓派服务") {
                    InfoRow("服务版本", state.serverVersion ?: Formatters.UNKNOWN)
                    InfoRow("运行时长", Formatters.duration(state.health.uptimeSeconds))
                    InfoRow(
                        "模拟模式",
                        when (state.health.mock) {
                            true -> "是（未接真实传感器，数据是模拟的）"
                            false -> "否（使用真实传感器）"
                            null -> Formatters.UNKNOWN
                        },
                    )
                    InfoRow("最近心跳", Formatters.timestamp(state.health.ts))
                    InfoRow(
                        "报警输出通道",
                        state.health.dispatcherOutputs
                            .takeIf { it.isNotEmpty() }
                            ?.joinToString("、")
                            ?: Formatters.UNKNOWN,
                    )
                    InfoRow(
                        "已下发报警次数",
                        Formatters.formatInt(state.health.dispatcherDispatched),
                    )
                    if (state.health.dispatcherEnabled == false) {
                        Text(
                            text = "⚠ 树莓派上的报警分发器当前是关闭的",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.error,
                        )
                    }
                }
            }
        }

        if (state.health.activeAlarms.isNotEmpty()) {
            item {
                Column(Modifier.padding(horizontal = 14.dp)) {
                    InfoStrip(
                        text = "当前报警：" + (
                            Formatters.activeAlarmSummary(state.health.activeAlarms)
                                ?: Formatters.UNKNOWN
                            ),
                        severity = state.health.activeAlarms.keys
                            .maxOfOrNull { com.dboycht.healthmonitor.domain.AlarmCatalog.defaultSeverity(it) }
                            ?: Severity.WARNING,
                    )
                }
            }
        }

        item {
            Column(Modifier.padding(horizontal = 14.dp)) {
                SectionCard(title = "设备状态（${state.health.devices.size} 个）") {
                    if (state.health.devices.isEmpty()) {
                        Text(
                            text = "还没读到设备状态；点上面的连接条重试。",
                            style = MaterialTheme.typography.bodySmall,
                        )
                    } else {
                        state.health.devices.forEach { device ->
                            Row(
                                Modifier
                                    .fillMaxWidth()
                                    .padding(vertical = 6.dp),
                                horizontalArrangement = Arrangement.SpaceBetween,
                                verticalAlignment = Alignment.CenterVertically,
                            ) {
                                Column(Modifier.fillMaxWidth(0.65f)) {
                                    Text(
                                        text = device.name,
                                        style = MaterialTheme.typography.bodyMedium,
                                        fontWeight = FontWeight.Medium,
                                    )
                                    Text(
                                        text = "驱动 ${device.driver ?: Formatters.UNKNOWN} · 读取 " +
                                            "${Formatters.formatInt(device.reads)} 次 · 失败 " +
                                            "${Formatters.formatInt(device.failures)} 次",
                                        style = MaterialTheme.typography.bodySmall,
                                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                                    )
                                    if (!device.lastError.isNullOrBlank()) {
                                        Text(
                                            text = "最近错误：${device.lastError}",
                                            style = MaterialTheme.typography.bodySmall,
                                            color = MaterialTheme.colorScheme.error,
                                        )
                                    }
                                }
                                SeverityBadge(
                                    text = device.statusText,
                                    severity = if (device.statusText == "正常") Severity.NORMAL else Severity.WARNING,
                                )
                            }
                        }
                    }
                }
            }
        }

        item {
            Column(Modifier.padding(horizontal = 14.dp)) {
                SectionCard(title = "接线说明（${state.devices.size} 个器件）") {
                    if (state.devices.isEmpty()) {
                        Text(
                            text = "还没读到接线信息（/api/v1/devices）。",
                            style = MaterialTheme.typography.bodySmall,
                        )
                    } else {
                        state.devices.forEach { info ->
                            DeviceWiring(info)
                            HorizontalDivider(Modifier.padding(vertical = 6.dp))
                        }
                    }
                }
            }
        }

        item {
            Column(Modifier.padding(horizontal = 14.dp)) {
                SectionCard(title = "手机端") {
                    InfoRow("App 版本", BuildConfig.VERSION_NAME)
                    InfoRow("包名", BuildConfig.APPLICATION_ID)
                    InfoRow("轮询周期", "当前读数 4 秒 / 报警 5 秒 / 健康 10 秒")
                    InfoRow("协议依据", "docs/05-安卓通信协议.md")
                    Text(
                        text = "数值显示规则：接口返回 null 一律显示 ${Formatters.UNKNOWN}，" +
                            "绝不显示 0；finger_detected=false 时心率/血氧显示「${Formatters.PLACE_FINGER}」。",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
        }

        item {
            TextButton(onClick = onRefresh, modifier = Modifier.padding(horizontal = 14.dp)) {
                Text("重新读取树莓派信息")
            }
        }
    }
}

@Composable
private fun DeviceWiring(info: DeviceInfo) {
    Column(Modifier.fillMaxWidth()) {
        Row(
            Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                text = info.name,
                style = MaterialTheme.typography.bodyMedium,
                fontWeight = FontWeight.SemiBold,
            )
            if (info.mock == true) {
                SeverityBadge(text = "模拟", severity = Severity.INFO)
            }
        }
        Text(
            text = "驱动 ${info.driver ?: Formatters.UNKNOWN} · 总线 ${info.bus ?: Formatters.UNKNOWN}" +
                (info.kind?.let { " · 类型 $it" } ?: ""),
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        if (info.pins.isNotEmpty()) {
            info.pins.forEach { (pinName, pinDesc) ->
                Text(
                    text = "• $pinName → $pinDesc",
                    style = MaterialTheme.typography.bodySmall,
                )
            }
        }
        if (!info.notes.isNullOrBlank()) {
            Text(
                text = "备注：${info.notes}",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
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
                text = title,
                style = MaterialTheme.typography.titleMedium,
                fontWeight = FontWeight.SemiBold,
            )
            content()
        }
    }
}

@Composable
private fun InfoRow(label: String, value: String) {
    Row(
        Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.SpaceBetween,
    ) {
        Text(
            text = label,
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Text(
            text = value,
            style = MaterialTheme.typography.bodyMedium,
            fontWeight = FontWeight.Medium,
        )
    }
}

/** 供预览/测试构造用：空状态。 */
val EmptyAboutState: AboutUiState = AboutUiState(health = SystemHealth.EMPTY)
