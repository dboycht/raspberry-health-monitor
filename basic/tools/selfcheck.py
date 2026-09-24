#!/usr/bin/env python3
"""基础版自检：把"这份代码到底能不能跑"用一个命令说清楚。

它检查的都是**不依赖硬件**的事情（所以电脑上、树莓派上、CI 里都能跑）：

1. 包的导入与版本；
2. **单总线解码**：合成一帧 → 解码 → 必须一模一样（协议类代码最快的查错方式）；
3. 引脚映射：GPIO4 = 物理脚 7，且"BCM 与物理脚号不是偏移关系"；
4. CSV 存档：写一行 → 读回来，**缺失值必须是空/None 而不是 0**；
5. 曲线数据窗口：滚动窗口、横轴、统计量；
6. 演示数据（`basic/data/sample_demo.csv`）：格式正确、数值在量程内；
7. DHT11 后端探测：本机到底能用 lgpio / gpiozero / 只能用 mock；
8. matplotlib 是否可用（没有也能只采集不画图）。

用法::

    python3 basic/tools/selfcheck.py              # 全部检查
    python3 basic/tools/selfcheck.py --make-demo  # 顺便重新生成演示数据

退出码：0 = 全部通过；1 = 有失败项（逐条打印）。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

ROOT = Path(__file__).resolve().parents[2]          # 仓库根
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from basic import __version__                        # noqa: E402
from basic.dht11read import (                        # noqa: E402
    Dht11Error,
    Dht11Reader,
    MockBackend,
    check_range,
    decode_frame,
    synth_frame,
)
from basic.model import Reading                      # noqa: E402
from basic.pins import bcm_to_physical, describe_pin, find_conflicts  # noqa: E402
from basic.series import Series                      # noqa: E402
from basic.store import CsvStore, DEMO_FILE_NAME, default_data_dir  # noqa: E402

OK = "PASS"
BAD = "FAIL"

#: 生成的演示数据用"当前时间往前推"的时间戳（看起来就像刚采完的一段数据）
DEMO_SAMPLES = 120
DEMO_INTERVAL_S = 2.5


# --------------------------------------------------------------------------
# 逐项检查
# --------------------------------------------------------------------------


def check_version() -> Tuple[bool, str]:
    if not __version__:
        return False, "basic.__version__ 是空的"
    return True, f"基础版 {__version__}（Python {sys.version.split()[0]}）"


def check_decode() -> Tuple[bool, str]:
    """合成时序 → 解码：这是 DHT11 那份代码最关键的验证。"""
    samples = [(25.0, 58.0), (0.0, 20.0), (50.0, 90.0), (23.5, 61.0)]
    for temperature, humidity in samples:
        got_t, got_h = decode_frame(synth_frame(temperature, humidity))
        if abs(got_t - temperature) > 0.05 or abs(got_h - humidity) > 0.05:
            return False, f"解码不对：期望 ({temperature}, {humidity})，得到 ({got_t}, {got_h})"
    edges = synth_frame(25.0, 58.0)
    if len(edges) != 82:
        return False, f"合成帧应为 82 个边沿（与真机 lgpio 回调形状一致），实际 {len(edges)}"
    return True, f"{len(samples)} 组合成时序全部解回原文（合成帧 {len(edges)} 边沿 / 41 个高电平段）"


def check_pins() -> Tuple[bool, str]:
    if bcm_to_physical(4) != 7:
        return False, f"GPIO4 应映射到物理脚 7，实际 {bcm_to_physical(4)}"
    if bcm_to_physical(27) == 28:
        return False, "GPIO27 被算成物理脚 28（这说明有人写了 pin+1 的偏移公式）"
    if not find_conflicts([("dht11", 4), ("other", 4)]):
        return False, "撞脚检查没发现两个器件抢 GPIO4"
    return True, f"{describe_pin(4)}；撞脚检查生效"


def check_store() -> Tuple[bool, str]:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "selfcheck.csv"
        store = CsvStore(path)
        store.save(Reading(1, time.time(), 25.0, 58.0))
        store.save(Reading(2, time.time() + 3, None, None, ok=False, note="自检：模拟读失败"))
        rows = store.load()
        if len(rows) != 2:
            return False, f"写入 2 行、读回 {len(rows)} 行"
        if rows[1][1] is not None or rows[1][2] is not None:
            return False, "读失败的行被写成了数值（**这违反『读不到就是 None』的红线**）"
        if store.rows_written != 2:
            return False, "rows_written 计数不对"
    return True, "写入/读回正确，失败行保持为空（不是 0）"


def check_series() -> Tuple[bool, str]:
    series = Series(window=3)
    for index in range(5):
        series.append(1000.0 + index, 20.0 + index, 50.0 + index)
    if len(series.ts) != 3 or series.total != 5:
        return False, f"滚动窗口不对：窗口内 {len(series.ts)}，累计 {series.total}"
    stats = series.stats()
    if (stats["temp_min"], stats["temp_max"]) != (22.0, 24.0):
        return False, f"统计量不对：{stats}"
    return True, "滚动窗口 / 统计量正确"


def check_demo_data(make_demo: bool = False) -> Tuple[bool, str]:
    path = default_data_dir() / DEMO_FILE_NAME
    if make_demo or not path.exists():
        try:
            make_demo_file(path)
        except Exception as exc:  # noqa: BLE001
            return False, f"生成演示数据失败：{type(exc).__name__}: {exc}"
    rows = CsvStore(path).load()
    if len(rows) < 20:
        return False, f"{path.name} 只有 {len(rows)} 行（至少 20 行，供没硬件的同学画曲线）"
    temperatures = [r[1] for r in rows if r[1] is not None]
    humidities = [r[2] for r in rows if r[2] is not None]
    if len(set(temperatures)) < 3:
        return False, "演示数据几乎是一条直线（不像真实环境）"
    for temperature, humidity in zip(temperatures, humidities):
        reason = check_range(temperature, humidity)
        if reason:
            return False, f"演示数据超量程：{reason}"
    return True, (
        f"{path.name}：{len(rows)} 行，温度 {min(temperatures)}~{max(temperatures)} ℃，"
        f"湿度 {min(humidities)}~{max(humidities)} %"
    )


def check_backends() -> Tuple[bool, str]:
    """如实报告本机能用哪个后端（**不假装能用**）。"""
    reader = Dht11Reader(pin=4)
    try:
        backend = reader.open()
    except Dht11Error:
        return True, "真硬件后端不可用（本机没有 GPIO）→ 用 --mock 跑；树莓派上应能自动选到 lgpio"
    finally:
        reader.close()
    return True, f"真硬件后端可用：{backend}"


def check_matplotlib() -> Tuple[bool, str]:
    try:
        import matplotlib
    except ImportError:
        return True, "未安装 matplotlib → 只采集与存档可用；画图需 sudo apt install -y python3-matplotlib python3-tk"
    return True, f"matplotlib {matplotlib.__version__}（可画动态曲线）"


# --------------------------------------------------------------------------
# 生成演示数据
# --------------------------------------------------------------------------


class StepClock:
    """手动推进的时钟：让"时间流逝"瞬时发生（生成 120 个点不用等 5 分钟）。"""

    def __init__(self, start: float = 0.0) -> None:
        self.value = float(start)

    def __call__(self) -> float:
        return self.value


def make_demo_file(path: Path, samples: int = DEMO_SAMPLES, interval_s: float = DEMO_INTERVAL_S) -> Path:
    """生成演示用 CSV（**明确标注：这是 mock 合成值，不是真实测量**）。

    做法：用与 `python3 run.py --mock` **完全相同的合成数据源**（`MockBackend`）采样，
    但把时钟手动往前推 —— 所以 120 个点只花几毫秒。
    时间戳按"每隔 ``interval_s`` 秒"倒推着写，最后一行落在"现在"。

    ⚠️ 同时写一个 `sample_demo.README.txt` 说明它是合成数据，
    避免以后有人把它当成真机测量结果引用（诚实标注是本项目的纪律）。
    """
    import random

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    store = CsvStore(path)
    clock = StepClock()
    backend = MockBackend(clock=clock)
    rng = random.Random(20260924)                  # 固定 seed：演示数据可复现
    now = time.time()
    for index in range(1, samples + 1):
        clock.value += interval_s                  # 推进"模拟时间"
        temperature, humidity = backend.read_once()
        # 叠一点传感器噪声，让曲线更像真实读数（±0.3 ℃ / ±1 %）
        stamp = now - (samples - index) * interval_s
        store.save(Reading(
            index, stamp,
            round(temperature + rng.uniform(-0.3, 0.3), 1),
            round(humidity + rng.uniform(-1.0, 1.0), 1),
            ok=True,
        ))
    readme = path.with_suffix(".README.txt")
    readme.write_text(
        "演示数据说明（basic/data/sample_demo.csv）\n"
        "==========================================\n"
        "这是**合成数据**：由 `python3 basic/tools/selfcheck.py --make-demo` 生成（走 mock 路径），\n"
        "用途是让没有树莓派 / 没有 DHT11 的同学也能验证\n"
        "「周期读取 → 存 CSV → 画动态曲线」这条链路（`python3 run.py --replay`）。\n"
        "它**不代表任何真实测量结果**，报告与答辩中请勿当作真机数据引用。\n"
        "重新生成：python3 basic/tools/selfcheck.py --make-demo\n",
        encoding="utf-8",
    )
    return path


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def run_checks(make_demo: bool = False) -> int:
    checks = [
        ("版本与导入", lambda: check_version()),
        ("单总线解码（合成时序）", lambda: check_decode()),
        ("引脚映射（GPIO4 = 物理脚 7）", lambda: check_pins()),
        ("CSV 存档（缺失值不写 0）", lambda: check_store()),
        ("曲线数据窗口", lambda: check_series()),
        ("演示数据 sample_demo.csv", lambda: check_demo_data(make_demo=make_demo)),
        ("DHT11 后端探测", lambda: check_backends()),
        ("matplotlib（画图依赖）", lambda: check_matplotlib()),
    ]
    print("=" * 74)
    print("基础版（课程作业 H：温湿度测量 + 动态曲线）· 自检")
    print("=" * 74)
    failures = []
    for name, func in checks:
        try:
            ok, detail = func()
        except Exception as exc:  # noqa: BLE001 - 检查自身出错也算失败，并如实打印
            ok, detail = False, f"检查自身异常：{type(exc).__name__}: {exc}"
        print(f"[{OK if ok else BAD}] {name}：{detail}")
        if not ok:
            failures.append(name)

    print("-" * 74)
    if failures:
        print(f"结果：{len(failures)} 项未通过 → {'；'.join(failures)}")
        return 1
    print(f"结果：全部 {len(checks)} 项通过 ✅")
    print()
    print("下一步（树莓派上）：")
    print("  python3 run.py            # 真实读取 DHT11 并看动态曲线")
    print("  python3 run.py --mock     # 没接传感器也能看程序效果")
    print("  python3 run.py --no-plot  # 只采集与存档")
    return 0


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="基础版自检（不依赖硬件）")
    parser.add_argument("--make-demo", action="store_true",
                        help="重新生成演示数据 basic/data/sample_demo.csv")
    args = parser.parse_args(argv)
    return run_checks(make_demo=args.make_demo)


if __name__ == "__main__":
    raise SystemExit(main())
