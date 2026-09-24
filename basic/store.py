#!/usr/bin/env python3
"""基础版的"连续采集数据保存"：把每次采样写进 CSV。

任务书原话："程序**保存连续采集的数据**"。基础版用 **CSV** 落盘：

- 一行一个采样点，**立刻 flush + fsync**，所以"拔电源 / Ctrl+C"也不会丢已采到的数据；
- 列见 :data:`basic.model.CSV_HEADER`；**读不到时留空字段**（不写 0）；
- 纯标准库，不依赖 pandas / numpy，树莓派上开箱可用；
- 另外导出一次运行小结 JSON（供报告/验收引用）。

⚠️ 运行产生的 CSV **不入库**（见 `basic/data/.gitignore`），仓库里只保留
一份演示样本 :data:`basic.store.DEMO_FILE_NAME`。
"""

from __future__ import annotations

import csv
import json
import os
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from .model import CSV_HEADER, Reading

#: 演示/离线验证用的样本数据（**入库**，让没有硬件的同学也能跑通画图）
DEMO_FILE_NAME = "sample_demo.csv"


def default_data_dir() -> Path:
    """基础版数据目录（``basic/data/``）。"""
    return Path(__file__).resolve().parent / "data"


def default_csv_path(now: Optional[float] = None, data_dir: Optional[Path] = None) -> Path:
    """默认 CSV 路径：``data/dht11_YYYYmmdd_HHMMSS.csv``。

    **一次运行 = 一个文件**（文件名带日期与启动时刻）。这样：
    ① 每次运行的"采样序号"都从 1 开始，曲线上不会出现序号跳变；
    ② 报告里一眼就能说清"这组数据是什么时候采的"；
    ③ 同一天多次运行不会互相覆盖，也不会混进彼此的行。
    """
    stamp = datetime.fromtimestamp(now if now is not None else __import__("time").time())
    return (data_dir or default_data_dir()) / f"dht11_{stamp:%Y%m%d_%H%M%S}.csv"


def load_rows(
    path: Path, limit: Optional[int] = None
) -> List[Tuple[float, Optional[float], Optional[float]]]:
    """**只读**地读回 ``[(时间戳, 温度, 湿度), ...]``（升序，无效值保持 ``None``）。

    ⚠️ 为什么单独做成模块级函数（而不是 `CsvStore.readonly(...)` 之类的类方法）：
    `CsvStore(path, append=False)` 的语义是"下一次写入时先清空文件"，
    用它当"只读"会**把数据文件删掉**（曾经真的在回放时删掉了演示数据）。
    所以"只读"必须是**碰都不碰文件**的一条独立路径。
    """
    rows: List[Tuple[float, Optional[float], Optional[float]]] = []
    target = Path(path)
    if not target.exists():
        return rows
    with open(target, "r", newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            ts = _parse_timestamp(row.get("timestamp", ""))
            if ts is None:
                continue                      # 时间解析不了的坏行直接跳过（不猜）
            rows.append((ts, _parse_float(row.get("temperature_c")), _parse_float(row.get("humidity_percent"))))
    rows.sort(key=lambda item: item[0])
    if limit:
        rows = rows[-int(limit):]
    return rows


class CsvStore:
    """把采样点写成 CSV，并支持读回来（画历史 / 离线验证）。"""

    def __init__(self, path: Path, append: bool = True) -> None:
        self.path = Path(path)
        self.append = bool(append)
        self.rows_written = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.append and self.path.exists():
            self.path.unlink()

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------

    def _needs_header(self) -> bool:
        """文件不存在、或大小为 0 ⇒ 需要写表头。"""
        try:
            return not self.path.exists() or self.path.stat().st_size == 0
        except OSError:
            return True

    def save(self, reading: Reading) -> None:
        """写一行并**立刻落盘**（flush + fsync，拔电源也不丢已采数据）。"""
        with open(self.path, "a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            if self._needs_header():
                writer.writerow(CSV_HEADER)
            writer.writerow(reading.to_csv_row())
            handle.flush()
            os.fsync(handle.fileno())
        self.rows_written += 1

    def save_summary(self, payload: dict) -> Path:
        """把运行小结写成同名的 ``.summary.json``（给报告/验收看）。"""
        target = self.path.with_suffix(".summary.json")
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return target

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    def load(self, limit: Optional[int] = None) -> List[Tuple[float, Optional[float], Optional[float]]]:
        """读回 ``[(时间戳, 温度, 湿度), ...]``（按时间升序，无效值保持 ``None``）。

        用途：把上次的数据预填进曲线。**离线回放请用模块级** :func:`load_rows`
        （它不经过本类，因此不会因为 ``append=False`` 而删掉文件）。
        """
        return load_rows(self.path, limit=limit)


def _parse_float(text: Optional[str]) -> Optional[float]:
    """空字符串 / 非法值 → ``None``（**绝不返回 0**）。"""
    if text is None or str(text).strip() == "":
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _parse_timestamp(text: str) -> Optional[float]:
    """解析 :meth:`basic.model.Reading.timestamp_text` 写出的本地时间字符串。"""
    raw = (text or "").strip()
    if not raw:
        return None
    for pattern in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, pattern).timestamp()
        except ValueError:
            continue
    return None


__all__ = [
    "CsvStore",
    "CSV_HEADER",
    "DEMO_FILE_NAME",
    "default_csv_path",
    "default_data_dir",
    "load_rows",
]
