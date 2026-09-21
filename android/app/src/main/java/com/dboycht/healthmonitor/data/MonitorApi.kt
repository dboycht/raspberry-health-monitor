package com.dboycht.healthmonitor.data

import retrofit2.http.GET
import retrofit2.http.Header
import retrofit2.http.POST
import retrofit2.http.Query

/**
 * 树莓派 HTTP 接口的 Retrofit 定义（**逐一对应协议 §3 的接口一览**）。
 *
 * 通用约定（协议 §2）：
 *  - 不用请求体，参数一律走 query string；
 *  - 服务器配了 `--token` 时，每个请求都要带 `X-Auth-Token` 头，否则 401。
 *    token 是可选的（null 时不发这个头），所以每个方法都显式收一个 `token` 参数。
 *
 * 方法名后加 `Dto` 后缀是为了和 domain 层的模型区分开。
 */
interface MonitorApi {

    /** 系统健康 + 每个设备状态（协议 §4.2）。首屏先打这个确认"连上了、服务在跑"。 */
    @GET("api/v1/health")
    suspend fun health(@Header("X-Auth-Token") token: String? = null): HealthResponse

    /** 当前读数（协议 §4.1，首页主数据，3~5 秒轮询一次）。 */
    @GET("api/v1/current")
    suspend fun current(@Header("X-Auth-Token") token: String? = null): CurrentResponse

    /** 报警事件（协议 §4.4）。 */
    @GET("api/v1/alarms")
    suspend fun alarms(
        @Query("limit") limit: Int = 50,
        @Header("X-Auth-Token") token: String? = null,
    ): AlarmsResponse

    /** 设备清单与接线说明（协议 §4.5）。 */
    @GET("api/v1/devices")
    suspend fun devices(@Header("X-Auth-Token") token: String? = null): DevicesResponse

    /** 历史曲线（协议 §4.3）。 */
    @GET("api/v1/history")
    suspend fun history(
        @Query("metric") metric: String,
        @Query("limit") limit: Int = 120,
        @Query("since") since: Double? = null,
        @Header("X-Auth-Token") token: String? = null,
    ): HistoryResponse

    /** 消音（协议 §4.6）：只让树莓派别响，**不代表报警解除**。 */
    @POST("api/v1/silence")
    suspend fun silence(@Header("X-Auth-Token") token: String? = null): SilenceResponse

    /** 紧急求助（协议 §4.7）。 */
    @POST("api/v1/sos")
    suspend fun sos(@Header("X-Auth-Token") token: String? = null): SosResponse

    /** 让音箱说话（协议 §4.8，调试用；text 最多 60 字，超长服务器返回 400）。 */
    @POST("api/v1/speak")
    suspend fun speak(
        @Query("text") text: String,
        @Header("X-Auth-Token") token: String? = null,
    ): ApiEnvelope
}
