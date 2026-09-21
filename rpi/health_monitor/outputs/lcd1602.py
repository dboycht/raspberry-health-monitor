"""LCD1602 液晶（I2C 转接板 / PCF8574 背包）驱动。

模块用途
--------
把当前状态显示在设备本体的小屏上：心率、血氧、报警原因、时间…
让老人不用掏手机就能看到"现在什么情况"。

接线表（LCD1602 + PCF8574 转接板 ↔ 树莓派 40-pin 物理脚号）
------------------------------------------------------------
===================  ===================  ============================================
转接板引脚            树莓派 40-pin        说明
===================  ===================  ============================================
VCC                  **5V**（物理脚 2/4）  ⚠️ 5V：3.3V 时对比度极低，屏幕几乎全黑
GND                  GND（物理脚 6）      共地
SDA                  GPIO2 / SDA1（脚 3） 与 MAX30102 **共用**这条 I2C 总线
SCL                  GPIO3 / SCL1（脚 5） 与 MAX30102 **共用**这条 I2C 总线
===================  ===================  ============================================

**与 MAX30102 共用 I2C 总线**：I2C 是总线型接口，多个从机靠**地址**区分，只要地址
不冲突就可并挂（MAX30102 固定在 0x57；PCF8574 常见 0x27 / 0x3F / 0x20 / 0x38，
都不会撞车）。注意两点：

1. 总线上所有器件**并联**到同一对 SDA/SCL，各自 VCC/GND 都要接；
2. 两个器件的上拉电阻不要重复堆太多（模块自带上拉时，总线总上拉不宜小于 1kΩ）。

PCF8574 → HD44780 的位分配（本项目固定用这一种，市面上 99% 的背包都是它）
------------------------------------------------------------------------
``P0=RS``、``P1=RW``、``P2=E``、``P3=背光``、``P4~P7=D4~D7``（**4 位模式**）。

设计要点（为什么这么写）
------------------------
1. **4 位模式初始化序列**：上电后 HD44780 处于 8 位模式且状态未知，必须按数据手册
   的"软复位"流程走：``0x33 → 0x32 → 功能设置 0x28 → 显示关 0x08 → 清屏 0x01
   → 输入模式 0x06 → 显示开 0x0C``。少一步就会出现"屏幕只亮不显示"或花屏。
2. **纯逻辑拆出来**：字节↔PCF8574 位↔I2C 缓冲区的编码放在
   :class:`Pcf8574LcdCodec`（不碰总线），单测不需要硬件也能验证时序与位序。
3. **自动探测地址**：0x27 / 0x3F 最常见，0x20 / 0x38 作备选；探测结果写进
   :meth:`Lcd1602.describe`，答辩时能直接说清"我板子到底是哪个地址"。
4. **中文必须降级**：LCD1602 内置的是 **ASCII（HD44780 字库）**，**显示不了中文**，
   直接下发中文会显示成随机点阵（看起来就是"花屏"）。因此驱动把非 ASCII 字符
   统一替换成 ``?``（可配置 ``non_ascii_fallback``），**绝不让非法字节进 I2C**。
   界面文案请用英文/拼音缩写：``HR 72 bpm``、``SpO2 98%``、``ALARM: HR HIGH``；
   中文留给手机端与语音播报（``bt_speaker``）。
5. **第二行必须补空格**：LCD 的 DDRAM 会保留上一次的字符，短文本不清屏就会留下
   残影（"HR 72 bpm" 后面跟着上一屏的 "ALARM"）。所以每行都要**按 ``cols`` 补齐**。

负责人：________（待分配）
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..hal.device import OutputDevice
from ..hal.exceptions import (
    AlarmDispatchError,
    DeviceInitError,
    DeviceIOError,
    UnsupportedError,
)
from ..hal.models import (
    DeviceKind,
    DisplayCommand,
    DisplayStatus,
)

_log = logging.getLogger(__name__)

#: 常见 PCF8574 转接板地址（按命中概率排序，探测时按此顺序尝试）
CANDIDATE_ADDRESSES: Tuple[int, ...] = (0x27, 0x3F, 0x20, 0x38)

#: PCF8574 位定义
BIT_RS = 0x01        # P0：0=指令，1=数据
BIT_RW = 0x02        # P1：0=写，1=读（本项目只写）
BIT_EN = 0x04        # P2：使能脉冲（高→低锁存）
BIT_BACKLIGHT = 0x08  # P3：背光（1=亮）
#: D4~D7 对应 P4~P7，位于高半字节，所以"数据字节"要左移 4 位对齐
DATA_SHIFT = 4

#: HD44780 指令码（数据手册表 6）
CMD_CLEAR_DISPLAY = 0x01
CMD_RETURN_HOME = 0x02
CMD_ENTRY_MODE = 0x06        # 光标右移、显示不滚动
CMD_DISPLAY_OFF = 0x08
CMD_DISPLAY_ON = 0x0C        # 显示开、光标关、不闪烁
CMD_FUNCTION_SET = 0x28      # 4 位总线、2 行、5x8 点阵
CMD_SET_DDRAM = 0x80         # 设置 DDRAM 地址（| 地址）

#: 两行在 DDRAM 中的起始地址（HD44780：第 2 行从 0x40 开始）
ROW_OFFSETS: Tuple[int, ...] = (0x00, 0x40)


# ==========================================================================
# 第一部分：纯逻辑（字节编码，不碰总线、可单测）
# ==========================================================================


class Pcf8574LcdCodec:
    """PCF8574 背包的字节编码器（纯逻辑）。

    只做一件事：把"HD44780 指令/数据字节"编成"该往 PCF8574 写的字节序列"，
    以及反向还原（便于测试与排查）。**不访问任何总线**。

    Args:
        backlight: 背光是否点亮（影响每一位的 bit3）。
        read_rs / read_rw / read_en: 三个控制位的"高电平"值，默认 1（见类文档）。
    """

    def __init__(
        self,
        backlight: bool = True,
        read_rs: int = 1,
        read_rw: int = 0,
        read_en: int = 1,
    ) -> None:
        self.backlight = bool(backlight)
        self.read_rs = 1 if read_rs else 0
        self.read_rw = 1 if read_rw else 0
        self.read_en = 1 if read_en else 0

    # -- 位拼装 ----------------------------------------------------------

    def _pack(self, nibble: int) -> int:
        """把低 4 位的一个半字节放到 PCF8574 的 **D4~D7**（bit4~bit7）上。

        位序：``P0=RS``、``P1=RW``、``P2=E``、``P3=背光``、``P4~P7=D4~D7``，
        所以半字节要从 bit0~bit3 左移 4 位到 bit4~bit7。
        """
        base = (nibble & 0x0F) << DATA_SHIFT
        base |= BIT_BACKLIGHT if self.backlight else 0
        return base & 0xFF

    @property
    def idle_byte(self) -> int:
        """EN 为低时的空闲字节（总线静止时应停在 EN=0，避免误锁存）。"""
        return self._pack(0x00)

    def control_byte(self, nibble: int, rs: int) -> List[int]:
        """返回"锁存一个半字节"的两个字节：EN 拉高 →  EN 拉低。

        Args:
            nibble: 低 4 位有效的半字节（0x0~0xF）。
            rs: 1=数据，0=指令。
        """
        rs_bit = BIT_RS if rs else 0
        rw_bit = BIT_RW if self.read_rw else 0
        base = self._pack(nibble) | rs_bit | rw_bit
        en = BIT_EN if self.read_en else 0
        return [base | en, base & ~en & 0xFF]

    def encode_nibbles(self, value: int, rs: int) -> List[int]:
        """把一个完整字节按 **高半字节→低半字节** 编成 4 个待写字节。

        HD44780 的 4 位模式规定：先传高 4 位，再传低 4 位，每次都用 EN 脉冲锁存。
        """
        value &= 0xFF
        return self.control_byte((value >> 4) & 0x0F, rs) + self.control_byte(
            value & 0x0F, rs
        )

    def encode_text(self, text: str, rs: int = 1) -> List[int]:
        """把一整行文本编成待写字节序列。"""
        out: List[int] = []
        for ch in text:
            out.extend(self.encode_nibbles(ord(ch), rs))
        return out

    def set_ddram_address(self, address: int) -> List[int]:
        """生成"设置 DDRAM 地址"（光标定位）的字节序列。

        HD44780 的 DDRAM 地址只占低 7 位（第 2 行从 0x40 起），所以掩码是 ``0x7F``
        ——**不能**用 ``0x3F``，那会把第 2 行的 0x40 位抹掉，导致第二行文字
        接在第一行后面显示。
        """
        return self.encode_nibbles(CMD_SET_DDRAM | (address & 0x7F), rs=0)

    # -- 反向还原（测试/排查用） ------------------------------------------
    #
    # ⚠️ 重要事实：**PCF8574 是"准双向 IO 扩展"，不是 RAM**。
    # 往它写一个字节只是改变 8 个引脚的电平，读回来的是**引脚当前电平**，
    # 不是"上次写入的内容"，更没有 HD44780 的读指针。所以"回读屏幕内容"
    # 在硬件上做不到；:meth:`Lcd1602.read` 返回的是驱动**自己记录的**内容，
    # 驱动不知道时一律返回空串（诚实），绝不编造。

    def decode_ops(self, ops: Sequence[int]) -> List[Tuple[str, int]]:
        """把一段 I2C 写字节流水还原成 ``[("cmd"|"data", 字节值), …]``。

        仅在 **mock 模式** 与测试中使用（真实模式没有"读回屏幕"的能力）。
        """
        decoded: List[Tuple[str, int]] = []
        pending: Optional[int] = None
        for byte in ops:
            if byte & BIT_EN:
                # EN 拉高的那一拍：D4~D7 上就是本次要锁存的半字节
                pending = byte
                continue
            if pending is None:
                continue
            last, pending = pending, None
            if last & BIT_RW:
                continue  # 读操作，跳过（本项目只写）
            nibble = (last >> DATA_SHIFT) & 0x0F
            decoded.append(("data" if last & BIT_RS else "cmd", nibble))
        # 每两个半字节合成一个字节（高半字节在前）
        merged: List[Tuple[str, int]] = []
        for i in range(0, len(decoded) - 1, 2):
            kind_a, hi = decoded[i]
            kind_b, lo = decoded[i + 1]
            if kind_a != kind_b:  # pragma: no cover - 说明时序被打断
                continue
            merged.append((kind_a, (hi << 4) | lo))
        return merged


# ==========================================================================
# 第二部分：驱动（硬件访问层）
# ==========================================================================


class Lcd1602(OutputDevice):
    """I2C 转接板 1602 液晶（16 列 × 2 行）。

    Args:
        bus: **I2C 总线号**（int，默认 1 = ``/dev/i2c-1``）。
             ⚠️ 注意与基类的 ``bus`` 参数含义不同：本驱动按项目约定把
             ``bus`` 当"总线号"，总线对象由 :meth:`open` 内部按需创建。
        address: I2C 地址；``0``（默认）表示**自动探测**
                 :data:`CANDIDATE_ADDRESSES`（0x27/0x3F/0x20/0x38）。
        cols: 列数，默认 16。
        rows: 行数，默认 2。
        backlight: 背光是否点亮，默认 True。
        non_ascii_fallback: 非 ASCII（中文等）字符的替换字符，默认 ``"?"``。
        mock: 模拟模式。**True 时绝不打开 ``/dev/i2c-*``**。
        name: 实例名（默认 ``lcd1602``）。
    """

    KIND = DeviceKind.DISPLAY
    NAME = "lcd1602"

    def __init__(
        self,
        bus: int = 1,
        address: int = 0,
        cols: int = 16,
        rows: int = 2,
        backlight: bool = True,
        non_ascii_fallback: str = "?",
        mock: bool = False,
        name: str = "",
    ) -> None:
        # ⚠️ 这里刻意把 ``bus``（总线号）单独保存，不直接传给基类的 bus=…：
        #    契约要求 ``bus`` 是"总线号 int"，但注册表 create_device() 会注入一个
        #    总线**对象**（MockBus / RealBus）。为兼容两种用法，这里做一次拆解：
        #    int  → 当作 I2C 总线号；非 int → 当作已就绪的总线句柄。
        bus_handle: Any = None
        i2c_bus = 1
        if bus is None or isinstance(bus, int):
            i2c_bus = 1 if bus is None else int(bus)
        else:
            bus_handle = bus
        super().__init__(bus=bus_handle, mock=mock, name=name)
        if cols not in (16, 20, 40):
            _log.warning("LCD1602 收到非常见列数 cols=%s（本驱动按 16 列器件设计）", cols)
        self.i2c_bus = i2c_bus
        self.address = int(address)
        self.cols = int(cols)
        self.rows = int(rows)
        self.backlight = bool(backlight)
        self.non_ascii_fallback = non_ascii_fallback or "?"
        self._address_auto = self.address == 0
        self._handle: Any = bus_handle
        self._codec = Pcf8574LcdCodec(backlight=self.backlight)
        # 驱动"自己记录的"当前屏幕内容（硬件读不回来，见模块文档）
        self._lines: Tuple[str, str] = ("", "")
        self._page = 0
        self._non_ascii_replaced = 0

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def open(self) -> None:
        """探测地址 → 走 HD44780 的 4 位模式初始化序列。

        Raises:
            DeviceInitError: 探测不到地址，或初始化时序写不进去
                             （消息里含 ``i2cdetect`` 等排查线索）。
        """
        if self._opened:
            return
        if self.mock:
            if self._handle is None:
                from ..hal.mock_bus import MockBus

                self._handle = MockBus()
                # 给候选地址都注册一个"假装在线"的响应，让地址探测在 mock 下也有意义
                for addr in CANDIDATE_ADDRESSES:
                    self._handle.set_i2c_reply(addr, b"\x00")
            if self._address_auto:
                self.address = self._detect_address()
            self._initialise_panel()
            self._opened = True
            return

        if self._handle is None:
            from ..hal.mock_bus import RealBus

            self._handle = RealBus(i2c_bus=self.i2c_bus)
        if self._address_auto:
            self.address = self._detect_address()
        self._initialise_panel()
        self._opened = True
    def _detect_address(self) -> int:
        """在候选地址里探测 PCF8574；找不到就抛 :class:`DeviceInitError`。

        优化：如果总线对象自带 ``i2c_scan()``（MockBus 与 RealBus 都有），
        先用它拿一次"总线上所有在线地址"，命中候选就直接返回，少发一堆无效读。
        真实模式的 :meth:`RealBus.i2c_scan` 本身可能要遍历 0x03~0x77，所以**只在
        总线对象提供扫描能力时**才用它，否则退回逐个 read 探测。
        """
        assert self._handle is not None
        scanned = self._scan_bus()
        if scanned:
            for addr in CANDIDATE_ADDRESSES:
                if addr in scanned:
                    _log.info("LCD1602 通过总线扫描找到 PCF8574 地址 0x%02X", addr)
                    return addr
        for addr in CANDIDATE_ADDRESSES:
            try:
                self._handle.i2c_read(self.i2c_bus, addr, 1)
            except Exception as exc:  # noqa: BLE001 - 探测失败就该试下一个
                _log.debug("I2C 地址 0x%02X 无应答：%s", addr, exc)
                continue
            _log.info("LCD1602 自动探测到 PCF8574 地址 0x%02X", addr)
            return addr
        raise DeviceInitError(
            "LCD1602 自动探测失败：候选地址 "
            + "/".join(f"0x{a:02X}" for a in CANDIDATE_ADDRESSES)
            + " 均无应答。排查步骤："
            "1) `i2cdetect -y 1` 看总线上到底有什么地址（LCD 不一定是 0x27，也可能是 0x3F）；"
            "2) `sudo raspi-config` → Interface Options → I2C 是否已启用；"
            "3) SDA=物理脚 3 / SCL=物理脚 5 是否接反；"
            "4) 转接板 VCC 是否接了 5V（3.3V 时可能不响应且对比度极低）；"
            "5) 若已知地址，请显式传 address=0x?? 跳过探测"
        )

    def _scan_bus(self) -> List[int]:
        """尝试用总线对象的 ``i2c_scan()`` 拿在线地址列表；拿不到就返回空表。"""
        assert self._handle is not None
        scan = getattr(self._handle, "i2c_scan", None)
        if scan is None:
            return []
        try:
            return [int(a) for a in scan()]
        except Exception as exc:  # noqa: BLE001 - 扫描失败就退回逐个探测
            _log.debug("i2c_scan() 不可用（%s），退回逐个地址探测", exc)
            return []

    def _initialise_panel(self) -> None:
        """按数据手册写 HD44780 的 4 位模式初始化序列。

        序列（缺一不可）：``0x33 → 0x32 → 功能设置 0x28 → 显示关 0x08
        → 清屏 0x01 → 输入模式 0x06 → 显示开 0x0C``。
        """
        # 上电后芯片可能停在 8 位模式：连续 0x33/0x32（8 位模式下的软复位字节，
        # 在本驱动的 4 位编码里等价于连续发送 0x3 半字节）把它拉回已知状态。
        self._write_nibbles(0x33, rs=0)
        self._write_nibbles(0x32, rs=0)
        self._write_nibbles(CMD_FUNCTION_SET, rs=0)   # 4 位 / 2 行 / 5x8
        self._write_nibbles(CMD_DISPLAY_OFF, rs=0)    # 先关显示，避免清屏时闪白
        self._write_nibbles(CMD_CLEAR_DISPLAY, rs=0)  # 清屏 + 光标回原点
        self._write_nibbles(CMD_ENTRY_MODE, rs=0)     # 写入后光标右移
        self._write_nibbles(CMD_DISPLAY_ON, rs=0)     # 显示开、光标/闪烁关
        self._set_backlight_hw(self.backlight)
        self._lines = ("", "")  # 清屏后驱动记录的内容也要归零

    # ------------------------------------------------------------------
    # 底层写
    # ------------------------------------------------------------------

    def _write_bytes(self, payload: Sequence[int]) -> None:
        """把一串字节写到 PCF8574（真实模式走 I2C，mock 模式走 MockBus）。"""
        if self._handle is None:  # pragma: no cover - open() 已保证
            raise DeviceIOError("LCD1602 尚未初始化（总线句柄为空）")
        data = bytes(payload)
        self._handle.i2c_write(self.i2c_bus, self.address, data)

    def _write_nibbles(self, value: int, rs: int) -> None:
        """按 4 位模式写一个字节（高半字节 → 低半字节）。"""
        self._write_bytes(self._codec.encode_nibbles(value, rs))

    def _write_char(self, ch: str) -> None:
        self._write_nibbles(ord(ch), rs=1)

    def _write_command(self, cmd: int) -> None:
        self._write_nibbles(cmd, rs=0)

    # ------------------------------------------------------------------
    # 文本处理
    # ------------------------------------------------------------------

    def sanitize(self, text: str) -> str:
        """安全降级 + 按列截断 + 补空格。

        1. 非 ASCII（中文等）→ 替换成 ``non_ascii_fallback``（默认 ``?``），
           **绝不让非法字节进 I2C**，否则屏上就是一片乱点阵（花屏）；
        2. 超过 ``cols`` 的部分截断；
        3. 不足 ``cols`` 的部分用空格补齐 —— 覆盖上一屏的残留字符。
        """
        safe_chars: List[str] = []
        for ch in str(text):
            if ord(ch) < 128:
                safe_chars.append(ch)
            else:
                safe_chars.append(self.non_ascii_fallback)
                self._non_ascii_replaced += 1
        safe = "".join(safe_chars)
        if len(safe) > self.cols:
            safe = safe[: self.cols]
        return safe.ljust(self.cols)

    def _format_lines(self, lines: Sequence[str]) -> Tuple[str, str]:
        """把任意长度的行列表规范成"恰好 rows 行、每行 cols 字符"。"""
        normalised: List[str] = []
        for i in range(self.rows):
            raw = lines[i] if i < len(lines) else ""
            normalised.append(self.sanitize(raw))
        while len(normalised) < 2:
            normalised.append(" " * self.cols)
        return (normalised[0], normalised[1])

    # ------------------------------------------------------------------
    # 指令入口
    # ------------------------------------------------------------------

    def send(self, command: Any) -> None:
        """执行一条显示指令。

        Args:
            command: :class:`~health_monitor.hal.models.DisplayCommand`。

        Raises:
            DeviceNotReady: 未 ``open()``。
            UnsupportedError: 收到本驱动不认识的指令类型（**绝不静默忽略**）。
            AlarmDispatchError: I2C 写入失败（转接板掉线 / 总线被拉死）。
        """
        self._require_open()
        if not isinstance(command, DisplayCommand):
            exc = UnsupportedError(
                f"LCD1602 不支持指令类型 {type(command).__name__}；"
                "它只接受 DisplayCommand(lines=(第一行, 第二行), page=n)"
            )
            self._note_fault(exc)
            raise exc
        try:
            self._render(command)
        except Exception as exc:  # noqa: BLE001 - 统一翻译成报警下发失败
            self._note_fault(exc)
            raise AlarmDispatchError(
                f"LCD1602（I2C {self.i2c_bus} 地址 0x{self.address:02X}）写入失败：{exc}。"
                "排查：转接板是否掉电、杜邦线是否松动、`i2cdetect -y 1` 地址是否还在"
            ) from exc
        self._note_ok()

    def _render(self, command: DisplayCommand) -> None:
        """真正把两行文本写到屏幕（按 rows/cols 定位）。"""
        lines = self._format_lines(tuple(command.lines))
        for row in range(self.rows):
            self._write_command(CMD_SET_DDRAM | ROW_OFFSETS[row])
            for ch in lines[row]:
                self._write_char(ch)
        self._lines = lines  # 只有整屏都写成功才更新"我记录的内容"
        self._page = int(command.page)

    # ------------------------------------------------------------------
    # 便捷方法
    # ------------------------------------------------------------------

    def clear(self) -> None:
        """清屏并把光标复位（幂等，可重复调用）。"""
        self._require_open()
        try:
            self._write_command(CMD_CLEAR_DISPLAY)
            self._write_command(CMD_RETURN_HOME)
        except Exception as exc:  # noqa: BLE001
            self._note_fault(exc)
            raise AlarmDispatchError(f"LCD1602 清屏失败：{exc}") from exc
        self._lines = ("", "")
        self._note_ok()

    def set_backlight(self, on: bool) -> None:
        """开关背光（只改 PCF8574 的 P3，不动屏幕内容）。"""
        self._require_open()
        try:
            self._set_backlight_hw(bool(on))
        except Exception as exc:  # noqa: BLE001
            self._note_fault(exc)
            raise AlarmDispatchError(f"LCD1602 背光设置失败：{exc}") from exc
        self.backlight = bool(on)
        self._note_ok()

    def _set_backlight_hw(self, on: bool) -> None:
        self._codec.backlight = bool(on)
        # 背光位在 PCF8574 的 P3，写一次空闲字节即可（EN 保持低，不误锁存）
        self._write_bytes([self._codec.idle_byte])

    def read(self) -> DisplayStatus:
        """回读**驱动记录的**屏幕内容。

        ⚠️ 诚实声明：PCF8574 只是 IO 扩展，**读不回** HD44780 的 DDRAM，
        所以这里返回的是"驱动自己最后一次成功写入的内容"，而不是硬件回读值；
        驱动不知道时（从未写过）返回空串，**绝不编造**。

        Raises:
            DeviceNotReady: 未 ``open()``。
        """
        self._require_open()
        self._note_ok()
        return DisplayStatus(device=self.name, lines=self._lines, page=self._page)

    def close(self) -> None:
        """释放总线（幂等，不抛异常）。

        真实模式下会关闭底层 :class:`RealBus`；若句柄是外部注入的（MockBus 或
        别人共享的 RealBus），**不**替对方关闭，避免把 MAX30102 的总线也关掉。
        """
        handle, self._handle = self._handle, None
        if handle is not None and not self.mock and hasattr(handle, "close"):
            try:
                handle.close()
            except Exception:  # noqa: BLE001 - 关闭失败不应影响收尾
                pass
        self._lines = ("", "")
        self._page = 0
        self._opened = False

    # ------------------------------------------------------------------
    # 状态 / 文档
    # ------------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        """扩展基类状态：把 I2C 地址、当前屏幕内容、中文降级次数暴露出来。"""
        info = super().status()
        info.update(
            {
                "i2c_bus": self.i2c_bus,
                "address": f"0x{self.address:02X}" if self.address else "未探测",
                "address_auto": self._address_auto,
                "cols": self.cols,
                "rows": self.rows,
                "backlight": self.backlight,
                "lines": list(self._lines),
                "page": self._page,
                "non_ascii_replaced": self._non_ascii_replaced,
            }
        )
        return info

    def describe(self) -> Dict[str, Any]:
        """接线说明（会被 ``docs`` 生成脚本读取）。"""
        info = super().describe()
        info.update(
            {
                "bus": f"I2C-{self.i2c_bus}（与 MAX30102 共用；地址不同互不影响）",
                "pins": {
                    "vcc": "5V（物理脚 2 或 4）——⚠️ 3.3V 时对比度极低、可能不响应",
                    "gnd": "GND（物理脚 6）",
                    "sda": "GPIO2 / SDA1（物理脚 3）",
                    "scl": "GPIO3 / SCL1（物理脚 5）",
                },
                "address": f"0x{self.address:02X}" if self.address else "自动探测中",
                "candidates": [f"0x{a:02X}" for a in CANDIDATE_ADDRESSES],
                "notes": (
                    "PCF8574 背包，4 位模式；⚠️ 屏幕是 ASCII 字库，"
                    f"**中文显示不了**（非 ASCII 会降级为 {self.non_ascii_fallback!r}）；"
                    "界面文案请用英文/拼音缩写（HR 72 bpm / SpO2 98% / ALARM: HR HIGH），"
                    "中文交给手机端与语音播报；每行按 cols 截断并补空格，避免残留字符"
                ),
            }
        )
        return info

    def self_check(self) -> Dict[str, Any]:
        """自检：mock 模式看是否已初始化；真实模式探一次 I2C 地址是否还在。"""
        if not self._opened:
            return {"ok": False, "detail": "尚未 open()"}
        if self.mock:
            return {"ok": True, "detail": f"mock 模式；模拟地址 0x{self.address:02X}"}
        assert self._handle is not None
        try:
            self._handle.i2c_read(self.i2c_bus, self.address, 1)
        except Exception as exc:  # noqa: BLE001 - 自检要能捕获一切并如实上报
            return {
                "ok": False,
                "detail": f"I2C 地址 0x{self.address:02X} 无应答：{exc}；"
                "请跑 `i2cdetect -y 1` 确认转接板还在总线上",
            }
        return {"ok": True, "detail": f"I2C 地址 0x{self.address:02X} 应答正常"}


#: 保留别名，方便从本模块直接导入（契约要求的是 ``Lcd1602``）
Lcd1602Driver = Lcd1602

__all__ = ["Lcd1602", "Lcd1602Driver", "Pcf8574LcdCodec", "CANDIDATE_ADDRESSES"]
