"""支持 ``python -m health_monitor <命令>`` 的入口。"""

from __future__ import annotations

from .main import main

if __name__ == "__main__":
    raise SystemExit(main())
