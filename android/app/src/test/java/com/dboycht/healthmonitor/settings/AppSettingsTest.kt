package com.dboycht.healthmonitor.settings

import com.dboycht.healthmonitor.data.UrlNormalizer
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
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

    // ---- 云端（OneNET）只读参数：**可选**功能，密钥由用户手填、不进 APK ----

    @Test
    fun `云端三项都填了才算启用`() {
        assertFalse("默认没配", AppSettings().cloudConfigured)
        assertFalse("只填产品 ID 不算启用", AppSettings(cloudProductId = "p").cloudConfigured)
        assertFalse(
            "缺密钥不算启用",
            AppSettings(cloudProductId = "p", cloudDeviceName = "d").cloudConfigured,
        )
        assertTrue(
            "三项齐全才启用",
            AppSettings(cloudProductId = "p", cloudDeviceName = "d", cloudAccessKey = "k")
                .cloudConfigured,
        )
    }

    @Test
    fun `云端资源串按平台约定拼装`() {
        val settings = AppSettings(cloudProductId = " vmkgy5EP2t ", cloudDeviceName = " t1 ")
        assertEquals("products/vmkgy5EP2t/devices/t1", settings.cloudResource)
    }

    @Test
    fun `保存云端参数会去掉首尾空白并持久化`() = runTest {
        val repository = InMemorySettingsRepository()
        val saved = repository.saveCloud("  pid  ", "  dev  ", "  key=  ")
        assertTrue(saved.cloudConfigured)
        assertEquals("pid", saved.cloudProductId)
        assertEquals("dev", saved.cloudDeviceName)
        assertEquals("key=", saved.cloudAccessKey)
        assertEquals("pid", repository.current().cloudProductId)
    }

    @Test
    fun `保存云端参数不会动地址与 token`() = runTest {
        val repository = InMemorySettingsRepository()
        repository.save("192.168.1.50", "tok")
        repository.saveCloud("pid", "dev", "key")

        val after = repository.current()
        assertEquals("改云端不能动地址", "http://192.168.1.50:8080", after.baseUrl)
        assertEquals("改云端不能动 token", "tok", after.token)
    }

    @Test
    fun `改地址与 token 不会清掉云端参数`() = runTest {
        // 这条是"两次保存互相不干扰"的另一半：用户改一次树莓派地址，
        // 不该顺手把云端密钥抹掉（那会让人以为"配置自己丢了"）。
        val repository = InMemorySettingsRepository()
        repository.saveCloud("pid", "dev", "key")
        repository.save("192.168.1.50", "tok")

        val after = repository.current()
        assertTrue("云端参数必须还在", after.cloudConfigured)
        assertEquals("key", after.cloudAccessKey)
    }

    @Test
    fun `三项都清空等于关掉云端`() = runTest {
        val repository = InMemorySettingsRepository()
        repository.saveCloud("pid", "dev", "key")
        val cleared = repository.saveCloud("", "", "")
        assertFalse("清空后不再访问云端", cleared.cloudConfigured)
    }
}
