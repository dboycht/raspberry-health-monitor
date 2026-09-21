"""按键驱动 —— **本项目的参考实现（范本）**。

新写任何驱动前，先把这个文件从头到尾读一遍：
它演示了本项目要求的全部要素（纯逻辑与硬件分离、mock 支持、可注入时钟、
契约方法、自检、接线说明、失败留痕）。

接线
----
=================  ==========================  ==========================
按键引脚            树莓派 40-pin              说明
=================  ==========================  ==========================
一端                GPIO27（物理脚 13）        内部上拉，按下读到低电平
另一端              GND（物理脚 9）            按下即把引脚拉到 GND
=================  ==========================  ==========================

⚠️ **引脚以 ``config/devices.json`` 为准**（那里是 ``"pin": 27``，即物理脚 13）。
本文件早前的注释写成 GPIO17（物理脚 11），而 GPIO17 在整机里分给了 HC-SR501
（人体红外），照旧注释接线会**撞脚**：PIR 检测到人时会顺带把按钮读成"按下"，
从而莫名触发紧急求助。已改正（2026-09-21 硬件文档轮发现）。

⚠️ 不需要外接上拉电阻：驱动默认启用树莓派内部上拉（``pull_up=True``）。
若你的按键模块已自带电阻/高电平输出，请把 ``pull_up`` 设为 ``False``。

本例的三条设计要点（其他驱动请照抄）
------------------------------------
1. **纯逻辑抽出来**：去抖与长按判定放在 :class:`ButtonDebouncer`（不碰硬件、可注入时钟），
   这样单测不需要真的等 1 秒，也不会有偶发失败。
2. **mock 模式绝不碰硬件**：``mock=True`` 时用 :meth:`inject` 喂假事件，
   供业务层演示与集成测试使用。
3. **失败要留痕**：``read()`` 出问题时 ``status()["last_error"]`` 必须能看到原因。
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, Optional

from ..hal.device import Device
from ..hal.exceptions import DeviceInitError, DeviceIOError, UnsupportedError
from ..hal.models import ButtonAction, ButtonEvent, DeviceKind, now_ts
from ..hal.pins import describe_pin


# ==========================================================================
# 第一部分：纯逻辑（可单测，不碰任何硬件）
# ==========================================================================


@dataclass
class ButtonDebouncer:
    """去抖 + 短按/长按判定（纯逻辑，不依赖硬件）。

    Args:
        bounce_s: 去抖时间（秒）。按键抖动通常 <10ms，取 0.03~0.05 足够。
        long_press_s: 按住多久算长按。
        clock: 时钟函数，返回秒（默认 :func:`time.monotonic`）。
               **测试时注入假时钟**，就能"瞬时"验证长按逻辑。
        pressed_when_low: True 表示低电平=按下（上拉接法，本项目默认）。
    """

    bounce_s: float = 0.04
    long_press_s: float = 1.0
    clock: Callable[[], float] = time.monotonic
    pressed_when_low: bool = True

    # 内部状态
    _raw: bool = False           # 上一次看到的原始电平对应"按下"
    _stable: bool = False        # 去抖后的稳定状态
    _changed_at: float = 0.0     # 稳定状态最后一次变化的时间
    _long_fired: bool = False    # 本次按住是否已经报过长按

    def __post_init__(self) -> None:
        self._changed_at = self.clock()

    # -- 语义转换 ------------------------------------------------------

    def is_pressed(self, level: int) -> bool:
        """把原始电平（0/1）翻译成"是否按下"。"""
        return (level == 0) if self.pressed_when_low else (level != 0)

    # -- 核心：吃一个采样，吐 0~N 个事件 ---------------------------------

    def update(self, level: int, ts: Optional[float] = None) -> list[ButtonEvent]:
        """喂入当前电平，返回本时刻产生的事件（可能为空）。

        事件规则：
        - 去抖窗口内（``bounce_s``）的电平跳变被忽略；
        - 稳定后：按下沿 → ``PRESS``；抬起沿 → ``RELEASE``；
        - 抬起时若按住时长 < ``long_press_s`` → 追加 ``CLICK``；
        - 按住超过 ``long_press_s`` 且尚未报过 → ``LONG_PRESS``（只报一次）。
        """
        t = self.clock() if ts is None else ts
        pressed = self.is_pressed(level)
        events: list[ButtonEvent] = []

        if pressed != self._raw:
            # 原始电平刚变化，进入去抖观察期
            self._raw = pressed
            return events

        if pressed != self._stable and (t - self._changed_at) >= self.bounce_s:
            # 去抖期满，确认状态变化
            self._stable = pressed
            self._changed_at = t
            if pressed:
                self._long_fired = False
                events.append(self._event(ButtonAction.PRESS, t, 0.0))
            else:
                held = t - self._changed_at
                events.append(self._event(ButtonAction.RELEASE, t, held))
                if not self._long_fired:
                    events.append(self._event(ButtonAction.CLICK, t, held))

        if self._stable and not self._long_fired:
            held = t - self._changed_at
            if held >= self.long_press_s:
                self._long_fired = True
                events.append(self._event(ButtonAction.LONG_PRESS, t, held))

        return events

    def _event(self, action: ButtonAction, ts: float, held: float) -> ButtonEvent:
        return ButtonEvent(ts=ts, action=action, pressed_for_s=held)

    @property
    def pressed(self) -> bool:
        """去抖后的稳定按下状态。"""
        return self._stable


# ==========================================================================
# 第二部分：驱动（硬件访问层）
# ==========================================================================


class Button(Device):
    """按键输入（求救 / 消音 / 翻页）。

    Args:
        pin: BCM 编号引脚（默认 17 = 物理脚 11）。
        bounce_s: 去抖时间（秒）。
        long_press_s: 长按阈值（秒）。
        pull_up: 是否启用内部上拉（默认 True；按键另一端接 GND）。
        bus: 总线对象（本驱动用不到 I2C/SPI，保留以统一构造签名）。
        mock: 模拟模式。
        name: 实例名。
    """

    KIND = DeviceKind.BUTTON
    NAME = "button"

    def __init__(
        self,
        pin: int = 17,
        bounce_s: float = 0.04,
        long_press_s: float = 1.0,
        pull_up: bool = True,
        bus: Any = None,
        mock: bool = False,
        name: str = "",
    ) -> None:
        super().__init__(bus=bus, mock=mock, name=name)
        self.pin = int(pin)
        self.pull_up = bool(pull_up)
        self._btn: Any = None
        self._debouncer = ButtonDebouncer(
            bounce_s=bounce_s, long_press_s=long_press_s, pressed_when_low=pull_up
        )
        self._pending: Deque[ButtonEvent] = deque()
        self._mock_level: int = 1 if pull_up else 0  # 未按下时的默认电平

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def open(self) -> None:
        """初始化 GPIO（mock 模式下什么都不做）。"""
        if self._opened:
            return
        if self.mock:
            self._opened = True
            return
        try:
            from gpiozero import Button as GpioButton  # type: ignore import-not-found
        except ImportError as exc:
            raise DeviceInitError(
                f"按键 GPIO{self.pin} 初始化失败：未安装 gpiozero（{exc}）。"
                "树莓派上执行 `sudo apt install -y python3-gpiozero`；"
                "PC 上开发请用 mock=True"
            ) from exc
        try:
            # bounce_time 由 gpiozero 做一次硬件级去抖，我们自己在软件层再去一次，
            # 两层去抖是刻意的：机械按键在长导线上抖动会超过 40ms。
            self._btn = GpioButton(self.pin, pull_up=self.pull_up, bounce_time=0.02)
        except Exception as exc:  # noqa: BLE001 - gpiozero 会抛各种运行时错误
            raise DeviceInitError(
                f"按键初始化失败（GPIO{self.pin}）：{exc}。请检查接线，"
                "以及是否在 PC 上误用了 mock=False"
            ) from exc
        self._opened = True

    def read(self) -> ButtonEvent:
        """返回**一个**事件；没有事件时返回 ``action == ButtonAction.NONE`` 的样本。

        说明：业务层轮询 ``read()``，每帧最多取一个事件即可（按键不会喷发式产生事件）。
        **不要**用 ``RELEASE`` 冒充"无事件"——业务层无法区分"真的抬起了"和"什么都没发生"。
        """
        self._require_open()
        if self._pending:
            self._note_ok()
            return self._pending.popleft()
        if self.mock:
            self._note_ok()
            return ButtonEvent(device=self.name, action=ButtonAction.NONE)

        try:
            # gpiozero 的 is_pressed=True 表示"按下"；转成原始电平喂给去抖器
            raw_level = 0 if self._btn.is_pressed else 1
            events = self._debouncer.update(raw_level)
        except Exception as exc:  # noqa: BLE001
            self._note_fault(exc)
            raise DeviceIOError(f"读取按键 GPIO{self.pin} 失败：{exc}") from exc

        self._note_ok()
        if events:
            self._pending.extend(events[1:])
            return events[0]
        return ButtonEvent(device=self.name, action=ButtonAction.NONE)

    def close(self) -> None:
        """释放 GPIO（幂等，不抛异常）。"""
        if self._btn is not None:
            try:
                self._btn.close()
            except Exception:  # noqa: BLE001 - 关闭失败不应影响收尾
                pass
            self._btn = None
        self._pending.clear()
        self._opened = False

    # ------------------------------------------------------------------
    # 便捷方法
    # ------------------------------------------------------------------

    def is_pressed(self) -> bool:
        """当前是否按下（读取即时状态，不产生事件）。"""
        self._require_open()
        if self.mock:
            return self._mock_level == 0
        return bool(self._btn.is_pressed)

    def inject(self, action: ButtonAction = ButtonAction.PRESS, ts: Optional[float] = None) -> None:
        """**仅供 mock / 测试**：人为注入一个按键事件。

        业务层演示脚本与集成测试用它模拟"老人按下求救键"，
        这样无硬件也能完整演示报警链路。
        """
        if not self.mock:
            raise UnsupportedError("inject() 只能在 mock=True 时使用（防止误操作真实硬件）")
        self._pending.append(
            ButtonEvent(ts=ts or now_ts(), device=self.name, action=action, pressed_for_s=0.0)
        )

    def describe(self) -> Dict[str, Any]:
        """接线说明（会被 ``docs`` 生成脚本与 ``/api/v1/devices`` 读取）。

        ⚠️ **物理脚号必须用** :func:`health_monitor.hal.pins.describe_pin` 查表，
        不许自己写偏移公式（``pin + 1`` 这类公式是错的，曾把 GPIO27 报成"物理脚 28"）。
        """
        return {
            "name": self.name,
            "kind": self.KIND.value,
            "mock": self.mock,
            "bus": "GPIO（无 I2C/SPI）",
            "pins": {
                "signal": describe_pin(self.pin),
                "gnd": "GND（物理脚 9 或任意 GND）",
            },
            "notes": f"内部上拉={'开' if self.pull_up else '关'}；去抖 {self._debouncer.bounce_s*1000:.0f}ms；"
                     f"长按阈值 {self._debouncer.long_press_s:.1f}s",
        }


__all__ = ["Button", "ButtonDebouncer"]
