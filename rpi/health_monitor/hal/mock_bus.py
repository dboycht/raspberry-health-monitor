"""总线抽象与**模拟总线**（没有树莓派也能开发与跑测试的关键件）。

两种总线：
- :class:`MockBus` —— 内存模拟，不碰任何真实设备。支持"钩子函数"让每个驱动
  按自己的语义产生合理数据（例如按键按下、PIR 触发、DHT11 返回 26.5°C）。
- :class:`RealBus` —— 真树莓派上的真实 I2C / SPI。**故意做成薄封装**，
  真正的引脚操作交给各驱动自行调用 ``gpiozero`` / ``lgpio`` / ``spidev``。

⚠️ 纪律：驱动在 ``mock=True`` 时**只允许**走 MockBus；一旦发现驱动在 mock 模式下
打开了 ``/dev/i2c-*`` 或调用了 ``lgpio``，视为缺陷（测试会失败）。
"""

from __future__ import annotations

from collections import deque
from typing import Any, Callable, Deque, Dict, Iterator, List, Tuple

from .exceptions import DeviceIOError, UnsupportedError

# 钩子函数签名：(bus, payload) -> 返回值
I2CHook = Callable[["MockBus", bytes], bytes]
I2CWriteHook = Callable[["MockBus", bytes], None]
SPIHook = Callable[["MockBus", bytes], bytes]


class MockBus:
    """内存总线：记录所有调用，并按注册的钩子返回数据。

    典型用法（写在驱动自己的 ``open()`` 里）::

        if self.mock:
            self.bus = MockBus()
            # MAX30102：先写寄存器地址，再读 6 字节 FIFO
            self.bus.hook_i2c(0x57, lambda bus, data: b"\\x01\\x02\\x03\\x04\\x05\\x06")

    测试里检查"驱动到底发了什么字节"::

        calls = bus.operations("i2c_write")
        assert calls[-1] == (1, 0x57, b"\\x0f\\x00")
    """

    def __init__(self) -> None:
        self._i2c_hooks: Dict[int, I2CHook] = {}
        self._i2c_write_hooks: Dict[int, I2CWriteHook] = {}
        self._spi_hooks: Dict[int, SPIHook] = {}
        self._ops: List[Tuple[str, tuple]] = []
        self._errors: Deque[Exception] = deque()
        self._default_i2c_reply: bytes = b"\x00"

    # ------------------------------------------------------------------
    # 钩子注册
    # ------------------------------------------------------------------

    def hook_i2c(self, address: int, func: I2CHook) -> "MockBus":
        """注册 I2C 读钩子：``func(bus, payload) -> bytes``。"""
        self._i2c_hooks[address] = func
        return self

    def hook_i2c_write(self, address: int, func: I2CWriteHook) -> "MockBus":
        """注册 I2C 写钩子：``func(bus, data) -> None``。"""
        self._i2c_write_hooks[address] = func
        return self

    def hook_spi(self, bus: int, func: SPIHook) -> "MockBus":
        """注册 SPI 钩子：``func(bus, data) -> bytes``。"""
        self._spi_hooks[bus] = func
        return self

    def set_i2c_reply(self, address: int, payload: bytes) -> "MockBus":
        """便捷方法：让某个 I2C 地址**恒定**返回同一段字节。"""
        return self.hook_i2c(address, lambda _bus, _data: payload)

    # ------------------------------------------------------------------
    # 故障注入（给"驱动必须正确处理失败"的测试用）
    # ------------------------------------------------------------------

    def fail_next(self, exc: Exception | None = None) -> "MockBus":
        """让下一个总线操作抛出异常（默认 :class:`DeviceIOError`）。"""
        self._errors.append(exc or DeviceIOError("MockBus 注入的失败"))
        return self

    def fail_times(self, n: int, exc: Exception | None = None) -> "MockBus":
        """连续注入 ``n`` 次失败。"""
        for _ in range(n):
            self.fail_next(exc)
        return self

    def _maybe_fail(self) -> None:
        if self._errors:
            raise self._errors.popleft()

    # ------------------------------------------------------------------
    # I2C
    # ------------------------------------------------------------------

    def i2c_read(self, bus: int, address: int, length: int) -> bytes:
        """读 ``length`` 字节。未注册钩子时返回全 0（长度正确）。"""
        self._ops.append(("i2c_read", (bus, address, length)))
        self._maybe_fail()
        hook = self._i2c_hooks.get(address)
        if hook is None:
            return self._default_i2c_reply * length
        data = bytes(hook(self, b""))
        if len(data) != length:
            raise DeviceIOError(
                f"MockBus 钩子对 0x{address:02X} 返回了 {len(data)} 字节，"
                f"而驱动请求 {length} 字节（钩子与驱动对不上）"
            )
        return data

    def i2c_write(self, bus: int, address: int, data: bytes) -> None:
        """写数据（可选读回 ``read``）。"""
        payload = bytes(data)
        self._ops.append(("i2c_write", (bus, address, payload)))
        self._maybe_fail()
        hook = self._i2c_write_hooks.get(address)
        if hook is not None:
            hook(self, payload)

    def i2c_write_read(
        self, bus: int, address: int, write: bytes, read: int
    ) -> bytes:
        """写寄存器地址后立即读（I2C 器件最常见的访问方式）。

        同时触发读钩子与写钩子，并**按顺序**记录两次操作，方便断言。
        """
        self.i2c_write(bus, address, write)
        return self.i2c_read(bus, address, read)

    def i2c_scan(self) -> List[int]:
        """返回"假装在线"的地址列表（= 注册过钩子的地址）。"""
        return sorted(self._i2c_hooks)

    # ------------------------------------------------------------------
    # SPI
    # ------------------------------------------------------------------

    def spi_xfer(self, bus: int, device: int, data: bytes) -> bytes:
        """SPI 全双工传输。未注册钩子时返回等长全 0。"""
        payload = bytes(data)
        self._ops.append(("spi_xfer", (bus, device, payload)))
        self._maybe_fail()
        hook = self._spi_hooks.get(bus)
        if hook is None:
            return b"\x00" * len(payload)
        out = bytes(hook(self, payload))
        if len(out) != len(payload):
            raise DeviceIOError(
                f"MockBus SPI 钩子返回 {len(out)} 字节，期望 {len(payload)} 字节"
            )
        return out

    # ------------------------------------------------------------------
    # 断言辅助
    # ------------------------------------------------------------------

    def operations(self, kind: str | None = None) -> List[Tuple[str, tuple]]:
        """返回操作流水；``kind`` 可筛 ``i2c_read`` / ``i2c_write`` / ``spi_xfer``。"""
        if kind is None:
            return list(self._ops)
        return [op for op in self._ops if op[0] == kind]

    def op_count(self, kind: str | None = None) -> int:
        """操作次数（断言"驱动没有乱发指令"用）。"""
        return len(self.operations(kind))

    def clear(self) -> None:
        """清空操作流水（保留钩子与待注入故障）。"""
        self._ops.clear()

    def __iter__(self) -> Iterator[Tuple[str, tuple]]:
        return iter(self._ops)


class RealBus:
    """真实总线。

    ⚠️ 这是**薄封装**：I2C 用 ``smbus2``，SPI 用 ``spidev``，GPIO 由各驱动
    自行使用 ``gpiozero`` / ``lgpio``。本类只负责"统一入口 + 统一异常翻译"。

    在非树莓派平台（Windows 开发机）调用会抛 :class:`UnsupportedError`，
    这是**刻意**的：提醒你该用 ``mock=True`` 跑，而不是在 PC 上假装接上了硬件。
    """

    def __init__(self, i2c_bus: int = 1) -> None:
        self.i2c_bus = i2c_bus
        self._smbus: Any = None
        self._spi: Dict[int, Any] = {}

    # -- I2C -----------------------------------------------------------

    def _ensure_smbus(self) -> Any:
        if self._smbus is not None:
            return self._smbus
        try:
            from smbus2 import SMBus  # type: ignore import-not-found
        except ImportError as exc:  # pragma: no cover - 真实树莓派环境才有
            raise UnsupportedError(
                "未安装 smbus2：请在树莓派上执行 `sudo apt install -y python3-smbus i2c-tools`"
                " 或 `pip install smbus2`"
            ) from exc
        self._smbus = SMBus(self.i2c_bus)
        return self._smbus

    def i2c_read(self, bus: int, address: int, length: int) -> bytes:
        smb = self._ensure_smbus()
        data = smb.read_i2c_block_data(address, 0x00, length)
        return bytes(data)

    def i2c_write(self, bus: int, address: int, data: bytes) -> None:
        smb = self._ensure_smbus()
        if not data:
            raise ValueError("i2c_write 需要至少 1 字节（寄存器地址）")
        smb.write_i2c_block_data(address, data[0], list(data[1:]))

    def i2c_write_read(self, bus: int, address: int, write: bytes, read: int) -> bytes:
        smb = self._ensure_smbus()
        if not write:
            raise ValueError("i2c_write_read 需要寄存器地址")
        return bytes(smb.read_i2c_block_data(address, write[0], read))

    def i2c_scan(self) -> List[int]:
        smb = self._ensure_smbus()
        found: List[int] = []
        for addr in range(0x03, 0x78):
            try:
                smb.read_byte(addr)
                found.append(addr)
            except OSError:
                continue
        return found

    # -- SPI -----------------------------------------------------------

    def _ensure_spi(self, bus: int, device: int) -> Any:
        key = bus * 10 + device
        if key not in self._spi:
            try:
                import spidev  # type: ignore import-not-found
            except ImportError as exc:  # pragma: no cover
                raise UnsupportedError(
                    "未安装 spidev：请在树莓派上执行 `pip install spidev`"
                ) from exc
            spi = spidev.SpiDev()
            spi.open(bus, device)
            spi.max_speed_hz = 1_000_000
            spi.mode = 0
            self._spi[key] = spi
        return self._spi[key]

    def spi_xfer(self, bus: int, device: int, data: bytes) -> bytes:
        spi = self._ensure_spi(bus, device)
        return bytes(spi.xfer2(list(bytes(data))))

    def close(self) -> None:
        if self._smbus is not None:
            self._smbus.close()
            self._smbus = None
        for spi in self._spi.values():
            try:
                spi.close()
            except Exception:  # noqa: BLE001 - 关闭失败不应影响收尾
                pass
        self._spi.clear()


__all__ = ["MockBus", "RealBus", "I2CHook", "I2CWriteHook", "SPIHook"]
