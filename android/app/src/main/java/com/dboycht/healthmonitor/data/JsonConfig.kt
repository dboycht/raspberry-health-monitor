package com.dboycht.healthmonitor.data

import kotlinx.serialization.json.Json

/**
 * 全局唯一的 [Json] 实例（Retrofit 转换器与单元测试共用同一个配置）。
 *
 * 为什么用 **kotlinx.serialization** 而不是 Gson：
 *
 * 1. **字段缺失必须映射成 null**。协议 §2 明确"数值缺失一律是 JSON null，绝不是 0"，
 *    而 Gson 对 `Double`（Kotlin 的非空基本类型）在字段缺失时会留成 `0.0`——
 *    于是界面上就出现"心率 0 bpm"，这正是协议要禁止的。本项目的 DTO 全部用
 *    `Double?` + 默认值 `null`，kotlinx.serialization 在字段缺失或为 null 时都得到 null。
 * 2. **类型严格**：服务器把 `72.4` 写成 `"72.4"` 时，kotlinx.serialization 会报错，
 *    能第一时间发现"树莓派改了接口"；Gson 会默默容忍，把问题拖到线上。
 * 3. 它没有反射、没有注解处理器，编译期生成序列化器，配置少、混淆友好。
 *
 * 三个开关的含义：
 *  - `ignoreUnknownKeys = true`：协议 §6 说新增字段是兼容的，手机端要忽略不认识的字段；
 *  - `explicitNulls = false`：编码时省略 null 字段（本项目只发无 body 的请求，
 *    但保持与解码行为一致，避免"读进来是 null、写出去变缺字段"的怪现象）；
 *  - `coerceInputValues = true`：只对"不可空且带默认值"的字段生效（本项目 DTO
 *    都可空），留着是为了将来加非空字段时不会因为服务器多给一个 null 就整体解析失败。
 */
val HealthJson: Json = Json {
    ignoreUnknownKeys = true
    explicitNulls = false
    coerceInputValues = true
    isLenient = false
}
