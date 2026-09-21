package com.dboycht.healthmonitor.ui.settings

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.dboycht.healthmonitor.data.UrlNormalizer
import com.dboycht.healthmonitor.domain.Formatters
import com.dboycht.healthmonitor.domain.Severity
import com.dboycht.healthmonitor.ui.component.InfoStrip

/**
 * 设置页：树莓派地址 + 可选 token，保存后持久化到 DataStore。
 *
 * 交互上的两个贴心点：
 *  - **实时预览归一化结果**：用户只填 `192.168.1.20`，下面立刻显示
 *    "将连接 http://192.168.1.20:8080/"，省掉"为什么连不上"的排查；
 *  - **测试连接**用当前输入框里的地址打一次 `/health`，不改已保存的设置。
 */
@Composable
fun SettingsScreen(
    state: SettingsUiState,
    onBaseUrlChange: (String) -> Unit,
    onTokenChange: (String) -> Unit,
    onSave: () -> Unit,
    onTest: () -> Unit,
    onOpenAbout: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Column(
        modifier = modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(14.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Card(elevation = CardDefaults.cardElevation(defaultElevation = 1.dp)) {
            Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                Text("连接设置", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)

                OutlinedTextField(
                    value = state.baseUrl,
                    onValueChange = onBaseUrlChange,
                    modifier = Modifier.fillMaxWidth(),
                    label = { Text("树莓派地址") },
                    placeholder = { Text("192.168.1.20:8080") },
                    singleLine = true,
                    isError = state.baseUrl.isNotBlank() && !state.addressValid,
                    supportingText = {
                        Text(
                            text = when {
                                state.baseUrl.isBlank() -> "在树莓派上执行 hostname -I 可以查到 IP"
                                !state.addressValid -> UrlNormalizer.INVALID_MESSAGE
                                state.normalizedPreview != null -> "将连接 ${state.normalizedPreview}"
                                else -> ""
                            },
                        )
                    },
                )

                OutlinedTextField(
                    value = state.token,
                    onValueChange = onTokenChange,
                    modifier = Modifier.fillMaxWidth(),
                    label = { Text("Token（可选）") },
                    placeholder = { Text("服务器用 --token 启动时才需要") },
                    singleLine = true,
                    supportingText = {
                        Text("留空表示无鉴权；填了会作为 X-Auth-Token 请求头发送")
                    },
                )

                Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                    Button(onClick = onSave, enabled = state.addressValid) { Text("保存") }
                    OutlinedButton(onClick = onTest, enabled = !state.testing && state.addressValid) {
                        Text(if (state.testing) "连接中…" else "测试连接")
                    }
                }

                if (state.savedMessage != null) {
                    InfoStrip(
                        text = state.savedMessage,
                        severity = if (state.saved) Severity.NORMAL else Severity.CRITICAL,
                    )
                }
                if (state.testResult != null) {
                    InfoStrip(
                        text = state.testResult,
                        severity = if (state.testOk) Severity.NORMAL else Severity.CRITICAL,
                    )
                }
            }
        }

        Card(elevation = CardDefaults.cardElevation(defaultElevation = 1.dp)) {
            Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                Text("怎么找到树莓派地址", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
                Text(
                    text = "1) 树莓派开机并连上家里 WiFi；",
                    style = MaterialTheme.typography.bodyMedium,
                )
                Text(
                    text = "2) 在树莓派终端执行 hostname -I，记下 192.168.x.x；",
                    style = MaterialTheme.typography.bodyMedium,
                )
                Text(
                    text = "3) 确认服务已启动：health_monitor serve --port 8080；",
                    style = MaterialTheme.typography.bodyMedium,
                )
                Text(
                    text = "4) 手机连**同一个** WiFi（不能是流量），填地址后点「测试连接」。",
                    style = MaterialTheme.typography.bodyMedium,
                )
                Text(
                    text = "提示：地址可以只填 IP，App 会自动补成 http://<IP>:8080。",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }

        Card(elevation = CardDefaults.cardElevation(defaultElevation = 1.dp)) {
            Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                Text("数据来源说明", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
                Text(
                    text = "所有数值都来自树莓派 /api/v1/*；接口返回 null 时显示 " +
                        "${Formatters.UNKNOWN}（未知），不会显示 0。",
                    style = MaterialTheme.typography.bodyMedium,
                )
                Text(
                    text = "消音只让树莓派不响铃/不播报，报警状态不会解除。",
                    style = MaterialTheme.typography.bodyMedium,
                )
                OutlinedButton(onClick = onOpenAbout) { Text("查看硬件与版本信息") }
            }
        }
    }
}
