package com.dboycht.healthmonitor.settings

import com.dboycht.healthmonitor.data.UrlNormalizer
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * 设置的持久化与归一化。
 *
 * 这里用 [InMemorySettingsRepository]（与 DataStore 实现同一接口）：
 * JVM 单测里没有 Android Context，不能真的建 DataStore 文件；
 * 真要验证"关掉 App 还在"，需要在真机上点一遍（见 README 的"未验证项"）。
 */
@OptIn(ExperimentalCoroutinesApi::class)
class AppSettingsTest {

    @Test
    fun `默认设置指向协议示例地址`() {
        val settings = AppSettings()
        assertEquals("http://192.168.1.20:8080", settings.baseUrl)
        assertEquals("http://192.168.1.20:8080/", settings.normalizedBaseUrl)
        assertNull("默认不带 token", settings.effectiveToken)
    }

    @Test
    fun `token 全是空白时视为没配`() {
        assertNull(AppSettings(token = "   ").effectiveToken)
        assertNull(AppSettings(token = "").effectiveToken)
        assertEquals("abc123", AppSettings(token = " abc123 ").effectiveToken)
    }

    @Test
    fun `保存时把用户输入的地址归一化`() = runTest {
        val repository = InMemorySettingsRepository()

        val saved = repository.save("192.168.1.20", " secret ")
        assertEquals("http://192.168.1.20:8080", saved.baseUrl)
        assertEquals("http://192.168.1.20:8080/", saved.normalizedBaseUrl)
        assertEquals("secret", saved.effectiveToken)

        // 真的读得到（持久化的行为）。
        val current = repository.current()
        assertEquals("http://192.168.1.20:8080", current.baseUrl)
        assertEquals("secret", current.token)
        assertEquals(current, repository.settings.first())
    }

    @Test
    fun `保存非法地址时退回默认地址_不会把 App 卡在连不上的状态`() = runTest {
        val repository = InMemorySettingsRepository()
        val saved = repository.save("这不是地址！！！", "t")
        assertEquals(AppSettings.DEFAULT_BASE_URL, saved.baseUrl)
        assertEquals(UrlNormalizer.normalizeOrNull(AppSettings.DEFAULT_BASE_URL), saved.normalizedBaseUrl)
    }

    @Test
    fun `已带端口或 scheme 的地址保存后不变形`() = runTest {
        val repository = InMemorySettingsRepository()
        assertEquals(
            "http://10.0.0.9:9000",
            repository.save("http://10.0.0.9:9000/", "").baseUrl,
        )
        // 再存一次还是同一个（幂等）。
        assertEquals(
            "http://10.0.0.9:9000",
            repository.save("http://10.0.0.9:9000", "").baseUrl,
        )
        assertEquals(
            "https://pi.example.com",
            repository.save("https://pi.example.com", "").baseUrl,
        )
    }

    @Test
    fun `非法地址时 normalizedBaseUrl 为 null_由界面提示去设置页`() {
        val broken = AppSettings(baseUrl = "http://")
        assertNull(broken.normalizedBaseUrl)
    }
}
