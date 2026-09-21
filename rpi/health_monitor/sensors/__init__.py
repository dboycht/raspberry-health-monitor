"""输入类驱动（传感器 / 按键）—— 每个文件一个器件，互不依赖。

⚠️ 纪律：本包内的文件**只允许** ``from ..hal import ...``，不要互相 import。
（传感器之间不该有耦合；若两个器件真的需要协同，请放到 ``core/`` 里做。）

文件名与类名的对应关系见 ``health_monitor/hal/registry.py`` 的 :data:`MANIFEST`：
驱动名 ``xxx`` → 文件 ``sensors/xxx.py`` → 类 ``Xxx``。
"""

from .button import Button, ButtonDebouncer

__all__ = ["Button", "ButtonDebouncer"]
