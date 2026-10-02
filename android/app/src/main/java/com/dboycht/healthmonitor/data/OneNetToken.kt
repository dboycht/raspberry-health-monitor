package com.dboycht.healthmonitor.data

import java.util.Base64
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

/**
 * OneNET（中国移动物联网平台）的**访问 token 签名**——纯 Kotlin，与 Android 无关，可单测。
 *
 * 为什么手机端要自己签：新版 OneNET 的 HTTP API **不再接受裸密钥**，
 * 要求请求头带一个签名 token（`version/res/et/method/sign`）。密钥**不内置在 App 里**，
 * 由用户在「设置 → 云端（可选）」里手填（见 [com.dboycht.healthmonitor.settings.AppSettings]）。
 *
 * 算法（与树莓派侧 `rpi/health_monitor/net/onenet.py::sign_token` **必须逐字符一致**）：
 *
 * ```
 * 签名原文 org = "{et}\n{method}\n{res}\n{version}"        // 只取 value，按参数名排序
 * sign = base64( hmac_{method}( base64decode(access_key), org ) )
 * token = "version={version}&res={urlencode(res)}&et={et}&method={method}&sign={urlencode(sign)}"
 * ```
 *
 * ⚠️ 两端一致这件事**有条测试钉着**：`OneNetTokenTest` 里的期望值是**用 Python 侧
 * `sign_token()` 生成**的（生成脚本与向量一起写在测试注释里），Kotlin 差一个字符就会红。
 * 这是本项目"跨语言契约"的写法：**不要靠人眼比对两段实现**。
 */
object OneNetToken {

    /** 平台规定的算法版本，固定值。 */
    const val VERSION: String = "2018-10-31"

    /** 默认签名方法（树莓派侧默认也是 sha256）。 */
    const val DEFAULT_METHOD: String = "sha256"

    /** 设备级资源串（设备连接与**设备数据查询**都用它）。 */
    fun deviceResource(productId: String, deviceName: String): String =
        "products/$productId/devices/$deviceName"

    /**
     * 生成 `authorization` 请求头的值。
     *
     * @param accessKey 平台分配的密钥（**base64 字符串**），用户手填。
     * @param res 资源串，见 [deviceResource]。
     * @param et 过期时间（unix 秒）。
     * @param method [DEFAULT_METHOD] / `"sha1"` / `"md5"`。
     */
    fun sign(
        accessKey: String,
        res: String,
        et: Long,
        method: String = DEFAULT_METHOD,
    ): String {
        val m = method.trim().lowercase()
        val algorithm = when (m) {
            "md5" -> "HmacMD5"
            "sha1" -> "HmacSHA1"
            "sha256" -> "HmacSHA256"
            else -> throw IllegalArgumentException("不支持的签名方法 $method（只支持 md5/sha1/sha256）")
        }
        val key = try {
            Base64.getDecoder().decode(accessKey.trim())
        } catch (exc: IllegalArgumentException) {
            throw IllegalArgumentException("密钥不是合法 base64（平台的密钥末尾常带 =）", exc)
        }
        require(key.isNotEmpty()) { "密钥解码后为空，请核对是否复制完整" }
        require(res.isNotBlank()) { "res 不能为空" }

        val org = "$et\n$m\n$res\n$VERSION"
        val mac = Mac.getInstance(algorithm)
        mac.init(SecretKeySpec(key, algorithm))
        val sign = Base64.getEncoder().encodeToString(mac.doFinal(org.toByteArray(Charsets.UTF_8)))

        return "version=$VERSION&res=${percentEncode(res)}&et=$et&method=$m&sign=${percentEncode(sign)}"
    }

    /**
     * RFC 3986 的百分号编码，等价于 Python `urllib.parse.quote(s, safe="")`。
     *
     * ⚠️ **不能直接用 `java.net.URLEncoder`**：它把空格编成 `+`、且不编码 `*`，
     * 与 Python 的 `quote(safe="")` 语义不同。虽然 token 里只出现 base64 字符集
     * （`+/=`）与 `/`，两者结果恰好相同，但"恰好相同"不是契约 —— 这里按 RFC 显式实现，
     * 判据是**跟 Python 侧逐字符一致**（有跨语言测试钉着）。
     */
    internal fun percentEncode(text: String): String {
        val sb = StringBuilder(text.length * 3)
        for (byte in text.toByteArray(Charsets.UTF_8)) {
            val ch = byte.toInt().toChar()
            val unreserved = ch in 'A'..'Z' || ch in 'a'..'z' || ch in '0'..'9' ||
                ch == '-' || ch == '.' || ch == '_' || ch == '~'
            if (unreserved) {
                sb.append(ch)
            } else {
                sb.append('%')
                sb.append(HEX[(byte.toInt() shr 4) and 0x0F])
                sb.append(HEX[byte.toInt() and 0x0F])
            }
        }
        return sb.toString()
    }

    private val HEX = "0123456789ABCDEF".toCharArray()
}
