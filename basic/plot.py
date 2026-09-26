#!/usr/bin/env python3
"""基础版的动态曲线（课程任务 H 的核心考点：**Matplotlib 动态曲线**）。

任务书原话：程序保存连续采集的数据，并用 Matplotlib 绘制动态曲线；每得到一组新数据，
图中的曲线随之刷新。图中应明确标出温度、湿度以及采样顺序或时间。

本模块把这条要求做成三件**分离**的事（好测、也好改）：

1. :func:`read_once` —— 读一次传感器，包成 :class:`basic.model.Reading`（失败也留痕）；
2. :class:`Runner` —— 负责"读 → 存 CSV → 进窗口 → 刷新曲线"的主循环；
3. :func:`run_curve` —— 命令行入口（解析参数、选后端、决定用窗口还是无头模式）。

关于依赖：**只有画图这一步需要 matplotlib**（`sudo apt install -y python3-matplotlib python3-tk`）。
没有 matplotlib 时，本模块会给出安装命令并退出，不影响 CSV 照常记录（``--no-plot``）。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import List, Optional

# 允许 `python3 run.py` 直接跑（把 basic/ 的上一级加进 sys.path，好 import 本包）
if __package__ in (None, ""):  # pragma: no cover - 只影响"直接执行脚本"的路径
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

#: `basic/` 目录本身（用于定位随作业一起交的字体，见 `FONT_SEARCH_DIRS`）
BASIC_DIR = Path(__file__).resolve().parent

from basic.dht11read import Dht11Error, Dht11Reader, MIN_INTERVAL_S  # noqa: E402
from basic.console import safe_print, safe_text  # noqa: E402
from basic.model import Reading  # noqa: E402
from basic.pins import describe_pin  # noqa: E402
from basic.series import Series  # noqa: E402
from basic.store import (  # noqa: E402
    CsvStore,
    DEMO_FILE_NAME,
    default_csv_path,
    default_data_dir,
    load_rows,
)

#: 中文显示优先使用的字体。
#: ⚠️ 这里是**候选清单**，实际用哪个要问过 matplotlib 的字体管理器（见 `pick_cjk_font`）——
#: 2026-09-25 真机实测：树莓派上没装 fonts-noto-cjk，matplotlib 就一路退到列表末尾的
#: **DejaVu Sans（没有中文字形）**，于是每画一帧都刷一排
#: `UserWarning: Glyph 28201 ... missing from font(s) DejaVu Sans`，
#: 导出的 PNG 里中文全变方框（而终端日志一切正常，很容易漏看）。
CJK_FONTS = [
    # 首选：Noto Sans CJK（**拉丁数字 + 中文都全**；Debian 的 fonts-noto-cjk 装的就是它）
    "Noto Sans CJK SC", "Noto Sans CJK JP", "Noto Sans SC", "Source Han Sans SC",
    # 备选
    "WenQuanYi Zen Hei", "WenQuanYi Micro Hei", "Microsoft YaHei", "SimHei", "PingFang SC",
    # ⚠️ 最后才是 Droid Sans Fallback：**它只有 CJK 字形、没有任何拉丁字符**
    #    （实测 `font has '0': False`）⇒ 单独用它会让图里所有数字变方框（ERROR.md E39）。
    #    放在备选里是因为"有中文总比没有好"，但调用方应当优先选到 Noto。
    "Droid Sans Fallback",
]


def _normalize_font_name(name: str) -> str:
    """把字体名归一化后再比：`WenQuanYi Zen Hei` / `wqy-zenhei` 视为同一个。"""
    return "".join(ch for ch in name.lower() if ch.isalnum())


#: 会**主动扫描**的字体目录（找到字体文件就 `addfont` 注册进来）。
#: 为什么需要（2026-09-25 真机实测，ERROR.md E39）：把字体文件拷进
#: `~/.local/share/fonts/` 之后，**matplotlib 的字体缓存里还没有它** ——
#: 直接 `pick_cjk_font()` 仍然只看得到旧的 Droid Sans Fallback（那个只有 CJK 字形、
#: 没有拉丁数字）。主动扫一遍并注册，就不依赖"用户手动重建字体缓存"。
#: 第一条 `basic/fonts/` 是**随作业一起交的字体**（拉丁 + 中文都全的 Noto Sans CJK），
#: 这样"把 basic/ 拷到任意树莓派上"都能出中文图，不用先 sudo 装 fonts-noto-cjk。
FONT_SEARCH_DIRS = (
    str(BASIC_DIR / "fonts"),
    "~/.local/share/fonts",
    "~/.fonts",
    "/usr/local/share/fonts",
    "/usr/share/fonts/opentype/noto",
    "/usr/share/fonts/truetype/noto",
)


def register_local_fonts() -> int:
    """把常见字体目录里的字体文件注册进 matplotlib；返回这次注册的个数（幂等）。

    ⚠️ 没装 matplotlib 时直接返回 0（不抛）：matplotlib 是可选依赖。
    """
    import glob
    import os

    try:
        import matplotlib.font_manager as fm
    except ImportError:  # pragma: no cover
        return 0

    added = 0
    for directory in FONT_SEARCH_DIRS:
        path = os.path.expanduser(directory)
        if not os.path.isdir(path):
            continue
        for pattern in ("**/*.ttc", "**/*.otf", "**/*.ttf"):
            for font_file in glob.glob(os.path.join(path, pattern), recursive=True):
                try:
                    fm.fontManager.addfont(font_file)
                    added += 1
                except Exception:  # noqa: BLE001 - 单个字体坏了不该影响画图
                    continue
    return added


def pick_cjk_font(available: Optional[List[str]] = None) -> Optional[str]:
    """从**系统真的装了**的字体里挑一个能显示中文的（挑不到返回 ``None``）。

    为什么要问字体管理器而不是直接写死一个名字：写死的名字没装时 matplotlib **不报错**，
    只是静默退到默认字体、把中文画成方框（本项目踩过，见 `CJK_FONTS` 的注释）。

    匹配顺序：① 候选清单里的名字（归一化后精确匹配）→ ② 名字里带中文字体常见字样兜底
    （各发行版命名差异很大：`wqy-zenhei`、`Droid Sans Fallback`、`Source Han Sans`…）。

    ⚠️ **没装 matplotlib 时不要抛异常**：基础版把 matplotlib 当**可选依赖**，
    这个函数被测试与"提示装字体"的路径调用，抛出去会让"没装 matplotlib"变成红。
    """
    try:
        import matplotlib.font_manager as fm

        names = list(available if available is not None else fm.get_font_names())
    except ImportError:  # pragma: no cover - 没装 matplotlib 时无法查系统字体
        return None
    normalized = {_normalize_font_name(n): n for n in names}
    for candidate in CJK_FONTS:
        hit = normalized.get(_normalize_font_name(candidate))
        if hit is not None:
            return hit
    # 退一步：名字里带中文字体常见字样也算候选（发行版命名差异很大：
    # `Noto Sans CJK JP`、`wqy-zenhei`、`Source Han Sans SC`…）；
    # ⚠️ 排序让 **Noto/思源** 优先于 Droid Sans Fallback —— 后者只有 CJK 字形、
    #    没有拉丁数字（ERROR.md E39）。
    tokens = ("notosanscjk", "notosanssc", "sourcehansans", "cjk", "wqy", "wenquanyi", "droidsansfallback")
    candidates = [n for n in sorted(names) if any(t in _normalize_font_name(n) for t in tokens)]
    for token in tokens:
        for name in candidates:
            if token in _normalize_font_name(name):
                return name
    return None


def _cjk_font_file(family: str) -> Optional[str]:
    """拿到某个字体家族对应的**文件路径**（拿不到返回 ``None``）。"""
    try:
        import matplotlib.font_manager as fm

        return fm.findfont(fm.FontProperties(family=family), fallback_to_default=False)
    except Exception:  # noqa: BLE001 - 没装 matplotlib / 找不到字体，都不该让调用方挂掉
        return None


def cjk_font_chain() -> List[str]:
    """返回**可直接吃的字体链**：拉丁通用字体在前、CJK 字体在后。

    ⚠️ 2026-09-25 真机实测（ERROR.md E39）踩了三层，逐条记下来：
    1. **顺序**：CJK 字体放**第一位** ⇒ 图里所有**数字/单位变方框**
       （树莓派自带那个 `DroidSansFallbackFull.ttf` **只有 CJK 字形**，实测 `'0'` 都不含）；
    2. **matplotlib 3.11 在"单个文本对象内部"不做逐字回退**：
       链写成 `['DejaVu Sans', 'Droid Sans Fallback']` 时中文仍是方框（实测 7 条缺字形警告）
       ⇒ 靠"两个都不全的字体拼起来"这条路**在文本内部走不通**；
    3. **正解是换一个"拉丁 + 中文都全"的字体**：`fonts-noto-cjk`（本项目已把
       `NotoSansCJK-Regular.ttc` 放到 `~/.local/share/fonts/` 并把字体**源文件**写进
       `basic/fonts/`，不需要 sudo 装系统包）⇒ 实测**缺字形警告 0 条**。

    所以：`pick_cjk_font()` 会优先选到 Noto；选不到时才退回 Droid（那时中文能显示、
    但数字可能要试 `--font` 或装字体，见 README）。
    """
    import matplotlib.font_manager as fm

    chosen = _register_then_pick()
    chain = ["DejaVu Sans"]
    if chosen is None:
        return chain
    path = _cjk_font_file(chosen)
    if path:
        try:
            fm.fontManager.addfont(path)          # 按文件注册，比只写家族名可靠
        except Exception:  # noqa: BLE001 - 注册失败就退回"只用家族名"
            pass
    chain.append(chosen)
    chain.extend(name for name in CJK_FONTS if name != chosen)
    return chain


def _register_then_pick() -> Optional[str]:
    """先注册本地字体再挑 —— 否则新拷进来的字体（缓存里还没有）会被漏掉。"""
    first = pick_cjk_font()
    if first is not None and "droid" not in _normalize_font_name(first):
        return first                              # 已经挑到"较好的"（例如 Noto）就不折腾
    register_local_fonts()
    return pick_cjk_font() or first


def configure_cjk_font() -> Optional[str]:
    """把 matplotlib 的中文显示配好；返回实际选中的中文字体名（挑不到返回 ``None``）。

    挑不到时的提醒是**刻意**的：让用户知道"图里的中文会是方框"以及一行修复命令，
    而不是让一张方框图悄悄交上去。

    判据（老实话）：**选完必须真的渲染一次并看图** —— `pick_cjk_font()` 只能证明
    "这个名字在系统里"，证明不了"matplotlib 渲染时真的用它"（本轮两层坑都是这么来的）。
    """
    import matplotlib.pyplot as plt

    chain = cjk_font_chain()
    plt.rcParams["font.sans-serif"] = chain
    #: 有些图会显式传 `fontfamily=`，这里把 family 别名也指过去（双保险）
    plt.rcParams["font.family"] = "sans-serif"
    plt.rcParams["axes.unicode_minus"] = False
    return chain[1] if len(chain) > 1 else None


def read_once(reader: Dht11Reader, index: int, now: Optional[float] = None) -> Reading:
    """读一次并包成 :class:`Reading`（**失败也返回记录，不抛异常**）。"""
    stamp = time.time() if now is None else float(now)
    result = reader.read()
    if result.ok:
        return Reading(index, stamp, result.temperature_c, result.humidity_percent, ok=True)
    return Reading(index, stamp, None, None, ok=False, note=result.note)


class Runner:
    """采集主循环：读 → 存 → 进窗口 → 刷新（三件事都可以关掉，方便分步验证）。

    Args:
        reader: DHT11 读取器（真实或 mock）。
        store: CSV 存档（``None`` = 只显示不落盘）。
        series: 曲线数据窗口。
        interval_s: 采样间隔（秒）。
        max_failures: 连续失败多少次就停止（**避免对着坏接线刷屏**）。
        clock: 可注入的时钟（默认 :func:`time.monotonic`，与读取器的时钟一致）。
        sleep: 可注入的 sleep（测试时换成"不睡"，就不会真的等 3 秒）。
    """

    def __init__(
        self,
        reader: Dht11Reader,
        store: Optional[CsvStore],
        series: Series,
        interval_s: float = 3.0,
        max_failures: int = 5,
        clock=time.monotonic,
        sleep=time.sleep,
    ) -> None:
        self.reader = reader
        self.store = store
        self.series = series
        self.interval_s = float(interval_s)
        self.max_failures = int(max_failures)
        self._clock = clock
        self._sleep = sleep
        self.last: Optional[Reading] = None
        self.stopped_reason = ""

    # ------------------------------------------------------------------
    # 一轮采集
    # ------------------------------------------------------------------

    def tick(self, now: Optional[float] = None) -> Reading:
        """读一次、落盘、进窗口；返回这次记录。"""
        stamp = time.time() if now is None else float(now)
        reading = read_once(self.reader, self.series.total + 1, now=stamp)
        self.last = reading
        if self.store is not None:
            self.store.save(reading)
        self.series.append(reading.ts, reading.temperature_c, reading.humidity_percent, reading.index)
        if (
            self.max_failures > 0
            and self.reader.consecutive_failures >= self.max_failures
            and not self.stopped_reason
        ):
            self.stopped_reason = (
                f"连续 {self.reader.consecutive_failures} 次读不到数据，已停止采集。"
                "排查顺序：① 供电 3.3V；② 数据线在 GPIO4（物理脚 7）；"
                "③ 裸传感器要接 4.7kΩ~10kΩ 上拉电阻；④ 换一个 DHT11 模块试试。"
            )
        return reading

    @property
    def should_stop(self) -> bool:
        return bool(self.stopped_reason)

    def sleep_until_next(self, started_at: float) -> float:
        """睡到"下一次采样时刻"（把读取耗时扣掉，采样周期才稳定）。

        ⚠️ 至少睡 1 ms：树莓派对"刚好 3.000 秒"的判定很敏感——若读取耗时恰好等于周期，
        实际间隔会差几毫秒，DHT11 侧就会被判成"读太快"而拒读（曲线出现空洞）。
        多睡 1 ms 换来的是**每个采样点都稳稳拿到**，代价可以忽略。
        """
        remaining = max(0.001, self.interval_s - (self._clock() - started_at))
        self._sleep(remaining)
        return remaining

    def summary(self, started_at: float) -> dict:
        """一次运行的小结（写进 ``*.summary.json``，也打印在终端）。"""
        stats = self.series.stats()
        read_summary = self.last.summary() if self.last else "还没有读到数据"
        return {
            "started_at": Reading(0, started_at, None, None).timestamp_text,
            "finished_at": Reading(0, time.time(), None, None).timestamp_text,
            "duration_s": round(time.time() - started_at, 1),
            "interval_s": self.interval_s,
            "source": self.reader.describe(),
            "backend": self.reader.backend_name,
            "csv": str(self.store.path) if self.store else "",
            "csv_rows": self.store.rows_written if self.store else 0,
            "samples": stats["samples"],
            "ok": stats["ok"],
            "failed": stats["failed"],
            "temperature_min_c": stats["temp_min"],
            "temperature_max_c": stats["temp_max"],
            "humidity_min_percent": stats["humidity_min"],
            "humidity_max_percent": stats["humidity_max"],
            "last_reading": read_summary,
            "stopped_reason": self.stopped_reason,
        }


# --------------------------------------------------------------------------
# 画图
# --------------------------------------------------------------------------


class CurveWindow:
    """双轴动态曲线窗口（温度红 / 湿度蓝），标题实时显示最新一次读数。"""

    def __init__(self, series: Series, use_index: bool, source_text: str, interval_s: float,
                 save_path: str = "") -> None:
        import matplotlib.pyplot as plt

        # ⚠️ 建图**之前**再配一次字体（2026-09-25 真机实测，ERROR.md E39）：
        #    只在启动时配一次并不保险 —— 中途重新 import pyplot / 换后端会把
        #    `rcParams["font.sans-serif"]` 打回默认，结果是"数字正常、中文全方框"。
        configure_cjk_font()
        self.plt = plt
        self.series = series
        self.use_index = use_index
        self.source_text = source_text
        self.interval_s = interval_s
        self.save_path = save_path
        self._cjk_prop = None            # 中文字体属性（首次刷新时解析并缓存，见 _apply_font_to_all_text）
        self.fig, self.ax_temp = plt.subplots(figsize=(9, 5))
        # 给底部留出空间：横轴标签 + 两行状态行都要放得下（判据见几何断言测试）
        self.fig.subplots_adjust(bottom=0.21)
        self.ax_humid = self.ax_temp.twinx()
        (self.line_temp,) = self.ax_temp.plot(
            [], [], color="#d62728", marker="o", markersize=3, label="温度 (℃)"
        )
        (self.line_humid,) = self.ax_humid.plot(
            [], [], color="#1f77b4", marker="s", markersize=3, label="湿度 (%)"
        )
        self.ax_temp.set_xlabel("采样序号" if use_index else "时间 (秒)")
        self.ax_temp.set_ylabel("温度 (℃)", color="#d62728")
        self.ax_humid.set_ylabel("湿度 (%)", color="#1f77b4")
        self.ax_temp.tick_params(axis="y", labelcolor="#d62728")
        self.ax_humid.tick_params(axis="y", labelcolor="#1f77b4")
        self.ax_temp.grid(True, linestyle=":", alpha=0.5)
        self.title = self.ax_temp.set_title("温湿度动态曲线（等待第一组数据…）")
        self.footer = self.fig.text(0.01, 0.01, "正在启动…", fontsize=9, color="#555555")
        self.ax_temp.legend([self.line_temp, self.line_humid], ["温度 (℃)", "湿度 (%)"], loc="upper left")
        # 底部状态行放低一点（fig.text 的 y 是"图坐标" 0~1，不是像素）：
        # y=0.01 会与 x 轴标签"时间 (秒)"叠在一起（实测截图确认），压到 0.005 更稳
        self.footer.set_position((0.01, 0.005))

    def refresh(self) -> None:
        """把最新数据画上去（每采到一个点调一次）。"""
        xs = self.series.x_axis(self.use_index)
        self.line_temp.set_data(xs, list(self.series.temps))
        self.line_humid.set_data(xs, list(self.series.humids))
        latest = self.series.latest
        if latest is None:
            text = "最新一次读数　　温度: 未知　　湿度: 未知"
        elif latest[0] is None or latest[1] is None:
            text = "最新一次读数　　温度: 未知　　湿度: 未知（本次没读到）"
        else:
            text = f"最新一次读数　　温度: {latest[0]:.1f} ℃　　湿度: {latest[1]:.0f} %"
        self.title.set_text(text)
        # ⚠️ 每次刷新都把字体属性重新挂一遍（2026-09-25 真机实测，ERROR.md E39）：
        #    标题文字每次 set_text 后都会重新解析字体，回退链在**动态刷新**里同样不可靠
        #    （实测原地 draw 时中文是方框）。挂在文本对象上才稳。
        self._apply_font_to_all_text()

        if xs:
            self.ax_temp.set_xlim(min(xs), max(xs) if max(xs) > min(xs) else min(xs) + 1)
        else:
            self.ax_temp.set_xlim(0, 1)
        for axis, values in ((self.ax_temp, self.series.temps), (self.ax_humid, self.series.humids)):
            valid = [v for v in values if v is not None]
            if valid:
                low, high = min(valid), max(valid)
                pad = max(0.5, (high - low) * 0.2)
                axis.set_ylim(low - pad, high + pad)

        stats = self.series.stats()
        # ⚠️ 分两行写：单行时状态行会**压住横轴标签"时间 (秒)"**（实测截图确认）。
        #    判据不是"看着还行"，而是像素矩形不相交（见 basic/tests/test_basic_curve.py 的几何断言）。
        self.footer.set_text(
            f"数据源：{self.source_text}　采样周期：{self.interval_s:g}s\n"
            f"累计样本：{stats['samples']}（成功 {stats['ok']} / 失败 {stats['failed']}）　"
            f"温度：{fmt(stats['temp_min'])} ~ {fmt(stats['temp_max'])} ℃　"
            f"湿度：{fmt(stats['humidity_min'])} ~ {fmt(stats['humidity_max'])} %"
        )
        self.fig.canvas.draw_idle()

    def _apply_font_to_all_text(self) -> None:
        """把中文字体的 `FontProperties` 直接挂到**每一个文本对象**上。

        为什么必须这么"笨"（2026-09-25 真机实测，ERROR.md E39）：
        `rcParams["font.sans-serif"]` 的**回退链**在动态刷新与换 DPI 导出时都不可靠 ——
        同一个窗口实测：`draw()` 缺字形 0 条，但 `savefig(dpi=110)` **43 条**
        （DPI 变了 ⇒ matplotlib 重新解析字体，回退链没跟上），
        而**每次 `set_text` 之后**同样会退回默认字体 ⇒ 屏幕上的曲线标题变方框。
        直接把字体属性挂到文本对象上就不依赖链（实测导出与刷新都是 0 条）。

        性能：`FontProperties` 只构造一次并缓存（每 3 秒刷新一次不该反复查字体表）。
        """
        from matplotlib.text import Text

        if getattr(self, "_cjk_prop", None) is None:
            chosen = pick_cjk_font()
            if chosen is None:
                self._cjk_prop = False            # 标记"没有可用中文字体"，别反复查
            else:
                import matplotlib.font_manager as fm

                path = _cjk_font_file(chosen)
                self._cjk_prop = fm.FontProperties(fname=path) if path else fm.FontProperties(family=chosen)
        if not self._cjk_prop:
            return
        prop = self._cjk_prop
        for text in self.fig.findobj(Text):
            try:
                text.set_fontproperties(prop)
            except Exception:  # noqa: BLE001 - 个别对象不接受就跳过，不影响出图
                continue

    def save(self) -> None:
        if self.save_path:
            self._apply_font_to_all_text()
            self.fig.savefig(self.save_path, dpi=110, bbox_inches="tight")

    def xlabel_bbox(self):
        """x 轴标签的位置（供"状态行不许压住标签"的回归测试使用）。"""
        return self.ax_temp.xaxis.label.get_window_extent(renderer=self.fig.canvas.get_renderer())

    def footer_bbox(self):
        """底部状态行的位置（同上）。"""
        return self.footer.get_window_extent(renderer=self.fig.canvas.get_renderer())


def fmt(value: Optional[float]) -> str:
    """数值格式化：``None`` → ``未知``（**绝不显示 0**）。"""
    return "未知" if value is None else f"{value:.1f}"


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def run_curve(args: argparse.Namespace) -> int:
    """按命令行参数跑一次采集 + 动态曲线。"""
    started_at = time.time()

    # ① 数据源
    if args.replay:
        return run_replay(args)

    try:
        # ⚠️ 门禁用**硬件下限**，不要用采样周期（2026-09-25 真机实测，ERROR.md E38）：
        #    以前传 `max(args.interval, 2.0)`，于是"3 秒采样 + 读/存/画花掉 60ms"
        #    ⇒ 真实间隔 2.94 秒 < 3.0 秒 ⇒ **隔一次被拒**（实测 4 次里失败 2 次）。
        #    节奏由主循环的 `--interval` 控，门禁只负责"别读得比硬件允许的更快"。
        reader = Dht11Reader(pin=args.pin, mock=args.mock, min_interval_s=MIN_INTERVAL_S)
        backend = reader.open()
    except Dht11Error as exc:
        print(safe_text(f"❌ 打不开 DHT11：{exc}"))
        return 2
    print(f"数据源：{reader.describe()}　后端：{backend}")

    # ② 存档
    store = None
    if not args.no_csv:
        path = Path(args.csv) if args.csv else default_csv_path()
        store = CsvStore(path, append=not args.overwrite)
        print(f"数据存档：{path}")

    series = Series(window=args.window)
    runner = Runner(reader, store, series, interval_s=args.interval, sleep=time.sleep)

    # ③ 画图（可选）
    window: Optional[CurveWindow] = None
    if not args.no_plot:
        try:
            import matplotlib

            if args.headless or not sys.stdout.isatty():
                matplotlib.use("Agg")
            import matplotlib.pyplot as plt

            if configure_cjk_font() is None:
                print(safe_text("⚠️ 没找到中文字体，图里的中文会变成方框。装一个即可："))
                print("   sudo apt install -y fonts-noto-cjk")
            window = CurveWindow(series, args.xaxis == "index", reader.describe(),
                                 args.interval, save_path=args.save)
        except ImportError:
            print(safe_text("⚠️ 没装 matplotlib，改用『只记录不画图』模式。装上即可看曲线："))
            print("   sudo apt install -y python3-matplotlib python3-tk")
    else:
        print("（--no-plot：只采集与存档，不画图）")

    headless = args.headless or not sys.stdout.isatty() or window is None
    if headless and args.duration <= 0:
        args.duration = 15.0
        print(f"无窗口模式：默认采集 {args.duration:g} 秒后退出（可用 --duration 改）")

    try:
        deadline = time.time() + args.duration if args.duration > 0 else None
        while True:
            loop_started = time.monotonic()
            reading = runner.tick()
            stamp = time.strftime("%H:%M:%S", time.localtime(reading.ts))
            print(f"[{stamp}] #{reading.index:>4}　{reading.summary()}"
                  + (f"　（{reading.note}）" if not reading.ok and reading.note else ""))
            if window is not None:
                window.refresh()
            if runner.should_stop:
                safe_print(f"⛔ {runner.stopped_reason}")
                break
            if deadline is not None and time.time() >= deadline:
                break
            runner.sleep_until_next(loop_started)
    except KeyboardInterrupt:
        print("\n已中断（Ctrl+C）——已采集的数据都已经写进 CSV 了")

    # ④ 收尾
    if window is not None:
        window.refresh()
        window.save()
        if args.save:
            print(f"曲线已导出：{args.save}")
    summary = runner.summary(started_at)
    if store is not None:
        target = store.save_summary(summary)
        print(f"运行小结：{target}（{summary['samples']} 次采样，成功 {summary['ok']}，失败 {summary['failed']}）")
    print(f"最新一次读数　　{summary['last_reading']}")
    reader.close()
    return 0


def run_replay(args: argparse.Namespace) -> int:
    """离线回放模式：读既有 CSV 当数据源（**没接硬件也能验证画图这条链路**）。

    ``--replay`` 不带路径时会用仓库里的演示数据（`basic/data/sample_demo.csv`）。
    """
    path = Path(args.replay) if args.replay else default_data_dir() / DEMO_FILE_NAME
    if not path.exists():
        path = default_data_dir() / DEMO_FILE_NAME
    # ⚠️ 用**只读**的 load_rows，**不要** new 一个 append=False 的 CsvStore 来读：
    #    后者的语义是"下次写入前清空文件"，会把要回放的数据删掉（真的踩过）。
    rows = load_rows(path, limit=args.window)
    if not rows:
        print(safe_text(f"❌ {path} 里没有可回放的数据（需要 index,timestamp,temperature_c,humidity_percent,status,note 表头）"))
        return 2
    print(f"回放数据：{path}（{len(rows)} 个点）")

    series = Series(window=args.window)
    try:
        import matplotlib

        if args.headless or not sys.stdout.isatty():
            matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        if configure_cjk_font() is None:
            print(safe_text("⚠️ 没找到中文字体，图里的中文会变成方框。装一个即可："))
            print("   sudo apt install -y fonts-noto-cjk")
    except ImportError:
        print(safe_text("❌ 回放需要 matplotlib（它本来就是演示画图用的）：sudo apt install -y python3-matplotlib python3-tk"))
        return 2

    window = CurveWindow(series, args.xaxis == "index", f"CSV 回放 {path.name}",
                         args.interval, save_path=args.save)
    delay = max(0.05, args.replay_speed)
    for index, (ts, temp, humid) in enumerate(rows, start=1):
        series.append(ts, temp, humid, index)
        window.refresh()
        if not args.headless and delay:
            time.sleep(delay)
    window.save()
    stats = series.stats()
    print(safe_text(f"回放完成：{stats['samples']} 个点，温度 {fmt(stats['temp_min'])}~{fmt(stats['temp_max'])} ℃，"
                    f"湿度 {fmt(stats['humidity_min'])}~{fmt(stats['humidity_max'])} %"))
    if args.save:
        print(f"曲线已导出：{args.save}")
    if not args.headless:
        try:
            window.plt.show()
        except KeyboardInterrupt:
            pass
    return 0


def build_parser() -> argparse.ArgumentParser:
    """命令行参数（每一个都有默认值，所以"什么都不带"也能跑）。"""
    parser = argparse.ArgumentParser(
        description="温湿度测量 + 动态曲线（课程作业 H 的基础版）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--pin", type=int, default=4, help="DHT11 数据脚 BCM 编号（4 = 物理脚 7）")
    parser.add_argument("--interval", type=float, default=3.0, help="采样周期（秒）；DHT11 必须 ≥2 秒")
    parser.add_argument("--window", type=int, default=120, help="曲线窗口保留的采样点数")
    parser.add_argument("--xaxis", choices=["time", "index"], default="time",
                        help="横轴：相对时间（秒）或采样序号")
    parser.add_argument("--csv", default="", help="CSV 存档路径（默认 basic/data/dht11_日期.csv）")
    parser.add_argument("--no-csv", action="store_true", help="不写 CSV（只看曲线）")
    parser.add_argument("--overwrite", action="store_true", help="覆盖同名 CSV（默认追加）")
    parser.add_argument("--mock", action="store_true", help="用合成数据（**没有树莓派时必须加**）")
    parser.add_argument("--no-plot", action="store_true", help="不画图，只采集与存档")
    parser.add_argument("--save", default="", help="把曲线导出成 PNG（报告插图用）")
    parser.add_argument("--duration", type=float, default=0.0, help="采集多少秒后退出（0 = 一直跑到关窗）")
    parser.add_argument("--headless", action="store_true", help="不弹窗口（配合 --duration/--save）")
    parser.add_argument("--replay", nargs="?", const=str(default_data_dir() / DEMO_FILE_NAME), default="",
                        help="回放既有 CSV（离线验证画图，不需硬件）；不带路径则用演示数据")
    parser.add_argument("--replay-speed", type=float, default=0.15, help="回放时每点之间的间隔秒")
    parser.add_argument("--max-failures", type=int, default=5, help="连续失败多少次后停止（0 = 不停）")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.interval < MIN_INTERVAL_S and not args.mock:
        print(safe_text(f"⚠️ DHT11 两次读取必须间隔 ≥{MIN_INTERVAL_S:g} 秒（硬件限制），已自动提到 {MIN_INTERVAL_S:g} 秒"))
        args.interval = MIN_INTERVAL_S
    return run_curve(args)


__all__ = ["main", "run_curve", "run_replay", "Runner", "CurveWindow", "read_once", "fmt"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
