"""`stage_run.py` 的纯逻辑测试（分级验收"只开本级器件"的筛选 + **绝不误杀进程**）。

为什么测（2026-09-26）：
1. `--only` 一旦写错，最坏结果是**静默什么都不开**（服务起来但没有任何器件）
   或者**多开了还没接的器件**（满屏报错把本级信息淹掉）⇒ 抽成纯函数锁死；
2. 🔴 **Windows 上 `os.kill(pid, 0)` 是"杀进程"不是"探活"**（CPython 走 TerminateProcess）——
   第一版单测里那句 `pid_alive(os.getpid())` **把 pytest 进程自己杀了**，
   命令零输出、连开发用的 Harness 都被打断两次。
   所以这里**钉死一条**：Windows 上 `pid_exists()` 必须返回"未知"，
   **且绝不能调用 `os.kill`**（用 monkeypatch 监视它，一旦被调就失败）。
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

_RPI_DIR = Path(__file__).resolve().parents[2]      # .../rpi
_SCRIPTS_DIR = _RPI_DIR / "scripts"
for _path in (_RPI_DIR, _SCRIPTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import stage_run  # noqa: E402


class TestParseOnly(unittest.TestCase):
    def test_逗号去空白去重保序(self) -> None:
        self.assertEqual(
            stage_run.parse_only(" ambient, display ,ambient "),
            ["ambient", "display"],
        )

    def test_空与None(self) -> None:
        self.assertEqual(stage_run.parse_only(None), [])
        self.assertEqual(stage_run.parse_only(""), [])
        self.assertEqual(stage_run.parse_only(" , "), [])


class TestSelectEnabled(unittest.TestCase):
    DEVICES = {
        "vitals": {},
        "ambient": {},
        "display": {},
        "alarm_buzzer": {},
        "status_led": {},
    }

    def test_只开点名的且保持配置顺序(self) -> None:
        enabled, unknown, disabled = stage_run.select_enabled(
            self.DEVICES, ["status_led", "ambient"]
        )
        self.assertEqual(enabled, ["ambient", "status_led"])
        self.assertEqual(unknown, [])
        self.assertEqual(disabled, ["vitals", "display", "alarm_buzzer"])

    def test_名字写错必须能被发现(self) -> None:
        """★ 关键：写错名字**不能**变成"什么都没开却看起来正常"。"""
        enabled, unknown, _ = stage_run.select_enabled(self.DEVICES, ["ambientt"])
        self.assertEqual(enabled, [])
        self.assertEqual(unknown, ["ambientt"])

    def test_不给only时一个都不关(self) -> None:
        enabled, unknown, disabled = stage_run.select_enabled(self.DEVICES, [])
        self.assertEqual(enabled, list(self.DEVICES))
        self.assertEqual((unknown, disabled), ([], []))

    def test_全体点名等价于不限制(self) -> None:
        enabled, _, disabled = stage_run.select_enabled(self.DEVICES, list(self.DEVICES))
        self.assertEqual(enabled, list(self.DEVICES))
        self.assertEqual(disabled, [])

    def test_空配置不炸(self) -> None:
        self.assertEqual(stage_run.select_enabled({}, ["ambient"]), ([], ["ambient"], []))


class TestPidExistsNeverKills(unittest.TestCase):
    """🔴 存活判断**绝不许**变成"发信号"（Windows 上 `os.kill` 会真杀进程）。"""

    def test_非法pid直接False(self) -> None:
        self.assertFalse(stage_run.pid_exists(0))
        self.assertFalse(stage_run.pid_exists(-1))

    @unittest.skipUnless(os.name == "nt", "只在 Windows 上有这条危险语义")
    def test_windows上返回未知而不是探活(self) -> None:
        self.assertIsNone(stage_run.pid_exists(1234))

    @unittest.skipUnless(os.name == "nt", "只在 Windows 上有这条危险语义")
    def test_windows上绝不调用os_kill(self) -> None:
        """★ 回归钉：把 `os.kill` 换成"一被调用就炸"的桩，断言 `pid_exists` 不碰它。"""
        with mock.patch.object(
            stage_run.os, "kill", side_effect=AssertionError("不许用 os.kill 探活！")
        ):
            self.assertIsNone(stage_run.pid_exists(os.getpid()))

    @unittest.skipIf(os.name == "nt", "POSIX 上 os.kill(pid,0) 才是正常的探活方式")
    def test_posix上仍在用os_kill探活(self) -> None:
        self.assertTrue(stage_run.pid_exists(os.getpid()))


class TestServiceAlive(unittest.TestCase):
    """判活的口径 = **问服务自己的健康接口**（不看 PID、不看端口占用）。"""

    def test_健康接口返回json算活着(self) -> None:
        with mock.patch.object(stage_run, "api_get", return_value='{"ok": true}'):
            self.assertTrue(stage_run.service_alive(8090))

    def test_失败说明不算活着(self) -> None:
        for text in ("(请求 /api/v1/health 失败：URLError)", "", "<html>hello</html>"):
            with mock.patch.object(stage_run, "api_get", return_value=text):
                self.assertFalse(stage_run.service_alive(8090), f"不该把 {text!r} 当作活着")


class TestStateFile(unittest.TestCase):
    def test_状态文件缺失或损坏都不抛(self) -> None:
        tmp = _RPI_DIR / "config" / ".stage-run.test.json"
        real = stage_run.STATE_PATH
        try:
            stage_run.STATE_PATH = tmp
            self.assertEqual(stage_run.read_state(), {})          # 不存在
            tmp.write_text("{ 坏 json", encoding="utf-8")
            self.assertEqual(stage_run.read_state(), {})          # 坏文件
            tmp.write_text('["不是字典"]', encoding="utf-8")
            self.assertEqual(stage_run.read_state(), {})          # 类型不对
        finally:
            stage_run.STATE_PATH = real
            try:
                tmp.unlink()
            except OSError:
                pass


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
