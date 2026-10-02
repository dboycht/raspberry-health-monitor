package com.dboycht.healthmonitor.testing

/**
 * 测试样本：**逐字抄自 `docs/05-安卓通信协议.md` 的真实响应样本**。
 *
 * 单元测试只用字符串，不起 HTTP 服务、不连真树莓派（用假仓库/假 HTTP 客户端），
 * 这样 `testDebugUnitTest` 在任何机器上都能离线跑绿。
 */
object JsonSamples {

    /** §4.1 的完整样本（字段全都有值）。 */
    val CURRENT_HEALTHY: String = """
        {
          "ok": true,
          "data": {
            "ts": 1790001600.5,
            "heart_rate_bpm": 72.4,
            "spo2_percent": 97.9,
            "finger_detected": true,
            "vitals_quality": 0.95,
            "ambient_temp_c": 24.5,
            "humidity_percent": 55.2,
            "motion_state": "detected",
            "motion_silent_s": 0.0,
            "sensor_failures": {}
          },
          "active_alarms": {}
        }
    """.trimIndent()

    /**
     * 手指没放好 + 多项传感器读取失败：`finger_detected=false`，
     * 心率/血氧为 **null**，`sensor_failures` 非空（这是最容易被写错成 0 的场景）。
     */
    val CURRENT_FINGER_MISSING_AND_FAILURES: String = """
        {
          "ok": true,
          "data": {
            "ts": 1790001700.25,
            "heart_rate_bpm": null,
            "spo2_percent": null,
            "finger_detected": false,
            "vitals_quality": null,
            "ambient_temp_c": null,
            "humidity_percent": 48.0,
            "motion_state": "unknown",
            "motion_silent_s": null,
            "sensor_failures": {"ambient": 3, "vitals": 1}
          },
          "active_alarms": {"spo2_too_low": 1790001500.0}
        }
    """.trimIndent()

    /** 极端情况：`data` 整体缺失（服务刚起来还没采到数），必须不能崩。 */
    val CURRENT_DATA_ABSENT: String = """{"ok": true, "active_alarms": {}}"""

    /**
     * §4.4 的报警样本：live + history，history 里 `value` 可能是 null。
     *
     * ⚠️ 2026-10-02：这条 `pushed` 原来是 **`0`**（照当时契约文档手写），现在改成 **`false`** ——
     * 真机发的是布尔（`store.recent_alarms()` 里 `bool(row["pushed"])`）。
     * 留着 `0` 会直接解析失败（`Expected valid boolean literal prefix, but had '0'`），
     * 而那份"文档形状"的样本正是当初让这个不一致**躲过所有单测**的原因。
     */
    val ALARMS: String = """
        {"ok": true,
         "live": [{"ts": 1790001560.0, "code": "hr_too_high", "severity": 2,
                   "light": "yellow", "blink": true, "speak": "心率偏高，请注意休息",
                   "beep_times": 3, "silenced": false}],
         "history": [{"ts": 1790001560.0, "code": "hr_too_high", "severity": 2,
                      "message": "心率偏高 128.4bpm，请注意查看", "value": 128.4, "unit": "bpm",
                      "source": "vitals", "detail": {}, "pushed": false},
                     {"ts": 1790001500.0, "code": "sos_pressed", "severity": 3,
                      "message": "已收到紧急求助，请立即查看", "value": null, "unit": "",
                      "source": "sos", "detail": {}}, 
                     {"ts": 1790001400.0, "code": null, "severity": null, "message": null,
                      "value": null, "unit": null, "source": null, "detail": null}]}
    """.trimIndent()

    /**
     * ★ **真机原样抓下来的** `/api/v1/alarms` 响应（2026-10-02 11:52，树莓派 `t1`）。
     *
     * 为什么必须留一份"真机形状"的样本：上面那份 [ALARMS] 是**照契约文档手写**的，
     * 于是它恰好绕开了真机上的两个坑 —— `pushed` 真机发的是**布尔 `false`**（文档写成 `0`），
     * `detail` 真机里是**混合类型**（`{"ok":false,"valid_samples":0}`、`{"error":"…"}`）。
     * App 当时把二者声明成 `Int?` / `Map<String,String>` ⇒ **CI 全绿、真机报警列表全红**
     * （界面："响应格式不对：树莓派返回的不是预期 JSON"）。
     *
     * ⚠️ **纪律**：这类"线上形状"的样本要**从设备抓**，不要照文档手写 ——
     * 手写的样本只能证明"我们自洽"，证明不了"跟设备一致"。
     */
    val ALARMS_REAL_DEVICE: String = """
        {"ok": true,
         "live": [{"ts": 1790911714.8330173, "code": "system_start", "severity": 1,
                   "light": "green", "blink": false, "speak": "监护系统已启动",
                   "beep_times": 0, "silenced": false},
                  {"ts": 1790912692.5840764, "code": "ambient_temp_high", "severity": 1,
                   "light": "yellow", "blink": false, "speak": "室温偏高，建议通风",
                   "beep_times": 1, "silenced": false}],
         "history": [{"ts": 1790912701.408948, "code": "spo2_measured", "severity": 1,
                      "message": "血氧测量未取得有效读数", "value": null, "unit": "",
                      "source": "spo2",
                      "detail": {"ok": false, "heart_rate_bpm": null, "spo2_percent": null,
                                 "valid_samples": 0},
                      "pushed": false},
                     {"ts": 1790912692.5841634, "code": "ambient_temp_high", "severity": 1,
                      "message": "室温偏高 22.7°C，请注意查看", "value": 22.7, "unit": "°C",
                      "source": "ambient", "detail": {}, "pushed": false},
                     {"ts": 1790859319.3283668, "code": "sensor_fault", "severity": 2,
                      "message": "传感器 spo2_button 连续 3 次读取失败，请检查接线",
                      "value": 3.0, "unit": "次", "source": "spo2_button",
                      "detail": {"error": "数据陈旧（超过 3 × 0.2s 未更新）"}, "pushed": false},
                     {"ts": 1790867375.1975873, "code": "all_clear", "severity": 0,
                      "message": "室温偏高已恢复正常", "value": null, "unit": "", "source": "",
                      "detail": {"recovered_code": "ambient_temp_high"}, "pushed": false}]}
    """.trimIndent()

    /**
     * ★ **真机原样抓下来的** OneNET 响应（2026-10-02，`GET /datapoint/history-datapoints`）。
     *
     * 用途：云端只读功能的解析回归。这份样本有三件事**必须照抄真机**，否则又白测：
     *
     * 1. `value` **类型混着来** —— 数字（`89`、`22.7`、`0`）与字符串（`"unknown"`、`"1.0.1"`）都有；
     * 2. 时间字段是**两套**（可读字符串 `at` + 毫秒时间戳 `at_timestamp`），且各流的时间**差很远**
     *    （最新的是 `motion`，最旧的是 `heart_rate`，差了整整一天）；
     * 3. 顶层还有 `msg` / `request_id` 等我们用不上的键（靠 `ignoreUnknownKeys` 忽略）。
     *
     * ⚠️ 这是**节选**（真机那次 `count=18`，这里保留 5 条代表性数据流，含当时最旧/最新的两条）。
     */
    val ONENET_HISTORY_REAL: String = """
        {"code":0,"msg":"succ","request_id":"83e8e02a329ecee9c0701fa63d41c58e",
         "data":{"count":18,"datastreams":[
           {"id":"motion","datapoints":[{"at":"2026-10-01 23:30:23.000","at_timestamp":1790868623000,"value":"unknown"}]},
           {"id":"alarm_value","datapoints":[{"at":"2026-10-01 23:09:33.000","at_timestamp":1790867373000,"value":22.7}]},
           {"id":"heart_rate","datapoints":[{"at":"2026-09-30 21:38:13.000","at_timestamp":1790775493000,"value":89}]},
           {"id":"alarm_level","datapoints":[{"at":"2026-10-01 23:09:35.000","at_timestamp":1790867375000,"value":0}]},
           {"id":"version","datapoints":[{"at":"2026-10-01 23:30:23.000","at_timestamp":1790868623000,"value":"1.0.1"}]}]}}
    """.trimIndent()

    /** §4.2 的健康样本（含设备状态与 dispatcher）。 */
    val HEALTH: String = """
        {
          "ok": true,
          "ts": 1790001600.5,
          "uptime_s": 612.3,
          "version": "1.0.1",
          "mock": false,
          "active_alarms": {"hr_too_high": 1790001500.0},
          "dispatcher": {"enabled": true, "outputs": ["alarm_buzzer", "display", "speaker", "status_led"],
                         "dispatched": 4, "errors": [], "silenced_until": 0.0},
          "devices": {
            "vitals": {
              "driver": "max30102", "interval_s": 1.0, "reads": 612, "failures": 0,
              "last_error": null, "stale": false, "last_ok_age_s": 0.4, "optional": false,
              "device_status": {"name": "vitals", "kind": "vital", "opened": true, "mock": false,
                                "read_count": 612, "fault_count": 0, "last_error": null}
            },
            "ambient": {
              "driver": "dht11", "interval_s": 2.0, "reads": 300, "failures": 7,
              "last_error": "checksum error", "stale": true, "last_ok_age_s": null,
              "optional": false,
              "device_status": {"name": "ambient", "kind": "climate", "opened": true, "mock": false,
                                "read_count": 293, "fault_count": 7, "last_error": "checksum error"}
            }
          }
        }
    """.trimIndent()

    /** §4.5 的设备清单样本（接线说明里有 pins）。 */
    val DEVICES: String = """
        {"ok": true, "devices": {"vitals": {"driver": "max30102", "interval_s": 1.0,
          "describe": {"name":"vitals","kind":"vital","mock":false,"bus":"I2C-1",
                       "pins":{"sda":"GPIO2 / 物理脚 3","scl":"GPIO3 / 物理脚 5"},"notes":"I2C 0x57"}}}}
    """.trimIndent()

    /** 服务器配置了 token 而手机端没带 → 401 的响应格式（协议 §2）。 */
    val UNAUTHORIZED: String = """{"ok": false, "error": "invalid or missing token"}"""
}
