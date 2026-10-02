package com.dboycht.healthmonitor.data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * OneNET token 签名：**与树莓派侧的 Python 实现逐字符对齐**。
 *
 * 期望值不是手写的，是**用 Python 侧 `rpi/health_monitor/net/onenet.py::sign_token()`
 * 生成的**（生成脚本 `_scratch/gen_token_vectors.py`，它同时用 Python 手算一遍自证）：
 *
 * ```
 * TEST_KEY = cmFzcGJlcnJ5LWhlYWx0aC1tb25pdG9yLXRlc3Qta2V5   （固定测试密钥，非真密钥）
 * TEST_RES = products/vmkgy5EP2t/devices/t1
 * TEST_ET  = 1790000000
 * sha256 -> version=2018-10-31&res=products%2F…&et=1790000000&method=sha256&sign=P%2BxAVpsLFIsKXn8ekgrsLFWJKzqP16iav0VOobrJS7M%3D
 * sha1   -> …&method=sha1&sign=bhE0O2hHN94zJwfHsQUCbczcj1I%3D
 * md5    -> …&method=md5&sign=Th%2B19Sir038zVlTUdjugaA%3D%3D
 * ```
 *
 * ⚠️ 这条测试的意义：App 与树莓派**各自实现了一遍**同一个算法，
 * "人眼比对两段代码"是不可靠的 —— 让两端**对同一组输入输出同一个字符串**才算契约。
 */
class OneNetTokenTest {

    private val testKey = "cmFzcGJlcnJ5LWhlYWx0aC1tb25pdG9yLXRlc3Qta2V5"
    private val testRes = "products/vmkgy5EP2t/devices/t1"
    private val testEt = 1790000000L

    @Test
    fun `与 Python 实现逐字符一致_sha256`() {
        val expected = "version=2018-10-31&res=products%2Fvmkgy5EP2t%2Fdevices%2Ft1" +
            "&et=1790000000&method=sha256" +
            "&sign=P%2BxAVpsLFIsKXn8ekgrsLFWJKzqP16iav0VOobrJS7M%3D"
        assertEquals(expected, OneNetToken.sign(testKey, testRes, testEt))
    }

    @Test
    fun `与 Python 实现逐字符一致_sha1 与 md5`() {
        assertEquals(
            "version=2018-10-31&res=products%2Fvmkgy5EP2t%2Fdevices%2Ft1" +
                "&et=1790000000&method=sha1&sign=bhE0O2hHN94zJwfHsQUCbczcj1I%3D",
            OneNetToken.sign(testKey, testRes, testEt, method = "sha1"),
        )
        assertEquals(
            "version=2018-10-31&res=products%2Fvmkgy5EP2t%2Fdevices%2Ft1" +
                "&et=1790000000&method=md5&sign=Th%2B19Sir038zVlTUdjugaA%3D%3D",
            OneNetToken.sign(testKey, testRes, testEt, method = "md5"),
        )
    }

    @Test
    fun `资源串按平台约定拼装`() {
        assertEquals("products/P1/devices/D1", OneNetToken.deviceResource("P1", "D1"))
    }

    @Test
    fun `百分号编码等价于 Python 的 quote safe 空`() {
        // Python: urllib.parse.quote("a/b+c=", safe="") == "a%2Fb%2Bc%3D"
        assertEquals("a%2Fb%2Bc%3D", OneNetToken.percentEncode("a/b+c="))
        // 保留字符（Python 的 quote 同样保留：字母数字与 -_.~）
        assertEquals("aZ0-_.~", OneNetToken.percentEncode("aZ0-_.~"))
        // 空格编成 %20（**不是** URLEncoder 的 '+'）
        assertEquals("a%20b", OneNetToken.percentEncode("a b"))
    }

    @Test
    fun `非法输入给出人话而不是崩`() {
        // 不是合法 base64
        val bad = assertThrows(IllegalArgumentException::class.java) {
            OneNetToken.sign("not-base64!!!", testRes, testEt)
        }
        assertTrue("错误信息要说清是 base64 的问题：${bad.message}", bad.message!!.contains("base64"))

        // 不支持的方法
        val method = assertThrows(IllegalArgumentException::class.java) {
            OneNetToken.sign(testKey, testRes, testEt, method = "sha512")
        }
        assertTrue(method.message!!.contains("sha512"))

        // 空 res / 空 key
        assertThrows(IllegalArgumentException::class.java) {
            OneNetToken.sign(testKey, "  ", testEt)
        }
        assertThrows(IllegalArgumentException::class.java) {
            OneNetToken.sign("", testRes, testEt)
        }
    }

    @Test
    fun `换 et 会改变签名_但格式不变`() {
        val a = OneNetToken.sign(testKey, testRes, 1790000000L)
        val b = OneNetToken.sign(testKey, testRes, 1790000001L)
        assertTrue("et 参与签名，换时间必须换 sign", a != b)
        assertTrue(a.startsWith("version=2018-10-31&res=products%2F"))
        assertTrue(a.contains("&et=1790000000&method=sha256&sign="))
    }
}
