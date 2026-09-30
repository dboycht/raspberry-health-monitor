"""配置里"独占型 GPIO"的抽取与撞脚检查（**库层**，``scripts/validate.py`` 与 Web 配置面板共用）。

为什么单独拎出这个模块（2026-10-01）
------------------------------------
这套逻辑原本只长在 ``scripts/validate.py`` 里。加了 **Web 配置面板**之后，
"写盘之前也要跑同一套撞脚检查" —— 而从 ``health_monitor/`` 反向 import ``scripts/``
是**层次倒挂**（``scripts/`` 不是包，靠 ``sys.path`` 注入才能 import，在真机上还取决于
启动方式）。所以把判据提到 core 层：

* 本模块只依赖 :mod:`health_monitor.hal.pins`，**不依赖 service / net**，因此不会产生循环 import；
* ``scripts/validate.py`` 改成从这里 import，并**保留同名符号**
  （``EXCLUSIVE_PIN_KEYS`` / ``exclusive_pin_claims``）—— 既有测试直接引用
  ``validate.exclusive_pin_claims``，不能把它们改没。
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Tuple

#: 驱动的"独占型 GPIO"参数名（**不能共用**）。
#: ⚠️ 2026-09-26 补：原来漏了 TFT 的 `dc_pin` / `reset_pin` ⇒
#: `status_led.red=24` 与 `tft.dc_pin=24` **真撞脚却报"无冲突"**（项目默认配置里就埋着这个雷）。
#: 往后新增"用 GPIO 的驱动"必须在这里登记（tests/scripts 里有回归钉）。
EXCLUSIVE_PIN_KEYS: Dict[str, Tuple[str, ...]] = {
    "dht11": ("pin",),
    "hc_sr501": ("pin",),
    "hc_sr04": ("trig_pin", "echo_pin"),
    "buzzer": ("pin",),
    "button": ("pin",),
    "tft_spi": ("dc_pin", "reset_pin"),
    "mcp3002": ("cs_pin",),
}


def exclusive_pin_claims(config: Mapping[str, Any]) -> List[Tuple[str, int]]:
    """从配置里抽出所有"独占型 GPIO"声明，形如 ``[(设备.参数, BCM), ...]``。

    **纯函数**（好测）：只认 :data:`EXCLUSIVE_PIN_KEYS` 里登记的键 + LED 的 `pins`；
    负数/缺省表示"该脚未使用"（例如 TFT 的 `backlight_pin: -1`）会被跳过。

    ⚠️ **刻意不过滤 `enabled`**（2026-09-26 决定，之后明确不许改）：
    未启用的设备将来会被启用，它的引脚声明**现在**就该参与撞脚检查 —— 否则
    "红灯=24 与 TFT 的 DC=24"这种雷只有在两者都开的时候才爆，而那时**已经在现场了**。
    （``tests/scripts/test_pin_conflict_check.py`` 里有一条测试专门钉住这个语义。）
    """
    claims: List[Tuple[str, int]] = []
    for dev_name, item in (config.get("devices") or {}).items():
        if not isinstance(item, Mapping):
            continue
        params = item.get("params") or {}
        if not isinstance(params, Mapping):
            continue
        driver = item.get("driver", "")
        for key in EXCLUSIVE_PIN_KEYS.get(driver, ()):
            value = params.get(key)
            if isinstance(value, int) and value >= 0:
                claims.append((f"{dev_name}.{key}", int(value)))
        if driver == "led":
            for color, pin in (params.get("pins") or {}).items():
                claims.append((f"{dev_name}.led.{color}", int(pin)))
        if driver == "tft_spi":
            backlight = params.get("backlight_pin")
            if isinstance(backlight, int) and backlight >= 0:
                claims.append((f"{dev_name}.backlight_pin", int(backlight)))
    return claims


def find_config_conflicts(config: Mapping[str, Any]) -> List[str]:
    """对整份配置跑一次撞脚检查，返回人类可读的冲突说明（空列表 = 无冲突）。

    Web 配置面板在**写盘之前**调它：改引脚改出撞脚时必须 400 拒绝，而不是写进去等现场炸。
    """
    from ..hal.pins import find_conflicts

    return find_conflicts(exclusive_pin_claims(config))


__all__ = ["EXCLUSIVE_PIN_KEYS", "exclusive_pin_claims", "find_config_conflicts"]
