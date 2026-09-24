#!/usr/bin/env python3
"""基础版的"滚动数据窗口"：保存最近 N 个点，并算出画图需要的横纵坐标与统计量。

为什么把这块单独拆出来（而不是写在画图函数里）
----------------------------------------------
1. **可单测**：滚动窗口、横轴取值、统计量都不依赖 GUI，也不依赖 matplotlib，
   所以在没有装 matplotlib 的机器上（甚至 CI 里）也能验证；
2. **两种横轴**：任务书要求"标出**采样顺序或时间**"，这里把两种都算好，
   由命令行 ``--xaxis index|time`` 选（默认时间）。
3. **缺失值一路保持为 ``None``**：画曲线时 matplotlib 会自然断线，
   而不是把"读不到"画成 0 ℃（本项目的一条红线）。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Deque, List, Optional

#: 画图用的一条采样点：``(时间戳, 温度, 湿度, 采样序号)``
Point = tuple


@dataclass
class Series:
    """固定长度的滚动数据窗口（超出容量自动丢最旧的）。

    Args:
        window: 窗口内保留的采样点数（默认 120）。
    """

    window: int = 120
    ts: Deque[float] = field(default_factory=deque)
    temps: Deque[Optional[float]] = field(default_factory=deque)
    humids: Deque[Optional[float]] = field(default_factory=deque)
    indices: Deque[int] = field(default_factory=deque)
    total: int = 0          # 累计采样次数（含失败的）
    ok_count: int = 0       # 累计**成功**次数
    fail_count: int = 0     # 累计**失败**次数

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def append(
        self,
        ts: float,
        temperature_c: Optional[float],
        humidity_percent: Optional[float],
        index: Optional[int] = None,
    ) -> None:
        """追加一个采样点。

        Args:
            ts: Unix 秒时间戳。
            temperature_c: 温度或 ``None``（读失败）。
            humidity_percent: 湿度或 ``None``。
            index: 采样序号；不传就用"累计次数 + 1"。
        """
        self.ts.append(float(ts))
        self.temps.append(None if temperature_c is None else float(temperature_c))
        self.humids.append(None if humidity_percent is None else float(humidity_percent))
        self.total += 1
        self.indices.append(self.total if index is None else int(index))
        if temperature_c is None or humidity_percent is None:
            self.fail_count += 1
        else:
            self.ok_count += 1
        while len(self.ts) > max(1, int(self.window)):
            self.ts.popleft()
            self.temps.popleft()
            self.humids.popleft()
            self.indices.popleft()

    # ------------------------------------------------------------------
    # 读出来画图
    # ------------------------------------------------------------------

    @property
    def latest(self) -> Optional[tuple]:
        """最近一个点的 ``(温度, 湿度)``；没有任何数据时返回 ``None``。"""
        if not self.ts:
            return None
        return self.temps[-1], self.humids[-1]

    def x_axis(self, use_index: bool = False) -> List[float]:
        """横轴数据：采样**序号**或**相对时间（秒）**。

        - ``use_index=True`` → ``[1, 2, 3, ...]``（采样顺序）；
        - 否则 → ``[0.0, 2.5, 5.0, ...]``（相对第一个点的秒数，任务书要的"时间"）。
        """
        if use_index:
            return list(self.indices)
        if not self.ts:
            return []
        base = self.ts[0]
        return [round(t - base, 1) for t in self.ts]

    def stats(self) -> dict:
        """窗口内的统计量（``None`` 表示窗口里还没有有效值）。"""
        temps = [v for v in self.temps if v is not None]
        humids = [v for v in self.humids if v is not None]
        return {
            "samples": self.total,
            "window": len(self.ts),
            "ok": self.ok_count,
            "failed": self.fail_count,
            "temp_min": min(temps) if temps else None,
            "temp_max": max(temps) if temps else None,
            "humidity_min": min(humids) if humids else None,
            "humidity_max": max(humids) if humids else None,
        }


__all__ = ["Series", "Point"]
