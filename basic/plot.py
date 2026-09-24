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

from basic.dht11read import Dht11Error, Dht11Reader, MIN_INTERVAL_S  # noqa: E402
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

#: 中文显示优先使用的字体（树莓派上建议 `sudo apt install -y fonts-noto-cjk`）
CJK_FONTS = [
    "Noto Sans CJK SC", "WenQuanYi Zen Hei", "WenQuanYi Micro Hei",
    "Microsoft YaHei", "SimHei", "PingFang SC", "DejaVu Sans",
]


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

        self.plt = plt
        self.series = series
        self.use_index = use_index
        self.source_text = source_text
        self.interval_s = interval_s
        self.save_path = save_path
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

    def save(self) -> None:
        if self.save_path:
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
        reader = Dht11Reader(pin=args.pin, mock=args.mock, min_interval_s=max(args.interval, MIN_INTERVAL_S))
        backend = reader.open()
    except Dht11Error as exc:
        print(f"❌ 打不开 DHT11：{exc}")
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

            plt.rcParams["font.sans-serif"] = CJK_FONTS
            plt.rcParams["axes.unicode_minus"] = False
            window = CurveWindow(series, args.xaxis == "index", reader.describe(),
                                 args.interval, save_path=args.save)
        except ImportError:
            print("⚠️ 没装 matplotlib，改用『只记录不画图』模式。装上即可看曲线：")
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
                print(f"⛔ {runner.stopped_reason}")
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
        print(f"❌ {path} 里没有可回放的数据（需要 index,timestamp,temperature_c,humidity_percent,status,note 表头）")
        return 2
    print(f"回放数据：{path}（{len(rows)} 个点）")

    series = Series(window=args.window)
    try:
        import matplotlib

        if args.headless or not sys.stdout.isatty():
            matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        plt.rcParams["font.sans-serif"] = CJK_FONTS
        plt.rcParams["axes.unicode_minus"] = False
    except ImportError:
        print("❌ 回放需要 matplotlib（它本来就是演示画图用的）：sudo apt install -y python3-matplotlib python3-tk")
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
    print(f"回放完成：{stats['samples']} 个点，温度 {fmt(stats['temp_min'])}~{fmt(stats['temp_max'])} ℃，"
          f"湿度 {fmt(stats['humidity_min'])}~{fmt(stats['humidity_max'])} %")
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
        print(f"⚠️ DHT11 两次读取必须间隔 ≥{MIN_INTERVAL_S:g} 秒（硬件限制），已自动提到 {MIN_INTERVAL_S:g} 秒")
        args.interval = MIN_INTERVAL_S
    return run_curve(args)


__all__ = ["main", "run_curve", "run_replay", "Runner", "CurveWindow", "read_once", "fmt"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
