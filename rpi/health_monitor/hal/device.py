"""``Device`` 抽象基类：**每个硬件驱动都必须继承它**。

设计目标（为什么这么定）
------------------------
1. **统一生命周期**：``open() -> read()* -> close()``，谁写驱动都长一个样，
   业务层与测试代码不需要为每个器件写一套调用方式。
2. **Mock 优先**：``mock=True`` 时驱动必须走 :class:`~health_monitor.hal.mock_bus.MockBus`，
   **不允许触碰真实硬件**。这样没有树莓派的人也能跑全部测试与演示。
3. **失败要"上报"而不是"抛穿"**：``read()`` 允许抛 :class:`DeviceIOError`（上层重试），
   也可以返回 ``ok=False`` 的 Sample（上层记录坏点）。**两种都可以，但要一致**：
   - 瞬时错误（总线抖动）→ 抛 ``DeviceIOError`` / ``DeviceTimeout``
   - 数据不可信（超量程）→ 返回 ``ok=False`` 的 Sample，或抛 ``DataInvalidError``
4. **绝不吞异常**：驱动里 ``except`` 之后必须在 ``self.error`` 里留下原因，
   并且 ``status()["fault_count"]`` 要能看到——**静默失败是本项目的一级缺陷**。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from .exceptions import DeviceNotReady
from .models import DeviceKind, Sample


class Device(ABC):
    """所有硬件驱动的基类。

    子类必须实现 :meth:`open` / :meth:`read` / :meth:`close`，
    并**推荐**覆盖 :meth:`describe`（写清接线）与 :meth:`self_check`（自检）。

    Args:
        bus: 总线对象。真实环境为 :class:`~health_monitor.hal.bus.RealBus`（或直接 ``None``
             让子类自行打开）；测试环境为 :class:`~health_monitor.hal.mock_bus.MockBus`。
        mock: 是否模拟模式。为 ``True`` 时**禁止**访问真实硬件。
        name: 设备实例名（与 ``config/devices.json`` 中的键一致）。
    """

    #: 器件大类，子类必须覆盖
    KIND: DeviceKind = DeviceKind.VITAL
    #: 驱动名（注册表里的名字），子类必须覆盖
    NAME: str = "device"

    def __init__(self, bus: Any = None, mock: bool = False, name: str = "") -> None:
        self.bus = bus
        self.mock = bool(mock)
        self.name = name or self.NAME
        self._opened = False
        self._error: Optional[str] = None
        self._fault_count = 0
        self._read_count = 0

    # ------------------------------------------------------------------
    # 必须实现的三件事
    # ------------------------------------------------------------------

    @abstractmethod
    def open(self) -> None:
        """初始化设备（打开 I2C/SPI、配置 GPIO、复位芯片…）。

        失败必须抛 :class:`~health_monitor.hal.exceptions.DeviceInitError`，
        消息里**写清排查线索**（例如 "I2C 地址 0x57 无应答：先跑 ``i2cdetect -y 1``"）。
        """

    @abstractmethod
    def read(self) -> Sample:
        """读取一次数据。

        Returns:
            对应的 ``Sample`` 子类实例（见 :mod:`health_monitor.hal.models`）。

        Raises:
            DeviceNotReady: 未 ``open()`` 就调用。
            DeviceIOError / DeviceTimeout: 总线错误（上层可重试）。
        """

    @abstractmethod
    def close(self) -> None:
        """释放资源。**必须可重复调用**（幂等），且不应抛异常。"""

    # ------------------------------------------------------------------
    # 可选覆盖
    # ------------------------------------------------------------------

    def describe(self) -> Dict[str, Any]:
        """返回接线说明，供 ``docs`` 自动生成与学生自查使用。

        建议包含：``kind`` / ``name`` / ``bus`` / ``pins`` / ``notes``。
        """
        return {
            "name": self.name,
            "kind": self.KIND.value,
            "mock": self.mock,
            "bus": type(self.bus).__name__ if self.bus is not None else None,
            "pins": {},
            "notes": "",
        }

    def self_check(self) -> Dict[str, Any]:
        """自检：在不产生副作用的前提下确认设备"真的在"。

        默认实现即"能否读一次"。有更轻量手段的驱动应覆盖它
        （例如 I2C 器件读 WHO_AM_I 寄存器、LCD 只回读状态）。

        Returns:
            至少包含 ``ok`` (bool) 与 ``detail`` (str) 的字典。
        """
        try:
            sample = self.read()
        except Exception as exc:  # noqa: BLE001 - 自检要能捕获一切并如实上报
            return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
        return {"ok": bool(sample.ok), "detail": sample.error or "ok"}

    def status(self) -> Dict[str, Any]:
        """运行状态快照（给 Web 服务 / 日志 / 测试用）。"""
        return {
            "name": self.name,
            "kind": self.KIND.value,
            "driver": type(self).__name__,
            "opened": self._opened,
            "mock": self.mock,
            "read_count": self._read_count,
            "fault_count": self._fault_count,
            "last_error": self._error,
        }

    # ------------------------------------------------------------------
    # 给子类用的小工具（统一计数与错误留痕）
    # ------------------------------------------------------------------

    def _note_ok(self) -> None:
        """成功读到一次数据时调用（子类在 ``read()`` 成功路径里调用）。"""
        self._read_count += 1

    def _note_fault(self, exc: BaseException) -> None:
        """出现异常时调用：留痕 + 计数。**调用后要把异常继续抛出**。"""
        self._fault_count += 1
        self._error = f"{type(exc).__name__}: {exc}"

    def _require_open(self) -> None:
        """在 ``read()`` 开头调用，防止未初始化就使用。"""
        if not self._opened:
            raise DeviceNotReady(
                f"设备 {self.name}（{type(self).__name__}）尚未 open()，不能读取数据"
            )

    # ------------------------------------------------------------------
    # 上下文管理器：`with driver as d: d.read()`
    # ------------------------------------------------------------------

    def __enter__(self) -> "Device":
        self.open()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        return (
            f"<{type(self).__name__} name={self.name!r} "
            f"mock={self.mock} opened={self._opened}>"
        )


class OutputDevice(Device):
    """输出类器件（LCD / 音箱 / 蜂鸣器 / LED）的基类。

    与传感器不同，它们的 ``read()`` 语义是"回读自身状态"；
    主要能力是通过 :meth:`send` 接收 :class:`~health_monitor.hal.models.Command`。
    """

    @abstractmethod
    def send(self, command: Any) -> None:
        """执行一条指令（``SpeakCommand`` / ``BeepCommand`` / ``LightCommand`` / ``DisplayCommand``）。"""

    def read(self) -> Sample:
        """回读状态。默认实现返回一条"无状态"样本，子类**应当**覆盖。

        覆盖要求：返回 :class:`~health_monitor.hal.models.DisplayStatus`
        （显示类）或带 ``device`` 字段的 ``Sample``，**不要**在这里访问硬件以外的资源。
        """
        from .models import DisplayStatus

        self._require_open()
        self._note_ok()
        return DisplayStatus(device=self.name)


__all__ = ["Device", "OutputDevice"]
