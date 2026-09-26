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

import json
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


class TestStageSnapshots(unittest.TestCase):
    """分级快照 `config/stages.json`：**"每一级开哪些器件"的单一来源**，必须能被机器校验。

    为什么（2026-09-26 用户选定"只存每级配置快照"）：快照一旦写错（名字写错/漏一档），
    后果是"某一级起服务时少开一个器件却说不出哪里不对"。所以这里钉住四条：
    文件可解析且结构完整；器件名都真实存在；`verified` 的级必须留下日期；
    **级别是单调递增的**（后一级 ⊇ 前一级的器件集合）。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.data = stage_run.load_stages()
        cls.stages = cls.data["stages"]
        example = _RPI_DIR / "config" / "devices.example.json"
        cls.known_devices = set(json.loads(example.read_text(encoding="utf-8"))["devices"])

    def test_每一级结构完整(self) -> None:
        for name, info in self.stages.items():
            with self.subTest(stage=name):
                self.assertRegex(name, r"^T\d+$", "级名必须是 T<数字>")
                self.assertTrue(info.get("title"), "缺 title")
                self.assertIn(info.get("status"), ("verified", "planned"), "status 只能是 verified/planned")
                self.assertIsInstance(info.get("devices"), list, "devices 必须是列表")

    def test_器件名都真实存在(self) -> None:
        for name, info in self.stages.items():
            unknown = [d for d in info["devices"] if d not in self.known_devices]
            self.assertEqual(unknown, [], f"{name} 里有配置中不存在的器件名：{unknown}")

    def test_器件不重复(self) -> None:
        for name, info in self.stages.items():
            self.assertEqual(len(info["devices"]), len(set(info["devices"])), f"{name} 有重复器件")

    def test_已验证的级必须留下日期(self) -> None:
        for name, info in self.stages.items():
            if info.get("status") == "verified":
                self.assertTrue(info.get("verified_on"), f"{name} 标了 verified 却没写 verified_on")

    def test_级别单调递增(self) -> None:
        """★ 后一级只能"多开"，不能"少开"——阶梯的本意就是一次只加一个元件。"""
        ordered = stage_run.sorted_stage_names(self.stages)
        previous: set = set()
        for name in ordered:
            current = set(self.stages[name]["devices"])
            missing = previous - current
            self.assertEqual(missing, set(), f"{name} 比上一级少了器件：{sorted(missing)}")
            previous = current

    def test_级别按数字序排(self) -> None:
        """T2 必须在 T10 前面（字符串排序会把 T10/T11 排到 T2 前面）。"""
        ordered = stage_run.sorted_stage_names(self.stages)
        self.assertLess(ordered.index("T2"), ordered.index("T10"))
        self.assertEqual(ordered[0], "T0")

    def test_T0走basic不占rpi配置(self) -> None:
        self.assertEqual(self.stages["T0"]["devices"], [])
        self.assertTrue(self.stages["T0"].get("note"), "T0 必须写明'走 basic/'，否则读者会以为漏配了")

    def test_至少T1T2T3已验证(self) -> None:
        verified = [n for n, i in self.stages.items() if i.get("status") == "verified"]
        for stage in ("T1", "T2", "T3"):
            self.assertIn(stage, verified, f"{stage} 已在真机验收通过，快照里应标 verified")


class TestStageDevices(unittest.TestCase):
    def test_取某一级的器件(self) -> None:
        data = stage_run.load_stages()
        self.assertIn("display", stage_run.stage_devices(data, "t2"), "级名大小写不敏感")
        self.assertEqual(stage_run.stage_devices(data, "T0"), [])

    def test_没有这一级要报错并给出可用列表(self) -> None:
        data = stage_run.load_stages()
        with self.assertRaises(KeyError) as ctx:
            stage_run.stage_devices(data, "T99")
        self.assertIn("T1", str(ctx.exception))

    def test_结构不对要报错(self) -> None:
        tmp = _RPI_DIR / "config" / ".stages.test.json"
        try:
            tmp.write_text('{"note": "没有 stages"}', encoding="utf-8")
            with self.assertRaises(ValueError):
                stage_run.load_stages(tmp)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass


class Test临时阈值覆盖(unittest.TestCase):
    """`--set thresholds.no_motion_timeout_s=20`（2026-09-26 加，为 T4"久无活动"验收）。

    为什么需要：默认 `no_motion_timeout_s=1800`（30 分钟），真机验收不可能等半小时；
    而"临时改阈值"必须**可还原**（`--stop` 从备份恢复）且**写错要报错**（别静默失效）。
    """

    def test_解析各类值(self) -> None:
        got = stage_run.parse_overrides(
            ["thresholds.no_motion_timeout_s=20", "onenet.enabled=true", "x=1.5", "y=abc", "z=null"]
        )
        self.assertEqual(got[0], (["thresholds", "no_motion_timeout_s"], 20))
        self.assertEqual(got[1], (["onenet", "enabled"], True))
        self.assertEqual(got[2], (["x"], 1.5))
        self.assertEqual(got[3], (["y"], "abc"), "解析不了的当字符串")
        self.assertEqual(got[4], (["z"], None))

    def test_缺等号要报错(self) -> None:
        with self.assertRaises(ValueError):
            stage_run.parse_overrides(["thresholds.no_motion_timeout_s"])

    def test_空键要报错(self) -> None:
        with self.assertRaises(ValueError):
            stage_run.parse_overrides(["=20"])

    def test_空列表返回空(self) -> None:
        self.assertEqual(stage_run.parse_overrides(None), [])
        self.assertEqual(stage_run.parse_overrides([]), [])

    def test_按路径写值(self) -> None:
        data = {"thresholds": {"a": 1}}
        stage_run.set_path(data, ["thresholds", "a"], 9)
        stage_run.set_path(data, ["thresholds", "no_motion_timeout_s"], 20)
        stage_run.set_path(data, ["new", "deep", "key"], True)     # 中间层不存在就建
        self.assertEqual(data["thresholds"]["a"], 9)
        self.assertEqual(data["thresholds"]["no_motion_timeout_s"], 20)
        self.assertTrue(data["new"]["deep"]["key"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
