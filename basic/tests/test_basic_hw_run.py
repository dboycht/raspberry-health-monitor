#!/usr/bin/env python3
"""单文件作业版 `basic/hw/run.py` 的后端选择测试（2026-10-13 新增）。

为什么单独给它写一份测试
------------------------
`basic/hw/run.py` 是"**一个文件交作业**"的那一份：它不带 `__init__.py`、不 import 主项目，
所以有**自己的** `Dht11` 实现。本轮按课程口径把它的后端顺序改成
**gpiozero 优先、lgpio 回退** —— 而"顺序写反了不会有任何报错"，
在开发机上更是看不出来（两个库都没装 ⇒ 两条都失败 ⇒ 行为看起来一样）。
所以这里把两个库都换成假货，逼出"都可用时选谁"这个分支。

⚠️ 它是**单文件**：用 `importlib` 按路径加载，不要求 `basic/hw/__init__.py` 存在
（那个文件刻意不存在 —— 作业版要能整个文件夹拷走）。

跑法：`python -m pytest basic/tests -q`
"""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from typing import Any, Dict
from unittest import mock

_RUN_PY = Path(__file__).resolve().parents[1] / "hw" / "run.py"


def _load_single_file() -> Any:
    """按路径加载 `hw/run.py`（模块名固定，便于 unittest 报错里认人）。"""
    name = "basic_hw_run_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _RUN_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


RUN = _load_single_file()


class _FakeSensor:
    temperature = 25.0
    humidity = 58.0

    def close(self) -> None:
        pass


class _FakeLgpio:
    """只实现构造/释放用得到的那几个函数。"""

    BOTH_EDGES = 1
    SET_PULL_UP = 2

    def gpiochip_open(self, _chip: int) -> int:
        return 0

    def gpio_free(self, _handle: int, _pin: int) -> None:
        pass

    def gpiochip_close(self, _handle: int) -> None:
        pass


def _modules(*, gpiozero: bool, lgpio: bool) -> Dict[str, Any]:
    patch: Dict[str, Any] = {}
    if gpiozero:
        mod = types.ModuleType("gpiozero")
        mod.DHT11 = lambda *a, **k: _FakeSensor()      # type: ignore[attr-defined]
        patch["gpiozero"] = mod
    else:
        patch["gpiozero"] = None
    patch["lgpio"] = _FakeLgpio() if lgpio else None
    return patch


class TestSingleFileBackendOrder(unittest.TestCase):
    """★ 单文件作业版的后端顺序：**先 gpiozero、后 lgpio**（课程口径）。"""

    def test_两个后端都可用时选gpiozero(self) -> None:
        with mock.patch.dict(sys.modules, _modules(gpiozero=True, lgpio=True)):
            sensor = RUN.Dht11(pin=4, mock=False)
            try:
                self.assertEqual(sensor.open(), "gpiozero",
                                 "gpiozero 必须优先（课程口径：GPIO 一律走 gpiozero）")
            finally:
                sensor.close()

    def test_只有lgpio时回退到lgpio(self) -> None:
        with mock.patch.dict(sys.modules, _modules(gpiozero=False, lgpio=True)):
            sensor = RUN.Dht11(pin=4, mock=False)
            try:
                self.assertEqual(sensor.open(), "lgpio", "gpiozero 不可用时必须能回退")
            finally:
                sensor.close()

    def test_两个都没有时如实报错而不是降级成mock(self) -> None:
        """报错要**点名两个库**并给出 `--mock`，不能悄悄给假数据（那是骗自己）。"""
        with mock.patch.dict(sys.modules, _modules(gpiozero=False, lgpio=False)):
            sensor = RUN.Dht11(pin=4, mock=False)
            with self.assertRaises(RUN.Dht11Error) as ctx:
                sensor.open()
            text = str(ctx.exception)
            self.assertIn("gpiozero", text)
            self.assertIn("lgpio", text)
            self.assertIn("--mock", text)
            self.assertEqual(sensor.backend, "none")

    def test_mock后端不碰任何库(self) -> None:
        with mock.patch.dict(sys.modules, _modules(gpiozero=False, lgpio=False)):
            sensor = RUN.Dht11(pin=4, mock=True)
            self.assertEqual(sensor.open(), "mock")
            temperature, humidity, note = sensor.read()
            self.assertIsNotNone(temperature)
            self.assertIsNotNone(humidity)
            self.assertIn("mock", note)
            sensor.close()


class TestSingleFileGpiozeroRead(unittest.TestCase):
    """选中 gpiozero 后端后，`read()` 必须真的走它（而不是仍然调到 lgpio）。"""

    def test_read走gpiozero器件(self) -> None:
        with mock.patch.dict(sys.modules, _modules(gpiozero=True, lgpio=True)):
            sensor = RUN.Dht11(pin=4, mock=False, retries=1)
            try:
                self.assertEqual(sensor.open(), "gpiozero")
                temperature, humidity, note = sensor.read()
            finally:
                sensor.close()
        self.assertEqual((temperature, humidity), (25.0, 58.0),
                         "数值必须来自 gpiozero 器件")
        self.assertEqual(note, "", "读成功时说明字段应为空串")

    def test_读数超量程按失败处理(self) -> None:
        """gpiozero 器件给的越界值不许当"正常读数"（缺失就是缺失，绝不补 0）。"""

        class BadSensor(_FakeSensor):
            temperature = 99.0

        with mock.patch.dict(sys.modules, _modules(gpiozero=True, lgpio=True)):
            sys.modules["gpiozero"].DHT11 = lambda *a, **k: BadSensor()   # type: ignore[attr-defined]
            sensor = RUN.Dht11(pin=4, mock=False, retries=1)
            try:
                sensor.open()
                temperature, humidity, note = sensor.read()
            finally:
                sensor.close()
        self.assertIsNone(temperature)
        self.assertIsNone(humidity)
        self.assertIn("量程", note)


if __name__ == "__main__":
    unittest.main(verbosity=2)
