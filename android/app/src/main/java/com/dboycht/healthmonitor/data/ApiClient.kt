package com.dboycht.healthmonitor.data

import okhttp3.OkHttpClient
import retrofit2.Retrofit
import retrofit2.converter.kotlinx.serialization.asConverterFactory
import java.util.concurrent.TimeUnit
import kotlinx.serialization.json.Json
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.logging.HttpLoggingInterceptor

/**
 * 网络客户端：Retrofit + OkHttp + kotlinx.serialization 转换器。
 *
 * **超时取 8 秒**是刻意的：接口在局域网里，正常响应是毫秒级；8 秒还没回来
 * 基本就是"手机没连上家里 WiFi"或"树莓派服务没跑"，早点失败才能早点把
 * "连接中…"的状态反馈给用户（协议 §5.2：请求失败不要弹崩溃对话框）。
 */
object ApiClient {

    /** 局域网请求超时（秒）。 */
    const val TIMEOUT_SECONDS: Long = 8L

    fun create(
        baseUrl: String,
        json: Json = HealthJson,
        httpClient: OkHttpClient = okHttpClient(),
    ): MonitorApi {
        // Retrofit 要求 baseUrl 必须以 '/' 结尾（UrlNormalizer 已经保证）。
        val normalized = if (baseUrl.endsWith("/")) baseUrl else "$baseUrl/"
        return Retrofit.Builder()
            .baseUrl(normalized)
            .client(httpClient)
            .addConverterFactory(json.asConverterFactory("application/json".toMediaType()))
            .build()
            .create(MonitorApi::class.java)
    }

    /** 默认 OkHttp 客户端。单元测试会自己 new 一个假的，不碰这里。 */
    fun okHttpClient(enableLogging: Boolean = false): OkHttpClient {
        val builder = OkHttpClient.Builder()
            .connectTimeout(TIMEOUT_SECONDS, TimeUnit.SECONDS)
            .readTimeout(TIMEOUT_SECONDS, TimeUnit.SECONDS)
            .writeTimeout(TIMEOUT_SECONDS, TimeUnit.SECONDS)
            .retryOnConnectionFailure(true)
        if (enableLogging) {
            builder.addInterceptor(
                HttpLoggingInterceptor().apply { level = HttpLoggingInterceptor.Level.BASIC },
            )
        }
        return builder.build()
    }
}
