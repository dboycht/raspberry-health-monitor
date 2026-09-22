"""pytest 全局配置：把 ``rpi/`` 与 ``rpi/scripts/`` 加入 sys.path。

这样无论从仓库根目录还是 ``rpi/`` 目录运行 ``pytest`` 都能正常工作::

    cd rpi
    python -m pytest -q

把 ``scripts/`` 也加进来，是为了让测试能直接 ``import live_plot`` 这类脚本模块
（脚本里的**纯逻辑**——例如滚动窗口 ``Series``——同样需要单测；GUI 部分靠人工目检）。
"""

from __future__ import annotations

import sys
from pathlib import Path

_RPI_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _RPI_DIR / "scripts"
for _path in (_RPI_DIR, _SCRIPTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
