package com.dboycht.healthmonitor.data

/**
 * 请求结果的统一包装：**成功带数据，失败带一句人话**。
 *
 * 刻意不往上抛异常：协议 §5.2 要求"请求失败不要弹崩溃对话框，显示'连接中…'并保留
 * 上次数据"，所以网络异常、超时、401、非 2xx 都在这一层被翻译成
 * [Failure] + 中文提示，界面只负责显示。
 */
sealed interface MonitorApiResult<out T> {

    data class Success<T>(val data: T) : MonitorApiResult<T>

    data class Failure(val message: String, val cause: Throwable? = null) : MonitorApiResult<Nothing>

    val isSuccess: Boolean get() = this is Success
}

/** 把任意结果转成"成功的数据 or null"。 */
fun <T> MonitorApiResult<T>.dataOrNull(): T? = (this as? MonitorApiResult.Success)?.data

/** 失败时的人话提示。 */
fun <T> MonitorApiResult<T>.errorMessageOrNull(): String? = (this as? MonitorApiResult.Failure)?.message
