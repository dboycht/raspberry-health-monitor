package com.dboycht.healthmonitor.data

import okhttp3.HttpUrl.Companion.toHttpUrl
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 测试组⑤：**地址归一化**（用户在设置页里怎么填都能用）。
 *
 * 规则来自协议 §1（地址形如 `http://<树莓派局域网IP>:8080`）：
 *  1. 没写 scheme → 补 `http://`（局域网直连树莓派没有证书，https 不现实）；
 *  2. 没写端口且用 http → 补 `:8080`（服务默认端口）；
 *  3. 已经带了 scheme / 端口 → **原样不动**，只保证结尾有 `/`
 *     （Retrofit 的 baseUrl 必须以 `/` 结尾，否则会把路径最后一段吃掉）。
 */
class UrlNormalizerTest {

    @Test
    fun `只填 IP 时补成 http 与默认端口 8080`() {
        assertEquals("http://192.168.1.20:8080/", UrlNormalizer.normalizeOrNull("192.168.1.20"))
        // 归一化结果必须以 '/' 结尾（Retrofit baseUrl 的硬要求）。
        assertTrue(UrlNormalizer.normalizeOrNull("192.168.1.20")!!.endsWith("/"))
        // 主机名也一样（树莓派常配 pi.local / raspberrypi.local）。
        assertEquals("http://pi.local:8080/", UrlNormalizer.normalizeOrNull("pi.local"))
        assertEquals("http://raspberrypi:8080/", UrlNormalizer.normalizeOrNull("raspberrypi"))
        // 带连字符的主机名不能被当成非法字符。
        assertEquals("http://my-pi.local:8080/", UrlNormalizer.normalizeOrNull("my-pi.local"))
    }

    @Test
    fun `只填 IP 与端口时补 scheme_端口保持用户写的值`() {
        assertEquals("http://192.168.1.20:8080/", UrlNormalizer.normalizeOrNull("192.168.1.20:8080"))
        // 用户改了端口（例如演示时用 9000），不能被我们改回 8080。
        assertEquals("http://192.168.1.20:9000/", UrlNormalizer.normalizeOrNull("192.168.1.20:9000"))
        assertEquals("http://10.0.0.5:80/", UrlNormalizer.normalizeOrNull("10.0.0.5:80"))
    }

    @Test
    fun `已带 http scheme 的地址不动_只补结尾斜杠`() {
        assertEquals("http://192.168.1.20:8080/", UrlNormalizer.normalizeOrNull("http://192.168.1.20:8080"))
        assertEquals("http://192.168.1.20:8080/", UrlNormalizer.normalizeOrNull("http://192.168.1.20:8080/"))
        // 明确写了 http 但没写端口 → 仍然按默认端口补 8080。
        assertEquals("http://192.168.1.20:8080/", UrlNormalizer.normalizeOrNull("http://192.168.1.20"))
    }

    @Test
    fun `https 地址保留 scheme 且不擅自加 8080`() {
        assertEquals("https://pi.example.com/", UrlNormalizer.normalizeOrNull("https://pi.example.com"))
        // 用户显式写了端口就尊重他（https 常见 8443）。
        assertEquals("https://pi.example.com:8443/", UrlNormalizer.normalizeOrNull("https://pi.example.com:8443"))
        // 大小写不敏感。
        assertEquals("https://pi.example.com/", UrlNormalizer.normalizeOrNull("HTTPS://pi.example.com"))
    }

    @Test
    fun `前后空白被去掉_粘贴的地址也能用`() {
        assertEquals("http://192.168.1.20:8080/", UrlNormalizer.normalizeOrNull("  192.168.1.20 "))
        assertEquals("http://192.168.1.20:8080/", UrlNormalizer.normalizeOrNull("\t192.168.1.20\n"))
    }

    @Test
    fun `粘贴成完整接口地址时只保留主机与端口`() {
        // 用户可能直接从浏览器里把 /api/v1/current 一起复制过来。
        assertEquals("http://192.168.1.20:8080/", UrlNormalizer.normalizeOrNull("http://192.168.1.20:8080/api/v1/current"))
        assertEquals("http://192.168.1.20:8080/", UrlNormalizer.normalizeOrNull("192.168.1.20:8080/api/v1/alarms?limit=50"))
    }

    @Test
    fun `非法输入返回 null_由界面给出提示`() {
        assertNull("空串", UrlNormalizer.normalizeOrNull(""))
        assertNull("null", UrlNormalizer.normalizeOrNull(null))
        assertNull("只有空白", UrlNormalizer.normalizeOrNull("   "))
        assertNull("只有 scheme", UrlNormalizer.normalizeOrNull("http://"))
        assertNull("只有冒号", UrlNormalizer.normalizeOrNull("192.168.1.20:"))
        assertNull("端口不是数字", UrlNormalizer.normalizeOrNull("192.168.1.20:abc"))
        assertNull("不支持的协议", UrlNormalizer.normalizeOrNull("ftp://192.168.1.20"))
        assertNull("中间有空格", UrlNormalizer.normalizeOrNull("192.168.1.20 8080"))
        assertNull("中文主机名", UrlNormalizer.normalizeOrNull("树莓派"))
        assertNull("带路径斜杠但没有主机", UrlNormalizer.normalizeOrNull("/api/v1"))
    }

    @Test
    fun `非法输入时可以用 fallback 兜住上次保存的地址`() {
        assertEquals(
            "http://192.168.1.20:8080/",
            UrlNormalizer.normalizeOrNull("坏地址 ", "192.168.1.20"),
        )
        // fallback 也非法 → 还是 null，不编造地址。
        assertNull(UrlNormalizer.normalizeOrNull("", "也是坏地址 ！！！"))
    }

    @Test
    fun `归一化结果是幂等的_存过一次再存不变形`() {
        // 这个属性很重要：AppSettings.save() 保存的是归一化后的值，
        // 下次再读到它做归一化时结果必须完全一样，否则会在地址上越滚越多尾巴。
        val first = UrlNormalizer.normalizeOrNull("192.168.1.20")!!
        val second = UrlNormalizer.normalizeOrNull(first)!!
        val third = UrlNormalizer.normalizeOrNull(second)!!
        assertEquals(first, second)
        assertEquals(second, third)
        assertEquals("http://192.168.1.20:8080/", third)
    }

    @Test
    fun `归一化后的地址可以被 Retrofit 解析`() {
        // 用 OkHttp 的 HttpUrl 真解析一次，确认不是"看起来对但 Retrofit 用不了"。
        val normalized = UrlNormalizer.normalizeOrNull("192.168.1.20")!!
        val url = normalized.toHttpUrl()
        assertEquals("http", url.scheme)
        assertEquals("192.168.1.20", url.host)
        assertEquals(8080, url.port)
        // 相对路径拼接后必须落在正确位置（这就是要求结尾带 '/' 的原因）。
        val resolved = url.resolve("api/v1/current")!!
        assertEquals("http://192.168.1.20:8080/api/v1/current", resolved.toString())
    }

    @Test
    fun `无 scheme 的 https 风格的域名不会误判`() {
        // 用户只写域名时补 http（不是 https），并且不保留 443。
        val normalized = UrlNormalizer.normalizeOrNull("pi.example.com")!!
        assertEquals("http://pi.example.com:8080/", normalized)
        assertFalse(normalized.startsWith("https"))
    }
}
