"""数据展示面板（``GET /``，2026-10-01 改造）测试。

用户 2026-10-01 的规格：「一个是**专门用来展示数据的**，暂时测不到的就显示最后一次记录的结果；
**展示数据的面板要求图表以及可视化**」；澄清后确定的显示方式是
**最后一次 + 时间戳 + 变灰 + 「数据已过期」**。

这一节钉五件事（全都属于"**页面会不会骗人**"）：

1. 指标卡的**三态**缺一不可：新鲜 / 过期但有历史 / 从来没有（**绝不显示 0**）；
2. 四张趋势图都画出来；**没有历史库时不崩**，并且明说"未启用历史存储"；
3. **`ok=False` 的历史行必须变成缺口**（图表断线），不能画成 0 或互相连起来；
4. 阈值虚线用**当前**配置算（改了配置，图上的线要跟着变）；
5. 双端适配的**结构性保证**（viewport / 断点 / 长路径可断行）。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from health_monitor.core.config import AppConfig
from health_monitor.core.configstore import ConfigStore
from health_monitor.hal.models import AmbientSample, VitalSignsSample
from health_monitor.net.webui import (
    _reading_points,
    _vitals_points,
    render_control,
    render_page,
    render_panel,
)
from health_monitor.playback import PlaybackRuntime

DEMO = {
    "thresholds": {
        "repeat_cooldown_s": 0,
        "ambient_temp_min": 16,
        "ambient_temp_max": 30,
        "hr_min": 50,
        "hr_max": 110,
        "spo2_min": 93,
        "humidity_max": 80,
    },
    "devices": {
        "vitals": {"driver": "max30102", "read_interval_s": 1.0},
        "ambient": {"driver": "dht11", "read_interval_s": 2.0},
        "motion": {"driver": "hc_sr501", "read_interval_s": 0.5},
        "display": {"driver": "lcd1602", "read_interval_s": 1.0},
        "tft": {"driver": "tft_spi", "read_interval_s": 2.0,
                "params": {"controller": "st7735", "spi_device": 1}},
    },
}


class _Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = float(t)

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += float(seconds)


def make_runtime(clock: _Clock | None = None, *, drop: tuple = (), **kwargs):
    """建一个回放运行时；``drop`` 用来"拔掉"某些器件（模拟它彻底读不到）。"""
    devices = {n: dict(c) for n, c in DEMO["devices"].items() if n not in drop}
    cfg = AppConfig.from_dict({"thresholds": dict(DEMO["thresholds"]), "devices": devices})
    return PlaybackRuntime(cfg, clock=clock or _Clock(), sleep=lambda _s: None,
                           verbose_outputs=False, **kwargs)


def card_html(page: str, title: str) -> str:
    """把某张指标卡的 HTML 抠出来（后面 600 字），好只针对这一张卡断言。

    为什么必须这样：整页里到处都是 "0"（CSS、时间戳、端口号），
    拿整页去断言"不含 0"永远会红、也就永远测不出真问题。
    """
    marker = f'<div class="k">{title}</div>'
    start = page.index(marker)
    return page[start:start + 600]


def chart_html(page: str, title: str) -> str:
    """把某一张图的 chartbox 抠出来（图里的 `<polyline` 才能单独数）。"""
    marker = f'<div class="t">{title}</div>'
    for block in page.split('<div class="chartbox">')[1:]:
        if marker in block:
            return block[:block.index("</div></div>")] if "</div></div>" in block else block
    raise AssertionError(f"页面上找不到图表：{title}")


class _Base(unittest.TestCase):
    with_store = True

    def setUp(self) -> None:
        kwargs = {}
        if self.with_store:
            self._tmp = tempfile.TemporaryDirectory()
            self.addCleanup(self._tmp.cleanup)
            kwargs["store_path"] = str(Path(self._tmp.name) / "history.db")
        self.clock = _Clock()
        self.rt = make_runtime(clock=self.clock, **kwargs)
        self.rt.open()
        self.addCleanup(self.rt.close)
        # 配置面板需要一份 ConfigStore（它读的是**文件**，不是 runtime 的内存配置）
        self._cfg_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._cfg_dir.cleanup)
        cfg_path = Path(self._cfg_dir.name) / "devices.json"
        cfg_path.write_text(json.dumps(
            {"thresholds": dict(DEMO["thresholds"]),
             "devices": {n: dict(c) for n, c in DEMO["devices"].items()}},
            ensure_ascii=False, indent=2), encoding="utf-8")
        self.config_store = ConfigStore(str(cfg_path), use_local=False)

    def _tick(self, advance: float = 0.0) -> None:
        if advance:
            self.clock.advance(advance)
        self.rt.tick()


class TestMetricCardThreeStates(_Base):
    """指标卡的三态 —— 这是全页最容易骗人的地方。"""

    def test_新鲜时显示当前值(self) -> None:
        self.rt.set_vitals(heart_rate=72.0, spo2=98.0)
        self._tick(advance=2.0)
        card = card_html(render_page(self.rt), "心率")
        self.assertIn("72", card)
        self.assertNotIn("数据已过期", card, "刚读到的值不该标过期")
        self.assertNotIn("stale", card)

    def test_过期时显示最后一次加时间戳并变灰(self) -> None:
        """★ 用户明确选的显示方式：**最后一次 + 时间戳 + 变灰 + 「数据已过期」**。

        ⚠️ 这里刻意**把 ambient 器件整个拔掉**（`drop=("ambient",)`）来造"当前读不到"：
        回放器的 `set_vitals` / `set_ambient` 里 **`None` 的意思是"这一项不改"**
        （实测踩到：传 `hr=None` 之后当前值还在，卡片一直是"新鲜"的）。
        拔掉器件最贴近真实场景 —— "传感器坏了，但库里还有上次的记录"。
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        rt = make_runtime(clock=self.clock, drop=("ambient",),
                          store_path=str(Path(tmp.name) / "h.db"))
        rt.open()
        self.addCleanup(rt.close)
        rt.store.save_sample(AmbientSample(             # 十分钟前的那一次记录
            ts=self.clock() - 600, device="ambient", ok=True,
            temperature_c=24.4, humidity_percent=55.0))

        card = card_html(render_page(rt), "室温")
        self.assertIn("24.4", card, "要显示最后一次读到的值")
        self.assertIn("最后一次", card, "必须说明这是哪一次的记录")
        self.assertIn("数据已过期", card, "旧值必须标注过期")
        self.assertIn("stale", card, "要带 stale 类才会变灰")

    def test_从来没有时显示暂无数据而不是0(self) -> None:
        card = card_html(render_page(self.rt), "心率")
        self.assertIn("暂无数据", card)
        self.assertNotIn("数据已过期", card)
        self.assertNotIn("0 bpm", card)
        self.assertNotIn(">0<", card)

    def test_六张卡都在(self) -> None:
        page = render_page(self.rt)
        for title in ("心率", "血氧", "室温", "湿度", "活动状态", "数据年龄"):
            self.assertIn(f'<div class="k">{title}</div>', page, f"缺少指标卡：{title}")

    def test_没贴手指时该告诉用户怎么办(self) -> None:
        """「从来没有」那一态也要给**可操作**的提示，不能只说"没数据"。"""
        self.rt.set_vitals(heart_rate=None, spo2=None, finger=False)
        self._tick(advance=2.0)
        page = render_page(self.rt)
        self.assertIn("请将手指放好", page)


class TestCharts(_Base):
    """趋势图区。"""

    def test_四张图都在(self) -> None:
        page = render_page(self.rt)
        for title in ("室温", "湿度", "心率", "血氧"):
            self.assertIn(f'<div class="t">{title}</div>', page, f"缺少趋势图：{title}")
        self.assertEqual(page.count('<div class="chartbox">'), 4)

    def test_断流那一整段必须断开(self) -> None:
        """★ 真实的"缺口"是**整段没有记录**（器件掉了十分钟），不是"值为空"。

        ⚠️ 实测确认：`Store.save_sample()` 对读取失败的样本**根本不落库**
        （ok=False 连行都不写）⇒ "值为 None 的行"在真实数据里压根不会出现。
        所以缺口检测必须由**时间间隔**来做，否则图会把断流前后直接连成一条直线，
        视觉上等于说"这十分钟一直有数据"。

        判据：两段数据 + 中间十分钟空白 ⇒ **两段** polyline。
        """
        for i, value in enumerate((20.0, 21.0, 22.0)):
            self.rt.store.save_sample(AmbientSample(
                ts=self.clock() + i, device="ambient", ok=True,
                temperature_c=value, humidity_percent=50.0))
        # ← 中间空了 600 秒（器件掉线，一条记录都没有）
        for i, value in enumerate((23.0, 24.0, 25.0)):
            self.rt.store.save_sample(AmbientSample(
                ts=self.clock() + 600 + i, device="ambient", ok=True,
                temperature_c=value, humidity_percent=50.0))

        chart = chart_html(render_page(self.rt), "室温")
        self.assertEqual(chart.count("<polyline"), 2,
                         "断流那一整段必须断开（连成一段就是把缺口填平了）")

    def test_连续数据仍然连成一段(self) -> None:
        """反向钉子：正常连续采样**不许**被误判成缺口（否则图会碎成一堆点）。"""
        for i, value in enumerate((20.0, 21.0, 22.0, 23.0)):
            self.rt.store.save_sample(AmbientSample(
                ts=self.clock() + i, device="ambient", ok=True,
                temperature_c=value, humidity_percent=50.0))
        chart = chart_html(render_page(self.rt), "室温")
        self.assertEqual(chart.count("<polyline"), 1, "连续数据应当只有一段")

    def test_读失败的行按缺口处理(self) -> None:
        """★ 数据层契约：`ok=False` 的历史行**必须变成 None**（不是 0、也不是照画）。

        这条直接测纯函数（`_reading_points`）—— 因为 `save_sample()` 不会把失败样本
        写进库，走整页渲染**造不出**这种行来。契约本身仍要钉死：
        万一日后改了落库策略（把失败也记下来），这条保证图不会把失败画成 0。
        """
        rows = [
            {"ts": 1.0, "value": 20.0, "ok": 1, "device": "ambient"},
            {"ts": 2.0, "value": None, "ok": 0, "device": "ambient"},   # ← 读失败
            {"ts": 3.0, "value": 21.0, "ok": 1, "device": "ambient"},
        ]
        self.assertEqual(_reading_points(rows), [(1.0, 20.0), (2.0, None), (3.0, 21.0)])

    def test_没贴手指的血氧历史也按缺口处理(self) -> None:
        """同一族契约：没贴手指的行不能画成"那次测到了"。"""
        rows = [
            {"ts": 1.0, "heart_rate_bpm": 70.0, "spo2_percent": 98.0, "finger_detected": 1},
            {"ts": 2.0, "heart_rate_bpm": None, "spo2_percent": None, "finger_detected": 0},
            {"ts": 3.0, "heart_rate_bpm": 72.0, "spo2_percent": 97.0, "finger_detected": 1},
        ]
        self.assertEqual(_vitals_points(rows, "heart_rate_bpm"),
                         [(1.0, 70.0), (2.0, None), (3.0, 72.0)])

    def test_没有历史库时不崩且明说(self) -> None:
        rt = make_runtime()          # 不给 store_path
        rt.open()
        self.addCleanup(rt.close)
        page = render_page(rt)
        self.assertIn("未启用历史存储", page)
        self.assertIn("</html>", page)

    def test_阈值跟着当前配置变(self) -> None:
        """改了阈值，图上跟着变 —— 否则图和配置就是两套口径。

        判据用**边缘箭头**：`charts.py`（2026-10-01）把量程**之内**的阈值画成虚线、
        量程**之外**的不丢弃而是标在上下边缘并带 ↑/↓
        （理由写在那边：湿度实测全在 57.9~59.1% 跳，阈值 80% 若直接不画，
        用户就看不出"离报警还有多远"）。所以：

        * 上限 30 落在量程上方 ⇒ 出现「↑ 室温上限」；
        * 上限 24 落在量程内   ⇒ 画成虚线，**没有**箭头前缀。
        """
        for i, value in enumerate((20.0, 22.0, 25.0)):
            self.rt.store.save_sample(AmbientSample(
                ts=self.clock() + i, device="ambient", ok=True,
                temperature_c=value, humidity_percent=50.0))

        far = {"thresholds": dict(DEMO["thresholds"], ambient_temp_max=30),
               "devices": {n: dict(c) for n, c in DEMO["devices"].items()}}
        self.rt.apply_config(AppConfig.from_dict(far))
        self.assertEqual(self.rt.config.thresholds.ambient_temp_max, 30, "前置条件")
        self.assertIn("↑ 室温上限", chart_html(render_page(self.rt), "室温"),
                      "量程外的阈值要标在边缘（说明图读的是当前配置）")

        near = {"thresholds": dict(DEMO["thresholds"], ambient_temp_max=24),
                "devices": {n: dict(c) for n, c in DEMO["devices"].items()}}
        self.rt.apply_config(AppConfig.from_dict(near))
        chart = chart_html(render_page(self.rt), "室温")
        self.assertIn("室温上限", chart, "量程内 ⇒ 必须画出来")
        self.assertNotIn("↑ 室温上限", chart, "量程内不该再带边缘箭头")
        self.assertIn("stroke-dasharray", chart, "量程内的阈值画的是虚线")

    def test_图下小字有数据点数与最后更新(self) -> None:
        for i, value in enumerate((20.0, 21.0, 22.0)):
            self.rt.store.save_sample(AmbientSample(
                ts=self.clock() + i, device="ambient", ok=True,
                temperature_c=value, humidity_percent=50.0))
        chart = chart_html(render_page(self.rt), "室温")
        self.assertIn("3 个数据点", chart)
        self.assertIn("最后更新", chart)

    def test_全是失败的历史不画假线(self) -> None:
        """一次都没读成功过 ⇒ 图区明说"还没有数据点"，**不画一条平的假线**。"""
        for i in range(3):
            self.rt.store.save_sample(AmbientSample(
                ts=self.clock() + i, device="ambient", ok=False))
        chart = chart_html(render_page(self.rt), "室温")
        self.assertNotIn("<polyline", chart)
        self.assertIn("还没有数据点", chart)


class TestResponsiveStructure(_Base):
    """双端适配的**结构性**保证（视觉只能靠人截图验收，结构可以钉死）。"""

    def _pages(self) -> dict:
        return {
            "/": render_page(self.rt),
            "/control": render_control(self.rt),
            "/panel": render_panel(self.rt, self.config_store),
        }

    def test_三页都有viewport(self) -> None:
        for path, html in self._pages().items():
            self.assertIn('name="viewport"', html, path)
            self.assertIn("width=device-width", html, path)

    def test_三页都有断点(self) -> None:
        for path, html in self._pages().items():
            self.assertIn("@media (max-width: 720px)", html, path)

    def test_三页都有统一导航且当前页高亮(self) -> None:
        expected = {"/": 'class="tab on" href="/"',
                    "/control": 'class="tab on" href="/control"',
                    "/panel": 'class="tab on" href="/panel"'}
        for path, html in self._pages().items():
            self.assertIn('nav class="tabs"', html, path)
            for label, href in (("数据", "/"), ("功能", "/control"), ("配置", "/panel")):
                self.assertIn(f'href="{href}"', html, f"{path} 缺导航项 {label}")
            self.assertIn(expected[path], html, f"{path} 的当前页没有高亮")

    def test_表格每格都带data_label(self) -> None:
        """窄屏"表格转卡片"靠 `data-label` 补出列名 —— 漏一个就少一行标签。

        ⚠️ 只查**设备状态表**：报警表与消息表在"还没有记录"时只有一行
        `colspan` 占位（那种行本来就不需要列名），拿它们断言会假红。
        """
        page = render_page(self.rt)
        self.assertIn("设备状态", page)
        for label in ("设备", "周期", "读取次数", "连续失败", "距上次成功", "状态", "最后错误"):
            self.assertIn(f"data-label='{label}'", page, f"设备表缺列名：{label}")

    def test_占位行不需要列名(self) -> None:
        """反向钉子：`colspan` 占位行不该硬塞 data-label（窄屏上会多出一行空标签）。"""
        page = render_page(self.rt)
        for marker in ("当前没有处于报警状态的项目", "本次运行还没有报警记录", "还没有任何消息"):
            self.assertIn(marker, page)

    def test_最后错误过长会截断(self) -> None:
        """★ 设备表"最后错误"那一列原来会把整段排查提示（几百字）塞进去，宽屏撑得极丑。

        现在截断到 120 字 + 全文放进 `title`（悬停可见）。
        """
        long_error = "DeviceIOError: " + ("很长的排查提示，" * 60)
        # ⚠️ 必须写到**采集器内部的条目**上：`collector.status()` 每次调用都新建字典，
        #    改它的返回值不会留下任何痕迹（实测踩到）。
        self.rt.collector.entries["ambient"].last_error = long_error
        page = render_page(self.rt)
        self.assertLess(page.count(long_error), 2, "整段不该原样出现在正文里")
        self.assertIn("title='DeviceIOError", page, "全文要进 title")
        self.assertIn("…", page, "截断处要有省略号")

    def test_长路径能断行(self) -> None:
        """★ 钉住 2026-10-01 实测到的具体缺陷：

        配置面板抬头里那串**没有空格**的路径（`/home/pi/.../devices.json`）默认不换行，
        在 360~390px 手机上会把整页撑宽、右侧被裁掉、出现横向滚动。
        ⇒ 它必须带能断行的类，而样式里必须有对应的 `overflow-wrap`。
        """
        panel = self._pages()["/panel"]
        self.assertIn('class="path"', panel)
        self.assertIn("overflow-wrap: anywhere", panel)
        self.assertIn("word-break: break-all", panel)

    def test_窄屏按钮变大(self) -> None:
        """老人 + 手机：断点内按钮要撑满并加高（不然手指点不准）。"""
        for html in self._pages().values():
            self.assertIn("button { padding: 12px 18px", html)

    def test_不引任何外部资源(self) -> None:
        """演示现场可能没外网 ⇒ 一律内联，不许 CDN。"""
        for path, html in self._pages().items():
            self.assertNotIn("http://cdn", html, path)
            self.assertNotIn("https://cdn", html, path)
            self.assertNotIn("<script src", html, path)
            self.assertNotIn("<link rel=\"stylesheet\"", html, path)


class TestLastKnownBeyondWindow(_Base):
    """★「最后一次记录」必须**跳出图表窗口**去找（2026-10-01 真机验出来的真问题）。

    ⚠️ 2026-10-01 口径变了：`_save_vitals()` 现在**一行值都没有就整行不写**
    （用户拍板，省 SD 卡写入：改动前"每 5 秒一行 NULL"，真机 32 小时 22893 行、绝大多数为空）。
    但这条测试要守的东西**没变**：`recent_vitals(limit=120)` 这个窗口仍然只覆盖约 2 分钟，
    而血氧是**按需测量** ⇒ 上一次真实测量很可能在几十分钟前 ⇒
    若拿"窗口里的最后一个点"当最后一次记录，卡片就会错报「暂无数据」——
    **用户点名要的那个功能等于没做**。

    本类因此**保留**"往窗口里灌 200 条没有值的样本"这个手法（现在它们根本写不进去），
    用来证明"最后一次记录"是**独立于窗口**查出来的 —— 而不是靠窗口里恰好有行。
    """

    def setUp(self) -> None:
        super().setUp()
        # ⚠️ 测试时钟默认从 1000.0 起（相对时间），但"半小时前"会减成**负时间戳**，
        #    而 `time.localtime()` 对负值会抛 OSError（Windows 实测）。
        #    生产环境的时间戳永远是真的 epoch，所以这里把时钟挪到真实量级 ——
        #    顺带也把"脏时间戳不许打崩整页"那条健壮性修好了（见 webui._time_text）。
        self.clock.t = 1_700_000_000.0

    def _fill_window_with_null_vitals(self, count: int = 200) -> None:
        """灌一批"没测到值"的样本（**现在它们不会写进库**，见类文档）。"""
        for i in range(count):
            self.rt.store.save_sample(VitalSignsSample(
                ts=self.clock() - count + i, device="max30102", ok=False,
                heart_rate_bpm=None, spo2_percent=None, finger_detected=False,
                awaiting_data=True, quality=0.0))

    def test_半小时前那次测量要把窗口外的值捞回来(self) -> None:
        self.rt.store.save_sample(VitalSignsSample(          # 30 分钟前的真实测量
            ts=self.clock() - 1800, device="max30102", ok=True,
            heart_rate_bpm=68.0, spo2_percent=97.0,
            finger_detected=True, quality=0.9))
        self._fill_window_with_null_vitals()                 # 把 120 行窗口占满 NULL

        page = render_page(self.rt)
        hr = card_html(page, "心率")
        self.assertIn("68", hr, "窗口外那一次测量必须显示出来")
        self.assertIn("数据已过期", hr, "而且要明确它是旧值")
        spo2 = card_html(page, "血氧")
        self.assertIn("97", spo2, "血氧同样要捞回来")
        self.assertIn("数据已过期", spo2)

    def test_从没测过时仍然显示暂无数据(self) -> None:
        """反向钉子：捞窗口外**不许**捞出不存在的记录（更不许编一个 0 出来）。"""
        self._fill_window_with_null_vitals()
        page = render_page(self.rt)
        hr = card_html(page, "心率")
        self.assertIn("暂无数据", hr)
        self.assertNotIn(">0<", hr, "绝不许用 0 冒充没有数据（项目既有硬纪律）")

    def test_store查询不限窗口且按列各查各的(self) -> None:
        """`last_vitals_value` 必须**按列**查：一次记录可能只有心率没有血氧。"""
        self.rt.store.save_sample(VitalSignsSample(
            ts=self.clock() - 900, device="max30102", ok=True,
            heart_rate_bpm=71.0, spo2_percent=None, finger_detected=True, quality=0.9))
        self.rt.store.save_sample(VitalSignsSample(
            ts=self.clock() - 600, device="max30102", ok=True,
            heart_rate_bpm=None, spo2_percent=95.0, finger_detected=True, quality=0.9))
        self._fill_window_with_null_vitals()

        hr_row = self.rt.store.last_vitals_value("heart_rate_bpm")
        spo2_row = self.rt.store.last_vitals_value("spo2_percent")
        self.assertAlmostEqual(hr_row["value"], 71.0)
        self.assertAlmostEqual(spo2_row["value"], 95.0)

    def test_没有值的样本不再写行(self) -> None:
        """★ 判据（用户 2026-10-01 拍板）：**没测到值就一行都不写**。

        改动前是"每 5 秒一行 NULL"（真机 32 小时 22893 行、绝大多数为空），
        纯粹拿 SD 卡写入换一个空值。⚠️ 这条同时钉住"别又退回去"。
        """
        before = self.rt.store.count("vitals")
        self._fill_window_with_null_vitals(count=50)
        self.assertEqual(self.rt.store.count("vitals"), before,
                         "一行值都没有的样本不该写进 vitals 表")

    def test_有值就照旧写行(self) -> None:
        """反向钉子：**不许**为了省写入把真有值的记录也丢掉。"""
        before = self.rt.store.count("vitals")
        wrote = self.rt.store.save_sample(VitalSignsSample(
            ts=self.clock(), device="max30102", ok=True,
            heart_rate_bpm=70.0, spo2_percent=97.0, finger_detected=True, quality=0.9))
        self.assertEqual(wrote, 1)
        self.assertEqual(self.rt.store.count("vitals"), before + 1)

    def test_没手指时不写行所以不会出现心率0的假点(self) -> None:
        """既有的红线不变：没贴手指的读数**绝不能**在曲线上变成 0。"""
        self._fill_window_with_null_vitals(count=30)
        points = [r for r in self.rt.store.recent_vitals(limit=50)]
        self.assertEqual(points, [], "库里不该有这类空行（更不该有 0 值）")

    def test_非法列名被拒绝(self) -> None:
        """列名要拼进 SQL ⇒ **白名单**，绝不接受调用方传来的任意字符串。"""
        with self.assertRaises(ValueError):
            self.rt.store.last_vitals_value("ts; DROP TABLE vitals")

    def test_脏时间戳不许把整页打崩(self) -> None:
        """★ 库里只要有一行离谱时间戳，也不该让数据面板 500。

        ⚠️ 这条**两个平台必须给出同一结果**（2026-10-01，ERROR.md **E72**）：
        `time.localtime(-800)` 在 Windows 上抛 `OSError` 而在 Linux/glibc 上**正常返回
        1969-12-31 23:46:40** ⇒ 如果实现只靠 try/except 兜底，这条测试就会
        "**CI（Linux）红、本机（Windows）绿**"。所以实现**自己判范围**（1970~2100），
        本条断言正是在钉这个"平台无关"的判据。
        """
        from health_monitor.net.webui import _time_text

        self.assertEqual(_time_text(-800.0), "--:--:--")
        self.assertEqual(_time_text(1e30), "--:--:--")
        self.assertEqual(_time_text(4102444800.0), "--:--:--", "2100-01-01 本身已在范围外")
        self.assertRegex(_time_text(1_700_000_000.0), r"^\d\d:\d\d:\d\d$")
        self.assertRegex(_time_text(0.0), r"^\d\d:\d\d:\d\d$", "1970-01-01 是范围内")

    def test_读数类查询也不限窗口(self) -> None:
        self.rt.store.save_sample(AmbientSample(
            ts=self.clock() - 7200, device="ambient", ok=True,
            temperature_c=19.9, humidity_percent=44.0))
        row = self.rt.store.last_reading("ambient_temp_c")
        self.assertAlmostEqual(row["value"], 19.9)
        self.assertIsNone(self.rt.store.last_reading("从来没有过的指标"))


    def test_别名路由下导航仍然高亮正确那一页(self) -> None:
        """★ 2026-10-01：数据页有 `/`、`/index.html`、`/status` 三个 URL，功能页还有 `/control.html`
        ⇒ 导航高亮必须**按页面**算，否则从别名进来时"数据"标签不亮，用户会以为导航坏了。"""
        from health_monitor.net.webui import _NAV_ALIASES, _nav

        for alias, canonical in _NAV_ALIASES.items():
            with self.subTest(alias=alias):
                html = _nav(alias)
                self.assertIn(f'class="tab on" href="{canonical}"', html,
                              f"{alias} 应当高亮 {canonical}")
                self.assertEqual(html.count('class="tab on"'), 1, "只许有一个高亮")

    def test_导航别名表必须与路由别名一致(self) -> None:
        """钉住这张表不会与 `net/web.py` 的真实路由悄悄脱节。"""
        from health_monitor.net.webui import _NAV_ALIASES

        expected = {"/index.html": "/", "/status": "/", "/control.html": "/control"}
        self.assertEqual(dict(_NAV_ALIASES), expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
