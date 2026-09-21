// 顶层构建脚本：只声明插件，不 apply（各模块按需 apply）。
// 版本号统一由 gradle/libs.versions.toml（version catalog）管理。
plugins {
    alias(libs.plugins.android.application) apply false
    alias(libs.plugins.kotlin.android) apply false
    alias(libs.plugins.kotlin.compose) apply false
    alias(libs.plugins.kotlin.serialization) apply false
}
