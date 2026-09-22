"""配置加载测试：**本机覆盖（devices.local.json）与深度合并**。

为什么这些测试重要（2026-09-22 的实战教训）
-------------------------------------------
树莓派上要填 **OneNET 密钥**，而密钥**绝对不能入库**。解决办法是：
仓库里的 ``config/devices.json`` 只放"产品ID / 设备名 / topic"这类非敏感项，
密钥写在**不入库**的 ``config/devices.local.json`` 里，由 ``load_config()`` 深度合并。

这类"合并"逻辑最容易出的错是**看起来生效、其实把基础配置整段覆盖掉了**
（例如覆盖文件里只写了一个字段，结果同一层里其它字段全丢），
所以这里逐条钉死：嵌套字典**按键合并**、其它类型**整体替换**。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

RPI_DIR = Path(__file__).resolve().parents[2]
if str(RPI_DIR) not in sys.path:
    sys.path.insert(0, str(RPI_DIR))

from health_monitor.core.config import (  # noqa: E402
    _deep_merge,
    load_config,
    local_config_path,
)


class TestDeepMerge(unittest.TestCase):
    def test_嵌套字典按键合并(self) -> None:
        base = {"onenet": {"enabled": False, "product_id": "pid1", "access_key": ""}, "x": 1}
        override = {"onenet": {"enabled": True, "access_key": "SECRET"}}
        merged = _deep_merge(base, override)
        # 被覆盖的键用新值
        self.assertTrue(merged["onenet"]["enabled"])
        self.assertEqual(merged["onenet"]["access_key"], "SECRET")
        # **没被覆盖的键必须保留**（这是"深度"合并与"整体替换"的关键区别）
        self.assertEqual(merged["onenet"]["product_id"], "pid1")
        self.assertEqual(merged["x"], 1)

    def test_不改动入参(self) -> None:
        base = {"a": {"b": 1}}
        override = {"a": {"c": 2}}
        _deep_merge(base, override)
        self.assertEqual(base, {"a": {"b": 1}}, "不得就地修改基础配置")
        self.assertEqual(override, {"a": {"c": 2}}, "不得就地修改覆盖配置")

    def test_非字典类型整体替换(self) -> None:
        base = {"list": [1, 2, 3], "num": 1}
        merged = _deep_merge(base, {"list": [9], "num": 5})
        self.assertEqual(merged["list"], [9], "列表整体替换，不做拼接")
        self.assertEqual(merged["num"], 5)

    def test_深层嵌套也能合并(self) -> None:
        base = {"devices": {"tft": {"params": {"rotate": 90, "bgr": None}}}}
        override = {"devices": {"tft": {"params": {"rotate": 0}}}}
        merged = _deep_merge(base, override)
        self.assertEqual(merged["devices"]["tft"]["params"]["rotate"], 0)
        self.assertIsNone(merged["devices"]["tft"]["params"]["bgr"])


class TestLoadConfigWithLocalOverride(unittest.TestCase):
    """真的走一遍文件：基础配置 + 本机覆盖。

    ⚠️ 这里**一律传 ``use_local=False``**：本机覆盖是"这台机器的环境"，
    测试必须能控制它，否则同一份测试会在"有覆盖文件的机器"上莫名其妙地红。
    （2026-09-22 实测：开发机无覆盖→绿；树莓派有覆盖→红。）
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name) / "base.json"
        self.base.write_text(json.dumps({
            "devices": {
                "display": {"driver": "lcd1602", "enabled": True, "read_interval_s": 1.0,
                            "params": {"bus": 1}},
            },
            "onenet": {
                "enabled": False,
                "platform": "legacy",
                "product_id": "vmkgy5EP2t",
                "device_name": "t1",
                "access_key": "",
                "method": "sha1",
                "topic_template": "$sys/{pid}/{device}/dp/post/json",
            },
        }, ensure_ascii=False), encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_无覆盖文件时按基础配置(self) -> None:
        cfg = load_config(self.base, use_local=False)
        self.assertFalse(cfg.onenet["enabled"])
        self.assertEqual(cfg.onenet["product_id"], "vmkgy5EP2t")
        self.assertEqual(cfg.onenet["access_key"], "")

    def test_开启本机覆盖时叠加而非替换(self) -> None:
        """用一个**临时覆盖文件**验证真实合并（不改动仓库里的路径）。"""
        from unittest import mock

        from health_monitor.core import config as config_module

        local = Path(self.tmp.name) / "devices.local.json"
        local.write_text(json.dumps({
            "onenet": {"enabled": True, "access_key": "LOCAL-SECRET"},
        }), encoding="utf-8")
        with mock.patch.object(config_module, "local_config_path", return_value=local):
            cfg = config_module.load_config(self.base, use_local=True)
        # 覆盖文件里的值生效
        self.assertTrue(cfg.onenet["enabled"])
        self.assertEqual(cfg.onenet["access_key"], "LOCAL-SECRET")
        # **基础配置里没被覆盖的字段必须保留**（这正是"深度合并"的意义）
        self.assertEqual(cfg.onenet["product_id"], "vmkgy5EP2t")
        self.assertEqual(cfg.onenet["device_name"], "t1")

    def test_local_config_path指向不入库文件(self) -> None:
        """路径必须是 ``rpi/config/devices.local.json``（.gitignore 里忽略的就是它）。"""
        p = local_config_path()
        self.assertEqual(p.name, "devices.local.json")
        self.assertEqual(p.parent.name, "config")

    def test_未知字段仍要报错(self) -> None:
        from health_monitor.hal.exceptions import ConfigError

        bad = Path(self.tmp.name) / "bad.json"
        bad.write_text(json.dumps({
            "devices": {"d": {"driver": "lcd1602", "enabled": True, "read_interval_s": 1.0,
                              "params": {}, "no_such_field": 1}},
            "onenet": {},
        }), encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(bad, use_local=False)

    def test_JSON语法错误要报行号(self) -> None:
        from health_monitor.hal.exceptions import ConfigError

        bad = Path(self.tmp.name) / "broken.json"
        bad.write_text('{ "devices": {  ', encoding="utf-8")
        with self.assertRaises(ConfigError) as ctx:
            load_config(bad, use_local=False)
        self.assertIn("JSON", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
