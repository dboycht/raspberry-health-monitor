"""pytest 全局配置：把 ``rpi/`` 加入 sys.path，使 ``import health_monitor`` 生效。

这样无论从仓库根目录还是 ``rpi/`` 目录运行 ``pytest`` 都能正常工作::

    cd rpi
    python -m pytest -q
"""

from __future__ import annotations

import sys
from pathlib import Path

_RPI_DIR = Path(__file__).resolve().parent
if str(_RPI_DIR) not in sys.path:
    sys.path.insert(0, str(_RPI_DIR))
