# 本模块 release 构建未开启混淆（isMinifyEnabled=false），此文件留作后续
# 开启 R8 时的 keep 规则位置。
#
# 若将来打开 isMinifyEnabled=true，kotlinx.serialization 必须保留 @Serializable 类：
#-keepattributes *Annotation*, InnerClasses
#-dontnote kotlinx.serialization.**
#-keepclassmembers class com.dboycht.healthmonitor.data.** {
#    *** Companion;
#}
#-keepclasseswithmembers class com.dboycht.healthmonitor.data.** {
#    kotlinx.serialization.KSerializer serializer(...);
#}
