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
            "body_temp_c": 36.5,
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
            "body_temp_c": 36.1,
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

    /** §4.4 的报警样本：live + history，history 里 `value` 可能是 null。 */
    val ALARMS: String = """
        {"ok": true,
         "live": [{"ts": 1790001560.0, "code": "hr_too_high", "severity": 2,
                   "light": "yellow", "blink": true, "speak": "心率偏高，请注意休息",
                   "beep_times": 3, "silenced": false}],
         "history": [{"ts": 1790001560.0, "code": "hr_too_high", "severity": 2,
                      "message": "心率偏高 128.4bpm，请注意查看", "value": 128.4, "unit": "bpm",
                      "source": "vitals", "detail": {}, "pushed": 0},
                     {"ts": 1790001500.0, "code": "sos_pressed", "severity": 3,
                      "message": "已收到紧急求助，请立即查看", "value": null, "unit": "",
                      "source": "sos", "detail": {}}, 
                     {"ts": 1790001400.0, "code": null, "severity": null, "message": null,
                      "value": null, "unit": null, "source": null, "detail": null}]}
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
