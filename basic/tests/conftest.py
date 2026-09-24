#!/usr/bin/env python3
"""pytest 的公共配置：让 `basic` 包能被导入，并把运行数据写进 `basic/data/`。

用法（在**仓库根目录**或 `basic/` 目录下都能跑）::

    python -m pytest basic/tests -q
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]      # 仓库根（basic/tests/ → basic/ → 根）
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
