"""多色 LED 状态指示驱动（绿 / 黄 / 红三色，可扩展蓝色）。

模块用途
--------
用**颜色**表达系统状态，是老人与家属"一眼就懂"的本地提示：

============  ==================  ============================================
颜色           含义（业务层约定）  典型场景
============  ==================  ============================================
``green``      正常               一切正常 / 已解除报警
``yellow``     注意               数值轻度异常、传感器质量差
``red``        报警               心率血氧越界、呼救按钮按下
``blue``       信息 / 配网中       系统启动、蓝牙配对等待
``off``        全灭               静音模式、夜间免打扰
============  ==================  ============================================

接线表（LED ↔ 树莓派 40-pin 物理脚号）
---------------------------------------
===================  ==========================  ======================================
LED 颜色              树莓派 40-pin              限流电阻
===================  ==========================  ======================================
绿（green）           GPIO22（物理脚 15）         220Ω~1kΩ 串在信号线上
黄（yellow）          GPIO23（物理脚 16）         220Ω~1kΩ
红（red）             GPIO24（物理脚 18）         220Ω~1kΩ
共阴（−）             GND（物理脚 6/9/14）        共地
===================  ==========================  ======================================

设计要点（为什么这么写）
------------------------
1. **任何时刻只有一种状态灯亮**：:meth:`Led.send` 会**先熄灭其他所有颜色**再点亮
   目标色。否则红灯与绿灯同时亮，肉眼看过去就是**黄色** —— 会把"报警"读成"注意"，
   这是会耽误救援的显示缺陷。
2. **闪烁不阻塞主流程**：``LightCommand(blink=True)`` 的闪烁由 :meth:`Led.blink`
   驱动，节奏交给**可注入的 sleep**（默认 :func:`time.sleep`），测试注入假函数即
   可瞬时验证；并且闪 ``blink_duration_s``（默认 3.0 秒）后**自动停止并保持目标色**
   （不会一直闪到耗电/扰民，也不会卡死调用线程）。
3. **颜色未知必须报错**：传 ``color="purple"`` 抛 :class:`UnsupportedError`
   并**列出可用颜色**，绝不静默当成绿色（静默降级会让"红色报警"变成"绿色正常"）。
4. **mock 模式绝不碰 GPIO**：只维护"逻辑电平表"（:meth:`Led.level`），
   测试可直接断言"点灯前先熄了谁"。

负责人：________（待分配）
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, Iterable, List, Optional

from ..hal.device import OutputDevice
from ..hal.exceptions import (
    AlarmDispatchError,
    DeviceInitError,
    UnsupportedError,
)
from ..hal.models import DeviceKind, LightCommand, Sample

_log = logging.getLogger(__name__)

#: 默认引脚映射（BCM 编号）：绿=22（脚15）、黄=23（脚16）、红=24（脚18）
DEFAULT_PINS: Dict[str, int] = {"green": 22, "yellow": 23, "red": 24}

#: BCM → 物理脚号（只列常用几个）
_BCM_TO_PHYSICAL: Dict[int, int] = {
    17: 11, 18: 12, 27: 13, 22: 15, 23: 16, 24: 18, 25: 22, 5: 29, 6: 31,
    12: 32, 13: 33, 16: 36, 19: 35, 20: 38, 21: 40, 26: 37,
}

#: 允许的颜色集合：引脚表里的颜色 + 逻辑颜色 ``off``
LOGICAL_OFF = "off"


def _physical(bcm: int) -> str:
    """BCM → "GPIO22（物理脚 15）"。"""
    physical = _BCM_TO_PHYSICAL.get(bcm)
    return f"GPIO{bcm}（物理脚 {physical}）" if physical else f"GPIO{bcm}"


class Led(OutputDevice):
    """多色 LED 状态指示灯。

    Args:
        pins: 颜色 → BCM 引脚 的映射，默认
              ``{"green": 22, "yellow": 23, "red": 24}``（可加 ``"blue"``）。
        active_low: True 表示低电平点亮（共阳/反相驱动模块），默认 False。
        blink_hz: 闪烁频率（Hz），默认 1.0（即亮 0.5s 灭 0.5s）。
        blink_duration_s: ``blink=True`` 时总共闪多少秒后自动停，默认 3.0。
        sleep: **可注入的 sleep 函数**（默认 :func:`time.sleep`）。
        bus: 总线对象（本驱动用 GPIO，保留以统一构造签名）。
        mock: 模拟模式。**True 时绝不创建 GPIO 对象**。
        name: 实例名（默认 ``led``）。

    Raises:
        UnsupportedError: ``pins`` 里出现空颜色名或非法引脚号。
    """

    KIND = DeviceKind.LIGHT
    NAME = "led"

    def __init__(
        self,
        pins: Optional[Dict[str, int]] = None,
        active_low: bool = False,
        blink_hz: float = 1.0,
        blink_duration_s: float = 3.0,
        sleep: Callable[[float], None] = time.sleep,
        bus: Any = None,
        mock: bool = False,
        name: str = "",
    ) -> None:
        super().__init__(bus=bus, mock=mock, name=name)
        raw_pins = dict(DEFAULT_PINS if pins is None else pins)
        cleaned: Dict[str, int] = {}
        for color, pin in raw_pins.items():
            key = str(color).strip().lower()
            if not key:
                raise UnsupportedError("LED 颜色名不能为空字符串")
            cleaned[key] = int(pin)
        if not cleaned:
            raise UnsupportedError("LED 至少需要一个颜色引脚（例如 {'green': 22}）")
        self.pins: Dict[str, int] = cleaned
        self.active_low = bool(active_low)
        self.blink_hz = float(blink_hz) if blink_hz and blink_hz > 0 else 1.0
        self.blink_duration_s = max(0.0, float(blink_duration_s))
        self.sleep: Callable[[float], None] = sleep if sleep is not None else time.sleep

        self._devs: Dict[str, Any] = {}          # 真实模式下的 gpiozero 对象
        self._levels: Dict[str, int] = {c: 0 for c in self.pins}  # 逻辑电平（1=亮）
        self._current: str = LOGICAL_OFF         # 当前显示的颜色
        self._slept_s = 0.0                      # 累计 sleep 秒数（测试断言用）
        self._blink_count = 0                    # 累计闪烁轮次

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def open(self) -> None:
        """初始化各色 GPIO，并保证开机时**全灭**（不残留上一轮的颜色）。

        Raises:
            DeviceInitError: GPIO 初始化失败（含排查线索）。
        """
        if self._opened:
            return
        if self.mock:
            self._opened = True
            return
        try:
            from gpiozero import DigitalOutputDevice  # type: ignore import-not-found
        except ImportError as exc:
            raise DeviceInitError(
                f"LED 初始化失败：未安装 gpiozero（{exc}）。"
                "树莓派上执行 `sudo apt install -y python3-gpiozero`；"
                "PC 上开发请用 mock=True。" + self._wiring_hint()
            ) from exc
        created: Dict[str, Any] = {}
        try:
            for color, pin in self.pins.items():
                created[color] = DigitalOutputDevice(
                    pin, active_high=not self.active_low, initial_value=False
                )
        except Exception as exc:  # noqa: BLE001 - gpiozero 会抛各种运行时错误
            for dev in created.values():  # 建了一半就失败：把已建的关掉，别留垃圾
                try:
                    dev.close()
                except Exception:  # noqa: BLE001
                    pass
            raise DeviceInitError(
                f"LED 初始化失败（引脚 {self.pins}）：{exc}。" + self._wiring_hint()
            ) from exc
        self._devs = created
        self.all_off()
        self._opened = True

    def _wiring_hint(self) -> str:
        """最常见的接线错误（报错信息里必须带上，学生才能自救）。"""
        return (
            "排查：1) 每个 LED 是否串了 220Ω~1kΩ 限流电阻（不串可能烧 GPIO）；"
            "2) 共阴脚是否接 GND、共阳是否接 3.3V（共阳需 active_low=True）；"
            "3) 是否与 LCD/蜂鸣器抢用了同一 GPIO；"
            "4) 是否在 PC 上误用了 mock=False"
        )

    def close(self) -> None:
        """全灭并释放 GPIO（幂等，不抛异常）。"""
        if self._devs:
            for dev in self._devs.values():
                try:
                    dev.off()
                    dev.close()
                except Exception:  # noqa: BLE001 - 关闭失败不应影响收尾
                    pass
            self._devs = {}
        for color in self._levels:
            self._levels[color] = 0
        self._current = LOGICAL_OFF
        self._opened = False

    # ------------------------------------------------------------------
    # 低层：点灯
    # ------------------------------------------------------------------

    def _set(self, color: str, on: bool) -> None:
        """把某个颜色点亮/熄灭（只碰这一个引脚）。"""
        dev = self._devs.get(color)
        if dev is not None:
            if on:
                dev.on()
            else:
                dev.off()
        self._levels[color] = 1 if on else 0

    def level(self, color: str) -> int:
        """查询某颜色的**逻辑电平**（1=亮，0=灭）——测试与排障用。"""
        return self._levels.get(str(color).strip().lower(), 0)

    @property
    def current_color(self) -> str:
        """当前显示的颜色；全灭时为 ``"off"``。"""
        return self._current

    @property
    def available_colors(self) -> List[str]:
        """可用颜色列表（含 ``"off"``）。"""
        return sorted(self.pins) + [LOGICAL_OFF]

    def all_off(self) -> None:
        """熄灭所有颜色（幂等）。"""
        for color in self.pins:
            self._set(color, False)
        self._current = LOGICAL_OFF

    # ------------------------------------------------------------------
    # 指令入口
    # ------------------------------------------------------------------

    def send(self, command: Any) -> None:
        """执行一条灯光指令。

        Args:
            command: :class:`~health_monitor.hal.models.LightCommand`。

        Raises:
            DeviceNotReady: 未 ``open()``。
            UnsupportedError: 颜色未知或指令类型不对（**绝不静默忽略/降级**）。
            AlarmDispatchError: GPIO 操作失败。
        """
        self._require_open()
        if not isinstance(command, LightCommand):
            exc = UnsupportedError(
                f"LED 不支持指令类型 {type(command).__name__}；"
                "它只接受 LightCommand(color='green'|'yellow'|'red'|'off', blink=False)"
            )
            self._note_fault(exc)
            raise exc

        color = str(command.color).strip().lower()
        if color != LOGICAL_OFF and color not in self.pins:
            exc = UnsupportedError(
                f"未知 LED 颜色 {command.color!r}；可用颜色："
                + "、".join(self.available_colors)
                + "（如需新增颜色，请在构造时通过 pins={'blue': 25} 声明引脚）"
            )
            self._note_fault(exc)
            raise exc

        try:
            if color == LOGICAL_OFF:
                self.all_off()
            else:
                # ⚠️ 关键顺序：先熄灭其他颜色，再点亮目标色。
                # 若先点后熄，会在两个 I2C/GPIO 操作之间出现"红+绿同亮"的瞬间，
                # 肉眼看到的就是黄色（=注意），会把报警误读成提示。
                for other in self.pins:
                    if other != color:
                        self._set(other, False)
                self._set(color, True)
                self._current = color
                if command.blink:
                    self.blink(color)
        except Exception as exc:  # noqa: BLE001 - 统一翻译成报警下发失败
            self._note_fault(exc)
            raise AlarmDispatchError(
                f"LED（{color}，引脚 {self.pins.get(color, '?')}）操作失败：{exc}。"
                "请检查限流电阻与杜邦线，并确认 GPIO 未被其他进程占用"
            ) from exc
        self._note_ok()

    # ------------------------------------------------------------------
    # 闪烁（用可注入的 sleep，不阻塞业务太久）
    # ------------------------------------------------------------------

    def blink(self, color: str, duration_s: Optional[float] = None) -> int:
        """让 ``color`` 闪烁一段时间后**停在点亮状态**。

        Args:
            color: 要闪的颜色（必须在 ``pins`` 里）。
            duration_s: 闪多久，默认取 ``blink_duration_s``。

        Returns:
            实际完成的"灭→亮"轮数（测试可断言）。

        Note:
            本方法会阻塞调用线程 ``duration_s`` 秒。业务层若不想被阻塞，
            请在单独的线程里调用（本项目的主循环默认接受这个代价：
            默认只有 3 秒，且此时正好在报警，不需要立刻回去采样）。
        """
        self._require_open()
        if color not in self.pins:
            raise UnsupportedError(
                f"未知 LED 颜色 {color!r}；可用颜色：" + "、".join(self.available_colors)
            )
        total = self.blink_duration_s if duration_s is None else max(0.0, float(duration_s))
        half = 0.5 / self.blink_hz          # 半个周期 = 亮/灭各占的时间
        rounds = int(total / (2 * half)) if half > 0 else 0
        for _ in range(rounds):
            self._set(color, False)
            self._sleep(half)
            self._set(color, True)
            self._sleep(half)
            self._blink_count += 1
        self._set(color, True)              # 闪完停在亮态（报警不能被"闪灭"吞掉）
        self._current = color
        return rounds

    def _sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        self._slept_s += seconds
        self.sleep(seconds)

    # ------------------------------------------------------------------
    # 状态回读
    # ------------------------------------------------------------------

    def read(self) -> Sample:
        """回读自身状态。

        按项目契约，``Sample`` 是 frozen dataclass 且**不允许随便加字段**
        （改它等于改所有人的接口），所以这里只返回带 ``device`` 的基类 ``Sample``；
        当前颜色等细节通过 :meth:`status` 的 ``current_color`` 与 :meth:`describe`
        暴露，业务层与 Web 界面从那里取。

        Raises:
            DeviceNotReady: 未 ``open()``。
        """
        self._require_open()
        self._note_ok()
        return Sample(device=self.name)

    def status(self) -> Dict[str, Any]:
        """扩展基类状态：暴露当前颜色与各色逻辑电平。"""
        info = super().status()
        info.update(
            {
                "current_color": self._current,
                "levels": dict(self._levels),
                "pins": dict(self.pins),
                "active_low": self.active_low,
                "blink_count": self._blink_count,
                "slept_s": self._slept_s,
            }
        )
        return info

    def describe(self) -> Dict[str, Any]:
        """接线说明（会被 ``docs`` 生成脚本读取）。"""
        info = super().describe()
        pins_doc = {
            color: f"{_physical(pin)}（信号线串 220Ω~1kΩ 限流电阻）"
            for color, pin in self.pins.items()
        }
        pins_doc["common"] = (
            "共阴（−）接 GND（物理脚 6/9/14）；共阳接 3.3V 并把 active_low 设为 True"
        )
        info.update(
            {
                "bus": "GPIO（无 I2C/SPI）",
                "pins": pins_doc,
                "notes": (
                    "任何时刻只亮一种颜色（send() 会先熄灭其他颜色，避免红绿同亮被看成黄色）；"
                    f"有效电平={'低' if self.active_low else '高'}；"
                    f"闪烁 {self.blink_hz:.1f}Hz、持续 {self.blink_duration_s:.1f}s 后自动停在亮态；"
                    f"可用颜色：{'、'.join(self.available_colors)}"
                ),
            }
        )
        return info


__all__ = ["Led", "DEFAULT_PINS", "LOGICAL_OFF"]
