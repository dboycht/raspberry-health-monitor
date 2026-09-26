#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""课程作业 H：温湿度测量（单个文件，直接运行）

题设要求（逐条对应到本文件的实现）
----------------------------------
1. **使用树莓派 5 和课程学过的温湿度传感器（DHT11）周期性读取温度和湿度**
   → 见 `Dht11` 类：自己按单总线协议读（lgpio 边沿时间戳），默认 3 秒一次（DHT11 硬件要求 ≥2 秒）
2. **程序保存连续采集的数据**
   → 见 `CsvStore` 类：每采到一个点就**立刻写进 CSV**（每行 fsync，断电也不丢）
3. **用 Matplotlib 绘制动态曲线；每得到一组新数据，图中的曲线随之刷新**
   → 见 `CurveWindow` 类 + `main()` 主循环：`FuncAnimation`/逐点重绘，无需重开窗口
4. **图中明确标出温度、湿度以及采样顺序或时间**
   → 左轴「温度 (℃)」红、右轴「湿度 (%)」蓝、横轴「时间 (秒)」或「采样序号」、图例齐全
5. **示例输出：最新一次数据为「温度: 25.0 ℃，湿度: 58%」**
   → 窗口标题实时显示「最新一次读数　温度: xx.x ℃　湿度: xx %」，终端同步打印每一组

用法（在树莓派上）
------------------
    python3 run.py                      # 真机读取 + 弹出动态曲线窗口（需要桌面）
    python3 run.py --no-plot            # 没有显示器：只采集 + 存 CSV
    python3 run.py --mock               # 没接传感器也能看效果（合成数据）
    python3 run.py --interval 3 --duration 60 --save curve.png
    python3 run.py --help               # 全部参数

依赖：Python 3.9+；真机读数需要 `python3-lgpio`；画图需要 `python3-matplotlib` + `python3-tk`。
        （没装 matplotlib 也能 `--no-plot` 正常采集存档。）
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

# ==========================================================================
# 一、常量（改这里就能改行为）
# ==========================================================================

#: DHT11 数据脚（BCM 编号）。BCM 4 = 物理脚 7（本作业按接线表接的就是这个）
DEFAULT_PIN = 4

#: BCM → 40-pin 物理脚号（只列常用脚；程序只用来在提示里显示"物理脚几"）。
#: ⚠️ 两者**不是偏移关系**（GPIO4→7，但 GPIO27→13），所以必须查表，不能 `pin + 1`。
BCM_TO_PHYSICAL = {
    2: 3, 3: 5, 4: 7, 17: 11, 27: 13, 22: 15, 10: 19, 9: 21, 11: 23, 8: 24,
    7: 26, 5: 29, 6: 31, 12: 32, 13: 33, 19: 35, 26: 37, 16: 36, 20: 38, 21: 40,
    14: 8, 15: 10, 18: 12, 23: 16, 24: 18, 25: 22,
}


def describe_pin(bcm: int) -> str:
    """把 BCM 编号说成人话：`GPIO4（物理脚 7）`（查表，不许写偏移公式）。"""
    physical = BCM_TO_PHYSICAL.get(int(bcm))
    return f"GPIO{bcm}（物理脚 {physical}）" if physical else f"GPIO{bcm}（非标准 40-pin 脚）"


#: 打印用的 ASCII 替身（只作用于会崩控制台的符号）。
#: 为什么单文件版也要有它（2026-09-26 实测，见仓库 ERROR.md E41）：中文 Windows
#: （GBK 控制台）上 `print("⚠️ …")` 会抛 UnicodeEncodeError 把程序**当场崩掉** ——
#: 不是在树莓派上，而是在同学自己的笔记本上。本文件是"一个文件交作业"，不能 import
#: 仓库里的 `basic/console.py`，所以这里内嵌一份等价的最小实现（判据同源）。
_ASCII_FALLBACK = {
    "\u2705": "[OK]", "\u274c": "[X]", "\u26a0": "[!]", "\ufe0f": "",
    "\u2192": "->", "\u2190": "<-", "\u2194": "<->", "\u21d2": "=>",
    "\u25b6": ">", "\u23ed": ">>", "\u26d4": "[!]", "\u00b5": "u",
    "\u2103": "degC", "\u00d7": "x",
}


def _encodable(text: str) -> bool:
    """这段文字能不能被当前控制台编码打出来（取不到编码就当 UTF-8）。"""
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        text.encode(enc)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def safe_print(*args, **kwargs) -> None:
    """`print` 的安全版：窄编码控制台上把打不出的字符降级成 ASCII 替身。

    * UTF-8 环境（树莓派 / CI / 管道）：**原样输出**，一个字符都不改；
    * GBK 之类的窄编码：`⚠️` → `[!]`、`❌` → `[X]`，其余换成 `?`。
    """
    rendered = []
    for arg in args:
        text = str(arg)
        if _encodable(text):
            rendered.append(text)
            continue
        rendered.append("".join(
            ch if _encodable(ch) else _ASCII_FALLBACK.get(ch, "?") for ch in text
        ))
    print(*rendered, **kwargs)


#: 两次读取的最小间隔（秒）。DHT11 数据手册要求 ≥1 秒，实测 ≥2 秒才稳。
MIN_INTERVAL_S = 2.0

#: 默认采样周期（秒）。题设说"周期性读取"，这里取 3 秒。
DEFAULT_INTERVAL_S = 3.0

#: 曲线默认保留多少个点（滚动窗口）
DEFAULT_WINDOW = 120

#: 数据保存目录（相对本文件所在目录，保证"拷走整个文件夹也能找到数据"）
HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"

#: 中文显示优先使用的字体（按优先级）。树莓派上装 `fonts-noto-cjk` 最好；
#: 没有中文字体时图里的中文会变方框，程序会**提醒**而不是默默出方框图。
CJK_FONTS = [
    "Noto Sans CJK SC", "Noto Sans CJK JP", "Noto Sans SC", "Source Han Sans SC",
    "WenQuanYi Zen Hei", "WenQuanYi Micro Hei", "Microsoft YaHei", "SimHei",
    "Droid Sans Fallback",
]


# ==========================================================================
# 二、DHT11 读取（单总线协议，不依赖第三方 DHT 库）
# ==========================================================================


class Dht11Error(RuntimeError):
    """读取失败（无应答 / 时序异常 / 校验和不符 / 数值超量程）。"""


class Dht11:
    """DHT11 单总线读取（lgpio 边沿时间戳；mock 模式用合成数据）。

    为什么不用 `Adafruit_DHT` / `gpiozero.DHT11`：
      * 树莓派 5 上 `RPi.GPIO` 不可用，而 gpiozero 2.x **删掉了 DHT11 类**；
      * 自己实现只需要"拉低 18ms → 收 83 个边沿 → 按高电平宽度判 0/1"，
        而且**边沿时间戳**比 Python 轮询微秒级电平可靠得多。
    """

    def __init__(self, pin: int = DEFAULT_PIN, mock: bool = False, retries: int = 3) -> None:
        if not 0 <= int(pin) <= 27:
            raise ValueError(f"DHT11 数据脚 GPIO{pin} 非法：BCM 编号应在 0~27")
        self.pin = int(pin)
        self.mock = bool(mock)
        self.retries = max(1, int(retries))
        self.backend = "mock" if mock else "none"
        self._lgpio = None
        self._handle = None
        self._last_ok_at: Optional[float] = None
        self._mock_value = (25.0, 58.0)          # mock 起始值（题设示例就是 25.0℃/58%）

    # -- 生命周期 ------------------------------------------------------

    def open(self) -> str:
        """打开传感器，返回后端名（`lgpio` 或 `mock`）。"""
        if self.mock:
            self.backend = "mock"
            return self.backend
        try:
            import lgpio  # type: ignore import-not-found
        except ImportError as exc:  # pragma: no cover - 只在没装 lgpio 的机器上
            raise Dht11Error(
                "没有 lgpio：请在树莓派上执行 `sudo apt install -y python3-lgpio`；"
                "在电脑上跑请加 `--mock`"
            ) from exc
        self._lgpio = lgpio
        self._handle = lgpio.gpiochip_open(0)
        self.backend = "lgpio"
        return self.backend

    def close(self) -> None:
        if self._lgpio is not None and self._handle is not None:
            try:
                self._lgpio.gpio_free(self._handle, self.pin)
            except Exception:  # noqa: BLE001 - 收尾失败不该影响退出
                pass
            try:
                self._lgpio.gpiochip_close(self._handle)
            except Exception:  # noqa: BLE001
                pass
        self._handle = None
        self._lgpio = None

    # -- 读取 ----------------------------------------------------------

    def wait_remaining(self, now: Optional[float] = None) -> float:
        """距"可以再次读取"还要等几秒（0 = 现在就能读）。"""
        if self._last_ok_at is None:
            return 0.0
        now = time.time() if now is None else float(now)
        return max(0.0, MIN_INTERVAL_S - (now - self._last_ok_at))

    def read(self) -> Tuple[Optional[float], Optional[float], str]:
        """读一次，返回 ``(温度℃, 湿度%, 说明)``。

        **读失败时温度/湿度返回 ``None``（绝不补 0）** —— 0℃ 会被当成"正常读数"，
        而 DHT11 是拿不到 -0 的：这是本项目的一条铁律（缺失就是缺失）。
        """
        if self.mock:
            # 合成数据：围绕基准值缓慢波动，看起来像真实环境
            temp, humid = self._mock_value
            drift = ((time.time() % 60) - 30) / 300.0
            return round(temp + drift, 1), round(humid + drift * 2, 1), "mock 合成数据"

        remaining = self.wait_remaining()
        if remaining > 0:
            return None, None, f"距上次成功读取不足 {MIN_INTERVAL_S:g} 秒（还差 {remaining:.1f}s）"

        last_error = ""
        for attempt in range(1, self.retries + 1):
            try:
                temperature, humidity = self._read_once_lgpio()
            except Exception as exc:  # noqa: BLE001 - 硬件层异常很杂，一律转成失败
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self.retries:
                    time.sleep(0.2)
                continue
            if not (0.0 <= temperature <= 50.0 and 20.0 <= humidity <= 90.0):
                last_error = f"读数超出量程：{temperature}℃ / {humidity}%（DHT11 是 0~50℃、20~90%）"
                continue
            self._last_ok_at = time.time()
            return temperature, humidity, ""
        return None, None, last_error or "读取失败"

    def _read_once_lgpio(self) -> Tuple[float, float]:
        """一次完整的单总线读取（拉低 18ms → 收边沿 → 解码）。"""
        lg, handle, pin = self._lgpio, self._handle, self.pin
        edges: List[Tuple[int, int]] = []
        callback = None
        try:
            try:
                lg.gpio_free(handle, pin)
            except Exception:  # noqa: BLE001
                pass
            # ① 主机把总线拉低 ≥18ms 作为起始信号
            lg.gpio_claim_output(handle, pin, 0)
            time.sleep(0.020)
            # ② 释放总线，收**双边沿**（回调自带纳秒时间戳）
            lg.gpio_free(handle, pin)
            lg.gpio_claim_alert(handle, pin, lg.BOTH_EDGES, lg.SET_PULL_UP)
            callback = lg.callback(handle, pin, lg.BOTH_EDGES,
                                   lambda _chip, _gpio, level, ts: edges.append((level, ts)))
            # ③ 一帧约 4ms，给 40ms 余量
            time.sleep(0.040)
        finally:
            if callback is not None:
                try:
                    callback.cancel()
                except Exception:  # noqa: BLE001
                    pass
            try:
                lg.gpio_free(handle, pin)
            except Exception:  # noqa: BLE001
                pass

        if not edges:
            raise Dht11Error("传感器没有应答（0 个边沿）：检查 VCC=3.3V、DATA=物理脚 7、GND 共地")

        # ④ 解码：先配出所有 "高电平段"，再取**最后 41 对**（应答 1 + 数据 40）
        widths_us: List[float] = []
        index = 0
        while index + 1 < len(edges):
            level, ts_high = edges[index]
            next_level, ts_low = edges[index + 1]
            if level == 1 and next_level == 0:
                widths_us.append((ts_low - ts_high) / 1000.0)
                index += 2
            else:
                index += 1

        if len(widths_us) < 41:
            raise Dht11Error(
                f"帧不完整：只配出 {len(widths_us)} 个高电平段（一帧应为 41 = 应答 1 + 数据 40）"
            )
        data_widths = widths_us[-41:][1:]          # 丢掉应答，剩 40 位
        data_widths = [w for w in data_widths if 0.0 < w < 200.0]
        if len(data_widths) < 40:
            raise Dht11Error(f"数据位不足：只解出 {len(data_widths)}/40 位")

        # ⑤ 宽度 >50µs 记为 1，否则记 0；拼成 5 个字节并校验
        bits = [1 if w > 50.0 else 0 for w in data_widths[:40]]
        data = bytearray()
        for byte_index in range(5):
            value = 0
            for bit in bits[byte_index * 8:(byte_index + 1) * 8]:
                value = (value << 1) | bit
            data.append(value)
        if (sum(data[:4]) & 0xFF) != data[4]:
            raise Dht11Error(f"校验和不符：算出 0x{sum(data[:4]) & 0xFF:02X}，收到 0x{data[4]:02X}")

        return float(data[2]) + float(data[3]) / 10.0, float(data[0]) + float(data[1]) / 10.0


# ==========================================================================
# 三、数据保存（CSV，每行即时落盘）
# ==========================================================================


class CsvStore:
    """把每次采样写进 CSV：**每写一行就 flush + fsync**（断电/拔电源也不丢已采数据）。"""

    HEADER = ["index", "timestamp", "temperature_c", "humidity_percent", "status", "note"]

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not self.path.exists() or self.path.stat().st_size == 0
        self._handle = open(self.path, "a", encoding="utf-8", newline="")
        self._writer = csv.writer(self._handle)
        if new_file:
            self._writer.writerow(self.HEADER)
            self._flush()

    def _flush(self) -> None:
        self._handle.flush()
        try:
            os.fsync(self._handle.fileno())
        except OSError:  # pragma: no cover - 某些文件系统不支持 fsync
            pass

    def save(self, index: int, timestamp: float, temperature: Optional[float],
             humidity: Optional[float], note: str = "") -> None:
        """写一行。**读失败时温度/湿度留空、status=fail**（绝不写 0）。"""
        stamp = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        ok = temperature is not None and humidity is not None
        self._writer.writerow([
            index, stamp,
            "" if temperature is None else f"{temperature:.1f}",
            "" if humidity is None else f"{humidity:.1f}",
            "ok" if ok else "fail",
            note if not ok else "",
        ])
        self._flush()

    def close(self) -> None:
        try:
            self._handle.close()
        except Exception:  # noqa: BLE001
            pass


# ==========================================================================
# 四、动态曲线（Matplotlib）
# ==========================================================================


def configure_cjk_font() -> Optional[str]:
    """让图里能显示中文；返回选中的字体名（挑不到返回 None，由调用方提醒）。

    ⚠️ 两个坑（2026-09-25 在树莓派上实测踩过，别再犯）：
    1. **字体链必须"拉丁在前、中文在后"**：树莓派自带的中文字体
       `DroidSansFallbackFull.ttf` **只含 CJK 字形、连数字 0 都没有** ——
       把它放第一位会让图里所有数字变方框；
    2. **只写 `rcParams` 不够**：matplotlib 在"每次重新 `set_text`"与
       "换 DPI 导出 PNG"时会重新解析字体、丢掉回退链（实测导出时中文全方框）。
       所以下面另配一个 `font_prop()`，把字体属性**直接挂到文本对象**上。
    """
    try:
        import matplotlib.font_manager as fm
        import matplotlib.pyplot as plt
    except ImportError:  # pragma: no cover
        return None

    # 把本目录 fonts/ 里的字体也登记进来（作业自带中文字体时靠这一步）
    local = HERE / "fonts"
    if local.is_dir():
        for pattern in ("*.ttc", "*.otf", "*.ttf"):
            for font_file in local.glob(pattern):
                try:
                    fm.fontManager.addfont(str(font_file))
                except Exception:  # noqa: BLE001
                    continue
    available = {n.lower() for n in fm.get_font_names()}
    chosen = next((name for name in CJK_FONTS if name.lower() in available), None)
    #: 拉丁字体在前（数字/单位靠它）、中文字体在后（汉字靠它）
    plt.rcParams["font.sans-serif"] = ["DejaVu Sans"] + ([chosen] if chosen else [])
    plt.rcParams["font.family"] = "sans-serif"
    plt.rcParams["axes.unicode_minus"] = False
    return chosen


def cjk_font_prop():
    """返回中文字体的 `FontProperties`（挑不到返回 None）。用于**直接挂到文本对象**。"""
    try:
        import matplotlib.font_manager as fm
        import matplotlib.pyplot as plt  # noqa: F401 - 确保 rcParams 已初始化
    except ImportError:  # pragma: no cover
        return None
    chosen = configure_cjk_font()
    if not chosen:
        return None
    try:
        path = fm.findfont(fm.FontProperties(family=chosen), fallback_to_default=False)
        return fm.FontProperties(fname=path)
    except Exception:  # noqa: BLE001
        return fm.FontProperties(family=chosen)


class CurveWindow:
    """一条温度 + 一条湿度的滚动曲线窗口（新数据来了就刷新）。

    横轴可切「时间(秒)」或「采样序号」（题设要求"标出采样顺序或时间"，两种都支持）。
    """

    def __init__(self, use_index: bool, interval_s: float, window: int, source_text: str,
                 show_window: bool = True) -> None:
        import matplotlib.pyplot as plt

        font_name = configure_cjk_font()
        self._font_prop = cjk_font_prop() if font_name else None
        self.show_window = bool(show_window)
        self.plt = plt
        self.use_index = use_index
        self.interval_s = interval_s
        self.window = window
        self.source_text = source_text
        self.times: List[float] = []
        self.indexes: List[int] = []
        self.temps: List[float] = []
        self.humids: List[float] = []
        self.started_at = time.time()
        self.ok_count = 0
        self.fail_count = 0

        self.fig, self.ax_temp = plt.subplots(figsize=(9, 5))
        self.fig.subplots_adjust(bottom=0.20)
        self.ax_humid = self.ax_temp.twinx()
        (self.line_temp,) = self.ax_temp.plot([], [], color="#d62728", marker="o",
                                              markersize=3, label="温度 (℃)")
        (self.line_humid,) = self.ax_humid.plot([], [], color="#1f77b4", marker="s",
                                                markersize=3, label="湿度 (%)")
        self.ax_temp.set_xlabel("采样序号" if use_index else "时间 (秒)")
        self.ax_temp.set_ylabel("温度 (℃)", color="#d62728")
        self.ax_humid.set_ylabel("湿度 (%)", color="#1f77b4")
        self.ax_temp.tick_params(axis="y", labelcolor="#d62728")
        self.ax_humid.tick_params(axis="y", labelcolor="#1f77b4")
        self.ax_temp.grid(True, linestyle=":", alpha=0.5)
        self.title = self.ax_temp.set_title("温湿度动态曲线（等待第一组数据…）")
        self.footer = self.fig.text(0.01, 0.01, "正在启动…", fontsize=9, color="#555555")
        self.ax_temp.legend([self.line_temp, self.line_humid], ["温度 (℃)", "湿度 (%)"], loc="upper left")
        if self.show_window:
            plt.ion()                  # 交互模式：不需要重开窗口就能刷新
        self._apply_font()
        if self.show_window:
            self.fig.show()

    def _apply_font(self) -> None:
        """把中文字体属性直接挂到**每个文本对象**上（不依赖 rcParams 的回退链）。

        为什么必须（真机实测）：只设 `rcParams` 时，`set_text` 之后与换 DPI 导出时
        都会退回默认字体 ⇒ 图里中文/数字变方框。挂在文本对象上就稳（导出实测 0 缺字形）。
        """
        if self._font_prop is None:
            return
        try:
            from matplotlib.text import Text

            for text in self.fig.findobj(Text):
                try:
                    text.set_fontproperties(self._font_prop)
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001
            pass

    # -- 数据 ----------------------------------------------------------

    def append(self, index: int, timestamp: float,
               temperature: Optional[float], humidity: Optional[float]) -> None:
        """加一个点（**失败的点不入曲线**，但仍计入统计 —— 曲线不能画假点）。"""
        self.indexes.append(index)
        self.times.append(timestamp - self.started_at)
        if temperature is None or humidity is None:
            self.fail_count += 1
        else:
            self.temps.append(temperature)
            self.humids.append(humidity)
            self.ok_count += 1
        if len(self.temps) > self.window:            # 滚动窗口：丢最旧的
            self.temps.pop(0)
            self.humids.pop(0)

    # -- 刷新 ----------------------------------------------------------

    def refresh(self) -> None:
        """按最新数据重画（每采到一个点调一次）。"""
        if self.use_index:
            xs: List[float] = list(range(len(self.temps)))
        else:
            # 用"温度序列对应的采样时刻"做横轴（失败点没进曲线，所以按最近 N 个 ok 点取时间）
            xs = self.times[-len(self.temps):] if self.temps else []
        self.line_temp.set_data(xs, self.temps)
        self.line_humid.set_data(xs, self.humids)
        if xs:
            left, right = min(xs), max(xs)
            self.ax_temp.set_xlim(left, right if right > left else left + 1)
        if self.temps:
            self.ax_temp.set_ylim(min(self.temps) - 0.5, max(self.temps) + 0.5)
            self.ax_humid.set_ylim(min(self.humids) - 1.0, max(self.humids) + 1.0)
            latest_temp, latest_humid = self.temps[-1], self.humids[-1]
            self.title.set_text(
                f"最新一次读数　　温度: {latest_temp:.1f} ℃　　湿度: {latest_humid:.0f} %"
            )
        else:
            self.title.set_text("最新一次读数　　温度: 未知　　湿度: 未知")
        span = ""
        if self.temps:
            span = (f"　窗口内温度 {min(self.temps):.1f}~{max(self.temps):.1f} ℃"
                    f"　湿度 {min(self.humids):.0f}~{max(self.humids):.0f} %")
        self.footer.set_text(
            f"数据源：{self.source_text}　采样周期：{self.interval_s:g}s\n"
            f"累计样本：{self.ok_count + self.fail_count}（成功 {self.ok_count} / 失败 {self.fail_count}）{span}"
        )
        self.fig.canvas.draw_idle()
        self._apply_font()             # 每次刷新都重挂一次（set_text 会丢掉回退链）
        if self.show_window:           # headless 时没有事件循环，别去 flush
            self.fig.canvas.flush_events()

    def wait_next(self, started_at: float) -> float:
        """睡到下一个采样时刻（把读取耗时扣掉，采样周期才稳）。"""
        remaining = max(0.001, self.interval_s - (time.time() - started_at))
        time.sleep(remaining)
        return remaining

    def save(self, path: str) -> None:
        """导出 PNG（报告插图用）。导出前再挂一次字体 —— 换 DPI 会重解析字体。"""
        self._apply_font()
        self.fig.savefig(path, dpi=110, bbox_inches="tight")

    def close(self) -> None:
        try:
            self.plt.ioff()
        except Exception:  # noqa: BLE001
            pass


# ==========================================================================
# 五、主流程（单个入口）
# ==========================================================================


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="课程作业 H：DHT11 温湿度周期性采集 + 保存 CSV + Matplotlib 动态曲线",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--pin", type=int, default=DEFAULT_PIN,
                        help="DHT11 数据脚 BCM 编号（BCM 4 = 物理脚 7）")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_S,
                        help="采样周期（秒）；DHT11 硬件要求 ≥2 秒")
    parser.add_argument("--window", type=int, default=DEFAULT_WINDOW,
                        help="曲线保留的采样点数（滚动窗口）")
    parser.add_argument("--xaxis", choices=["time", "index"], default="time",
                        help="横轴：时间(秒) 或 采样序号")
    parser.add_argument("--duration", type=float, default=0.0,
                        help="采集多少秒后自动退出（0 = 一直跑到关闭窗口/Ctrl+C）")
    parser.add_argument("--save", default="", help="把曲线导出成 PNG（报告插图）")
    parser.add_argument("--csv", default="", help="CSV 路径（默认 data/dht11_日期_时刻.csv）")
    parser.add_argument("--headless", action="store_true", help="不弹窗口，只采集（配合 --save）")
    parser.add_argument("--no-plot", action="store_true", help="完全不画图（只采集 + 存档）")
    parser.add_argument("--mock", action="store_true", help="用合成数据（没接传感器时）")
    parser.add_argument("--minutes", type=float, default=0.0,
                        help="采集多少分钟（与 --duration 二选一，方便写报告）")
    return parser.parse_args(argv)


def _explain_no_window(exc: Optional[BaseException] = None, backend: str = "") -> None:
    """窗口显示不出来时**说清为什么**、以及怎么才能看到窗口。

    为什么专门写这段（2026-09-26 真机实测）：原来"窗口出不来"是**静默**的 ——
    matplotlib 在没有 `DISPLAY` 时会悄悄退到 `agg` 后端（只出图片、不弹窗），
    **连异常都不抛**，用户看到的现象就是"为什么没有窗口弹出"而毫无线索。

    实测这台树莓派本身有桌面（labwc/Wayland + Xwayland 在 `:0`），但有**两个**拦路点：
    1. **通过 SSH 连进来时没有 `DISPLAY`** ⇒ 后端只能是 `agg`；
    2. **没装 `python3-pil.imagetk`** ⇒ 即使给了 DISPLAY，Tk 后端也会
       `ImportError: cannot import name 'ImageTk' from 'PIL'`（装了 python3-tk 与
       python3-pil，但缺 Tk 那半）。
    """
    import os

    safe_print("⚠️ 现在**不会弹出窗口**，本次改为『只采集 + 存档』。")
    if backend:
        print(f"   matplotlib 当前后端 = {backend}（`agg` = 只能出图片文件，不能开窗）")
    if exc is not None:
        print(f"   直接原因：{type(exc).__name__}: {str(exc)[:110]}")
    print("   要看窗口，按下面挑一个：")
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        print("   ① SSH 会话里没有 DISPLAY（最常见）——三选一：")
        print("      · **推荐**：在树莓派自己的桌面里开终端再跑：")
        print("        cd ~/raspberry-health-monitor/basic/hw && python3 run.py")
        print("      · 或者：DISPLAY=:0 python3 run.py   # 窗口出现在树莓派的屏幕上（需已登录桌面）")
        print("      · 或者：ssh -Y pi@<树莓派IP> 连（你的电脑要装 X server，如 VcXsrv/Xming）")
    print("   ② 缺 PIL 的 Tk 组件（本机实测就卡在这，按 ① 做完还要装它）：")
    print("      sudo apt install -y python3-pil.imagetk")
    print("   ③ 没装 matplotlib：sudo apt install -y python3-matplotlib python3-tk")
    print("   不想开窗也能交作业（报告插图够用）：")
    print("      python3 run.py --no-plot --duration 300                 # 只采集，存 CSV")
    print("      python3 run.py --headless --duration 60 --save c.png    # 采集并导出曲线图")


def _try_open_window(args, interval: float) -> Optional["CurveWindow"]:
    """建曲线对象：**弹窗**能不能成看后端，但"出图"不受影响（`--headless --save` 靠这条）。

    返回 `CurveWindow`（可刷新/可导出）或 `None`（连带弹窗都做不了，且已说明原因）。
    用 `window.show_window` 区分"是否真的弹了窗口"。
    """
    try:
        import matplotlib

        # ⚠️ 关键：没有 DISPLAY 时 matplotlib 会**静默**选 agg（不抛异常），
        #    所以这里主动读后端名来判断"到底能不能弹窗"。
        backend = matplotlib.get_backend().lower()
    except ImportError:
        _explain_no_window(None, "（没装 matplotlib）")
        return None

    gui_backends = ("tkagg", "qtagg", "qt5agg", "gtk3agg", "gtk4agg", "macosx", "wxagg")
    want_window = not args.headless
    can_window = backend in gui_backends
    if want_window and not can_window:
        # 用户想要窗口但环境给不了 —— 说清原因；**但图还是要能出**：
        # 如果同时给了 --save，就继续用 agg 出图（下面建对象时不 show）。
        _explain_no_window(None, backend)
        if not args.save:
            return None

    font_name = configure_cjk_font()
    if font_name is None:
        safe_print("⚠️ 没找到中文字体：图里的中文会变成方框（sudo apt install -y fonts-noto-cjk）")
    else:
        print(f"中文字体：{font_name}")
    try:
        window = CurveWindow(args.xaxis == "index", interval, args.window,
                             f"DHT11 ({describe_pin(args.pin)})",
                             show_window=want_window and can_window)
    except Exception as exc:  # noqa: BLE001 - 后端能选但建窗仍失败（缺 Tk/ImageTk 等）
        _explain_no_window(exc, backend)
        return None
    if want_window and can_window:
        print("已打开曲线窗口（窗口标题里就是「最新一次读数」）")
    return window


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    interval = max(args.interval, MIN_INTERVAL_S)
    if interval != args.interval:
        safe_print(f"⚠️ 采样周期已从 {args.interval:g}s 抬到硬件下限 {MIN_INTERVAL_S:g}s（DHT11 要求）")

    # ① 传感器
    sensor = Dht11(pin=args.pin, mock=args.mock)
    try:
        backend = sensor.open()
    except Dht11Error as exc:
        safe_print(f"❌ 打不开 DHT11：{exc}")
        return 2
    print(f"数据源：DHT11（{describe_pin(args.pin)}）　后端：{backend}")

    # ② 存档
    csv_path = Path(args.csv) if args.csv else DATA_DIR / f"dht11_{datetime.now():%Y%m%d_%H%M%S}.csv"
    store = CsvStore(csv_path)
    print(f"数据存档：{csv_path}")

    # ③ 曲线（可选）
    #    ⚠️ `--headless` 是"不弹窗、直接画到文件"，所以**仍然要建窗口对象**
    #    （只是后端是 agg）；否则 `--headless --save x.png` 会一张图都不出
    #    （2026-09-26 在解压包上实测踩到：`--save` 被跳过了）。
    window: Optional[CurveWindow] = None
    want_plot = (not args.no_plot) and (not args.headless or bool(args.save))
    if want_plot:
        window = _try_open_window(args, interval)

    duration = args.duration or args.minutes * 60.0
    started_at = time.time()
    print("=" * 72)
    print("开始采集（Ctrl+C 停止）")
    print("=" * 72)

    index = 0
    try:
        while True:
            index += 1
            loop_started = time.time()
            temperature, humidity, note = sensor.read()
            store.save(index, loop_started, temperature, humidity, note)
            if temperature is None:
                print(f"[{datetime.now():%H:%M:%S}] #{index:>4}　本次没读到数据：{note}")
            else:
                print(f"[{datetime.now():%H:%M:%S}] #{index:>4}　温度: {temperature:.1f} ℃，湿度: {humidity:.0f} %")
            if window is not None:
                window.append(index, loop_started, temperature, humidity)
                window.refresh()
                if args.save:
                    window.save(args.save)
            if duration and (time.time() - started_at) >= duration:
                break
            if window is not None:
                window.wait_next(loop_started)
            else:
                remaining = interval - (time.time() - loop_started)
                if remaining > 0:
                    time.sleep(remaining)
    except KeyboardInterrupt:
        print("\n已手动停止（已采集的数据都已写进 CSV）")

    # ④ 收尾
    if window is not None:
        window.refresh()
        if args.save:
            window.save(args.save)
            print(f"曲线已导出：{args.save}")
        window.close()
    store.close()
    elapsed = time.time() - started_at
    ok = index
    print("-" * 72)
    print(f"采集结束：{index} 个样本，用时 {elapsed:.0f} 秒，数据在 {csv_path}")
    if window is None or not getattr(window, "show_window", False):
        print("（本次没弹窗；要弹窗请按上面的提示做，或直接用 `--save 图片.png` 导出曲线图）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
