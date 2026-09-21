package com.dboycht.healthmonitor

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import com.dboycht.healthmonitor.data.AppContainer
import com.dboycht.healthmonitor.ui.about.AboutScreen
import com.dboycht.healthmonitor.ui.about.AboutViewModel
import com.dboycht.healthmonitor.ui.alarms.AlarmsScreen
import com.dboycht.healthmonitor.ui.alarms.AlarmsViewModel
import com.dboycht.healthmonitor.ui.dashboard.DashboardScreen
import com.dboycht.healthmonitor.ui.dashboard.DashboardViewModel
import com.dboycht.healthmonitor.ui.settings.SettingsScreen
import com.dboycht.healthmonitor.ui.settings.SettingsViewModel
import com.dboycht.healthmonitor.ui.theme.HealthMonitorTheme

/**
 * 唯一的 Activity：四个页面用底部导航切换。
 *
 * 为什么不用 Navigation-Compose 的 NavHost：本 App 只有 4 个互不传参的顶层页面，
 * 用 `rememberSaveable` 记一个下标更简单、也更好讲；深链接/参数传递在课设里用不上。
 * （少一个依赖也少一个版本风险。）
 *
 * 生命周期（协议 §5.1 的硬要求）：轮询都包在
 * `lifecycle.repeatOnLifecycle(STARTED)` 里，**App 一进后台（onStop）循环立即取消**；
 * 切到别的页面也一样，只有当前页在轮询。
 */
class MainActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val container = (application as HealthMonitorApp).container
        setContent {
            HealthMonitorTheme {
                MonitorApp(container = container)
            }
        }
    }
}

/** 底部导航的四个页面。符号当图标用：不引 material-icons-extended，APK 能小几十 MB。 */
private enum class MonitorTab(val title: String, val symbol: String) {
    DASHBOARD("监护", "♥"),
    ALARMS("报警", "⚠"),
    ABOUT("关于", "ⓘ"),
    SETTINGS("设置", "⚙"),
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun MonitorApp(container: AppContainer) {
    var tab by rememberSaveable { mutableStateOf(MonitorTab.DASHBOARD) }
    val lifecycle = LocalLifecycleOwner.current.lifecycle

    val dashboardViewModel: DashboardViewModel = viewModel(factory = DashboardViewModel.factory(container))
    val alarmsViewModel: AlarmsViewModel = viewModel(factory = AlarmsViewModel.factory(container))
    val aboutViewModel: AboutViewModel = viewModel(factory = AboutViewModel.factory(container))
    val settingsViewModel: SettingsViewModel = viewModel(factory = SettingsViewModel.factory(container))

    val dashboardState by dashboardViewModel.uiState.collectAsStateWithLifecycle()
    val alarmsState by alarmsViewModel.uiState.collectAsStateWithLifecycle()
    val aboutState by aboutViewModel.uiState.collectAsStateWithLifecycle()
    val settingsState by settingsViewModel.uiState.collectAsStateWithLifecycle()

    // 当前页面的轮询：进入页面时启动；离开页面或 App 进后台（onStop）时循环结束。
    LaunchedEffect(tab) {
        try {
            when (tab) {
                MonitorTab.DASHBOARD -> dashboardViewModel.startPolling(lifecycle)
                MonitorTab.ALARMS -> alarmsViewModel.startPolling(lifecycle)
                MonitorTab.ABOUT -> aboutViewModel.startPolling(lifecycle)
                // 设置页不轮询，只在进入时读一次已保存的值。
                MonitorTab.SETTINGS -> settingsViewModel.load()
            }
        } finally {
            // onStop 时显式停掉，协议 §5.1 要求"在 onStop() 里停掉轮询"。
            dashboardViewModel.stopPolling()
            alarmsViewModel.stopPolling()
            aboutViewModel.stopPolling()
        }
    }

    // 首页需要知道"当前连的是哪个地址"，用来显示在连接状态条上。
    LaunchedEffect(tab) {
        if (tab == MonitorTab.DASHBOARD) dashboardViewModel.loadInitialSettings()
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("居家老人监护 · ${tab.title}") },
                colors = TopAppBarDefaults.topAppBarColors(
                    containerColor = MaterialTheme.colorScheme.primary,
                    titleContentColor = Color.White,
                ),
                actions = {
                    if (tab == MonitorTab.DASHBOARD) {
                        TextButton(onClick = { dashboardViewModel.refreshNow() }) {
                            Text("刷新", color = Color.White)
                        }
                    }
                },
            )
        },
        bottomBar = {
            NavigationBar {
                MonitorTab.entries.forEach { item ->
                    NavigationBarItem(
                        selected = tab == item,
                        onClick = { tab = item },
                        // 不传 icon：NavigationBarItem 允许只有文字标签，
                        // 这样能省掉一整套图标资源（含 material-icons-extended 的几十 MB）。
                        label = {
                            Text(
                                text = "${item.symbol} ${item.title}",
                                fontWeight = FontWeight.Medium,
                            )
                        },
                    )
                }
            }
        },
    ) { padding ->
        Column(
            Modifier
                .fillMaxSize()
                .padding(padding),
        ) {
            when (tab) {
                MonitorTab.DASHBOARD -> DashboardScreen(
                    state = dashboardState,
                    onRefresh = { dashboardViewModel.refreshNow() },
                    onSilence = { dashboardViewModel.silence() },
                    onDismissSilenceFeedback = { dashboardViewModel.clearSilenceFeedback() },
                )

                MonitorTab.ALARMS -> AlarmsScreen(
                    state = alarmsState,
                    onRefresh = { alarmsViewModel.refreshNow() },
                    onSilence = { alarmsViewModel.silence() },
                    onSos = { alarmsViewModel.sendSos() },
                    onDismissFeedback = { alarmsViewModel.clearFeedback() },
                )

                MonitorTab.ABOUT -> AboutScreen(
                    state = aboutState,
                    onRefresh = { aboutViewModel.refreshNow() },
                )

                MonitorTab.SETTINGS -> SettingsScreen(
                    state = settingsState,
                    onBaseUrlChange = settingsViewModel::onBaseUrlChange,
                    onTokenChange = settingsViewModel::onTokenChange,
                    onSave = { settingsViewModel.save() },
                    onTest = { settingsViewModel.testConnection() },
                    onOpenAbout = { tab = MonitorTab.ABOUT },
                )
            }
        }
    }
}
