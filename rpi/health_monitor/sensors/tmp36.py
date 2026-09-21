"""TMP36 —— 模拟输出的精密温度传感器（**必须经 MCP3002 读取**）。

⚠️ 关键事实（答辩必答）
----------------------
**TMP36GT9Z 是模拟输出传感器，不是 I2C/1-Wire 器件。** 树莓派 40-pin 上
**没有任何模拟输入引脚**，所以它**必须**先接 MCP3002 这类 ADC 才能读数::

    TMP36 VOUT ──→ MCP3002 CH0 ──SPI0──→ 原始值0~1023 ──→ 电压V ──→ 温度°C

本驱动**组合** :class:`~health_monitor.sensors.mcp3002.Mcp3002` 完成 SPI 读取，
不复制任何 SPI 时序代码（全项目只有 mcp3002.py 一处 SPI 时序）。

接线表（TMP36 ↔ MCP3002 ↔ 树莓派 40-pin 物理脚号）
--------------------------------------------------
===================  ================================  ===================================
TMP36（TO-92）引脚    接到                              树莓派 40-pin 物理脚
===================  ================================  ===================================
1  +VS（供电）        3.3V                              **脚 1**（或脚 17）
2  VOUT（模拟输出）   MCP3002 脚 1（CH0）               —（模拟信号**不进**树莓派 GPIO）
3  GND                与 MCP3002 共地                  **脚 6**（或脚 9/14/20/25/30/34/39）
（推荐）0.1µF 电容    跨接 +VS 与 GND                  数据手册推荐，抑制电源噪声
===================  ================================  ===================================

MCP3002 侧（同一根 SPI 总线）::

    VDD/VREF=树莓派 3.3V（脚 1）  VSS=GND（脚 6）
    CLK=脚 23（GPIO11）  D_OUT/MISO=脚 21（GPIO9）  D_IN/MOSI=脚 19（GPIO10）  CS=脚 24（GPIO8）

⚠️ **绝不能用 5V 给 TMP36 供电**：5V 供电时它的输出在 25°C 就是 750mV、125°C 时可达 1.75V，
看似没问题，但一旦接错/过温，输出可能超过 ADC 的 3.3V 参考电压，读数直接顶到 1023，
既得不到正确温度，又可能损坏 ADC 输入。**请统一用 3.3V**。

器件特性
--------
- 灵敏度 ``10 mV/°C``，``25°C → 750 mV``，量程 ``-40 ~ 125°C``，精度 ±1°C（典型）。
- 换算（纯函数 :func:`voltage_to_celsius`）::

      T(°C) = (V - 0.75) * 100 + 25

  反推电压则要先过 ADC：``V = raw / 1023 * 3.3``。10 位 ADC 的 1 LSB ≈ 3.226mV ≈ 0.32°C，
  这是本方案的**理论分辨率下限**（报告里可以算这一条）。

标定方法（用两三个已知温度点求 calibration_offset_c）
---------------------------------------------------
1. 备好两三个**已知温度点**：冰水混合物 0°C、室温（用另一支已校准温度计读）、沸水 ~100°C（视气压）；
2. 每个点同时记录驱动读数 ``T_read`` 与参考温度 ``T_ref``，求偏差 ``Δ = T_ref - T_read``；
3. 取各点 Δ 的平均值作为 ``calibration_offset_c``（三点 Δ 很接近 ⇒ 线性度好，只修零点即可）；
4. 写入 ``config/devices.json``::

       {"tmp36": {"driver": "tmp36", "enabled": true,
                  "params": {"spi_bus": 0, "spi_device": 0, "channel": 0,
                             "calibration_offset_c": -0.6}}}

5. 复核一次；若三点 Δ 差异 >1°C，说明是参考电压/线性度问题，
   优先查 MCP3002 的 VDD 是否稳定 3.3V（加 0.1µF 去耦、别与其他大电流器件共用排针），
   必要时微调 ``vref``，而不是硬掰 offset。

设计要点
--------
1. **组合而非复制**：``self.adc`` 是 :class:`Mcp3002` 实例，构造参数直接透传。
2. **越界即报**：温度落在 -40~125°C 之外判为坏点，抛
   :class:`~health_monitor.hal.exceptions.DataInvalidError`（并在 ``status()`` 里留痕），
   因为这通常意味着"传感器没插好/接错通道"，静默返回 0 这种值是严禁的。
3. **mock 走完整换算链路**：mock 时驱动把合成温度的等效电压注入内层 ADC，
   于是 mock 也真实地经历 ``电压→raw（10 位量化）→电压→温度``，不是"直接编个温度"。

负责人占位
----------
负责人：``<填写姓名 / 学号>``；验收：``python -m pytest tests/sensors/test_tmp36.py -q``。
"""

from __future__ import annotations

import math
from typing import Any, Dict, Optional

from ..hal.device import Device
from ..hal.exceptions import (
    ConfigError,
    DataInvalidError,
    DeviceInitError,
    UnsupportedError,
)
from ..hal.models import DeviceKind, PrecisionTempSample
from .mcp3002 import ADC_MAX_RAW, Mcp3002, raw_to_voltage

# ==========================================================================
# 第一部分：纯逻辑（可单测，不碰任何硬件）
# ==========================================================================


def voltage_to_celsius(v: float, v25: float = 0.75, mv_per_c: float = 10.0) -> float:
    """TMP36 输出电压 → 摄氏温度。**纯函数**（实现复用 mcp3002 的同一份公式）。

    公式（TMP36 数据手册）::

        T(°C) = (V - V25) * 1000 / mv_per_c + 25
              = (V - 0.75) * 100 + 25          # 10mV/°C、25°C→750mV 时的常用写法

    已知值（单测已断言）::

        voltage_to_celsius(0.75) == 25.0
        voltage_to_celsius(1.00) == 50.0
        voltage_to_celsius(0.50) ==  0.0
        voltage_to_celsius(0.10) == -40.0

    Args:
        v: 电压（V）。
        v25: 25°C 时输出（V），默认 0.75。
        mv_per_c: 灵敏度（mV/°C），默认 10.0。

    Raises:
        ConfigError: ``mv_per_c`` 不是正数。
    """
    if mv_per_c <= 0:
        raise ConfigError(f"灵敏度 mv_per_c 必须为正数，收到 {mv_per_c!r}")
    return (v - v25) * 1000.0 / mv_per_c + 25.0


def celsius_to_voltage(
    temperature_c: float, v25: float = 0.75, mv_per_c: float = 10.0
) -> float:
    """:func:`voltage_to_celsius` 的反函数（mock 注入与标定时要用，纯函数）。

    公式::

        V = V25 + (T - 25) * mv_per_c / 1000

    已知值：``celsius_to_voltage(25) == 0.75``；``celsius_to_voltage(0) == 0.5``；
    ``celsius_to_voltage(100) == 1.5``。
    """
    return v25 + (temperature_c - 25.0) * mv_per_c / 1000.0


# ==========================================================================
# 第二部分：驱动（硬件访问层）
# ==========================================================================


class Tmp36(Device):
    """TMP36 精密测温（模拟输出 + MCP3002 ADC）。

    超时/失败策略：ADC 的 SPI 失败由 :class:`Mcp3002` 抛 ``DeviceIOError``（可重试）；
    本驱动的**数据越界**抛 :class:`DataInvalidError`（判为坏点，不该盲目重试）。

    Args:
        spi_bus: SPI 总线号（默认 0，透传给内层 :class:`Mcp3002`）。
        spi_device: 片选号 0/1（默认 0）。
        channel: MCP3002 通道（默认 0 = CH0，即 TMP36 的 VOUT 接的那一路）。
        vref: ADC 参考电压（默认 3.3V，须与 MCP3002 的 VDD 一致）。
        v25: 25°C 时的输出（V），默认 0.75。
        mv_per_c: 灵敏度（mV/°C），默认 10.0。
        calibration_offset_c: 标定偏移（°C），默认 0.0，见模块文档"标定方法"。
        bus: 总线对象（透传给内层 ADC）。
        mock: 模拟模式。
        name: 实例名。
    """

    KIND = DeviceKind.PRECISION
    NAME = "tmp36"

    #: 数据手册量程（°C）。超出即判为无效数据（坏点）。
    TEMP_MIN_C = -40.0
    TEMP_MAX_C = 125.0

    #: 浮点比较容差（°C）：让"0.1V → 正好 -40.0°C"这类压线读数不被误判
    EPS_C = 1e-6

    def __init__(
        self,
        spi_bus: int = 0,
        spi_device: int = 0,
        channel: int = 0,
        vref: float = 3.3,
        v25: float = 0.75,
        mv_per_c: float = 10.0,
        calibration_offset_c: float = 0.0,
        bus: Any = None,
        mock: bool = False,
        name: str = "",
    ) -> None:
        super().__init__(bus=bus, mock=mock, name=name)
        if mv_per_c <= 0:
            raise ConfigError(f"灵敏度 mv_per_c 必须为正数，收到 {mv_per_c!r}")
        if not 0.0 < v25 < vref:
            raise ConfigError(
                f"v25 必须在 (0, vref) 之间：收到 v25={v25!r}、vref={vref!r}"
                "（TMP36 在 25°C 输出 0.75V，若 vref 只有 0.75V 说明参数配错了）"
            )
        self.spi_bus = int(spi_bus)
        self.spi_device = int(spi_device)
        self.channel = int(channel)
        self.vref = float(vref)
        self.v25 = float(v25)
        self.mv_per_c = float(mv_per_c)
        self.calibration_offset_c = float(calibration_offset_c)

        # 组合（不继承、不复制 SPI 代码）：SPI 时序的唯一实现在 mcp3002.py
        self.adc = Mcp3002(
            spi_bus=self.spi_bus,
            spi_device=self.spi_device,
            vref=self.vref,
            channel=self.channel,
            bus=bus,
            mock=mock,
            name=f"{self.name}.adc",
        )

        self._mock_tick = 0                        # 合成温度相位（确定性）
        self._mock_temp_c: Optional[float] = None  # inject_temperature 设定值
        self._mock_voltage_v: Optional[float] = None  # inject_voltage 设定值
        self._self_injected = False                # 上一次注入是否由本驱动自己发的

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def open(self) -> None:
        """打开内层 MCP3002（mock 模式下什么都不做，只置位）。"""
        if self._opened:
            return
        if self.mock:
            self.adc.open()  # mock：ADC 也只置位，绝不碰 /dev/spidev*
            self._opened = True
            return
        try:
            self.adc.open()
        except DeviceInitError as exc:
            raise DeviceInitError(
                f"TMP36 初始化失败（错误来自内层 ADC）：{exc}。"
                "TMP36 是模拟输出器件，必须先让 MCP3002 通得上："
                "先单独跑 `python -m pytest tests/sensors/test_mcp3002.py -q`，"
                "再确认 VOUT 接的是 CH0（MCP3002 脚 1）、供电是 3.3V 而不是 5V"
            ) from exc
        self._opened = True

    def read(self) -> PrecisionTempSample:
        """读一次温度，返回 :class:`PrecisionTempSample`。

        同时填好三个字段（报告要写换算推导，缺一不可）：

        - ``raw_adc``：MCP3002 的 10 位原始值；
        - ``voltage_v``：``raw/1023*vref``；
        - ``temperature_c``：``(V-0.75)*100 + 25 + calibration_offset_c``。

        Raises:
            DeviceNotReady: 未 ``open()``。
            DeviceIOError: ADC 的 SPI 传输失败（上层可重试）。
            DataInvalidError: 换算出的温度超出 -40~125°C（判为坏点，见类文档）。
        """
        self._require_open()
        if self.mock:
            self._apply_mock_input()

        # 内层 ADC 已负责 _require_open/留痕/异常翻译
        raw = self.adc.read_raw(self.channel)
        voltage_v = raw_to_voltage(raw, self.vref)
        temperature_c = (
            voltage_to_celsius(voltage_v, self.v25, self.mv_per_c)
            + self.calibration_offset_c
        )

        if not self._in_range(temperature_c):
            exc = DataInvalidError(
                f"TMP36 读数越界：raw={raw} → {voltage_v:.4f}V → {temperature_c:.2f}°C，"
                f"超出数据手册量程 {self.TEMP_MIN_C:.0f}~{self.TEMP_MAX_C:.0f}°C。"
                "排查：1) TMP36 的 +VS 是否接 3.3V（脚 1）；2) VOUT 是否接到 MCP3002 CH0；"
                "3) 是否误接 5V 供电（会顶到 raw=1023）；"
                f"4) 通道号是否正确（当前 channel={self.channel}）"
            )
            self._note_fault(exc)
            raise exc

        self._note_ok()
        return PrecisionTempSample(
            device=self.name,
            temperature_c=temperature_c,
            raw_adc=raw,
            voltage_v=voltage_v,
        )

    def close(self) -> None:
        """释放内层 ADC（幂等、不抛异常）。"""
        self.adc.close()
        self._opened = False

    # ------------------------------------------------------------------
    # 纯逻辑辅助
    # ------------------------------------------------------------------

    def _in_range(self, temperature_c: float) -> bool:
        """温度是否落在 -40~125°C（含端点，留 1e-6 容差）。"""
        return (
            self.TEMP_MIN_C - self.EPS_C <= temperature_c <= self.TEMP_MAX_C + self.EPS_C
        )

    # ------------------------------------------------------------------
    # mock 数据源
    # ------------------------------------------------------------------

    def _synthetic_temp_c(self) -> float:
        """合成温度：36.2~36.8°C 之间的平滑正弦（确定性，无随机，方便演示与复现）。

        ⚠️ 中心值取 **36.5°C（人体体温）**而不是室温：
        本器件在本项目里贴的是**体温**通道，默认阈值区间是 35.5~37.5°C。
        如果这里合成 25°C 左右的室温，一启动就会触发"体温偏低"误报，
        演示与联调都会被这声假警报带偏（2026-09-21 实测踩到）。
        """
        self._mock_tick += 1
        return 36.5 + 0.3 * math.sin(self._mock_tick * 0.12)

    def _apply_mock_input(self) -> None:
        """mock：把"要测的温度"换算成电压注入内层 ADC，走完整的量化链路。

        尊重的优先级：外部 SPI 钩子 > 外部注入 > 本驱动的合成/设定温度。
        """
        if self.adc.mock_source == "hook":
            # 测试直接驱动了 SPI 总线（钩子），此时不要插手
            return
        if self.adc.mock_source == "inject" and not self._self_injected:
            # 外部调用过 adc.inject_raw()/tmp.inject_raw()，尊重它（用于造坏点）
            return

        if self._mock_voltage_v is not None:
            voltage_v = self._mock_voltage_v
        else:
            temperature_c = (
                self._mock_temp_c
                if self._mock_temp_c is not None
                else self._synthetic_temp_c()
            )
            voltage_v = celsius_to_voltage(temperature_c, self.v25, self.mv_per_c)
        self.adc.inject_voltage(voltage_v)
        self._self_injected = True

    def inject_temperature(self, temperature_c: float) -> None:
        """**仅供 mock / 测试**：指定要模拟的温度（驱动会换算成等效电压）。

        量程外的温度请用 :meth:`inject_raw` 造坏点：真机上 ADC 只会给出
        0~1023 的原始值，"电压超出 0~vref"这种事物理上不存在。
        """
        if not self.mock:
            raise UnsupportedError(
                "inject_temperature() 只能在 mock=True 时使用（真实模式不允许伪造温度）"
            )
        t = float(temperature_c)
        if not (self.TEMP_MIN_C <= t <= self.TEMP_MAX_C):
            raise ConfigError(
                f"注入温度必须在数据手册量程 {self.TEMP_MIN_C:.0f}~{self.TEMP_MAX_C:.0f}°C 之间，"
                f"收到 {temperature_c!r}；要模拟「传感器掉线/过压」这类坏点请用 "
                "inject_raw(0) / inject_raw(1023)"
            )
        self._mock_temp_c = t
        self._mock_voltage_v = None
        self._self_injected = False

    def inject_voltage(self, voltage_v: float) -> None:
        """**仅供 mock / 测试**：直接指定 TMP36 的输出电压（V，范围 0~vref）。"""
        if not self.mock:
            raise UnsupportedError(
                "inject_voltage() 只能在 mock=True 时使用（真实模式不允许伪造采样值）"
            )
        v = float(voltage_v)
        if not 0.0 <= v <= self.vref:
            raise ConfigError(
                f"注入电压必须在 0~{self.vref}V 之间（真机上 TMP36 输出也在这个区间），收到 {voltage_v!r}"
            )
        self._mock_voltage_v = v
        self._mock_temp_c = None
        self._self_injected = False

    def inject_raw(self, raw: int) -> None:
        """**仅供 mock / 测试**：直接指定 ADC 原始值（用来造 raw=0 / raw=1023 坏点）。"""
        if not self.mock:
            raise UnsupportedError(
                "inject_raw() 只能在 mock=True 时使用（真实模式不允许伪造采样值）"
            )
        self.adc.inject_raw(raw)
        self._mock_temp_c = None
        self._mock_voltage_v = None
        self._self_injected = False

    # ------------------------------------------------------------------
    # 说明与状态
    # ------------------------------------------------------------------

    def describe(self) -> Dict[str, Any]:
        """接线说明（会被 ``docs`` 生成脚本读取）。"""
        return {
            "name": self.name,
            "kind": self.KIND.value,
            "mock": self.mock,
            "bus": f"SPI{self.spi_bus}.{self.spi_device}（经 MCP3002，CH{self.channel}）",
            "pins": {
                "tmp36_vs": "TMP36 脚 1（+VS）→ 3.3V，树莓派物理脚 1（⚠ 绝不能接 5V）",
                "tmp36_vout": "TMP36 脚 2（VOUT）→ MCP3002 脚 1（CH0），不直接进树莓派 GPIO",
                "tmp36_gnd": "TMP36 脚 3（GND）→ GND，树莓派物理脚 6（与 MCP3002 共地）",
                "adc_spi": "MCP3002：CLK=脚 23、D_OUT=脚 21、D_IN=脚 19、CS=脚 24、VDD/VREF=脚 1(3.3V)、VSS=脚 6",
                "bypass_cap": "0.1µF 陶瓷电容跨接 TMP36 的 +VS 与 GND（数据手册推荐）",
            },
            "notes": (
                f"模拟输出器件，**必须经 MCP3002 ADC**（树莓派无模拟引脚）；"
                f"换算：raw/1023*{self.vref}V → (V-{self.v25})*1000/{self.mv_per_c}+25 °C，"
                f"再叠加标定偏移 {self.calibration_offset_c:+.2f}°C；"
                f"量程 {self.TEMP_MIN_C:.0f}~{self.TEMP_MAX_C:.0f}°C，越界判坏点；"
                "1 LSB ≈ 3.23mV ≈ 0.32°C（10 位 ADC 的分辨率下限）；"
                "标定：冰水 0°C / 室温 / 沸水 ~100°C 各点求 Δ=参考-读数，取平均写入 calibration_offset_c"
            ),
        }


__all__ = ["Tmp36", "voltage_to_celsius", "celsius_to_voltage", "ADC_MAX_RAW"]
