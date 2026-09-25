#!/usr/bin/env python3
"""`health_monitor selfcheck` 的表必须列**配置里的设备名**，而不是驱动名。

为什么单独立一条（2026-09-25 真机实测，`ERROR.md` E36）
--------------------------------------------------------
本项目**配置里的设备名与驱动名不一样**：

    配置（`config/devices.json`）   {"ambient": {"driver": "dht11", ...}}
    MANIFEST（注册表）               "dht11" / "max30102" / "lcd1602" …

`cmd_selfcheck()` 原来是 `for name in sorted(MANIFEST)`（按**驱动名**遍历），
而 `Runtime.device_report()` 的键是**设备名**（`ambient`/`vitals`/`display`…）⇒
`name in report` 永远为假 ⇒ 全部打成 `skip 未在配置中启用`，
真机体检**等于什么都没测**，却打印"装配失败/自检失败：0 项"。

真实后果：DHT11 明明接在脚 7 上、读不到数，体检报告却把它标成"未启用"，
把最该看的那一行藏了起来。

判据（可执行）
--------------
1. 报告里必须出现**配置里的每个设备名**（从 `DEFAULT_CONFIG` 取，不写死）；
2. 每个设备的**驱动名**列必须与配置一致（`ambient` 那行要写 `dht11`）；
3. `mock` 模式下这些行必须是 `OK/OK`（驱动实现有问题才该红）；
4. 实现了但没配置的驱动（`hc_sr04` / `mcp3002`）必须仍然以 `skip` 列出，
   否则"我没接线"和"驱动坏了"就又混在一起了。
"""

from __future__ import annotations

import contextlib
import io
import unittest

from health_monitor.core.config import DEFAULT_CONFIG
from health_monitor.hal.registry import MANIFEST
from health_monitor.main import cmd_selfcheck


class _Args:
    def __init__(self) -> None:
        self.real = False        # 走 mock：不碰硬件
        self.config = None


class TestSelfcheckReport(unittest.TestCase):
    def _run(self) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cmd_selfcheck(_Args())
        self.assertEqual(code, 0, "mock 模式下自检不该失败（驱动实现有问题才会失败）")
        return buf.getvalue()

    def test_报告按配置的设备名列出(self) -> None:
        text = self._run()
        enabled = {
            name: item["driver"]
            for name, item in DEFAULT_CONFIG["devices"].items()
            if item.get("enabled", True)
        }
        self.assertTrue(enabled, "默认配置里应当有启用的设备（否则本测试没意义）")
        for name in enabled:
            self.assertIn(name, text, f"体检报告里缺少配置中的设备 {name}（按驱动名遍历就会这样）")

    def test_每个设备那一行写的是它的驱动名(self) -> None:
        text = self._run()
        lines = {line.split()[0]: line for line in text.splitlines() if line and not line.startswith(("=", "-"))}
        enabled = {
            name: item["driver"]
            for name, item in DEFAULT_CONFIG["devices"].items()
            if item.get("enabled", True)
        }
        for name, driver in enabled.items():
            line = lines.get(name)
            self.assertIsNotNone(line, f"没有 {name} 这一行")
            self.assertIn(driver, line, f"{name} 那一行的驱动名应当是 {driver}：{line}")
            self.assertNotIn("skip", line, f"{name} 已启用，不该是 skip：{line}")

    def test_没配置的驱动仍然以skip列出(self) -> None:
        text = self._run()
        configured_drivers = {
            item["driver"]
            for item in DEFAULT_CONFIG["devices"].values()
            if item.get("enabled", True)
        }
        for driver in sorted(set(MANIFEST) - configured_drivers):
            self.assertIn(driver, text, f"没配置的驱动 {driver} 也应当以 skip 列出（信息行）")

    def test_真机上这一项能看出DHT11(self) -> None:
        """回归的**真正目的**：DHT11 读不到数时，那一眼必须出现在报告里。"""
        text = self._run()
        ambient_lines = [ln for ln in text.splitlines() if ln.startswith("ambient")]
        self.assertEqual(len(ambient_lines), 1, "ambient 应当正好一行")
        self.assertIn("dht11", ambient_lines[0])
        self.assertNotIn("未在配置中启用", ambient_lines[0],
                         "DHT11 已配置，绝不能显示成'未在配置中启用'（那正是本 bug 的症状）")


if __name__ == "__main__":
    unittest.main()
