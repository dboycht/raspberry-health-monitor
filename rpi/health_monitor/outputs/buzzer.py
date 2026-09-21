"""蜂鸣器驱动（**有源蜂鸣器**，GPIO 直驱）。

模块用途
--------
本地声音报警：警告级间歇鸣叫、紧急级长鸣，是"不用看屏幕也知道出事了"的最后一道
本地提示（老人可能没戴手机、也没看 LCD）。

接线表（有源蜂鸣器 ↔ 树莓派 40-pin 物理脚号）
----------------------------------------------
===================  ==========================  ======================================
蜂鸣器引脚            树莓派 40-pin              说明
===================  ==========================  ======================================
正极（长脚 / +）      GPIO18（物理脚 12）         串 **220Ω~1kΩ 限流电阻**后再接 GPIO
负极（短脚 / −）      GND（物理脚 6/9/14）        共地
===================  ==========================  ======================================

⚠️ 三个必须注意的点
1. **有源 vs 无源**：本驱动只适用于**有源**蜂鸣器（自带振荡电路，给高电平就响）。
   无源蜂鸣器必须用 PWM 送方波，否则只会"咔"一声 —— 那是另一条实现路径，请在
   ``config/devices.json`` 里确认型号后再用。
2. **必须串限流电阻**（220Ω~1kΩ）：蜂鸣器直流电阻很小，直接接 GPIO 会超过
   BCM2712 单脚 16mA 的推荐电流上限，长期可能烧引脚。
3. **严禁长鸣**：见下面"防吵人设计"。

防吵人设计（安全设计，务必保留）
--------------------------------
单次 :meth:`Buzzer.send` 的**总时长硬上限默认 10 秒**（``max_total_ms``）。
超过上限的指令会被**自动截断**、记一条 WARNING 日志，并通过
:meth:`Buzzer.status` 的 ``truncated_count`` 暴露出来。理由：

- 业务层一旦出现 bug（例如把 ``on_ms`` 传成 600000），蜂鸣器会一直尖叫，
  既扰民又会让"报警"这件事被邻居当成噪声污染 —— 这是本项目要避免的事故；
- 有了上限，最坏情况也只是响 10 秒后自动停，属于可接受范围。

设计要点
--------
1. **可注入的 sleep**：鸣叫节奏靠 ``sleep`` 函数推进，默认 :func:`time.sleep`；
   **测试注入假 sleep** 就能"瞬时"验证 on/off 时序，单测不会真的等几秒。
2. **纯逻辑拆出来**：节拍计算放在 :func:`plan_beeps`（不碰 GPIO、不 sleep），
   单测可以直接断言"截断后还剩几响、最后一响多长"。
3. **mock 模式绝不碰 GPIO**：只记录"我响了几次、响的节奏是什么"。

负责人：________（待分配）
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from ..hal.device import OutputDevice
from ..hal.exceptions import (
    AlarmDispatchError,
    DeviceInitError,
    UnsupportedError,
)
from ..hal.models import BeepCommand, DeviceKind, Sample

_log = logging.getLogger(__name__)

#: 单次 send() 的总时长硬上限（毫秒）。见模块文档"防吵人设计"。
DEFAULT_MAX_TOTAL_MS = 10_000
#: 单响的最长时间（毫秒），防止 on_ms 被传成天量数字。
DEFAULT_MAX_ON_MS = 5_000

#: BCM → 物理脚号（只列常用几个）
_BCM_TO_PHYSICAL: Dict[int, int] = {
    17: 11, 18: 12, 27: 13, 22: 15, 23: 16, 24: 18, 25: 22, 5: 29, 6: 31,
    12: 32, 13: 33, 16: 36, 19: 35, 20: 38, 21: 40, 26: 37,
}


def physical_pin(bcm: int) -> str:
    """把 BCM 编号翻成 "GPIO18（物理脚 12）" 这种给人看的字符串。"""
    physical = _BCM_TO_PHYSICAL.get(bcm)
    return f"GPIO{bcm}（物理脚 {physical}）" if physical else f"GPIO{bcm}"


# ==========================================================================
# 第一部分：纯逻辑（节拍规划，不碰 GPIO、不 sleep、可单测）
# ==========================================================================


@dataclass(frozen=True)
class BeepStep:
    """一个鸣叫节拍：先响 ``on_ms``，再停 ``off_ms``。"""

    on_ms: int
    off_ms: int


def plan_beeps(
    times: int,
    on_ms: int,
    off_ms: int,
    max_total_ms: int = DEFAULT_MAX_TOTAL_MS,
    max_on_ms: int = DEFAULT_MAX_ON_MS,
) -> List[BeepStep]:
    """把 ``BeepCommand`` 的参数折算成**实际要执行的节拍列表**（纯逻辑）。

    规则（安全优先）：

    1. ``times <= 0`` / ``on_ms <= 0`` → 返回空列表（**不响**，而不是"响 0 毫秒"）；
    2. 单响时长超过 ``max_on_ms`` → 夹到上限；
    3. 单响时长超过 ``max_total_ms`` → 连一响都放不下，返回**被夹到上限的单响**
       （"报警要么响，要么不响"，不静默丢弃）；
    4. 整段总时长（含中间静音）超过 ``max_total_ms`` → 在**完整的节拍边界**上截断，
       并保证**至少响一次**，
       **绝不切一半的鸣叫**（半响听起来像故障）。

    Args:
        times: 期望鸣叫次数。
        on_ms: 单次鸣叫毫秒数。
        off_ms: 两次之间的静音毫秒数。
        max_total_ms: 总时长上限（毫秒）。
        max_on_ms: 单响时长上限（毫秒）。

    Returns:
        实际执行的 :class:`BeepStep` 列表（可能为空 = 不响）。
    """
    times = int(times)
    on_ms = int(on_ms)
    off_ms = max(0, int(off_ms))
    if times <= 0 or on_ms <= 0:
        return []

    # 单响上限优先（例如 on_ms=9999、max_on_ms=1000 → 只响 1000ms）
    on_ms = min(on_ms, max(1, int(max_on_ms)))
    if on_ms > max_total_ms:
        # 单响就已超过总量上限：夹到总量上限，仍然响一次
        return [BeepStep(on_ms=max_total_ms, off_ms=0)]

    steps: List[BeepStep] = []
    elapsed = 0  # 已排入的总时长（毫秒）
    for i in range(times):
        is_last = i == times - 1
        cost = on_ms + (0 if is_last else off_ms)  # 最后一响后面不需要静音
        if elapsed + cost > max_total_ms:
            break
        steps.append(BeepStep(on_ms=on_ms, off_ms=0 if is_last else off_ms))
        elapsed += cost

    if not steps:
        # 放不下一整响（例如 off_ms 特别大）：退化为一响，绝不静默丢弃报警
        return [BeepStep(on_ms=min(on_ms, max_total_ms), off_ms=0)]
    # 被截断时，列表的最后一响其实并不是"原本计划的最后一响"，
    # 它后面的静音不该再等（否则总时长会超出上限，也白等一段）。
    return steps[:-1] + [BeepStep(on_ms=steps[-1].on_ms, off_ms=0)]


def total_ms(steps: List[BeepStep]) -> int:
    """节拍列表的总时长（毫秒）——含中间的静音，不含末尾静音。"""
    return sum(s.on_ms + s.off_ms for s in steps)


# ==========================================================================
# 第二部分：驱动（硬件访问层）
# ==========================================================================


class Buzzer(OutputDevice):
    """有源蜂鸣器（本地声音报警）。

    Args:
        pin: BCM 编号引脚（默认 18 = 物理脚 12）。
        active_low: True 表示低电平触发（部分模块驱动级反相，默认 False）。
        max_total_ms: 单次 ``send()`` 总时长上限（毫秒），默认 10000。
        max_on_ms: 单响时长上限（毫秒），默认 5000。
        sleep: **可注入的 sleep 函数**（默认 :func:`time.sleep`）。
               测试注入假 sleep，即可瞬时验证时序而不用真的等几秒。
        bus: 总线对象（本驱动用 GPIO，保留以统一构造签名）。
        mock: 模拟模式。**True 时绝不创建 GPIO 对象**。
        name: 实例名（默认 ``buzzer``）。
    """

    KIND = DeviceKind.AUDIO
    NAME = "buzzer"

    def __init__(
        self,
        pin: int = 18,
        active_low: bool = False,
        max_total_ms: int = DEFAULT_MAX_TOTAL_MS,
        max_on_ms: int = DEFAULT_MAX_ON_MS,
        sleep: Callable[[float], None] = time.sleep,
        bus: Any = None,
        mock: bool = False,
        name: str = "",
    ) -> None:
        super().__init__(bus=bus, mock=mock, name=name)
        self.pin = int(pin)
        self.active_low = bool(active_low)
        self.max_total_ms = int(max_total_ms)
        self.max_on_ms = int(max_on_ms)
        self.sleep: Callable[[float], None] = sleep if sleep is not None else time.sleep

        self._dev: Any = None            # 真实模式下的 gpiozero 对象
        self._sounding = False           # 当前是否正在响
        self._slept_ms = 0               # 累计"睡"了多少毫秒（测试断言用）
        self._beep_count = 0             # 累计响了几声
        self._truncated_count = 0        # 因超上限被截断的次数
        self._planned_steps = 0          # 最近一次 send 实际执行的节拍数

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def open(self) -> None:
        """初始化 GPIO（mock 模式下什么都不做）。

        Raises:
            DeviceInitError: 真实模式下 GPIO 初始化失败（含排查线索）。
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
                f"蜂鸣器初始化失败：未安装 gpiozero（{exc}）。"
                "树莓派上执行 `sudo apt install -y python3-gpiozero`；"
                "PC 上开发请用 mock=True。" + self._wiring_hint()
            ) from exc
        try:
            self._dev = DigitalOutputDevice(
                self.pin, active_high=not self.active_low, initial_value=False
            )
        except Exception as exc:  # noqa: BLE001 - gpiozero 会抛各种运行时错误
            raise DeviceInitError(
                f"蜂鸣器初始化失败（{physical_pin(self.pin)}）：{exc}。" + self._wiring_hint()
            ) from exc
        self._opened = True

    def _wiring_hint(self) -> str:
        """最常见的接线错误（报错信息里必须带上，学生才能自救）。"""
        return (
            f"排查：1) 信号线是否接 {physical_pin(self.pin)}（GPIO18 = 物理脚 12）；"
            "2) 是否串了 220Ω~1kΩ 限流电阻（不串可能烧 GPIO）；"
            "3) 负极是否接 GND 并与树莓派共地；"
            "4) 是否用的是**有源**蜂鸣器（无源需 PWM）；"
            "5) 是否在 PC 上误用了 mock=False"
        )

    # ------------------------------------------------------------------
    # 指令入口
    # ------------------------------------------------------------------

    def send(self, command: Any) -> None:
        """执行一条鸣叫指令。

        Args:
            command: :class:`~health_monitor.hal.models.BeepCommand`。

        Raises:
            DeviceNotReady: 未 ``open()``。
            UnsupportedError: 指令类型不对（**绝不静默忽略**）。
            AlarmDispatchError: GPIO 操作失败（会保证先静音再抛出）。
        """
        self._require_open()
        if not isinstance(command, BeepCommand):
            exc = UnsupportedError(
                f"蜂鸣器不支持指令类型 {type(command).__name__}；"
                "它只接受 BeepCommand(times=n, on_ms=200, off_ms=200)"
            )
            self._note_fault(exc)
            raise exc

        steps = plan_beeps(
            command.times,
            command.on_ms,
            command.off_ms,
            max_total_ms=self.max_total_ms,
            max_on_ms=self.max_on_ms,
        )
        planned_ms = total_ms(steps)
        if planned_ms < self._requested_ms(command):
            self._truncated_count += 1
            _log.warning(
                "蜂鸣器指令 %s 超过单次 %dms 上限，已截断为 %dms（%d 响）。"
                "这是防长鸣的安全设计（见 buzzer.py 模块文档）",
                command, self.max_total_ms, planned_ms, len(steps),
            )
        self._planned_steps = len(steps)

        try:
            for step in steps:
                self.beep_on()
                self._sleep_ms(step.on_ms)
                self.beep_off()
                if step.off_ms:
                    self._sleep_ms(step.off_ms)
        except Exception as exc:  # noqa: BLE001 - 统一翻译成报警下发失败
            # ⚠️ 出错时先确保静音，再抛：避免"异常了还在叫"这种最糟的情况
            try:
                self.beep_off()
            except Exception:  # noqa: BLE001
                pass
            self._note_fault(exc)
            raise AlarmDispatchError(
                f"蜂鸣器（GPIO{self.pin}）鸣叫失败：{exc}。"
                "已尝试静音；请检查 GPIO 是否被别的进程占用、接线是否松动"
            ) from exc
        self._note_ok()

    @staticmethod
    def _requested_ms(command: BeepCommand) -> int:
        """换算"业务层原本要求的总时长"（用于判断是否发生了截断）。"""
        times = max(0, int(command.times))
        on_ms = max(0, int(command.on_ms))
        off_ms = max(0, int(command.off_ms))
        if times <= 0 or on_ms <= 0:
            return 0
        return times * on_ms + (times - 1) * off_ms

    # ------------------------------------------------------------------
    # 基本动作
    # ------------------------------------------------------------------

    def beep_on(self) -> None:
        """开始鸣叫（不 sleep）。"""
        self._require_open()
        if self._dev is not None:
            self._dev.on()
        self._sounding = True
        self._beep_count += 1

    def beep_off(self) -> None:
        """停止鸣叫（幂等）。"""
        self._require_open()
        if self._dev is not None:
            self._dev.off()
        self._sounding = False

    def silence(self) -> None:
        """立即静音（业务层"消音"按钮用；幂等、不抛异常）。"""
        try:
            if self._dev is not None:
                self._dev.off()
        except Exception as exc:  # noqa: BLE001 - 消音失败也要留痕，但不该阻断主流程
            self._note_fault(exc)
        self._sounding = False

    def _sleep_ms(self, ms: int) -> None:
        """用**可注入的 sleep** 等待（毫秒→秒）。测试注入假函数即瞬时返回。"""
        if ms <= 0:
            return
        self._slept_ms += int(ms)
        self.sleep(ms / 1000.0)

    # ------------------------------------------------------------------
    # 状态回读
    # ------------------------------------------------------------------

    def read(self) -> Sample:
        """回读自身状态（基类 ``Sample``，本项目约定蜂鸣器不用 DisplayStatus）。

        额外的运行细节（是否正在响、累计响了多少声、被截断几次）通过
        :meth:`status` 与 :meth:`describe` 暴露 —— ``Sample`` 是 frozen dataclass，
        契约不允许随意加字段。
        """
        self._require_open()
        self._note_ok()
        return Sample(device=self.name)

    def close(self) -> None:
        """释放 GPIO（幂等，不抛异常）；关闭前先静音。"""
        if self._dev is not None:
            try:
                self._dev.off()
                self._dev.close()
            except Exception:  # noqa: BLE001 - 关闭失败不应影响收尾
                pass
            self._dev = None
        self._sounding = False
        self._opened = False

    # ------------------------------------------------------------------
    # 文档 / 状态
    # ------------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        """扩展基类状态：把"响了几声 / 被截断几次 / 睡了多久"暴露出来。"""
        info = super().status()
        info.update(
            {
                "pin": self.pin,
                "active_low": self.active_low,
                "sounding": self._sounding,
                "beep_count": self._beep_count,
                "planned_steps": self._planned_steps,
                "truncated_count": self._truncated_count,
                "slept_ms": self._slept_ms,
                "max_total_ms": self.max_total_ms,
            }
        )
        return info

    def describe(self) -> Dict[str, Any]:
        """接线说明（会被 ``docs`` 生成脚本读取）。"""
        info = super().describe()
        info.update(
            {
                "bus": "GPIO（无 I2C/SPI）",
                "pins": {
                    "signal": physical_pin(self.pin),
                    "gnd": "GND（物理脚 6/9/14）",
                    "series_resistor": "信号线上串 220Ω~1kΩ 限流电阻（必须）",
                },
                "notes": (
                    "**有源**蜂鸣器（给高电平即响）；无源型号需改用 PWM，本驱动不支持。"
                    f"有效电平={'低' if self.active_low else '高'}；"
                    f"单次 send() 总时长上限 {self.max_total_ms / 1000:.1f}s、"
                    f"单响上限 {self.max_on_ms / 1000:.1f}s（防长鸣的安全设计）"
                ),
            }
        )
        return info


__all__ = ["Buzzer", "BeepStep", "plan_beeps", "total_ms", "DEFAULT_MAX_TOTAL_MS"]
