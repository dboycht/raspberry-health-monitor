#!/usr/bin/env python3
"""温湿度**动态曲线窗口**（课程任务 H 的考点：Matplotlib 实时绘图）。

任务书原话（H：温湿度测量）：
> 使用树莓派 5 和课程中学习过的温湿度传感器（如 DHT11）**周期性读取温度和湿度**。
> 程序**保存连续采集的数据**，并用 **Matplotlib 绘制动态曲线**；每得到一组新数据，
> 图中的曲线随之刷新。图中应**明确标出温度、湿度以及采样顺序或时间**，
> 使读者能够观察环境参数随时间的变化趋势。

本脚本把这条要求做成一个**可独立运行的可视化程序**，并接到本项目的两层能力上：

- ``--source device``：**直接读 DHT11 驱动**（不依赖服务；这是"传感器 → 曲线"的最短链路）
- ``--source api``   ：从**本项目 HTTP 接口**取历史数据（复用已落库的连续采集数据）
  —— 演示"采集持久化 + 可视化"是同一个系统的两部分

界面（自上而下）：
1. 大字标题 + **最新一次读数**（例如 `温度: 25.0 ℃   湿度: 58 %`）；
2. **双轴曲线**：温度（红，左轴 ℃）与湿度（蓝，右轴 %）随时间/采样序号向前延伸；
3. 底部状态行：数据源、样本数、采样间隔、时间范围。

用法::

    cd ~/raspberry-health-monitor/rpi
    python3 scripts/live_plot.py                      # 默认直接读 DHT11（GPIO4）
    python3 scripts/live_plot.py --source api --base-url http://127.0.0.1:8080
    python3 scripts/live_plot.py --interval 3 --window 60      # 3 秒一次，窗口保留 60 点
    python3 scripts/live_plot.py --save curves.png             # 每帧同时导出图片
    python3 scripts/live_plot.py --headless --duration 30      # 无显示器：跑 30 秒后导出 PNG

⚠️ 依赖（树莓派上装，PC 上不装也能跑单测）：
    sudo apt install -y python3-tk python3-matplotlib
没有 matplotlib 时本脚本会给出安装命令并退出（**不影响**系统其它功能）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Deque, List, Optional, Tuple

# 让 `basic.console.safe_print` 可导入（打印 ✅/❌/⚠ 时在窄编码控制台上自动降级）
# ⚠️ 为什么（2026-09-25 真机实测，ERROR.md E32/E35）：中文 Windows / GBK 控制台上
#    `print` 直接打印这些符号时会抛 UnicodeEncodeError 把**整个脚本**崩掉；这些工具主要跑在
#    树莓派（UTF-8）上，导入失败就退回内置 print（行为与过去一致）。
try:
    from pathlib import Path  # noqa: E402
except ImportError:  # pragma: no cover - Path 是标准库，理论上不会失败
    Path = None
ROOT = Path(__file__).resolve().parents[2] if Path is not None else None
if ROOT is not None and str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    from basic.console import safe_print  # noqa: E402
except ImportError:  # pragma: no cover - 只在 basic 不可用时
    safe_print = print

RPI_DIR = Path(__file__).resolve().parents[1]
if str(RPI_DIR) not in sys.path:
    sys.path.insert(0, str(RPI_DIR))

#: 采样点：(时间戳, 温度, 湿度, 序号)
Sample = Tuple[float, Optional[float], Optional[float], int]


@dataclass
class Series:
    """连续采集的数据缓冲（固定长度，自动丢弃最旧的点）。

    单独抽成一个类是为了**可单测**：滚动窗口、x 轴取值、统计量都不依赖 GUI。
    """

    window: int = 120
    times: Deque[float] = field(default_factory=deque)
    temps: Deque[Optional[float]] = field(default_factory=deque)
    humids: Deque[Optional[float]] = field(default_factory=deque)
    indices: Deque[int] = field(default_factory=deque)
    total: int = 0

    def append(self, ts: float, temp: Optional[float], humid: Optional[float]) -> None:
        self.times.append(ts)
        self.temps.append(temp)
        self.humids.append(humid)
        self.indices.append(self.total)
        self.total += 1
        while len(self.times) > self.window:
            self.times.popleft()
            self.temps.popleft()
            self.humids.popleft()
            self.indices.popleft()

    def load_history(self, rows: List[Tuple[float, Optional[float], Optional[float]]]) -> None:
        """用历史数据预填充（``--source api`` 时先画上已有曲线，避免从空开始）。"""
        for ts, temp, humid in rows[-self.window:]:
            self.append(ts, temp, humid)

    @property
    def latest(self) -> Optional[Tuple[Optional[float], Optional[float]]]:
        if not self.temps:
            return None
        return self.temps[-1], self.humids[-1]

    def x_axis(self, use_index: bool) -> List[float]:
        """x 轴数据：采样**序号**或**相对时间（秒）**。

        任务书要求"标出采样顺序或时间" —— 两种都给得了，用 ``--xaxis`` 选。
        """
        if use_index:
            return list(self.indices)
        if not self.times:
            return []
        base = self.times[0]
        return [round(t - base, 1) for t in self.times]

    def stats(self) -> dict:
        temps = [t for t in self.temps if t is not None]
        humids = [h for h in self.humids if h is not None]
        return {
            "samples": self.total,
            "window": len(self.times),
            "temp_min": min(temps) if temps else None,
            "temp_max": max(temps) if temps else None,
            "humidity_min": min(humids) if humids else None,
            "humidity_max": max(humids) if humids else None,
        }


# --------------------------------------------------------------------------
# 数据源
# --------------------------------------------------------------------------


class DeviceSource:
    """直接读 DHT11 驱动（``--source device``）。"""

    def __init__(self, pin: int = 4, mock: bool = False) -> None:
        from health_monitor.hal import create_device

        self.device = create_device("dht11", params={"pin": pin}, mock=mock, name="ambient")
        self.device.open()
        self.describe = f"DHT11（GPIO{pin}，{'模拟' if mock else '真实'}）"

    def read(self) -> Tuple[Optional[float], Optional[float]]:
        sample = self.device.read()
        # DHT11 有 2 秒硬件限制：间隔不足时驱动返回"缓存样本"（ok=False + is_cached）
        if not sample.ok and not getattr(sample, "is_cached", False):
            return None, None
        return sample.temperature_c, sample.humidity_percent

    def close(self) -> None:
        self.device.close()


class ApiSource:
    """从本项目的 HTTP 接口取数据（``--source api``）。"""

    def __init__(self, base_url: str, timeout: float = 3.0) -> None:
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.describe = f"HTTP {self.base}（本项目服务）"

    def _get(self, path: str) -> dict:
        with urllib.request.urlopen(f"{self.base}{path}", timeout=self.timeout) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))

    def read(self) -> Tuple[Optional[float], Optional[float]]:
        payload = self._get("/api/v1/current")
        data = payload.get("data") or {}
        return data.get("ambient_temp_c"), data.get("humidity_percent")

    def history(self, limit: int = 120) -> List[Tuple[float, Optional[float], Optional[float]]]:
        """取历史曲线（温度与湿度分别取，再按时间对齐）。

        说明：本项目把两个指标存在同一张表的不同 metric 里，所以这里各取一次、
        以时间为键合并——**缺失的一方填 None**（不补 0，与本项目"缺失不当正常"一致）。
        """
        temp_rows = self._get(f"/api/v1/history?metric=ambient_temp_c&limit={limit}").get("data") or []
        humid_rows = self._get(f"/api/v1/history?metric=humidity_percent&limit={limit}").get("data") or []
        merged: dict = {}
        for row in temp_rows:
            merged[round(float(row["ts"]), 1)] = [float(row["ts"]), row.get("value"), None]
        for row in humid_rows:
            key = round(float(row["ts"]), 1)
            if key in merged:
                merged[key][2] = row.get("value")
            else:
                merged[key] = [float(row["ts"]), None, row.get("value")]
        return [(float(v[0]), v[1], v[2]) for _, v in sorted(merged.items())]

    def close(self) -> None:
        return None


# --------------------------------------------------------------------------
# 绘图
# --------------------------------------------------------------------------


def run_plot(args: argparse.Namespace) -> int:
    """打开动态曲线窗口（阻塞直到关窗或 ``--duration`` 到期）。"""
    try:
        import matplotlib
    except ImportError:
        safe_print("❌ 未安装 matplotlib。安装：")
        print("   sudo apt install -y python3-matplotlib python3-tk")
        print("   （只想验证代码逻辑的话，跑单测即可：python3 -m pytest tests -q）")
        return 2

    if args.headless or not sys.stdout.isatty() and args.no_window:
        matplotlib.use("Agg")
    try:
        import matplotlib.pyplot as plt
        from matplotlib.animation import FuncAnimation
    except ImportError as exc:
        safe_print(f"❌ matplotlib 导入失败：{exc}")
        return 2

    # 中文显示：树莓派上装中文字体（否则中文会变方框）
    plt.rcParams["font.sans-serif"] = [
        "Noto Sans CJK SC", "WenQuanYi Zen Hei", "WenQuanYi Micro Hei",
        "Microsoft YaHei", "SimHei", "DejaVu Sans",
    ]
    plt.rcParams["axes.unicode_minus"] = False

    # 数据源
    if args.source == "api":
        source = ApiSource(args.base_url)
    else:
        source = DeviceSource(pin=args.pin, mock=args.mock)
    print(f"数据源：{getattr(source, 'describe', args.source)}")

    series = Series(window=args.window)
    if args.source == "api" and isinstance(source, ApiSource):
        try:
            history = source.history(limit=args.window)
            series.load_history(history)
            print(f"已从服务取回 {len(history)} 个历史点用于预填充")
        except Exception as exc:  # noqa: BLE001 - 服务没起也能继续（只是从空开始）
            safe_print(f"⚠️ 取历史失败（将从空曲线开始）：{type(exc).__name__}: {exc}")

    use_index = args.xaxis == "index"
    fig, ax_temp = plt.subplots(figsize=(9, 5))
    ax_humid = ax_temp.twinx()
    line_temp, = ax_temp.plot([], [], color="#d62728", marker="o", markersize=3, label="温度 (℃)")
    line_humid, = ax_humid.plot([], [], color="#1f77b4", marker="s", markersize=3, label="湿度 (%)")

    ax_temp.set_xlabel("采样序号" if use_index else "时间 (秒)")
    ax_temp.set_ylabel("温度 (℃)", color="#d62728")
    ax_humid.set_ylabel("湿度 (%)", color="#1f77b4")
    ax_temp.tick_params(axis="y", labelcolor="#d62728")
    ax_humid.tick_params(axis="y", labelcolor="#1f77b4")
    ax_temp.grid(True, linestyle=":", alpha=0.5)

    title = ax_temp.set_title("环境温湿度实时曲线")
    footer = fig.text(0.01, 0.01, "等待第一组数据…", fontsize=9, color="#555555")
    lines = [line_temp, line_humid]
    ax_temp.legend(lines, [l.get_label() for l in lines], loc="upper left")

    state = {"last_read": 0.0, "started": time.time(), "errors": 0, "last_error": ""}

    def pull() -> None:
        """按采样间隔读一次数据并写入缓冲（读到 None 也算一次采样，如实记录）。"""
        now = time.time()
        if now - state["last_read"] < args.interval:
            return
        state["last_read"] = now
        try:
            temp, humid = source.read()
            state["last_error"] = ""
        except Exception as exc:  # noqa: BLE001 - 单次读失败不该让窗口崩掉
            state["errors"] += 1
            state["last_error"] = f"{type(exc).__name__}: {exc}"
            temp, humid = None, None
        series.append(now, temp, humid)
        print(f"[{time.strftime('%H:%M:%S')}] 温度={temp if temp is not None else '未知'} 湿度={humid if humid is not None else '未知'}"
              + (f"   （读失败：{state['last_error']}）" if state["last_error"] else ""))

    def update(_frame: int):
        pull()
        xs = series.x_axis(use_index)
        line_temp.set_data(xs, list(series.temps))
        line_humid.set_data(xs, list(series.humids))

        latest = series.latest
        if latest is not None:
            temp, humid = latest
            text = (f"最新一次读数　　温度: {temp:.1f} ℃　　湿度: {humid:.0f} %"
                    if temp is not None and humid is not None
                    else "最新一次读数　　温度: 未知　　湿度: 未知（传感器本次没读到）")
            title.set_text(text)
        if xs:
            ax_temp.set_xlim(min(xs), max(xs) if max(xs) > min(xs) else min(xs) + 1)
        else:
            ax_temp.set_xlim(0, 1)
        for ax, values in ((ax_temp, series.temps), (ax_humid, series.humids)):
            vals = [v for v in values if v is not None]
            if vals:
                low, high = min(vals), max(vals)
                pad = max(1.0, (high - low) * 0.2)
                ax.set_ylim(low - pad, high + pad)

        stats = series.stats()
        footer.set_text(
            f"数据源：{getattr(source, 'describe', args.source)}　"
            f"采样间隔：{args.interval:g}s　累计样本：{stats['samples']}　窗口内：{stats['window']}　"
            f"温度范围：{fmt(stats['temp_min'])}~{fmt(stats['temp_max'])} ℃　"
            f"湿度范围：{fmt(stats['humidity_min'])}~{fmt(stats['humidity_max'])} %"
            + (f"　读取失败：{state['errors']} 次" if state["errors"] else "")
        )
        if args.save:
            fig.savefig(args.save, dpi=110, bbox_inches="tight")
        return lines

    interval_ms = max(200, int(args.interval * 1000))

    if args.duration:
        # 无显示器/自动导出模式：**不创建动画对象**（否则 matplotlib 会警告"没渲染就被删除"），
        # 直接按采样间隔手动跑 update()，到点导出并退出。
        deadline = time.time() + args.duration
        while time.time() < deadline:
            update(0)
            time.sleep(interval_ms / 1000.0)
        if args.save:
            fig.savefig(args.save, dpi=110, bbox_inches="tight")
            print(f"曲线已导出：{args.save}")
        print(f"共采集 {series.total} 个样本：{json.dumps(series.stats(), ensure_ascii=False)}")
        source.close()
        return 0

    animation = FuncAnimation(fig, update, interval=interval_ms, cache_frame_data=False)
    print("窗口已打开：关闭窗口即退出（数据同时打印在本终端）")
    try:
        plt.tight_layout()
        plt.show()
    except KeyboardInterrupt:
        print("\n已中断")
    finally:
        source.close()
    return 0


def fmt(value: Optional[float]) -> str:
    return "未知" if value is None else f"{value:.1f}"


def main() -> int:
    parser = argparse.ArgumentParser(description="温湿度动态曲线窗口（课程任务 H）")
    parser.add_argument("--source", choices=["device", "api"], default="device",
                        help="device=直接读 DHT11；api=从本项目 HTTP 接口取（默认 device）")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080", help="--source api 时用")
    parser.add_argument("--pin", type=int, default=4, help="DHT11 数据脚 BCM（默认 4 = 物理脚 7）")
    parser.add_argument("--mock", action="store_true", help="用模拟数据（没有硬件也能看曲线）")
    parser.add_argument("--interval", type=float, default=2.5,
                        help="采样间隔秒（DHT11 硬件限制：必须 ≥2 秒，默认 2.5）")
    parser.add_argument("--window", type=int, default=120, help="窗口内保留的样本数（默认 120）")
    parser.add_argument("--xaxis", choices=["time", "index"], default="time",
                        help="横轴用相对时间还是采样序号（任务书要求标出其一，默认时间）")
    parser.add_argument("--save", default="", help="每帧同时导出 PNG（给报告用）")
    parser.add_argument("--duration", type=float, default=0.0,
                        help="跑到指定秒数后导出并退出（无显示器时用）")
    parser.add_argument("--headless", action="store_true", help="不打开窗口（配合 --duration/--save）")
    parser.add_argument("--no-window", action="store_true", help="同 --headless（二者任一即可）")
    args = parser.parse_args()

    if args.interval < 2.0 and args.source == "device" and not args.mock:
        safe_print("⚠️ DHT11 两次读取必须间隔 ≥2 秒（硬件限制）；已自动提到 2.0 秒")
        args.interval = 2.0
    if args.headless or args.no_window:
        args.duration = args.duration or 10.0
    return run_plot(args)


if __name__ == "__main__":
    raise SystemExit(main())
