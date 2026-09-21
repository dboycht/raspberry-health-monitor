package com.dboycht.healthmonitor.data

import android.content.Context
import com.dboycht.healthmonitor.settings.AppSettings
import com.dboycht.healthmonitor.settings.DataStoreSettingsRepository
import com.dboycht.healthmonitor.settings.SettingsRepository

/**
 * 极简依赖容器（手写注入，不引 Hilt）。
 *
 * 课设规模用不上 DI 框架：一个 App 级的容器，把"设置仓库"和"网络仓库"装配好，
 * 由 MainActivity 取出来交给各 ViewModel 即可，读起来一目了然。
 */
class AppContainer(context: Context) {

    private val appContext: Context = context.applicationContext

    /** 设置：DataStore 持久化。 */
    val settingsRepository: SettingsRepository = DataStoreSettingsRepository(appContext)

    /**
     * 数据仓库：每次请求都从设置里现取地址与 token，所以用户改完地址
     * 下一次轮询就生效。
     */
    val monitorRepository: MonitorRepository = RetrofitMonitorRepository(
        settingsProvider = { settingsRepository.current() },
    )

    /** 当前设置（含归一化地址），供界面显示。 */
    suspend fun currentSettings(): AppSettings = settingsRepository.current()
}
