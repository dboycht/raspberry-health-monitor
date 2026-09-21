package com.dboycht.healthmonitor.data

/**
 * 用户输入地址的**归一化**（协议：地址形如 `http://<树莓派局域网IP>:8080`）。
 *
 * 用户（尤其是答辩演示时）在设置页里会填得很随意，常见四种写法都要能吃下：
 *
 * | 用户输入                     | 归一化结果                              |
 * | --------------------------- | -------------------------------------- |
 * | `192.168.1.20`              | `http://192.168.1.20:8080/`            |
 * | `192.168.1.20:8080`         | `http://192.168.1.20:8080/`            |
 * | `http://192.168.1.20:8080`  | `http://192.168.1.20:8080/`            |
 * | `https://pi.local`          | `https://pi.local/`（保留 scheme，不加默认端口）|
 *
 * 两条规则：
 *  1. **没有 scheme 就补 `http://`**（局域网直连树莓派没有证书，https 不现实）；
 *  2. **没有写端口且用的是 http 就补 `:8080`**（服务器默认端口，见协议 §1）。
 *
 * 顺带保证结果**以 `/` 结尾**：Retrofit 的 baseUrl 末尾必须带斜杠，
 * 否则 `@GET("api/v1/current")` 会把最后一段路径吃掉。
 *
 * 实现上有两个容易踩的坑，都已被单测钉住：
 *  - **先剥 scheme 再剥路径**：反过来会把 `http://192.168.1.20:8080` 截成 `http:`
 *    （第一个 `/` 在 `http://` 里），于是 `http://` 开头的地址全被误判成非法；
 *  - **主机名只允许 ASCII**：`Char.isLetterOrDigit()` 对中文也返回 true，
 *    用它校验会把「树莓派」这种明显不是主机名的输入放过去。
 */
object UrlNormalizer {

    /** 树莓派后端默认端口（`health_monitor serve` 的默认值，见协议 §1）。 */
    const val DEFAULT_PORT: Int = 8080

    /** 非空输入但归一化失败（含非法字符）时返回的提示，界面直接显示给用户。 */
    const val INVALID_MESSAGE: String = "服务器地址格式不正确"

    /**
     * 归一化；输入非法（空、含空格、含非法字符、不是合法主机名）时返回 `null`，
     * 由调用方决定怎么提示。
     */
    fun normalizeOrNull(raw: String?): String? {
        val trimmed = raw?.trim().orEmpty()
        if (trimmed.isEmpty()) return null
        // 里面有空白（含换行/制表）说明用户粘贴了奇怪的东西；地址里不允许空格。
        if (trimmed.any { it.isWhitespace() }) return null

        val lower = trimmed.lowercase()
        val scheme: String
        val afterScheme: String
        when {
            lower.startsWith("http://") -> {
                scheme = "http"
                afterScheme = trimmed.substring("http://".length)
            }

            lower.startsWith("https://") -> {
                scheme = "https"
                afterScheme = trimmed.substring("https://".length)
            }

            trimmed.contains("://") -> return null // 其它协议（ftp:// 等）不支持
            else -> {
                scheme = "http"
                afterScheme = trimmed
            }
        }

        // 只保留主机与端口：粘贴成 "http://ip:8080/api/v1" 时也能用。
        val authority = afterScheme.substringBefore('/')
        if (authority.isEmpty()) return null

        val host = authority.substringBefore(':')
        val portText = if (authority.contains(':')) authority.substringAfter(':') else null

        if (!isValidHost(host)) return null
        if (portText != null && (portText.isEmpty() || !portText.all { it in '0'..'9' })) return null

        val portPart = when {
            portText != null -> ":$portText"
            scheme == "http" -> ":$DEFAULT_PORT"
            else -> "" // https 不加默认端口
        }
        return "$scheme://$host$portPart/"
    }

    /**
     * 归一化；输入非法时退回 [fallback]（通常是"上次保存成功的地址"），
     * 连 fallback 也没有就返回 null。
     */
    fun normalizeOrNull(raw: String?, fallback: String?): String? =
        normalizeOrNull(raw) ?: normalizeOrNull(fallback)

    /**
     * 主机名校验：IP（IPv4）或普通主机名（`pi.local`、`raspberrypi`）。
     * 只允许 ASCII 字母/数字/`.`/`-`/`_`，且必须有点分段或至少一个字母数字，
     * 不接受空段（`192..168`、`.20`、`20.`）与中文。
     */
    private fun isValidHost(host: String): Boolean {
        if (host.isEmpty()) return false
        val allowed = host.all { c ->
            (c in 'a'..'z') || (c in 'A'..'Z') || (c in '0'..'9') || c == '.' || c == '-' || c == '_'
        }
        if (!allowed) return false
        // 点分段不能有空段，且首尾不能是点（避免 "192.168.1.20." 这种）。
        if (host.startsWith('.') || host.endsWith('.')) return false
        if (host.split('.').any { it.isEmpty() }) return false
        return true
    }
}
