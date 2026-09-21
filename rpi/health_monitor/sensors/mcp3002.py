"""MCP3002 —— SPI 接口的 10 位逐次逼近型 ADC（2 个模拟输入通道）。

模块用途
--------
树莓派**没有任何模拟输入引脚**，凡是"输出模拟电压"的器件（TMP36 温度、
光敏电阻、电位器等）都必须先经 ADC 数字化。本驱动给出 MCP3002 的完整链路::

    MCP3002（SPI0）→ 10 位原始值 0~1023 → 电压(V) → 交给具体传感器的换算函数

TMP36 驱动（``sensors/tmp36.py``）**组合**本驱动，不重复实现 SPI 时序。

接线表（MCP3002 引脚 ↔ 树莓派 40-pin 物理脚号）
-----------------------------------------------
=========================  ==========================  ===================================
MCP3002 引脚               接到                        树莓派 40-pin 物理脚
=========================  ==========================  ===================================
8  VDD / VREF             3.3V                        脚 1（或脚 17）
3  VSS                    GND                         脚 6（或 9/14/20/25/30/34/39）
4  CLK                    SPI0_SCLK（GPIO11）         脚 23
5  D_OUT                  SPI0_MISO（GPIO9）          脚 21
6  D_IN                   SPI0_MOSI（GPIO10）         脚 19
7  CS/SHDN                SPI0_CE0（GPIO8）           脚 24（**也可以换成任意 GPIO 软件控制**）
1  CH0                    模拟输入 0（TMP36 的 VOUT）  —
2  CH1                    模拟输入 1（备用：电位器/光敏）—
=========================  ==========================  ===================================

说明：MCP3002 是 2 通道器件，VDD/VREF 只能接 3.3V（接 5V 会把模拟输入量程提到 5V，
而树莓派 GPIO 与大多数传感器的模拟输出都按 3.3V 设计，读数会不准且有损坏风险）。

设计要点
--------
1. **纯逻辑与总线分离**：``raw_to_voltage`` / ``voltage_to_celsius`` 是模块级纯函数，
   不碰 SPI，单测直接喂假数据断言（报告里的换算推导也用它们）。
2. **mock 绝不碰硬件**：``mock=True`` 时 ``open()`` 只置位；数据来源有三种
   （见 :attr:`Mcp3002.mock_source`）：外部注入 → MockBus SPI 钩子 → 合成波形。
   第三种保证"没有树莓派也能演示"，第二种让单测能验证真实时序字节。
3. **SPI 全双工等长**：MockBus 的 ``spi_xfer`` 钩子必须返回与输入**等长**的字节，
   长度不符会被 :class:`~health_monitor.hal.mock_bus.MockBus` 直接判为钩子写错。
4. **失败留痕**：总线异常一律 ``_note_fault`` 后抛 ``DeviceIOError``；
   参数不合法（通道/参考电压）抛 ``ConfigError``（启动期错误，不该重试）。

负责人占位
----------
负责人：``<填写姓名 / 学号>``；验收：``python -m pytest tests/sensors/test_mcp3002.py -q``。
"""

from __future__ import annotations

import math
from typing import Any, Dict, Optional

from ..hal.device import Device
from ..hal.exceptions import (
    ConfigError,
    DeviceInitError,
    DeviceIOError,
    UnsupportedError,
)
from ..hal.models import DeviceKind, PrecisionTempSample

# ==========================================================================
# 第一部分：纯逻辑（可单测，不碰任何硬件）
# ==========================================================================

#: 10 位 ADC 的满量程原始值（0~1023，不是 1024；这样 1023 正好对应满量程电压）
ADC_MAX_RAW = 1023

#: MCP3002 只有 2 个单端通道
ADC_CHANNELS = (0, 1)

#: MCP3002 在 SPI 模式 0 下的控制字节公共部分（单端、MSB 先行，见 :meth:`Mcp3002._build_command`）
CMD_SINGLE_ENDED = 0x68  # 0b0110_1000 = 前置0 + START1 + SGL/DIFF1 + ODD/SIGN0 + MSBF1


def raw_to_voltage(raw: int, vref: float = 3.3) -> float:
    """ADC 原始值 → 电压（V）。**纯函数**，答辩报告里的换算推导就用它。

    公式::

        V = raw / 1023 * vref

    10 位 ADC：``raw=0`` → 0V，``raw=1023`` → ``vref``（默认 3.3V）。

    Args:
        raw: ADC 原始值，必须落在 0~1023。
        vref: ADC 参考电压，树莓派上就是 3.3V。

    Raises:
        ConfigError: ``raw`` 越界（负数 / >1023）或 ``vref`` 不是正数。
            越界是"接线或代码写错了"，**必须响亮地失败**（见 HAL 纪律：不静默）。

    已知值（单测已断言）::

        raw_to_voltage(0)    == 0.0
        raw_to_voltage(1023) == 3.3
        raw_to_voltage(465)  == 1.5     # 465 = 1.5/3.3*1023，正好整除，便于出题
    """
    if vref <= 0:
        raise ConfigError(f"ADC 参考电压 vref 必须为正数，收到 {vref!r}")
    if not 0 <= raw <= ADC_MAX_RAW:
        raise ConfigError(
            f"MCP3002 原始值必须在 0~{ADC_MAX_RAW} 之间，收到 {raw!r}"
            "（负数/超量程说明通道或换算写错了）"
        )
    return raw / ADC_MAX_RAW * vref


def voltage_to_celsius(v: float, v25: float = 0.75, mv_per_c: float = 10.0) -> float:
    """模拟电压 → 摄氏温度。**纯函数**，TMP35/TMP36/TMP37 家族共用。

    公式（TMP36 数据手册，10mV/°C、25°C 时 750mV）::

        T(°C) = (V - V25) * 1000 / mv_per_c + 25
              = (V - 0.75) * 100 + 25           # mv_per_c = 10mV/°C 时的常用写法

    Args:
        v: 传感器输出电压（V）。**不做量程校验**——越界判断属于驱动职责
            （见 :class:`~health_monitor.sensors.tmp36.Tmp36`，它按 -40~125°C 判无效）。
        v25: 25°C 时的输出电压（V），默认 0.75。
        mv_per_c: 灵敏度（mV/°C），默认 10.0。

    Returns:
        摄氏温度（float，可能为负）。

    已知值（单测已断言）：0.75V → 25.0°C；1.00V → 50.0°C；0.50V → 0.0°C；0.10V → -40.0°C。
    """
    if mv_per_c <= 0:
        raise ConfigError(f"灵敏度 mv_per_c 必须为正数，收到 {mv_per_c!r}")
    return (v - v25) * 1000.0 / mv_per_c + 25.0


# ==========================================================================
# 第二部分：驱动（硬件访问层）
# ==========================================================================


class Mcp3002(Device):
    """MCP3002（SPI 10 位 ADC，2 通道）。

    真实读取时序（单端模式，SPI 模式 0 / MSB 先行）::

        1) CS 拉低（硬件片选由 SPI0_CE0 自动完成；配了 ``cs_pin`` 则用软件片选）
        2) 发 3 字节：``[控制字节, 0x00, 0x00]``（见 :meth:`_build_command`）
        3) 同时回读 3 字节，10 位结果在**前 2 个字节**里（见 :meth:`_decode`）
        4) CS 拉高

    ⚠️ 第 3 个字节只是把 SCLK 补满 24 拍（MCP3002 在 11 拍后就输出 0），
    不参与解码；写 3 字节是为了与任务书一致，且方便用现成的 ``spi.xfer2`` 一把收发。

    Args:
        spi_bus: SPI 总线号，树莓派默认 0（对应 ``/dev/spidev0.x``）。
        spi_device: 片选号 0 或 1（对应 SPI0_CE0=物理脚 24 / SPI0_CE1=物理脚 26）。
        vref: 参考电压（V），默认 3.3。
        channel: 默认读取的通道（0 或 1）。
        cs_pin: 软件片选用 BCM 编号引脚；``None`` 表示用硬件片选（SPI0_CE0/CE1）。
        bus: 总线对象（真实为 ``RealBus``；mock/测试为 ``MockBus``；``None`` 时真实模式自行创建）。
        mock: 模拟模式。为 ``True`` 时**绝不触碰** ``/dev/spidev*``。
        name: 实例名（与 ``config/devices.json`` 的键一致）。
    """

    KIND = DeviceKind.PRECISION
    NAME = "mcp3002"

    def __init__(
        self,
        spi_bus: int = 0,
        spi_device: int = 0,
        vref: float = 3.3,
        channel: int = 0,
        cs_pin: Optional[int] = None,
        bus: Any = None,
        mock: bool = False,
        name: str = "",
    ) -> None:
        super().__init__(bus=bus, mock=mock, name=name)
        if spi_bus < 0:
            raise ConfigError(f"spi_bus 不能为负：{spi_bus!r}")
        if spi_device not in (0, 1):
            raise ConfigError(
                f"spi_device 只能是 0 或 1（SPI0_CE0=物理脚 24 / SPI0_CE1=物理脚 26），收到 {spi_device!r}"
            )
        if vref <= 0:
            raise ConfigError(f"vref 必须为正数（树莓派上应取 3.3），收到 {vref!r}")
        if channel not in ADC_CHANNELS:
            raise ConfigError(
                f"MCP3002 只有 2 个单端通道：0=CH0（MCP3002 脚 1）、1=CH1（脚 2），"
                f"收到 channel={channel!r}"
            )
        self.spi_bus = int(spi_bus)
        self.spi_device = int(spi_device)
        self.vref = float(vref)
        self.channel = int(channel)
        self.cs_pin = None if cs_pin is None else int(cs_pin)
        self._cs: Any = None                 # 软件片选 GPIO 对象（真实模式才有）
        self._mock_raw: Optional[int] = None  # 外部注入的原始值（仅 mock）
        self._mock_tick = 0                   # 合成波形的相位计数（确定性的，可复现）

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def open(self) -> None:
        """打开 SPI（mock 模式下什么都不做，只置位）。"""
        if self._opened:
            return
        if self.mock:
            # 纪律：mock 模式绝不打开 /dev/spidev*、绝不创建 GPIO
            self._opened = True
            return

        if self.bus is None:
            from ..hal import RealBus  # 延迟导入：PC 上不 import 真实总线也无所谓

            self.bus = RealBus()

        # 软件片选：先建 GPIO 并保持高电平（空闲时 CS 必须为高）
        if self.cs_pin is not None:
            try:
                from gpiozero import DigitalOutputDevice  # type: ignore import-not-found
            except ImportError as exc:
                raise DeviceInitError(
                    f"软件片选需要 gpiozero（GPIO{self.cs_pin}）：{exc}。"
                    "树莓派上执行 `sudo apt install -y python3-gpiozero`；"
                    "或改用硬件片选（cs_pin=None，CS 接物理脚 24）；PC 上请用 mock=True"
                ) from exc
            try:
                self._cs = DigitalOutputDevice(self.cs_pin, initial_value=True, active_high=True)
            except Exception as exc:  # noqa: BLE001 - gpiozero 会抛各种运行时错误
                raise DeviceInitError(
                    f"软件片选 GPIO{self.cs_pin} 初始化失败：{exc}。"
                    "请确认该引脚未被占用、接线牢固；树莓派 5 上请用 gpiozero+lgpio（RPi.GPIO 不可用）"
                ) from exc

        # 探一次总线：尽早暴露"SPI 没开/权限不足/设备节点不存在"
        try:
            self.bus.spi_xfer(self.spi_bus, self.spi_device, bytes(3))
        except Exception as exc:  # noqa: BLE001 - 统一翻译成 DeviceInitError 并给排查线索
            raise DeviceInitError(
                f"SPI{self.spi_bus}.{self.spi_device} 打不开（MCP3002 接线检查）：{exc}。"
                "排查顺序：1) `sudo raspi-config` → Interface Options → SPI → Enable；"
                "2) 确认 /dev/spidev0.0 或 /dev/spidev0.1 存在（`ls -l /dev/spidev*`）；"
                f"3) 当前用户需在 spi 组（`sudo adduser $USER spi` 后重新登录）；"
                "4) 接线核对：CLK=脚23、D_OUT(MISO)=脚21、D_IN(MOSI)=脚19、CS=脚24、VDD=脚1(3.3V)、VSS=脚6"
            ) from exc
        self._opened = True

    def read_raw(self, channel: int) -> int:
        """读取指定通道的 10 位原始值（0~1023）。

        Args:
            channel: 0=CH0，1=CH1。

        Raises:
            DeviceNotReady: 未 ``open()``。
            ConfigError: 通道号不是 0/1。
            DeviceIOError: SPI 传输失败（真实模式）。
        """
        self._require_open()
        ch = self._check_channel(channel)

        try:
            if self.mock:
                raw = self._mock_read(ch)
            else:
                raw = self._spi_read(ch)
        except Exception as exc:  # noqa: BLE001 - 统一翻译 + 留痕（绝不静默）
            # 注意：mock 模式也要走这里——测试会用 MockBus 注入故障，
            # "mock 下不计数"会让故障统计失真。
            self._note_fault(exc)
            raise DeviceIOError(
                f"MCP3002（SPI{self.spi_bus}.{self.spi_device} 通道{ch}）读取失败：{exc}。"
                "常见原因：排线松动、CS 接错、ADC 未供电（VDD 必须接 3.3V）"
            ) from exc
        self._note_ok()
        return raw

    def read(self) -> PrecisionTempSample:
        """读一次默认通道，返回 :class:`PrecisionTempSample`。

        ``raw_adc`` 与 ``voltage_v`` 都有值，``temperature_c`` **为 None**：
        单读 ADC 并不知道 CH0 上挂的是什么传感器（温度由 TMP36 驱动负责换算）。
        """
        self._require_open()
        raw = self.read_raw(self.channel)
        return PrecisionTempSample(
            device=self.name,
            raw_adc=raw,
            voltage_v=raw_to_voltage(raw, self.vref),
            temperature_c=None,
        )

    def close(self) -> None:
        """释放软件片选 GPIO（幂等、不抛异常）。SPI 设备由 :class:`RealBus` 统一关闭。"""
        if self._cs is not None:
            try:
                self._cs.close()
            except Exception:  # noqa: BLE001 - 关闭失败不应影响收尾
                pass
            self._cs = None
        self._opened = False

    # ------------------------------------------------------------------
    # 纯逻辑/内部：通道校验、命令拼装、回读解码
    # ------------------------------------------------------------------

    @staticmethod
    def _check_channel(channel: int) -> int:
        """校验通道号并转成 int。"""
        try:
            ch = int(channel)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"通道号必须是整数 0 或 1，收到 {channel!r}") from exc
        if ch not in ADC_CHANNELS:
            raise ConfigError(
                f"MCP3002 只有通道 0（CH0）和通道 1（CH1），收到 channel={ch}"
            )
        return ch

    def _build_command(self, channel: int) -> bytes:
        """按数据手册拼出 3 字节发送内容（MSB 先行，SPI 模式 0）。

        MCP3002 单端模式的输入字节位定义::

            位号     7      6      5        4       3      2 1 0
                   前置   START  SGL/DIFF ODD/SIGN MSBF   无关位
                     0      1       1     通道号     1     x x x

        - 第 7 位固定 0：数据手册要求 START 位之前先来一个时钟的前置 0；
        - START = 1：启动一次转换；
        - SGL/DIFF = 1：单端模式（CH0/CH1 相对 GND，而不是差分）；
        - ODD/SIGN = 通道号：单端模式下 0→CH0、1→CH1；
        - MSBF = 1：结果高位先行（MSB first），这样 10 位结果落在前两个字节里。

        于是：通道 0 → ``0b0110_1000`` = ``0x68``；通道 1 → ``0b0111_1000`` = ``0x78``。

        ⚠️ 与任务书草稿的区别：草稿写的是 ``0x01 | ((channel & 1) << 7) | 0x60``，
        那是把 START 放在最低位、通道放在最高位的另一种记法，与 MCP3002 数据手册
        （以及 gpiozero 的 ``MCP3xx2`` 实现）的位序不符。本驱动**以数据手册为准**：
        第 6 位才是 START，通道在第 4 位。若真机上读数恒为 0/1023，先核对这里。

        返回 3 字节：第 1 字节是控制字，后 2 字节是 0 填充（只为把 SCLK 补满 24 拍）。
        """
        cmd = 0
        cmd |= 1 << 6              # START = 1
        cmd |= 1 << 5              # SGL/DIFF = 1（单端）
        cmd |= (channel & 1) << 4  # ODD/SIGN = 通道号
        cmd |= 1 << 3              # MSBF = 1（高位先行）
        return bytes([cmd, 0x00, 0x00])

    @staticmethod
    def _decode(reply: bytes) -> int:
        """从回读字节里取出 10 位结果。

        回读格式（MSBF=1）::

            字节  0        1
            Rx   xxxxx0RR RRRRRRRR
                 ^^^^^   ^^
                 无关位   空位(0) 后跟 R9 R8，第 2 字节是 R7..R0

        即：``raw = ((reply[0] & 0x03) << 8) | reply[1]``。
        """
        if len(reply) < 2:
            raise DeviceIOError(
                f"MCP3002 回读字节不足：{bytes(reply)!r}（至少需要 2 字节才能拼出 10 位结果）"
            )
        return ((reply[0] & 0x03) << 8) | reply[1]

    def _spi_read(self, channel: int) -> int:
        """一次完整的 SPI 事务：片选 → 收发 → 片选释放 → 解码。"""
        payload = self._build_command(channel)
        self._cs_assert()
        try:
            reply = self.bus.spi_xfer(self.spi_bus, self.spi_device, payload)
        finally:
            self._cs_release()
        return self._decode(bytes(reply))

    def _cs_assert(self) -> None:
        """拉低片选（硬件片选时由 SPI 控制器自动完成，这里只处理软件片选）。"""
        if self._cs is None:
            return
        if getattr(self._cs, "active_high", True):
            self._cs.off()
        else:
            self._cs.on()

    def _cs_release(self) -> None:
        """释放片选（拉高＝空闲态）。"""
        if self._cs is None:
            return
        if getattr(self._cs, "active_high", True):
            self._cs.on()
        else:
            self._cs.off()

    # ------------------------------------------------------------------
    # mock 数据源
    # ------------------------------------------------------------------

    def _has_spi_hook(self) -> bool:
        """探测当前 MockBus 是否为本总线注册了 SPI 钩子（只读探测，不修改总线）。

        为什么需要它：mock 模式默认给"合成波形"（保证无硬件也能演示），
        但测试要验证真实时序/解码时，会注册钩子；此时应当走**真实的那段代码**。
        """
        hooks = getattr(self.bus, "_spi_hooks", None)
        return isinstance(hooks, dict) and self.spi_bus in hooks

    @property
    def mock_source(self) -> str:
        """mock 模式下的数据来源：``inject`` / ``hook`` / ``synthetic``（真实模式为 ``spi``）。"""
        if not self.mock:
            return "spi"
        if self._mock_raw is not None:
            return "inject"
        if self._has_spi_hook():
            return "hook"
        return "synthetic"

    def _synthetic_raw(self) -> int:
        """合成波形：围绕中值缓慢起伏的 0~1023 值。

        用确定性正弦（而不是随机数）：演示曲线平滑好看，测试也能复现。
        """
        self._mock_tick += 1
        raw = 511.5 + 300.0 * math.sin(self._mock_tick * 0.15)
        return int(round(min(float(ADC_MAX_RAW), max(0.0, raw))))

    def _mock_read(self, channel: int) -> int:
        """mock 模式下取一个原始值：注入 > SPI 钩子（真实时序）> 合成波形。"""
        if self._mock_raw is not None:
            return self._mock_raw
        if self._has_spi_hook():
            return self._spi_read(channel)
        return self._synthetic_raw()

    def inject_raw(self, raw: int) -> None:
        """**仅供 mock / 测试**：直接指定下一次（以及之后）读到的原始值。

        用来验证"原始值 → 电压 → 温度"的边界，例如 ``inject_raw(0)`` 模拟传感器掉线。
        """
        if not self.mock:
            raise UnsupportedError(
                "inject_raw() 只能在 mock=True 时使用（真实模式不允许伪造采样值）"
            )
        if not 0 <= int(raw) <= ADC_MAX_RAW:
            raise ConfigError(
                f"注入的原始值必须在 0~{ADC_MAX_RAW} 之间，收到 {raw!r}"
                "（想模拟「输入超压」请注入 1023，再用电压判断）"
            )
        self._mock_raw = int(raw)

    def inject_voltage(self, voltage_v: float) -> None:
        """**仅供 mock / 测试**：按电压注入（内部换算成原始值，量化误差与真机一致）。"""
        if not self.mock:
            raise UnsupportedError(
                "inject_voltage() 只能在 mock=True 时使用（真实模式不允许伪造采样值）"
            )
        if not 0.0 <= float(voltage_v) <= self.vref:
            raise ConfigError(
                f"注入电压必须在 0~{self.vref}V 之间，收到 {voltage_v!r}"
                "（超过 vref 在真机上就是「输入过压」，ADC 会读出 1023）"
            )
        self.inject_raw(int(round(float(voltage_v) / self.vref * ADC_MAX_RAW)))

    def clear_inject(self) -> None:
        """**仅供 mock / 测试**：撤销注入，回到钩子/合成波形。"""
        if not self.mock:
            raise UnsupportedError("clear_inject() 只能在 mock=True 时使用")
        self._mock_raw = None

    # ------------------------------------------------------------------
    # 说明与状态
    # ------------------------------------------------------------------

    def describe(self) -> Dict[str, Any]:
        """接线说明（会被 ``docs`` 生成脚本读取）。"""
        cs_text = (
            f"软件片选 GPIO{self.cs_pin}（任意 GPIO 均可，例 GPIO25=物理脚 22）"
            if self.cs_pin is not None
            else "SPI0_CE0（GPIO8，物理脚 24）"
        )
        return {
            "name": self.name,
            "kind": self.KIND.value,
            "mock": self.mock,
            "bus": f"SPI{self.spi_bus}.{self.spi_device}（SPI0，模式 0，MSB 先行）",
            "pins": {
                "vdd_vref": "MCP3002 脚 8（VDD/VREF）→ 3.3V，树莓派物理脚 1（或脚 17）",
                "vss": "MCP3002 脚 3（VSS）→ GND，树莓派物理脚 6",
                "clk": "MCP3002 脚 4（CLK）→ SPI0_SCLK/GPIO11，树莓派物理脚 23",
                "d_out_miso": "MCP3002 脚 5（D_OUT）→ SPI0_MISO/GPIO9，树莓派物理脚 21",
                "d_in_mosi": "MCP3002 脚 6（D_IN）→ SPI0_MOSI/GPIO10，树莓派物理脚 19",
                "cs": f"MCP3002 脚 7（CS/SHDN）→ {cs_text}",
                "ch0": "MCP3002 脚 1（CH0）→ 模拟输入 0（TMP36 的 VOUT）",
                "ch1": "MCP3002 脚 2（CH1）→ 模拟输入 1（备用：电位器/光敏电阻）",
            },
            "notes": (
                f"10 位 ADC，量程 0~{ADC_MAX_RAW} 对应 0~{self.vref}V（V = raw/1023*vref）；"
                "默认通道 CH0；VDD/VREF 只能接 3.3V（接 5V 会改变量程且有损坏风险）。"
                "**CS 也可以换成任意 GPIO 软件控制**：构造时传 cs_pin=<BCM 编号>，"
                "此时 CS 不必接物理脚 24，SPI0_CE0 悬空即可（硬件片选会自动翻转但没接线，无影响）。"
            ),
        }


__all__ = ["Mcp3002", "raw_to_voltage", "voltage_to_celsius", "ADC_MAX_RAW"]
