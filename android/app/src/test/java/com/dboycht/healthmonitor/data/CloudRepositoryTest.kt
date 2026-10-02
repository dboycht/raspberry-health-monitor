package com.dboycht.healthmonitor.data

import com.dboycht.healthmonitor.testing.JsonSamples
import kotlinx.coroutines.test.runTest
import okhttp3.Interceptor
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Protocol
import okhttp3.Request
import okhttp3.Response
import okhttp3.ResponseBody.Companion.toResponseBody
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.IOException

/**
 * 云端只读仓库：URL 拼装 / 签名头 / 真机报文解析 / 各条失败分支。
 *
 * 为什么不引 MockWebServer：本机 Gradle 是**离线**构建（依赖都在缓存里），
 * 新加一个二进制依赖就要联网下载 —— 而这里需要的只是"让 OkHttp 回一个我指定的响应"，
 * 一个**拦截器**就够了（顺带还能把真实发出的 `Request` 抓下来断言）。
 */
class CloudRepositoryTest {

    private val testKey = "cmFzcGJlcnJ5LWhlYWx0aC1tb25pdG9yLXRlc3Qta2V5"
    private val fixedNow = 1_790_868_623_000L   // 与真机样本里 motion 的时间戳同刻

    private class Captured {
        var request: Request? = null
    }

    private fun repository(
        httpCode: Int = 200,
        body: String,
        captured: Captured? = null,
        throwIo: Boolean = false,
    ): CloudRepository {
        val interceptor = Interceptor { chain ->
            val request = chain.request()
            captured?.request = request
            if (throwIo) throw IOException("模拟断网")
            Response.Builder()
                .request(request)
                .protocol(Protocol.HTTP_1_1)
                .code(httpCode)
                .message("test")
                .body(body.toResponseBody("application/json".toMediaType()))
                .build()
        }
        return HttpCloudRepository(
            http = OkHttpClient.Builder().addInterceptor(interceptor).build(),
            nowMillis = { fixedNow },
        )
    }

    @Test
    fun `成功路径_真机报文能映射成读数且请求带签名头`() = runTest {
        val captured = Captured()
        val repo = repository(body = JsonSamples.ONENET_HISTORY_REAL, captured = captured)

        val result = repo.loadLatest("vmkgy5EP2t", "t1", testKey)
        assertTrue("应成功：$result", result is MonitorApiResult.Success)
        val snapshot = (result as MonitorApiResult.Success).data

        // 请求侧：域名、查询参数、签名头（不是裸密钥！）
        val request = captured.request!!
        assertTrue("域名要对：${request.url}", request.url.toString().startsWith(
            "https://iot-api.heclouds.com/datapoint/history-datapoints"))
        assertEquals("vmkgy5EP2t", request.url.queryParameter("product_id"))
        assertEquals("t1", request.url.queryParameter("device_name"))
        val auth = request.header("authorization")!!
        assertTrue("authorization 必须是签名 token：$auth", auth.startsWith("version=2018-10-31&res="))
        assertTrue("token 里必须带设备级 res", auth.contains("products%2Fvmkgy5EP2t%2Fdevices%2Ft1"))
        assertTrue("密钥本身不能出现在请求头里", !auth.contains(testKey))

        // 响应侧：数据流映射（最近的在最前）
        assertEquals(18, snapshot.streamCount)
        assertEquals(
            listOf("motion", "version", "alarm_level", "alarm_value", "heart_rate"),
            snapshot.readings.map { it.id },
        )
        val hr = snapshot.readings.first { it.id == "heart_rate" }
        assertEquals("心率", hr.label)
        assertEquals("89 bpm", hr.valueText)
        val motion = snapshot.readings.first { it.id == "motion" }
        assertEquals("活动", motion.label)
        assertEquals("unknown", motion.valueText, )
    }

    @Test
    fun `业务码非零要当失败_并说清平台给的原因`() = runTest {
        // 真机上真的收到过这个：10417 = 该产品没有物模型
        val repo = repository(body = """{"code":10417,"msg":"查询当前属性失败:产品物模型未找到"}""")
        val result = repo.loadLatest("p", "d", testKey)
        assertTrue(result is MonitorApiResult.Failure)
        val message = (result as MonitorApiResult.Failure).message
        assertTrue("要说清是平台拒绝：$message", message.contains("云端拒绝"))
        assertTrue("要带上平台的 code 与 msg：$message", message.contains("10417"))
    }

    @Test
    fun `HTTP 非 200 要当失败`() = runTest {
        val repo = repository(httpCode = 401, body = """{"code":10403,"msg":"authentication failed"}""")
        val result = repo.loadLatest("p", "d", testKey)
        assertTrue(result is MonitorApiResult.Failure)
        assertTrue((result as MonitorApiResult.Failure).message.contains("HTTP 401"))
    }

    @Test
    fun `响应不是 JSON 时给人话而不是崩`() = runTest {
        val repo = repository(body = "<html>502 Bad Gateway</html>")
        val result = repo.loadLatest("p", "d", testKey)
        assertTrue(result is MonitorApiResult.Failure)
        assertTrue((result as MonitorApiResult.Failure).message.contains("解析失败"))
    }

    @Test
    fun `网络异常要当失败`() = runTest {
        val repo = repository(body = "", throwIo = true)
        val result = repo.loadLatest("p", "d", testKey)
        assertTrue(result is MonitorApiResult.Failure)
        assertTrue((result as MonitorApiResult.Failure).message.contains("连不上云端"))
    }

    @Test
    fun `参数不全就不发请求`() = runTest {
        val captured = Captured()
        val repo = repository(body = JsonSamples.ONENET_HISTORY_REAL, captured = captured)
        val result = repo.loadLatest("", "t1", testKey)
        assertTrue(result is MonitorApiResult.Failure)
        assertTrue((result as MonitorApiResult.Failure).message.contains("不完整"))
        assertTrue("参数不全时**不该**发请求", captured.request == null)
    }

    @Test
    fun `坏密钥给出 base64 提示且不发请求`() = runTest {
        val captured = Captured()
        val repo = repository(body = JsonSamples.ONENET_HISTORY_REAL, captured = captured)
        val result = repo.loadLatest("p", "d", "not-base64!!!")
        assertTrue(result is MonitorApiResult.Failure)
        assertTrue((result as MonitorApiResult.Failure).message.contains("base64"))
        assertTrue("密钥不可用就不该发请求", captured.request == null)
    }
}
