#!/usr/bin/env python3
"""基础版入口：温湿度测量 + 动态曲线（课程作业 H）。

一行命令就能跑（参数说明见 `basic/README.md`）::

    python3 run.py                 # 树莓派上真实读取 DHT11（GPIO4 = 物理脚 7）
    python3 run.py --mock          # 没有硬件也能跑（合成数据）
    python3 run.py --no-plot       # 只采集与存档，不画图

本文件只做一件事：把命令行交给 `basic/plot.py`。这样"入口"和"实现"分开，
测试可以直接 import `basic.plot` 而不经过命令行。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 把仓库根加进 sys.path，这样 `from basic.xxx import ...` 在 basic/ 下直接执行也能用
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from basic.plot import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
