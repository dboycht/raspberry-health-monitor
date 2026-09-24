#!/usr/bin/env python3
"""基础版的数据模型：一条温湿度采样记录。

课程任务 H（温湿度测量）只要求"周期读取 → 存起来 → 画动态曲线"，
所以基础版的数据模型刻意只有**一个**结构：:class:`Reading`。

刻意遵守本项目的一条红线（三层业务里的同一原则，基础版也照做）：
**读不到就是"没有"，绝不当成 0**。所以失败时 ``temperature_c`` /
``humidity_percent`` 是 ``None``，CSV 里留**空字段**，曲线跳过该点 ——
不补 0、不插值，否则曲线上会凭空出现"0 ℃"这种假数据。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional, Tuple

#: CSV 表头（**顺序就是字段顺序**，改这里必须同步改 :meth:`Reading.to_csv_row`）
CSV_HEADER: Tuple[str, ...] = (
    "index",              # 采样序号（从 1 开始；任务书要求"标出采样顺序"）
    "timestamp",          # 采样时刻（本地时间 ISO，精确到毫秒）
    "temperature_c",      # 温度（℃）—— 读不到时留空
    "humidity_percent",   # 相对湿度（%）—— 读不到时留空
    "status",             # ok / fail
    "note",               # 失败原因（中文，便于答辩时解释）
)


@dataclass
class Reading:
    """一条采样记录。

    Attributes:
        index: 采样序号，1 起。
        ts: Unix 秒时间戳（用于画曲线）。
        temperature_c: 温度（℃）或 ``None``（**读不到就是 None**）。
        humidity_percent: 相对湿度（%）或 ``None``。
        ok: 本次是否读到了**有效**数据。
        note: 失败原因 / 备注（成功时为空字符串）。
    """

    index: int
    ts: float
    temperature_c: Optional[float]
    humidity_percent: Optional[float]
    ok: bool = True
    note: str = ""

    # ------------------------------------------------------------------
    # 显示与落盘
    # ------------------------------------------------------------------

    @property
    def timestamp_text(self) -> str:
        """本地时间字符串（``2026-09-24 20:15:03.123``）。"""
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.ts)) + (
            f".{int((self.ts % 1) * 1000):03d}"
        )

    def to_csv_row(self) -> Tuple[str, ...]:
        """转成一行 CSV 字段（缺失值 = **空字符串**，不是 0）。"""
        return (
            str(self.index),
            self.timestamp_text,
            "" if self.temperature_c is None else f"{self.temperature_c:.1f}",
            "" if self.humidity_percent is None else f"{self.humidity_percent:.1f}",
            "ok" if self.ok else "fail",
            self.note,
        )

    def summary(self) -> str:
        """一行中文摘要（终端打印用；最新一次读数的格式与任务书示例一致）。"""
        if self.ok and self.temperature_c is not None and self.humidity_percent is not None:
            return (
                f"温度: {self.temperature_c:.1f} ℃，"
                f"湿度: {self.humidity_percent:.0f} %"
            )
        return f"本次没读到数据（{self.note or '未知原因'}）"


__all__ = ["Reading", "CSV_HEADER"]
