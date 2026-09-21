"""输出类驱动（显示 / 声音 / 灯光）—— 每个文件一个器件。

输出类驱动统一继承 :class:`health_monitor.hal.device.OutputDevice`，
必须实现 ``open()`` / ``close()`` / ``send(command)``，
并**建议**覆盖 ``read()`` 返回自己的状态（便于测试与远程查看）。

驱动名 → 文件 → 类 的对应关系见 ``hal/registry.py`` 的 :data:`MANIFEST`。
"""
