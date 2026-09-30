"""树莓派端**状态网页**（`GET /`）—— 手机 App 之外的第二块屏。

为什么需要它
------------
1. **答辩现场可视化**：把树莓派接到 HDMI 屏 / 电脑浏览器，一眼能看到全部指标与最近报警，
   不用装 App、不用抓 JSON；
2. **课程叙事里"配套网页"的那一半**：题目要求"子女可远程查看"，网页版是最低成本的实现；
3. **排查利器**：出问题时打开这个页面，能同时看到"读数 / 新鲜度 / 每个设备的状态 / 最近报警"。

设计约束（刻意的）
------------------
- **纯标准库**：服务端拼字符串，不用模板引擎、不引入前端构建链、不依赖外网 CDN
  （树莓派可能没有外网，答辩现场更不能白屏）；
- **无 JavaScript 也能用**：用 ``<meta http-equiv="refresh">`` 自动刷新；
  顺手加了几行内联 JS 只在"页面可见时才刷新"，不可用时退化为定时整页刷新；
- **只读**：这个页面不做任何写操作（消音/求助仍在 API 里，避免误点导致误报解除）；
- **中文界面 + 无 markdown 标记**（界面文本里出现 markdown 会原样露出，见项目排错记录）。

⚠️ 与 JSON API 的关系：本模块只 **读** :class:`~health_monitor.net.web.WebApi` 依赖的
同一个 runtime 对象，不改变任何 API 行为。
"""

from __future__ import annotations

import html
from typing import Any, Dict, List, NamedTuple, Tuple

from ..hal.models import message_kind
from .charts import line_chart, sparkline

#: 报警码 → 中文（网页用；与安卓端 AlarmCatalog 保持同口径，改一处要改另一处）
ALARM_LABELS: Dict[str, str] = {
    "hr_too_high": "心率过高",
    "hr_too_low": "心率过低",
    "spo2_too_low": "血氧过低",
    "ambient_temp_high": "室温偏高",
    "ambient_temp_low": "室温偏低",
    "humidity_high": "湿度过高",
    "no_motion_too_long": "长时间无活动（疑似跌倒）",
    "night_frequent_wake": "夜间频繁起夜",
    "sos_pressed": "紧急求助",
    "sensor_fault": "传感器故障",
    "device_offline": "设备离线",
    "system_start": "系统已启动",
    "all_clear": "已恢复正常",
    "alarm_silenced": "已消音",
    "spo2_measured": "血氧测量",
}

#: 消息类别 → 中文（"最近消息"那一节的分组徽标；判据见 `hal.models.message_kind`）
KIND_LABELS: Dict[str, str] = {
    "alarm": "报警", "record": "记录", "clear": "解除", "info": "信息",
}

_MOTION_LABELS = {"detected": "有活动", "idle": "无活动", "unknown": "未知"}
_SEVERITY_NAMES = {0: "正常", 1: "提示", 2: "警告", 3: "紧急"}

_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin: 0; font-family: "Microsoft YaHei", "PingFang SC", system-ui, sans-serif;
       background: #f4f6f8; color: #1c2430; }
header { background: #1f6feb; color: #fff; padding: 14px 20px; }
header h1 { margin: 0; font-size: 19px; font-weight: 600; }
header .meta { margin-top: 4px; font-size: 12px; opacity: .9; }
main { max-width: 980px; margin: 0 auto; padding: 16px 20px 40px; }
.cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); gap: 12px; }
.card { background: #fff; border: 1px solid #dfe3e8; border-radius: 10px; padding: 12px 14px; }
.card .k { font-size: 12px; color: #5b6673; }
.card .v { font-size: 26px; font-weight: 600; margin-top: 6px; }
.card .v.unknown { color: #98a2b0; font-size: 20px; }
.card .s { font-size: 12px; margin-top: 6px; color: #5b6673; }
.card.warn { border-color: #e8a33d; background: #fff8ec; }
.card.bad { border-color: #d9534f; background: #fdf0ef; }
.banner { border-radius: 10px; padding: 12px 14px; margin-bottom: 14px; font-weight: 600; }
.banner.ok { background: #e7f6ec; border: 1px solid #58a55c; color: #24632c; }
.banner.warn { background: #fff8ec; border: 1px solid #e8a33d; color: #8a5a00; }
.banner.bad { background: #fdf0ef; border: 1px solid #d9534f; color: #8a2b28; }
h2 { font-size: 15px; margin: 22px 0 8px; }
table { width: 100%; border-collapse: collapse; background: #fff; border: 1px solid #dfe3e8;
        border-radius: 10px; overflow: hidden; font-size: 13px; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #eef1f4; }
th { background: #f7f9fb; font-weight: 600; color: #45505c; }
tr:last-child td { border-bottom: none; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
.pill { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 12px; }
.pill.ok { background: #e7f6ec; color: #24632c; }
.pill.warn { background: #fff3dd; color: #8a5a00; }
.pill.bad { background: #fdeceb; color: #8a2b28; }
footer { margin-top: 24px; font-size: 12px; color: #6b7480; }
code { background: #eef1f4; padding: 1px 5px; border-radius: 4px; font-size: 12px; }

/* 三个面板的统一导航（2026-10-01）。放在 header 里，三页都看得见、当前页高亮。 */
nav.tabs { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 10px; }
nav.tabs a.tab { padding: 6px 14px; border-radius: 999px; text-decoration: none;
                 color: #e8f1ff; font-size: 13px; background: rgba(255,255,255,.16); }
nav.tabs a.tab.on { background: #fff; color: #1f6feb; font-weight: 600; }

/* 图表区：SVG 由 net/charts.py 生成（内联、离线可用、可被无头浏览器截图） */
.charts { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 14px; }
.chartbox { background: #fff; border: 1px solid #dfe3e8; border-radius: 10px; padding: 10px 12px; }
.chartbox .t { font-size: 13px; font-weight: 600; color: #45505c; margin-bottom: 6px; }
.chartbox .cap { font-size: 12px; color: #6b7480; margin-top: 6px; }
.chartbox .cap.stale { color: #9aa0a6; }
svg.chart { max-width: 100%; }

/* 过期/旧值的视觉：**值变灰**，并给一行小字说清"这是几点的事" */
.card .v.stale { color: #9aa0a6; }
.card .s.stale { color: #9aa0a6; }
.card.stale { border-color: #dfe3e8; background: #fbfbfc; }
.tag { display: inline-block; padding: 0 6px; border-radius: 4px; font-size: 11px;
       background: #eef1f4; color: #6b7480; }

/* ⚠️ 长路径必须能断行（2026-10-01 用 390px 无头截图实测到的真实缺陷）：
   `/home/pi/raspberry-health-monitor/rpi/config/devices.json` 是一串**没有空格**的字符，
   默认**不换行** ⇒ 在 360~390px 手机上会把整页撑宽、右侧内容被裁掉、出现横向滚动。 */
code, .path { overflow-wrap: anywhere; word-break: break-all; }
header .meta, .hint, td, .card .s, .chartbox .cap { overflow-wrap: anywhere; }

/* ---------------- 双端适配（PC + 手机）----------------
   断点取 720px：比它窄就当成"手机/竖屏平板"处理。三件事必须发生：
   ① 指标卡降列（否则一列只有半张卡宽，数字被挤断行）；
   ② **表格转卡片**（6 列宽表在 360px 上根本没法看 —— 横向滚动也会把整页拖宽）；
   ③ 按钮变大（老人 + 手指点在手机上）。
   判据：**360px 宽时页面不许出现横向滚动**。 */
@media (max-width: 720px) {
  header { padding: 12px 14px; }
  header h1 { font-size: 17px; }
  main { padding: 12px 12px 32px; }
  .cards { grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 10px; }
  .card .v { font-size: 22px; }
  .charts { grid-template-columns: 1fr; }
  .fields { grid-template-columns: 1fr; }
  button { padding: 12px 18px; font-size: 15px; width: 100%; }
  .actions { flex-direction: column; align-items: stretch; gap: 10px; }
  .spo2btns { flex-direction: column; }

  /* 表格转卡片：每个 <tr> 一张小卡，每个 <td> 前面用 data-label 补出列名 */
  table { border: 0; background: transparent; font-size: 13px; }
  thead { display: none; }
  tbody tr { display: block; background: #fff; border: 1px solid #dfe3e8;
             border-radius: 10px; margin-bottom: 10px; padding: 6px 12px; }
  tbody td { display: flex; gap: 10px; border: 0; padding: 5px 0; }
  tbody td::before { content: attr(data-label); flex: 0 0 82px; color: #5b6673;
                     font-size: 12px; }
  tbody td.num { text-align: left; }
  tbody td[colspan] { justify-content: center; color: #6b7480; }
  tbody td[colspan]::before { content: none; }
}
"""

#: 页面可见性感知的刷新（不可用时退化为 meta refresh）
_SCRIPT = """
<script>
// 只在页面可见时整页刷新，避免手机锁屏后白白耗电/占带宽。
setTimeout(function () { location.reload(); }, %d);
</script>
"""


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _fmt(value: Any, unit: str = "", digits: int = 0) -> Tuple[str, bool]:
    """格式化数值；``None`` → ``("未知", True)``（**绝不显示 0**）。"""
    if value is None:
        return "未知", True
    try:
        number = float(value)
    except (TypeError, ValueError):
        return _esc(value), False
    text = f"{number:.{digits}f}{unit}" if digits else f"{number:g}{unit}"
    return text, False


#: 三个面板的统一导航：``(标签, 路径)``。当前页按路径高亮。
_NAV_ITEMS: Tuple[Tuple[str, str], ...] = (
    ("数据", "/"),
    ("功能", "/control"),
    ("配置", "/panel"),
)

#: 别名 URL → 规范 URL（2026-10-01 修）。
#: ⚠️ 一个页面可能有多个 URL（数据页就有 `/`、`/index.html`、`/status` 三个；
#: 功能页还有 `/control.html`）。导航高亮必须**按页面**算，而不是按 URL 字符串算 ——
#: 否则从别名进来时"数据"标签不亮，用户会以为**导航坏了**（比多写一行映射糟得多）。
#: 这张表必须与 `net/web.py` 的路由别名保持一致（有测试钉住）。
_NAV_ALIASES: Dict[str, str] = {
    "/index.html": "/",
    "/status": "/",
    "/control.html": "/control",
}


def _nav(current: str) -> str:
    """统一的三标签导航（三页共用；当前页高亮）。

    为什么放 header 里而不是页面底部：手机上一屏就一屏，底部链接得先滚到底才看得到 ——
    那等于没有。放在抬头，任何一页都能一步跳到另外两页。
    """
    here = _NAV_ALIASES.get(current, current)          # 别名归一化，见上
    tabs = "".join(
        f'<a class="tab{" on" if path == here else ""}" href="{path}">{_esc(label)}</a>'
        for label, path in _NAV_ITEMS
    )
    return f'<nav class="tabs">{tabs}</nav>'


def _age_text(seconds: float) -> str:
    """把"多久以前"说成人话（秒 → 秒 / 分钟 / 小时）。"""
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{seconds:.0f} 秒前"
    if seconds < 3600:
        return f"{seconds / 60:.0f} 分钟前"
    return f"{seconds / 3600:.1f} 小时前"


def _span_text(seconds: float) -> str:
    """时间跨度（用于图表下方那行小字）。"""
    seconds = max(0.0, float(seconds))
    if seconds < 120:
        return f"{seconds:.0f} 秒"
    if seconds < 7200:
        return f"{seconds / 60:.0f} 分钟"
    return f"{seconds / 3600:.1f} 小时"


def _fresh_window(interval_s: Any, *, factor: float = 3.0, floor_s: float = 10.0) -> float:
    """多久没有新数据就算"过期"：``max(3 × 该器件读取周期, 10 秒)``。

    ⚠️ 那个 **10 秒下限是必须的**，不是随手写的：

    * 乘 3 是给"偶尔漏一拍"留余量（DHT11 本身有 2 秒下限，还会偶发读取失败）；
    * **没有下限就会闹笑话**：按键（``sos_button`` / ``spo2_button``）的读取周期是
      0.2 秒，照 3 倍算只有 0.6 秒 —— 一个**完全正常**的按键会被永远判成"过期"，
      "数据已过期"这个标记也就彻底没人信了（狼来了）。
      与此同时 10 秒仍然足够抓住"真的断流几十秒"。
    """
    try:
        interval = float(interval_s)
    except (TypeError, ValueError):
        interval = 1.0
    if interval <= 0:
        interval = 1.0
    return max(factor * interval, floor_s)


class _Metric(NamedTuple):
    """一个指标的**全部口径**：卡片与图表共用一份，避免两处对不上。

    为什么要合到一处：卡片写"血氧下限 93"、图表却画一条 95 的虚线 ——
    这种自相矛盾只要出现一次，整页数字就没人敢信了（与血氧那次"同一个量两处口径不同"
    是同一族问题）。
    """

    key: str                                   # health_summary() 与历史行里的字段名
    title: str
    unit: str
    digits: int
    device: str                                # 用来查读取周期与"距上次成功"
    thresholds: Tuple[Tuple[str, str], ...]    # (阈值字段名, 图上标签)


#: 四个**有历史曲线**的生理/环境指标。顺序按"人关心的顺序"，与卡片一致。
_METRICS: Tuple[_Metric, ...] = (
    _Metric("heart_rate_bpm", "心率", " bpm", 0, "vitals",
            (("hr_min", "心率下限"), ("hr_max", "心率上限"))),
    _Metric("spo2_percent", "血氧", " %", 0, "vitals",
            (("spo2_min", "血氧下限"),)),
    _Metric("ambient_temp_c", "室温", " ℃", 1, "ambient",
            (("ambient_temp_min", "室温下限"), ("ambient_temp_max", "室温上限"))),
    _Metric("humidity_percent", "湿度", " %", 1, "ambient",
            (("humidity_max", "湿度上限"),)),
)


def _stateful_card(metric: _Metric, *, value: Any, now: float,
                   last_value: Any = None, last_ts: Any = None,
                   subtitle: str = "", state: str = "", spark: str = "",
                   empty_note: str = "还没有读到过这个指标") -> str:
    """指标卡的**三态**渲染（缺一不可，这是全页最容易骗人的地方）。

    1. **新鲜**          ⇒ 大字显示当前值；
    2. **不新鲜但有历史** ⇒ 值**变灰** + 「最后一次 HH:MM（N 分钟前）· 数据已过期」；
    3. **从来没有**       ⇒ 「暂无数据」。

    ⚠️ 两条硬纪律：

    * **绝不显示 0 来冒充"没有数据"**（项目既有要求：**"读不到" 与 "读数是 0" 必须能分开**，
      见 `ERROR.md` E58）；
    * 显示旧值时**必须同时给出时间与"已过期"**：只甩一个旧数字，等于把几小时前的心率
      当成此刻的报给子女看 —— 那比不显示更糟。
    """
    if value is not None:
        text, _ = _fmt(value, metric.unit, metric.digits)
        sub = f'<div class="s">{_esc(subtitle)}</div>' if subtitle else ""
        cls = "card" + (f" {state}" if state else "")
        return (
            f'<div class="{cls}"><div class="k">{_esc(metric.title)}</div>'
            f'<div class="v">{_esc(text)}</div>{spark}{sub}</div>'
        )

    if last_value is not None and last_ts is not None:
        text, _ = _fmt(last_value, metric.unit, metric.digits)
        age = _age_text(now - float(last_ts))
        return (
            f'<div class="card stale"><div class="k">{_esc(metric.title)}</div>'
            f'<div class="v stale">{_esc(text)}</div>{spark}'
            f'<div class="s stale">最后一次 {_esc(_time_text(last_ts))}（{_esc(age)}）'
            f' · <span class="tag">数据已过期</span></div></div>'
        )

    return (
        f'<div class="card"><div class="k">{_esc(metric.title)}</div>'
        f'<div class="v unknown">暂无数据</div>'
        f'<div class="s">{_esc(empty_note)}</div></div>'
    )


def _reading_points(rows: List[Dict[str, Any]]) -> List[Tuple[float, Any]]:
    """环境读数的历史行 → 图表点列。**``ok=False`` 的行必须变成 ``None``（缺口）**。

    为什么：``ok=False`` 的意思是"这一次读失败"。把它的值画进曲线（哪怕值是上一次的
    残留）等于凭空造出"那段时间有数据" —— 与 `charts.py` 开头那条"缺口必须断线"
    是同一条纪律：**图表不许替数据说话**。
    """
    out: List[Tuple[float, Any]] = []
    for row in rows:
        value = row.get("value")
        ok = row.get("ok")
        if value is None or ok in (0, False):
            out.append((float(row["ts"]), None))
        else:
            out.append((float(row["ts"]), float(value)))
    return out


def _vitals_points(rows: List[Dict[str, Any]], key: str) -> List[Tuple[float, Any]]:
    """心率/血氧历史行 → 图表点列。**没贴手指（或没测出值）就是缺口**。"""
    out: List[Tuple[float, Any]] = []
    for row in rows:
        value = row.get(key)
        finger = row.get("finger_detected")
        if value is None or finger in (0, False):
            out.append((float(row["ts"]), None))
        else:
            out.append((float(row["ts"]), float(value)))
    return out


def _last_pair(points: List[Tuple[float, Any]]) -> Tuple[Any, Any]:
    """点列里**最后一个非缺口**的值与时刻（没有就返回 ``(None, None)``）。"""
    for ts, value in reversed(points):
        if value is not None:
            return value, ts
    return None, None


def _insert_time_gaps(points: List[Tuple[float, Any]], max_gap_s: float) -> List[Tuple[float, Any]]:
    """相邻两点相隔太久 ⇒ 中间插一个 ``None``，让图**断开**。

    为什么必须自己插（2026-10-01 实测发现的真问题）：`charts.py` 的断线**只在值为
    ``None`` 时**发生，而 `Store.save_sample()` 对读取失败的样本**根本不落库**
    （实测确认：ok=False 的样本连行都不写）⇒ "值缺失"这条路在真实数据里**不会出现**，
    能出现的只有"**整段没有记录**"（器件掉线十分钟）。

    若不插 None，图会把断流前后**直接连成一条直线**，视觉上等于说"这段时间一直有数据" ——
    正是 `charts.py` 开头那条"缺口必须断线、图表不许说谎"要防的事。
    （时间断点画在缺口正中，纯粹是为了让线看起来从两个点中间断开。）
    """
    out: List[Tuple[float, Any]] = []
    prev_ts: Any = None
    for ts, value in points:
        if prev_ts is not None and float(ts) - float(prev_ts) > float(max_gap_s):
            out.append(((float(prev_ts) + float(ts)) / 2.0, None))
        out.append((ts, value))
        prev_ts = ts
    return out


def _chart_box(title: str, points: List[Tuple[float, Any]], metric: _Metric, *,
               thresholds: List[Tuple[float, str]], now: float,
               stale_after_s: float) -> str:
    """一张图 + 图下一行小字（数据点数 / 跨度 / 最后更新；过期就标出来）。"""
    svg = line_chart(
        points, unit=metric.unit.strip(), thresholds=thresholds,
        now=now, stale_after_s=stale_after_s,
    )
    values = [(ts, v) for ts, v in points if v is not None]
    if not values:
        cap, cap_cls = "还没有数据点", " cap"
    else:
        first_ts, last_ts = values[0][0], values[-1][0]
        stale = (now - last_ts) > stale_after_s
        cap = (f"{len(values)} 个数据点 · 跨度 {_span_text(last_ts - first_ts)} · "
               f"最后更新 {_time_text(last_ts)}")
        if stale:
            cap += ' · <span class="tag">数据已过期</span>'
        cap_cls = " cap stale" if stale else " cap"
    return (
        f'<div class="chartbox"><div class="t">{_esc(title)}</div>{svg}'
        f'<div class="{cap_cls.strip()}">{cap}</div></div>'
    )


def render_page(runtime: Any, refresh_s: int = 5) -> str:
    """渲染整页 HTML。
        runtime: :class:`~health_monitor.service.Runtime`（只读使用）。
        refresh_s: 自动刷新周期（秒）。
    """
    now = runtime.clock()
    snap = runtime.collector.snapshot()
    summary = snap.health_summary()
    status = runtime.collector.status()["devices"]
    active = runtime.engine.active_alarms()

    # ---- 顶部横幅：先回答"现在安全吗" ----
    if active:
        names = "、".join(ALARM_LABELS.get(code, code) for code in active)
        banner_cls, banner_text = "bad", f"⚠ 正在报警：{names}"
    elif snap.data_stale:
        banner_cls, banner_text = "warn", "数据可能已过期：树莓派尚未读到新的传感器数据"
    else:
        banner_cls, banner_text = "ok", "状态正常，监护中"

    # ---- 历史数据：**指标卡的兜底与图表共用同一份**（一处口径，两处使用）----
    # 为什么不各查各的：同一个量在两处用了不同的取数口径，迟早会出现
    # "卡片说最后一次 21:30、图表最后一点是 21:35"这种自相矛盾。
    store = getattr(runtime, "store", None)
    series: Dict[str, List[Tuple[float, Any]]] = {}
    #: 「最后一次有效读数」：**独立于图表窗口**（可能很久以前），见下面 `last_reading` 的说明。
    last_known: Dict[str, Tuple[Any, float]] = {}
    history_note = ""
    if store is None:
        history_note = ("未启用历史存储（启动时没给 store_path）⇒ 趋势图不可用；"
                        "上面的指标卡仍然照常工作，只是没有「最后一次记录」可回退。")
    else:
        try:
            ambient_rows = store.recent_readings("ambient_temp_c", limit=120)
            humidity_rows = store.recent_readings("humidity_percent", limit=120)
            vitals_rows = store.recent_vitals(limit=120)
            series["heart_rate_bpm"] = _vitals_points(vitals_rows, "heart_rate_bpm")
            series["spo2_percent"] = _vitals_points(vitals_rows, "spo2_percent")
            series["ambient_temp_c"] = _reading_points(ambient_rows)
            series["humidity_percent"] = _reading_points(humidity_rows)
            # ⚠️ **"最后一次记录"必须跳出上面那个窗口去查**（2026-10-01 真机验出来的）：
            #    历史库每秒一行 ⇒ `limit=120` 只有约 2 分钟；而上一次测血氧可能在几十分钟前。
            #    用窗口里的最后一个点当"最后一次记录"，只要过了两分钟就会错报"暂无数据"，
            #    而"测不到就显示最后一次记录"正是用户点名要的功能。
            for metric_key in ("ambient_temp_c", "humidity_percent"):
                row = store.last_reading(metric_key)
                if row and row.get("value") is not None:
                    last_known[metric_key] = (row["value"], float(row["ts"]))
            for column in store.VITALS_VALUE_COLUMNS:
                row = store.last_vitals_value(column)
                if row and row.get("value") is not None:
                    last_known[column] = (row["value"], float(row["ts"]))
        except Exception as exc:  # noqa: BLE001 - 历史库出问题不该让整页打不开
            history_note = f"读取历史库失败（{type(exc).__name__}: {exc}）⇒ 趋势图不可用。"

    thresholds = getattr(getattr(runtime, "config", None), "thresholds", None)

    def _thresholds_for(metric: _Metric) -> List[Tuple[float, str]]:
        """从**当前**配置取这张图的阈值虚线（改配置后图上的虚线要跟着变）。"""
        pairs: List[Tuple[float, str]] = []
        for field_name, label in metric.thresholds:
            value = getattr(thresholds, field_name, None)
            if value is not None:
                pairs.append((float(value), label))
        return pairs

    # ---- 指标卡（三态：新鲜 / 过期但有历史 / 从来没有）----
    # 六项：心率、血氧、室温、湿度、活动状态、数据年龄。前四项有历史曲线（带迷你趋势线），
    # 后两项没有曲线，但**同样**要遵守"绝不显示 0"与"没有就说没有"。
    finger = summary.get("finger_detected")
    hr_note = "请将手指放好" if finger is False else ("已贴合" if finger else "皮肤贴合状态未知")
    cards: List[str] = []
    for metric in _METRICS:
        points = series.get(metric.key) or []
        # 「最后一次记录」优先用**跳出窗口**查到的那一次；查不到才退回窗口里的最后一个点。
        # （窗口只有约 2 分钟，"上次测血氧"通常比这久得多 —— 那正是这个功能存在的意义。）
        known = last_known.get(metric.key)
        if known is not None:
            last_value, last_ts = known
        else:
            last_value, last_ts = _last_pair(points)
        if metric.key == "heart_rate_bpm":
            subtitle = hr_note
        elif metric.key == "spo2_percent":
            subtitle = "未测出（未贴合手指）" if finger is False else ""
        elif metric.key == "ambient_temp_c":
            subtitle = "DHT11"
        else:
            subtitle = "DHT11"
        # 「从来没有」那一态的说明文字：**该告诉用户怎么做就告诉他**。
        # 心率/血氧没数据时，最有用的一句话不是"还没有读到过"，而是"请将手指放好" ——
        # 前者只是在陈述事实，后者能让人当场把问题解决掉。
        if metric.device == "vitals" and finger is False:
            empty_note = "请将手指放好（食指指腹轻贴 MAX30102，也可在「功能」页按需测一次）"
        elif metric.device == "vitals":
            empty_note = "还没有测量过（可在「功能」页按需测一次）"
        else:
            empty_note = "还没有读到过这个指标"
        cards.append(_stateful_card(
            metric,
            value=summary.get(metric.key),
            now=now,
            last_value=last_value,
            last_ts=last_ts,
            subtitle=subtitle,
            state="warn" if (metric.device == "vitals" and finger is False) else "",
            empty_note=empty_note,
            # 迷你趋势线：只取最近 40 个点，卡片里放得下、也不抢大图的戏。
            # ⚠️ `sparkline()` 全无数据时返回**空串**（不是空 SVG），这里直接用它的返回值。
            spark=sparkline([value for _, value in points[-40:]]),
        ))

    motion_state = summary.get("motion_state")
    motion_silent = summary.get("motion_silent_s")
    motion_text = _MOTION_LABELS.get(motion_state or "")
    if motion_text:
        motion_sub = ("上次检测到人：{:.0f} 秒前".format(motion_silent)
                      if motion_silent is not None else "距上次检测到人的时间未知")
        cards.append(
            f'<div class="card"><div class="k">活动状态</div>'
            f'<div class="v">{_esc(motion_text)}</div>'
            f'<div class="s">{_esc(motion_sub)}</div></div>'
        )
    else:
        cards.append(
            '<div class="card"><div class="k">活动状态</div>'
            '<div class="v unknown">暂无数据</div>'
            '<div class="s">还没有读到过人体红外</div></div>'
        )

    data_age = summary.get("data_age_s")
    data_stale = bool(summary.get("data_stale"))
    if data_age is not None:
        cards.append(
            f'<div class="card{" warn" if data_stale else ""}">'
            f'<div class="k">数据年龄</div>'
            f'<div class="v">{"已过期" if data_stale else _esc(_age_text(data_age))}</div>'
            f'<div class="s">最近一次成功读取距今</div></div>'
        )
    else:
        cards.append(
            '<div class="card"><div class="k">数据年龄</div>'
            '<div class="v unknown">暂无数据</div>'
            '<div class="s">还没有成功读取过任何设备</div></div>'
        )

    # ---- 图表区：四张趋势图（缺口断线 / 阈值虚线 / 过期变灰都在 charts.py 里）----
    if history_note:
        charts_html = f'<div class="banner warn">{_esc(history_note)}</div>'
    else:
        boxes: List[str] = []
        for metric in _METRICS:
            interval = (status.get(metric.device) or {}).get("interval_s")
            window = _fresh_window(interval)
            boxes.append(_chart_box(
                metric.title,
                # ⚠️ 先补时间缺口再画：`charts.py` 只认"值为 None"的缺口，
                #    而真实的断流是"整段没有记录"（见 `_insert_time_gaps`）。
                _insert_time_gaps(series.get(metric.key) or [], window),
                metric,
                thresholds=_thresholds_for(metric),
                now=now,
                stale_after_s=window,
            ))
        charts_html = f'<div class="charts">{"".join(boxes)}</div>'

    # ---- 设备状态表 ----
    rows: List[str] = []
    for name, entry in sorted(status.items()):
        failures = entry.get("failures", 0)
        stale = entry.get("stale", False)
        if stale or failures:
            pill_cls, pill_text = ("bad", "异常") if failures else ("warn", "数据陈旧")
        else:
            pill_cls, pill_text = "ok", "正常"
        age = entry.get("last_ok_age_s")
        # 「最后错误」那一列原来会把**整段排查提示**（几百字）塞进表格 —— 宽屏把表格撑得
        # 极丑、窄屏更是灾难。截断到 120 字，全文放进 title（悬停可见）。
        last_error = str(entry.get("last_error") or "")
        error_short = last_error if len(last_error) <= 120 else last_error[:119] + "…"
        rows.append(
            "<tr>"
            f"<td data-label='设备'>{_esc(name)}<br>"
            f"<span class='s'><code>{_esc(entry.get('driver'))}</code></span></td>"
            f"<td class='num' data-label='周期'>{_fmt(entry.get('interval_s'), '', 1)[0]} 秒</td>"
            f"<td class='num' data-label='读取次数'>{_esc(entry.get('reads'))}</td>"
            f"<td class='num' data-label='连续失败'>{_esc(failures)}</td>"
            f"<td data-label='距上次成功'>{'—' if age is None else _fmt(age, ' 秒', 1)[0]}</td>"
            f"<td data-label='状态'><span class='pill {pill_cls}'>{pill_text}</span></td>"
            f"<td data-label='最后错误' title='{_esc(last_error)}'>{_esc(error_short)}</td>"
            "</tr>"
        )

    # ---- 最近报警 ----
    alarm_rows: List[str] = []
    for event in reversed(runtime.recent_events(limit=15)):
        alarm_rows.append(
            "<tr>"
            f"<td data-label='时间'>{_esc(_time_text(event.ts))}</td>"
            f"<td data-label='报警'>{_esc(ALARM_LABELS.get(event.code.value, event.code.value))}</td>"
            f"<td data-label='等级'>{_SEVERITY_NAMES.get(int(event.severity), int(event.severity))}</td>"
            f"<td data-label='说明'>{_esc(event.message)}</td>"
            "</tr>"
        )
    if not alarm_rows:
        alarm_rows.append("<tr><td colspan='4'>本次运行还没有报警记录</td></tr>")

    # ---- 最近消息（报警 + 老人动作 + 测量记录；比"最近报警"更全）----
    # 为什么单独一栏而不是让"最近报警"兼着：用户 2026-10-01 明确要求
    # "老人短按 A 关警报 / 长按 A 求救 / 测完血氧，**后台消息页都要收到指示**" ——
    # 那三类里有两类**不是报警**（消音、测量记录），塞进"报警"栏会在语义上骗人。
    # 与 `/api/v1/messages` 同源：**优先用历史库**（跨重启仍在，用户翻的就是记录）。
    msg_source: List[Dict[str, Any]] = []
    _store = getattr(runtime, "store", None)
    if _store is not None:
        msg_source = [
            {"ts": r.get("ts"), "code": str(r.get("code") or ""),
             "message": r.get("message") or ""}
            for r in reversed(_store.recent_alarms(limit=15))
        ]
    else:
        msg_source = [
            {"ts": e.ts, "code": e.code.value, "message": e.message}
            for e in runtime.recent_events(limit=15)
        ]
    message_rows: List[str] = [
        "<tr>"
        f"<td data-label='时间'>{_esc(_time_text(m['ts']))}</td>"
        f"<td data-label='类别'>{_esc(KIND_LABELS.get(message_kind(m['code']), message_kind(m['code'])))}</td>"
        f"<td data-label='类型'>{_esc(ALARM_LABELS.get(m['code'], m['code']))}</td>"
        f"<td data-label='说明'>{_esc(m['message'])}</td>"
        "</tr>"
        for m in msg_source
    ]
    if not message_rows:
        message_rows.append("<tr><td colspan='4'>还没有任何消息</td></tr>")

    # ---- 当前报警态 ----
    active_rows = [
        f"<tr><td data-label='报警'>{_esc(ALARM_LABELS.get(code, code))}</td>"
        f"<td data-label='代码'>{_esc(code)}</td>"
        f"<td data-label='首次触发'>{_esc(_time_text(ts))}</td></tr>"
        for code, ts in sorted(active.items())
    ] or ["<tr><td colspan='3'>当前没有处于报警状态的项目</td></tr>"]

    devices_errors = runtime.assembly_errors or {}
    warn_block = ""
    if devices_errors:
        items = "".join(f"<li><code>{_esc(k)}</code>：{_esc(v)}</li>" for k, v in devices_errors.items())
        warn_block = f'<div class="banner warn">以下设备装配/打开失败（功能可能不可用）：<ul>{items}</ul></div>'

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{int(refresh_s)}">
<title>居家老人健康监护 · 数据</title>
<style>{_CSS}</style>
</head>
<body>
<header>
  <h1>树莓派居家老人健康监护系统</h1>
  <div class="meta">版本 {_esc(runtime.version)} · {"模拟模式（无硬件）" if runtime.mock else "真实硬件模式"}
    · 运行 {_fmt(round(now - runtime.started_at, 1), ' 秒', 1)[0]} · 页面每 {int(refresh_s)} 秒自动刷新 ·
    本地时间 {_esc(_time_text(now))}</div>
  {_nav("/")}
</header>
<main>
  <div class="banner {banner_cls}">{_esc(banner_text)}</div>
  {warn_block}
  <div class="cards">{"".join(cards)}</div>

  <h2>趋势图（最近 120 个点；缺口处断线，虚线是报警阈值）</h2>
  {charts_html}

  <h2>当前报警态</h2>
  <table><thead><tr><th>报警</th><th>代码</th><th>首次触发</th></tr></thead>
  <tbody>{"".join(active_rows)}</tbody></table>

  <h2>设备状态</h2>
  <table><thead><tr><th>设备</th><th>周期</th><th>读取次数</th><th>连续失败</th>
  <th>距上次成功</th><th>状态</th><th>最后错误</th></tr></thead>
  <tbody>{"".join(rows) or "<tr><td colspan='7'>没有输入设备</td></tr>"}</tbody></table>

  <h2>最近报警（本次运行，最多 15 条）</h2>
  <table><thead><tr><th>时间</th><th>报警</th><th>等级</th><th>说明</th></tr></thead>
  <tbody>{"".join(alarm_rows)}</tbody></table>

  <h2>最近消息（含老人消音与血氧测量记录，最多 15 条）</h2>
  <table><thead><tr><th>时间</th><th>类别</th><th>类型</th><th>说明</th></tr></thead>
  <tbody>{"".join(message_rows)}</tbody></table>

  <footer>
    JSON 接口：<code>/api/v1/current</code> · <code>/api/v1/health</code> ·
    <code>/api/v1/alarms</code> · <code>/api/v1/messages</code> · <code>/api/v1/history?metric=ambient_temp_c&amp;limit=120</code><br>
    本页面只读，不会修改任何状态（要动手请去「功能」页，要改配置请去「配置」页）。<br>
    数值显示「暂无数据」表示该传感器当前没有有效数据——这是刻意设计：本系统不把"读不到"当作"正常"。
    显示灰色旧值时<strong>一定会同时给出时间并标注「数据已过期」</strong>，绝不把旧值当成此刻的值。
  </footer>
</main>
{_SCRIPT % (int(refresh_s) * 1000)}
</body>
</html>
"""


#: 面板上的**阈值分组**：``(组名, [(字段名, 中文标签, 单位), ...])``。
#: 顺序刻意按"人关心的顺序"排，而不是按 dataclass 的字段顺序。
#: ⚠️ 必须**覆盖全部** :class:`~health_monitor.core.config.Thresholds` 字段（有测试钉住）：
#: 漏掉一个，面板上就永远改不了它，而且从界面上**看不出来少了什么**。
THRESHOLD_GROUPS: List[Tuple[str, List[Tuple[str, str, str]]]] = [
    ("心率", [
        ("hr_min", "心率下限", "bpm"),
        ("hr_max", "心率上限", "bpm"),
    ]),
    ("血氧", [
        ("spo2_min", "血氧下限", "%"),
    ]),
    ("环境（DHT11）", [
        ("ambient_temp_min", "室温下限", "℃"),
        ("ambient_temp_max", "室温上限", "℃"),
        ("humidity_max", "湿度上限", "%"),
    ]),
    ("久无活动 / 夜间", [
        ("no_motion_timeout_s", "久无活动阈值", "秒"),
        ("night_start_hour", "夜间起始小时", "时"),
        ("night_end_hour", "夜间结束小时", "时"),
        ("night_wake_count", "夜间起夜次数阈值", "次"),
        ("night_window_s", "起夜统计窗口", "秒"),
    ]),
    ("报警节流 / 故障判定", [
        ("sensor_fault_after", "连续失败几次算故障", "次"),
        ("repeat_cooldown_s", "同一报警抑制时间", "秒"),
        ("re_alert_interval_s", "持续提醒间隔（0=关闭）", "秒"),
        ("hysteresis", "回差（防阈值抖动）", ""),
    ]),
    ("测血氧（按需测量）", [
        ("spo2_remind_interval_s", "提醒间隔（0=不提醒）", "秒"),
        ("spo2_remind_timeout_s", "叫人后等按键超时", "秒"),
        ("spo2_measure_s", "一次测量时长", "秒"),
    ]),
]

#: 面板专用样式（接在 :data:`_CSS` 后面；仍然**不依赖任何外部 CDN**）
_PANEL_CSS = """
.unsafe { background: #fdeceb; border: 2px solid #d9534f; color: #8a2b28;
          border-radius: 10px; padding: 12px 14px; margin-bottom: 14px; font-weight: 600; }
.unsafe .sub { display: block; font-weight: 400; margin-top: 6px; font-size: 13px; }
fieldset { border: 1px solid #dfe3e8; border-radius: 10px; background: #fff;
           margin: 0 0 12px; padding: 10px 14px 12px; }
legend { font-weight: 600; font-size: 13px; color: #45505c; padding: 0 6px; }
.fields { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 8px 16px; }
.field { display: flex; align-items: center; gap: 8px; font-size: 13px; }
.field > span.lbl { flex: 1; color: #45505c; }
.field input[type=number] { width: 92px; padding: 4px 6px; border: 1px solid #cfd6de;
                            border-radius: 6px; font-size: 13px; }
.unit { color: #8b95a1; font-size: 12px; min-width: 32px; }
.actions { margin: 16px 0; display: flex; gap: 14px; align-items: center; flex-wrap: wrap; }
button { background: #1f6feb; color: #fff; border: 0; border-radius: 8px;
         padding: 9px 20px; font-size: 14px; cursor: pointer; }
button[disabled] { opacity: .55; cursor: progress; }
.result { border-radius: 10px; padding: 12px 14px; margin-top: 14px; font-size: 13px; }
.result.ok { background: #e7f6ec; border: 1px solid #58a55c; }
.result.bad { background: #fdf0ef; border: 1px solid #d9534f; }
.result ul { margin: 6px 0 0 18px; padding: 0; }
.result li { margin: 2px 0; }
.hint { font-size: 12px; color: #6b7480; }
"""

#: **功能面板**才用得上的样式（2026-10-01 从 `_PANEL_CSS` 拆出来）。
#: 为什么值得拆：配置面板上已经没有血氧那一节了，却还带着它的 CSS ——
#: 不只是白带几行，更会让"**搬干净了没有**"这件事没法用机器判定
#: （反向钉子测试本来想检查配置面板里不含 `spo2state`，结果被这几行 CSS 挡住）。
_CONTROL_CSS = """
/* 「血氧检测」一节：放最上面（最常用），所以给它一圈更重的边框 */
.spo2box { border: 2px solid #1f6feb; }
.spo2state { font-size: 14px; font-weight: 600; margin: 2px 0 10px; color: #1b3a63; }
.spo2btns { display: flex; gap: 12px; flex-wrap: wrap; }
button.ghost { background: #fff; color: #1f6feb; border: 1px solid #1f6feb; }
button.ghost[disabled] { opacity: .45; cursor: not-allowed; }
"""

#: 面板的脚本。**刻意不做自动刷新**：这是表单，整页刷新会把没保存的输入抹掉
#: （状态页可以每 5 秒刷新，因为它只读）。
_PANEL_SCRIPT = """
<script>
function numOf(el) { var v = parseFloat(el.value); return isNaN(v) ? null : v; }

function collect() {
  var patch = { thresholds: {}, devices: {} };
  document.querySelectorAll('input[data-th]').forEach(function (el) {
    var v = numOf(el);
    if (v === null) { throw new Error('「' + el.dataset.label + '」不是数字'); }
    patch.thresholds[el.dataset.th] = v;
  });
  document.querySelectorAll('input[data-dev]').forEach(function (el) {
    var name = el.dataset.dev;
    if (!patch.devices[name]) { patch.devices[name] = {}; }
    if (el.type === 'checkbox') {
      patch.devices[name][el.dataset.field] = el.checked;
    } else {
      var v = numOf(el);
      if (v === null) { throw new Error('设备 ' + name + ' 的读取周期不是数字'); }
      patch.devices[name][el.dataset.field] = v;
    }
  });
  return patch;
}

// 前端只做"一眼能看出的错"；**后端校验才是准的**（未知键、撞脚、DHT11 周期下限都在那边）
function clientCheck(p) {
  var t = p.thresholds;
  function pair(lo, hi, what) {
    if (t[lo] !== undefined && t[hi] !== undefined && !(t[lo] < t[hi])) {
      return what + '：下限（' + t[lo] + '）必须小于上限（' + t[hi] + '）';
    }
    return '';
  }
  var bad = pair('hr_min', 'hr_max', '心率')
         || pair('ambient_temp_min', 'ambient_temp_max', '室温');
  if (bad) { return bad; }
  if (t.spo2_min !== undefined && !(t.spo2_min > 0 && t.spo2_min <= 100)) {
    return '血氧下限必须在 (0, 100] 之间';
  }
  for (var name in p.devices) {
    var iv = p.devices[name].read_interval_s;
    if (iv !== undefined && !(iv > 0)) { return '设备 ' + name + ' 的读取周期必须为正数'; }
  }
  return '';
}

function show(cls, title, items) {
  var box = document.getElementById('result');
  box.className = 'result ' + cls;
  var html = '<strong>' + title + '</strong>';
  if (items && items.length) {
    html += '<ul>';
    items.forEach(function (line) { html += '<li>' + line + '</li>'; });
    html += '</ul>';
  }
  box.innerHTML = html;
}

function esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

async function save() {
  var patch;
  try { patch = collect(); }
  catch (err) { show('bad', esc(err.message)); return; }

  var bad = clientCheck(patch);
  if (bad) { show('bad', '本地检查未通过：' + esc(bad) + '<br><span class="hint">修正后再保存</span>'); return; }

  var btn = document.getElementById('save');
  btn.disabled = true;
  try {
    var res = await fetch('/api/v1/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch)
    });
    var data = await res.json();
    if (!data.ok) {
      show('bad', '保存失败（HTTP ' + res.status + '）：' + esc(data.error || '未知错误'));
      return;
    }
    var lines = [];
    (data.changed || []).forEach(function (x) { lines.push('写盘：' + esc(x)); });
    (data.applied || []).forEach(function (x) { lines.push('已生效：' + esc(x)); });
    if (data.backup) { lines.push('备份文件：' + esc(data.backup)); }
    (data.warnings || []).forEach(function (x) { lines.push('注意：' + esc(x)); });
    show('ok', '保存成功' + (lines.length ? '' : '（无变化）'), lines);
  } catch (err) {
    show('bad', '请求失败：' + esc(err));
  } finally {
    btn.disabled = false;
  }
}

// ---------------- 血氧检测那一节**已搬到功能面板**（2026-10-01）----------------
// 连同它的轮询脚本一起搬去了 `_CONTROL_SCRIPT`（`GET /control`）。
// 为什么不留一份在这里：**一个动作只能有一个入口** —— 两页各放一套 JS，
// 迟早出现"一边改了、另一边没改"。本页从此只负责"改数字并保存"。
// ⚠️ 这里刻意**不写出那个方括号标题**：反向钉子测试会检查本页不含它，
//    注释里带上就会让"搬干净了没有"这件事没法用机器判定。
</script>
"""


#: **功能面板**（``GET /control``）的脚本：血氧那一节（从配置面板搬来）+ 屏显控制。
#:
#: ⚠️ 与配置面板同一条纪律：这里的轮询**只更新血氧那一节自己的 DOM**，
#: **绝不整页刷新** —— 功能面板上还有"屏显控制"的结果要留着给用户看。
_CONTROL_SCRIPT = """
<script>
function esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function show(cls, title, items) {
  var box = document.getElementById('result');
  box.className = 'result ' + cls;
  var html = '<strong>' + title + '</strong>';
  if (items && items.length) {
    html += '<ul>';
    items.forEach(function (line) { html += '<li>' + line + '</li>'; });
    html += '</ul>';
  }
  box.innerHTML = html;
}

// ---------------- 【血氧检测】（2026-10-01 从配置面板搬到这里）----------------
function spo2Line(st) {
  if (!st.enabled) { return '功能已关闭（配置里 spo2_button.enabled = false）'; }
  if (st.state === 'prompt') {
    return '正在叫人：剩余 ' + Math.ceil(st.prompt_left_s) +
           ' 秒 —— 短按 = 开始检测，长按 = 暂不检测';
  }
  if (st.state === 'measure') {
    return '测量中：剩余 ' + Math.ceil(st.measure_left_s) +
           ' 秒 —— 请把食指指腹轻贴、别用力压';
  }
  var parts = [];
  var lr = st.last_result;
  if (lr) {
    parts.push(lr.ok
      ? ('上次结果：HR ' + lr.heart_rate_bpm + ' bpm、SpO2 ' + lr.spo2_percent + ' %')
      : '上次测量没拿到读数');
  }
  parts.push('接受 ' + st.accepted_total + ' 次 / 暂不 ' + st.declined_total + ' 次');
  return '待机 · ' + parts.join(' · ');
}

function spo2Render(st) {
  var line = document.getElementById('spo2state');
  if (line) { line.textContent = spo2Line(st); }
  var go = document.getElementById('spo2go');
  if (go) { go.disabled = !st.enabled; }
  var no = document.getElementById('spo2no');
  // 「暂不检测」只在"正在叫人"时可用：灰掉是为了**不让用户点了拿到 409**
  if (no) { no.disabled = (st.state !== 'prompt'); }
}

async function spo2Poll() {
  try {
    var res = await fetch('/api/v1/spo2', { cache: 'no-store' });
    spo2Render(await res.json());
  } catch (err) {
    // 网络抖动不该弹错误框、更不该刷新页面；下一轮 2 秒后自然会重试
  }
}

async function spo2Act(what) {
  try {
    var res = await fetch('/api/v1/spo2/' + what, { method: 'POST' });
    var data = await res.json();
    if (!data.ok) {
      show('bad', '操作未执行（HTTP ' + res.status + '）：' + esc(data.error || '未知错误'));
    } else {
      show('ok', (what === 'decline' ? '已记下：本次暂不检测' : '已开始血氧检测'), []);
    }
  } catch (err) {
    show('bad', '请求失败：' + esc(err));
  }
  spo2Poll();
}

// ---------------- 【屏显控制】（2026-10-01）----------------
// 点一下 ⇒ POST /api/v1/screen ⇒ 板上那块屏**立刻**换成指定面板。
// 后端三种状态码都有各自的理由（400 参数 / 409 那块屏不在），**原样显示**，
// 不要在这里自作聪明地猜原因。
async function screenAct(target, page) {
  var box = document.getElementById('result');
  try {
    var res = await fetch('/api/v1/screen', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target: target, page: page })
    });
    var data = await res.json();
    if (!data.ok) {
      show('bad', '屏显未改变（HTTP ' + res.status + '）：' + esc(data.error || '未知错误'));
      return;
    }
    var who = (target === 'lcd' ? 'LCD 字符屏' : 'TFT 彩屏');
    show('ok', '已让 ' + who + ' 显示「' + esc(page) + '」',
         ['这块屏会保持该画面约 30 秒（报警仍然会立刻抢屏）。']);
  } catch (err) {
    show('bad', '请求失败：' + esc(err));
  }
}

spo2Poll();
setInterval(spo2Poll, 2000);
</script>
"""


def _num_text(value: Any) -> str:
    """把配置里的数字渲染成输入框的初值（``3.0`` → ``3``，``35.5`` → ``35.5``）。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return _esc(value)
    if number == int(number) and abs(number) < 1e15:
        return str(int(number))
    return repr(number)


#: 屏显控制按钮上的中文名（**只用来显示**；合法取值以前端从 runtime 读到的表为准）
_TARGET_LABELS = {"lcd": "LCD 字符屏", "tft": "TFT 彩屏"}
_PAGE_LABELS = {"env": "环境页", "debug": "调试面板", "alarm": "报警页"}


def render_control(runtime: Any) -> str:
    """渲染**功能面板**（``GET /control``，2026-10-01）。

    用户 2026-10-01 对后台的划分是三个面板：**数据展示** / **功能操作** / **配置**。
    这一页就是"功能操作" —— 它的共同点是**点一下会让板子上的东西发生变化**：

    * 【血氧检测】⇒ 板上按需测一次（与物理按键**同一条路径**）；
    * 【屏显控制】⇒ 让 LCD / TFT **立刻**换成指定面板（用户在手机上远程指挥屏上显示什么）。

    为什么血氧那一节要从 ``/panel`` **搬过来**而不是两处都放：一处动作只有一个入口，
    否则两页各改一半、迟早分叉（本项目"一个动作只有一条路径"的纪律）。
    ``/panel`` 只管"改数字"，不再管"触发动作"。

    与另两页一样：**不依赖任何外部 CSS/JS/CDN**（演示现场可能没外网）。
    """
    # ---- 屏显控制：按钮**从 runtime 的表里生成**，而不是写死 4 个 ----
    # 好处：合法页名只有一处定义（`Runtime.SCREEN_PAGES`），按钮与后端校验不可能对不上。
    drivers = getattr(runtime, "SCREEN_DRIVERS", {}) or {}
    pages = getattr(runtime, "SCREEN_PAGES", {}) or {}
    screen_buttons: List[str] = []
    for target in ("lcd", "tft"):
        if target not in drivers:
            continue
        for page in pages.get(target, ()):
            screen_buttons.append(
                '<button type="button" class="ghost" '
                f"onclick=\"screenAct('{_esc(target)}', '{_esc(page)}')\">"
                f"{_esc(_TARGET_LABELS.get(target, target))}："
                f"{_esc(_PAGE_LABELS.get(page, page))}</button>"
            )
    if not screen_buttons:
        screen_buttons.append('<span class="hint">当前配置里没有任何显示屏</span>')

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>居家老人健康监护 · 功能</title>
<style>{_CSS}{_PANEL_CSS}{_CONTROL_CSS}</style>
</head>
<body>
<header>
  <h1>功能面板（按需测量 / 屏显控制）</h1>
  <div class="meta">版本 {_esc(runtime.version)} · {"模拟模式（无硬件）" if runtime.mock else "真实硬件模式"}
    · 真实硬件模式下这些按钮会<strong>直接作用到板子上</strong></div>
  {_nav("/control")}
</header>
<main>
  <fieldset class="spo2box">
    <legend>【血氧检测】</legend>
    <div class="spo2state" id="spo2state">状态读取中…</div>
    <div class="spo2btns">
      <button type="button" id="spo2go" onclick="spo2Act('measure')">血氧检测</button>
      <button type="button" id="spo2no" class="ghost" onclick="spo2Act('decline')" disabled>暂不检测</button>
    </div>
    <div class="hint">与板上那个「测血氧」按键<strong>同一条路径</strong>：
      短按 = 开始检测；<strong>正在叫人时长按 = 暂不检测</strong>（其它阶段长按等于短按）。
      状态每 2 秒自动刷新，<strong>只刷新本区域</strong>。测完的结果会同时出现在
      TFT/LCD 屏上、并记进「最近消息」。</div>
  </fieldset>

  <fieldset>
    <legend>【屏显控制】让板子上的屏立刻换成指定画面</legend>
    <div class="spo2btns">{''.join(screen_buttons)}</div>
    <div class="hint">点一下 ⇒ 那块屏立刻切换，并<strong>保持约 30 秒</strong>
      （否则 LCD 的调试面板每 2 秒刷新一次，会把刚切过去的画面顶掉）。<br>
      ⚠️ <strong>报警优先</strong>：真出报警时，报警文案会立刻抢屏，手动指定的画面会让位。<br>
      页名合法性由后端判定：参数不认识回 <code>400</code>，那块屏没接/被关掉回 <code>409</code>。</div>
  </fieldset>

  <div id="result"></div>
</main>
{_CONTROL_SCRIPT}
</body>
</html>
"""


def render_panel(runtime: Any, store: Any, *, secured: bool = False) -> str:
    """渲染**配置面板**（``GET /panel``）。

    Args:
        runtime: :class:`~health_monitor.service.Runtime`（只读它的版本/模式信息）。
        store: :class:`~health_monitor.core.configstore.ConfigStore`（提供 ``snapshot()``）。
        secured: 本服务是否启用了 ``--token``。**只影响提示文案** ——
            真正拦人是在 :meth:`WebApi.handle`，页面文案不能当安全边界。
    """
    snap = store.snapshot()
    thresholds = snap.get("thresholds") or {}
    devices = snap.get("devices") or {}
    store_warnings = list(snap.get("warnings") or [])

    # ---- 顶部：这件工具是"能改配置"的，必须一眼看出来 ----
    if secured:
        unsafe = (
            '<div class="unsafe" style="background:#e7f6ec;border-color:#58a55c;color:#24632c">'
            "本服务启用了 <code>--token</code>：写操作必须带 <code>X-Auth-Token</code>"
            '<span class="sub">页面本身不提供口令输入（避免口令进入浏览器历史）；'
            "请在请求头带 token 调用 <code>/api/v1/config</code>。</span></div>"
        )
    else:
        unsafe = (
            '<div class="unsafe">⚠ 此面板可以修改配置，而且<strong>未设防</strong>'
            '<span class="sub">任何能访问到本机 8080 端口的人都能改报警阈值与器件开关。'
            "仅限局域网/答辩演示使用；要保护起来就用 <code>serve --token &lt;口令&gt;</code> 启动。"
            "</span></div>"
        )

    # ---- 阈值分组 ----
    groups: List[str] = []
    for title, fields in THRESHOLD_GROUPS:
        items: List[str] = []
        for name, label, unit in fields:
            value = thresholds.get(name, "")
            items.append(
                '<div class="field">'
                f'<span class="lbl">{_esc(label)}</span>'
                f'<input type="number" step="any" data-th="{_esc(name)}" '
                f'data-label="{_esc(label)}" value="{_esc(_num_text(value))}">'
                f'<span class="unit">{_esc(unit)}</span>'
                "</div>"
            )
        groups.append(
            f"<fieldset><legend>{_esc(title)}</legend>"
            f'<div class="fields">{"".join(items)}</div></fieldset>'
        )

    # ---- 器件开关与周期 ----
    dev_rows: List[str] = []
    for name, info in devices.items():
        optional = ' <span class="hint">可选件</span>' if info.get("optional") else ""
        driver = _esc(info.get("driver"))
        checked = " checked" if info.get("enabled") else ""
        interval_text = _esc(_num_text(info.get("read_interval_s", 1.0)))
        dev_rows.append(
            "<tr>"
            f"<td><code>{_esc(name)}</code><br><span class='hint'>{driver}{optional}</span></td>"
            "<td><label class='field'>"
            f'<input type="checkbox" data-dev="{_esc(name)}" data-field="enabled"{checked}>'
            " <span>启用</span></label></td>"
            f'<td class="num"><input type="number" step="any" min="0.1" '
            f'data-dev="{_esc(name)}" data-field="read_interval_s" value="{interval_text}"> 秒</td>'
            "</tr>"
        )

    warn_block = ""
    if store_warnings:
        items = "".join(f"<li>{_esc(w)}</li>" for w in store_warnings)
        warn_block = f'<div class="banner warn">本机覆盖（devices.local.json）影响了面板正在编辑的项：<ul>{items}</ul></div>'

    # ⚠️ 2026-10-01：原来放在这里的【血氧检测】一节**已搬到功能面板**（`/control`）。
    # 为什么必须搬走而不是两页都放：一个动作只能有一个入口 —— 两页各放一份，
    # 迟早出现"一边改了、另一边忘了"（本项目"一个动作只有一条路径"的纪律）。
    # 本页从此只管一件事：**改数字**。
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>居家老人健康监护 · 配置</title>
<style>{_CSS}{_PANEL_CSS}</style>
</head>
<body>
<header>
  <h1>配置面板（报警阈值 / 器件开关）</h1>
  <div class="meta">版本 {_esc(runtime.version)} · {"模拟模式（无硬件）" if runtime.mock else "真实硬件模式"}
    · 配置文件 <code class="path">{_esc(snap.get("config_path", ""))}</code></div>
  {_nav("/panel")}
</header>
<main>
  {unsafe}
  {warn_block}
  <noscript>
    <div class="banner bad">本页需要 JavaScript 才能保存（表单要拼 JSON 并 POST）。
      没有 JS 时请直接用接口：<code>GET /api/v1/config</code> 看配置，
      <code>POST /api/v1/config</code> 改配置。</div>
  </noscript>

  <h2>报警阈值</h2>
  {''.join(groups)}

  <h2>器件开关与读取周期</h2>
  <table><thead><tr><th>设备</th><th>开关</th><th>读取周期</th></tr></thead>
  <tbody>{''.join(dev_rows) or "<tr><td colspan='3'>配置里没有设备</td></tr>"}</tbody></table>

  <div class="actions">
    <button id="save" onclick="save()">保存并立即生效</button>
    <a href="/panel">放弃改动，重新载入</a>
    <span class="hint">保存 = 写回配置文件（自动备份）<strong>并立即生效，不重启服务</strong></span>
  </div>
  <div id="result"></div>

  <footer>
    <strong>写入前一定会校验</strong>：未知字段、阈值自洽性、器件读取周期下限（DHT11 ≥ 2 秒）、
    以及<strong>GPIO 撞脚</strong> —— 任何一项不过就 <code>400</code> 且<strong>文件一个字节都不改</strong>。<br>
    前端也做了一次检查，但那只是"少跑一趟"；<strong>以后端为准</strong>。<br>
    引脚号<strong>不在本面板的编辑范围</strong>（接线属于硬件，改错了现场很难查）。<br>
    器件关掉后会被<strong>立即关闭并从系统里移除</strong>（不再采集/不再下发）；
    打开时若没接线，会在结果里以"注意"的形式告诉你 —— 配置仍然保存。
  </footer>
</main>
{_PANEL_SCRIPT}
</body>
</html>
"""


def _time_text(ts: float) -> str:
    """Unix 秒 → 本地时间字符串（网页显示用）。

    ⚠️ **不许因为一个离谱的时间戳把整页打崩**（2026-10-01）：本函数的调用方在
    `render_page` 里逐个渲染历史库的行，而 `time.localtime()` 对负数/越界值会抛
    `OSError`（Windows 上实测 `-800` 就抛）⇒ 库里只要有一行脏时间戳，
    **整张数据面板就 500**。而这一段代码的既定立场是"历史库出问题不该让整页打不开"
    （见上面读历史的那圈 try/except）—— 一个格式化函数不该成为那个例外。

    ⚠️⚠️ **而且范围要自己判，不能靠"平台会不会抛异常"**（2026-10-01，ERROR.md **E72**）：
    同一个 `time.localtime(-800)`，**Windows 抛 `OSError`、Linux/glibc 正常返回
    `1969-12-31 23:46:40`** ⇒ 只靠 try/except 的话，同一份代码在两个平台上**显示不同结果**，
    测试也会"CI（Linux）红、本机（Windows）绿"。
    所以这里先自己把范围卡在 **1970-01-01 ~ 2100-01-01**（对本项目足够宽），
    超出就显示 `--:--:--` —— **两个平台结果一致**。
    """
    import time as _time

    try:
        value = float(ts)
    except (TypeError, ValueError):
        return "--:--:--"
    if not (0.0 <= value < 4102444800.0):        # 1970-01-01 ~ 2100-01-01
        return "--:--:--"
    try:
        return _time.strftime("%H:%M:%S", _time.localtime(value))
    except (OSError, OverflowError, ValueError, TypeError):
        return "--:--:--"


__all__ = ["render_page", "render_control", "render_panel", "ALARM_LABELS",
           "KIND_LABELS", "THRESHOLD_GROUPS"]
