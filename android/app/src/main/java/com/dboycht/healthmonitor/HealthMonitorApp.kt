package com.dboycht.healthmonitor

import android.app.Application
import com.dboycht.healthmonitor.data.AppContainer

/**
 * 应用入口：只负责建一个全局依赖容器。
 *
 * 放在 Application 里而不是 Activity 里，是因为 MainActivity 旋转屏幕会重建，
 * 容器重建会白白丢掉 Retrofit/OkHttp 的连接池与缓存。
 */
class HealthMonitorApp : Application() {

    /** 全局依赖容器。 */
    val container: AppContainer by lazy { AppContainer(this) }
}
