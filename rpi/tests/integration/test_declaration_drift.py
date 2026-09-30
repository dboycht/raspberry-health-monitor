"""守卫：几对"**声明面 ↔ 实现面**"清单必须一致（2026-10-01）。

## 为什么值得单独一个文件

2026-10-01 那轮抓到两个"文档说存在、实际不存在"的接口（`ERROR.md` **E68**）：
`POST /api/v1/cloud/callback` 因为**从没注册进路由表**而永远是 404，
`GET /api/v1/cloud/last` **只在 docstring 里被承诺过**。
它们的共同形状是：**两套本该一致的清单，漂移了，而没有任何东西会报警。**

那个教训的普适版是：**凡有"声明面"与"实现面"之分的地方，都要配一条双向守卫。**
`test_api_contract_docs.py` 管的是"路由 ↔ 契约文档"；本文件管**代码内部**那几对。

## 这里守的四对，失败方式**全都是静默的**

| 声明面 | 实现面 | 漂移后的现象 |
| --- | --- | --- |
| `THRESHOLD_GROUPS`（面板表单要渲染哪些阈值） | `Thresholds` 的真实字段 | 新增阈值**不出现在面板上** —— 用户改不到，且**一声不响** |
| 代码/注释里提到的 `/api/v1/...` | 路由表 | 提到一个不存在的端点（**E68 就是这么来的**） |
| `_METRICS` 的 key | `health_summary()` 的字段 | 那张卡**永远显示"暂无数据"**（看着像传感器没数据，其实是 key 写错了） |
| `ALARM_LABELS` / `KIND_LABELS` | `AlarmCode` 成员 / 消息类别 | 网页上**直接显示英文码**（能看出不对，但没人会主动去查） |

**四对在 2026-10-01 实测都是一致的** —— 也就是说这几条守卫现在**不会红**；
它们的价值在于**锁住**这个状态（并且每一条都配了"抽取器自证"，
免得将来变成"因为它什么都没读到，所以永远绿"）。
"""

from __future__ import annotations

import dataclasses
import re
import unittest
from pathlib import Path

from health_monitor.core.config import AppConfig, Thresholds
from health_monitor.hal.models import AlarmCode
from health_monitor.net import webui                          # 模块级引用，见下面的说明
from health_monitor.playback import PlaybackRuntime

ROOT = Path(__file__).resolve().parents[3]
RPI = ROOT / "rpi"

#: ⚠️ 刻意**不用** `from ... import THRESHOLD_GROUPS` 那种写法（第一版就是那样，已改）。
#: 原因：`from X import Y` 会在 **import 时把值绑死**，于是注入自测（把声明面改坏一次，
#: 断言守卫真的会红）**根本注入不进去** —— 守卫读到的永远是最初那份常量，
#: 注入测试会"永远是绿的"。改成每次调用时读 `webui.THRESHOLD_GROUPS`，
#: 注入才真的生效（这也是本项目"注入型自测必须断言注入真的生效"那条纪律的具体做法）。

DEMO = {
    "thresholds": {"no_motion_timeout_s": 60, "repeat_cooldown_s": 0},
    "devices": {
        "vitals": {"driver": "max30102", "read_interval_s": 1.0},
        "ambient": {"driver": "dht11", "read_interval_s": 3.0},
        "motion": {"driver": "hc_sr501", "read_interval_s": 0.5},
    },
}


class TestThresholdFieldsReachableFromPanel(unittest.TestCase):
    """① 每个阈值字段都要能在面板上改到；面板上也不许有已不存在的字段。"""

    def _fields(self) -> set[str]:
        return {f.name for f in dataclasses.fields(Thresholds)}

    def _panel(self) -> set[str]:
        return {name for _title, entries in webui.THRESHOLD_GROUPS
                for name, _label, _unit in entries}

    def test_抽取器真的抽到了东西(self) -> None:
        self.assertGreaterEqual(len(self._fields()), 15)
        self.assertGreaterEqual(len(self._panel()), 15)

    def test_每个阈值字段都能在面板上改(self) -> None:
        missing = sorted(self._fields() - self._panel())
        self.assertEqual(
            missing, [],
            "这些阈值在 Thresholds 里有、面板上却没有 ⇒ **用户改不到、而且没有任何报错**："
            f"{missing}。请加进 webui.THRESHOLD_GROUPS。",
        )

    def test_面板上没有已不存在的字段(self) -> None:
        extra = sorted(self._panel() - self._fields())
        self.assertEqual(extra, [], f"面板上这些字段 Thresholds 里已经没有了（提交会 400）：{extra}")


class TestMentionedApiPathsExist(unittest.TestCase):
    """② 代码里提到的 `/api/v1/...` 必须真的在路由表里（**E68 的钉子**）。"""

    def _routes(self) -> set[str]:
        text = (RPI / "health_monitor" / "net" / "web.py").read_text(encoding="utf-8")
        return {m.group(2) for m in
                re.finditer(r'\("(GET|POST)",\s*"(/api/v1[^"]*)"\)', text)}

    def _mentioned(self) -> dict[str, list[str]]:
        """扫源码（**不含 tests** —— 测试里会故意写不存在的端点来验 404）。"""
        found: dict[str, list[str]] = {}
        for path in sorted((RPI / "health_monitor").rglob("*.py")):
            if "__pycache__" in str(path):
                continue
            text = path.read_text(encoding="utf-8")
            for m in re.finditer(r"/api/v1/[A-Za-z0-9_/]*", text):
                url = m.group(0).rstrip("/")
                if url.endswith("/api/v1"):
                    continue
                found.setdefault(url, []).append(str(path.relative_to(RPI)))
        return found

    def test_抽取器真的抽到了东西(self) -> None:
        self.assertGreaterEqual(len(self._routes()), 10)
        self.assertGreaterEqual(len(self._mentioned()), 10)

    def test_提到的每个端点都真的注册了(self) -> None:
        routes = self._routes()
        unknown = []
        for url, where in sorted(self._mentioned().items()):
            if url in routes:
                continue
            if any(p.startswith(url + "/") for p in routes):   # 注释里写"整组"的前缀
                continue
            unknown.append((url, sorted(set(where))))
        self.assertEqual(
            unknown, [],
            "这些 /api/v1/... 在代码里被提到，但**路由表里没有**（E68 的形状："
            f"handler 写了却从没注册 ⇒ 永远 404）：{unknown}",
        )


class TestDashboardMetricsExist(unittest.TestCase):
    """③ 仪表盘每张卡要的 key，`health_summary()` 必须真的产出。"""

    def test_每张卡的key都能从快照拿到(self) -> None:
        rt = PlaybackRuntime(config=AppConfig.from_dict(DEMO))
        try:
            rt.open()
            available = set(rt.collector.snapshot().health_summary())
        finally:
            rt.close()
        self.assertGreaterEqual(len(available), 8, "抽取器没拿到快照字段")
        missing = [m.key for m in webui._METRICS if m.key not in available]
        self.assertEqual(
            missing, [],
            "这些指标卡要的 key 不在 health_summary() 里 ⇒ 那张卡**永远显示"
            f"「暂无数据」**（看着像没数据，其实是 key 写错了）：{missing}",
        )


class TestAlarmLabelsCoverEveryCode(unittest.TestCase):
    """④ 每个报警码都要有中文名、每个消息类别都要有类别名。"""

    def test_每个报警码都有中文名(self) -> None:
        codes = {c.value for c in AlarmCode}
        self.assertGreaterEqual(len(codes), 12, "抽取器没拿到报警码")
        missing = sorted(codes - set(webui.ALARM_LABELS))
        self.assertEqual(missing, [], f"这些报警码没有中文名，网页会显示英文码：{missing}")

    def test_没有已删报警码的残留标签(self) -> None:
        extra = sorted(set(webui.ALARM_LABELS) - {c.value for c in AlarmCode})
        self.assertEqual(extra, [], f"这些中文标签对应的报警码已经没有了：{extra}")

    def test_消息类别标签齐全(self) -> None:
        # 与 hal/models.py 的 message_kind() 返回值保持一致
        self.assertEqual(sorted(webui.KIND_LABELS), ["alarm", "clear", "info", "record"])


class TestInjectionSelfTest(unittest.TestCase):
    """**注入式反证**：真的把"声明面"改坏一次，断言判据会红。

    项目纪律（`memory/30`）：守卫必须**先抓到一次真的坏东西**再上岗 ——
    光有"抽取器抽到了东西"不够，还要证明**判据不是无论如何都绿**。
    注入方式是**违反规则**（把面板里的阈值字段砍掉），不是"删掉断言"。
    """

    def _drifted(self):
        return [("血氧", [("spo2_min", "血氧下限", "%")])]      # 其余 17 个字段全砍掉

    def test_注入真的生效(self) -> None:
        """先证明"注入得进去" —— 第一版用 `from ... import` 绑死了常量，
        注入根本不起作用，注入测试会变成"永远是绿的"。"""
        original = webui.THRESHOLD_GROUPS
        try:
            webui.THRESHOLD_GROUPS = self._drifted()
            panel = {n for _t, es in webui.THRESHOLD_GROUPS for n, _l, _u in es}
            self.assertEqual(panel, {"spo2_min"}, "注入没生效（常量被绑死了？）")
        finally:
            webui.THRESHOLD_GROUPS = original

    def test_注入后真守卫确实会红_复原后又绿(self) -> None:
        case = TestThresholdFieldsReachableFromPanel("test_每个阈值字段都能在面板上改")
        original = webui.THRESHOLD_GROUPS
        try:
            webui.THRESHOLD_GROUPS = self._drifted()
            with self.assertRaises(AssertionError, msg="判据是坏的：注入后居然还是绿的"):
                case.test_每个阈值字段都能在面板上改()
        finally:
            webui.THRESHOLD_GROUPS = original
        case.test_每个阈值字段都能在面板上改()      # 复原后必须重新变绿


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
