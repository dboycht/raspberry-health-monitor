"""HC-SR501 人体红外（PIR）传感器驱动。

模块用途
--------
判断房间里"有没有人在活动"，供业务层实现两个关键报警：

- ``NO_MOTION_TOO_LONG``（长时间无活动，疑似跌倒/昏迷）——靠
  :meth:`HcSr501.seconds_since_motion` 判"多久没动静"；
- 有人活动时点亮状态灯 / 刷新 LCD（配合 ``led`` 与 ``lcd1602`` 驱动）。

接线表（HC-SR501 ↔ 树莓派 40-pin 物理脚号）
--------------------------------------------
===================  ===================  ============================================
HC-SR501 引脚         树莓派 40-pin        说明
===================  ===================  ============================================
VCC（脚 1）           5V（物理脚 2 或 4）   ⚠️ **必须 5V**：3.3V 供电时模块工作不稳、
                                           探测距离明显变短
OUT（脚 2）           GPIO17（物理脚 11）   ⚠️ **输出 3.3V 电平，可直连 GPIO**，
                                           不需要电平转换（这是 HC-SR501 板载稳压
                                           带来的好处，与 HC-SR04 的 5V ECHO 不同）
GND（脚 3）           GND（物理脚 6/9/14）  共地，**不共地必然乱跳**
===================  ===================  ============================================

设计要点（为什么这么写）
------------------------
1. **纯逻辑拆出来**：去抖 + "最后一次检测到人的时刻"维护放在
   :class:`MotionTracker`（不碰硬件、可注入时钟），单测不需要真的等 1 秒。
2. **器件物理特性决定了驱动必须"记时间"**：HC-SR501 人走开后 OUT **不会立刻**
   变低，而是按板上电位器设定的延时（默认约 2~3 秒，可调到几分钟）继续保持高电平。
   因此"有人"这个结论要由驱动自己维护的时刻来算，而不是直接照抄引脚电平：
   - ``seconds_since_motion()``：距离最后一次"检测到人"过去多久（业务层判久无活动）；
   - ``active_for_s``：本次连续活动的持续秒数（``MotionSample`` 的字段）。
3. **上电初期必须忽略**：模块上电后约 1 分钟才稳定（内部基准电压建立 + 芯片自检），
   这段"热身期"里 OUT 会随机跳高（典型 0.5~3 秒的乱脉冲）。``settle_s``
   （默认 1.0 秒）用于把这段噪声挡掉；**真实部署建议传 ``settle_s=60``**
   （系统开机后先做别的初始化，等 PIR 稳定了再开始判定"久无活动"）。
4. **软件去抖**：默认 0.2 秒。PIR 模块在电源纹波/电磁干扰下会瞬间抖动，
   单纯读一次引脚容易误报；去抖后同时提供一个"稳定电平"和一个"本次读取的瞬时值"。
5. **mock 模式绝不碰硬件**：``mock=True`` 时可用 :meth:`HcSr501.inject_motion`
   手工喂事件，未注入时按 :attr:`HcSr501.mock_period_s`（默认 30 秒）
   做周期性触发（30 秒有人 / 30 秒无人），让无硬件的演示也能跑出完整报警链路。

负责人：________（待分配）
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from ..hal.device import Device
from ..hal.exceptions import DeviceInitError, DeviceIOError, UnsupportedError
from ..hal.models import DeviceKind, MotionSample, MotionState

#: BCM 编号 → 树莓派 40-pin 物理脚号（本项目用到的几个，避免整张表）
_BCM_TO_PHYSICAL: Dict[int, int] = {
    2: 3, 3: 5, 4: 7, 17: 11, 27: 13, 22: 15, 23: 16, 24: 18, 25: 22,
    5: 29, 6: 31, 12: 32, 13: 33, 16: 36, 19: 35, 20: 38, 21: 40,
    26: 37, 18: 12, 14: 8, 15: 10,
}

#: 上电热身期的默认时长（秒）。见模块文档"设计要点 3"。
DEFAULT_SETTLE_S = 1.0
#: mock 模式周期性触发的默认周期（秒）：每 30 秒进入一次"有人"阶段。
DEFAULT_MOCK_PERIOD_S = 30.0
#: mock 模式"有人"阶段默认持续（秒）。
DEFAULT_MOCK_CYCLE_S = 2.0


def physical_pin(bcm: int) -> str:
    """把 BCM 编号翻成 "GPIO17（物理脚 11）" 这种给人看的字符串。"""
    physical = _BCM_TO_PHYSICAL.get(bcm)
    return f"GPIO{bcm}（物理脚 {physical}）" if physical else f"GPIO{bcm}"


# ==========================================================================
# 第一部分：纯逻辑（可单测，不碰任何硬件、不 sleep）
# ==========================================================================


@dataclass
class MotionTracker:
    """PIR 去抖 + 活动时长统计（纯逻辑，不依赖硬件）。

    它同时干两件事，这是由 HC-SR501 的器件特性决定的：

    1. **去抖**：原始电平必须稳定 ``bounce_s`` 秒才被确认为状态变化；
       确认之前，"上一次的稳定状态 + 上一次 RAW 跳变时刻"都要保留下来
       ——``raw_changed_at`` 与 ``stable_changed_at`` 是两个不同的时间戳，
       混用会导致 ``active_for_s`` 在抖动时被错误重置。
    2. **维护"最后一次检测到人"的时刻**：人离开后模块 OUT 仍会保持高电平
       一段时间（板上延时电位器），业务层关心的是"人最后一次动是什么时候"，
       所以 :meth:`seconds_since_motion` 用的是**最后一次确认检测到人的时刻**，
       而不是"电平最后一次变低的时刻"。

    Args:
        bounce_s: 软件去抖时间（秒），默认 0.2。
        clock: 时钟函数，返回秒（默认 :func:`time.monotonic`）。
               **测试时注入假时钟**，就能"瞬时"验证去抖与延时机理。
        settle_s: 上电热身期（秒）。``settle_until`` 之前一律返回
                  :data:`MotionState.UNKNOWN`（**不猜**），并且不更新任何时间戳。
        active_high: True 表示 OUT 高电平=检测到人（HC-SR501 默认如此）。
    """

    bounce_s: float = 0.2
    clock: Callable[[], float] = time.monotonic
    settle_s: float = DEFAULT_SETTLE_S
    active_high: bool = True

    # -- 内部状态（下划线开头，外部只读） --------------------------------
    _raw: bool = False             # 上一次读到的原始电平（已翻译成"有人"）
    _stable: bool = False          # 去抖后的稳定状态（是否有人）
    _last_motion_ts: Optional[float] = None   # 最后一次确认"检测到人"的时刻
    _detected_since: float = 0.0   # 本次连续活动的起点（active_for_s 用）
    _raw_changed_at: float = 0.0   # 原始电平最后一次跳变时刻（去抖基准）
    _stable_changed_at: float = 0.0  # 稳定状态最后一次跳变时刻（供排查）
    _initialized: bool = False     # 是否已经吃过第一个采样
    _pending_since: Optional[float] = None  # 当前电平**首次**与稳定状态不一致的时刻

    def __post_init__(self) -> None:
        self._settle_until = self.clock() + max(0.0, float(self.settle_s))
        self._raw_changed_at = self.clock()
        self._stable_changed_at = self._raw_changed_at

    # -- 语义转换 --------------------------------------------------------

    def is_detected(self, level: int) -> bool:
        """把原始电平（0/1）翻译成"检测到人"。"""
        return (level != 0) if self.active_high else (level == 0)

    # -- 核心：吃一个采样，吐一个状态 -------------------------------------

    def update(self, level: int, ts: Optional[float] = None) -> MotionState:
        """喂入当前电平，返回**本时刻**的运动状态。

        Returns:
            :data:`MotionState.UNKNOWN` —— 仍在热身期（``settle_s`` 未到）；
            :data:`MotionState.DETECTED` / :data:`MotionState.IDLE` —— 去抖后的稳定状态。
        """
        t = self.clock() if ts is None else ts

        # 热身期：如实上报 UNKNOWN，不更新任何时间戳（否则会把噪声当成"有人在动"）
        if t < self._settle_until:
            return MotionState.UNKNOWN

        detected = self.is_detected(level)

        if not self._initialized:
            # 热身结束后的第一次采样作为基准，不产生"变化"，避免开机瞬间误报
            self._initialized = True
            self._raw = detected
            self._stable = detected
            self._raw_changed_at = t
            self._stable_changed_at = t
            self._pending_since = None
            if detected:
                self._last_motion_ts = t
                self._detected_since = t
            return self._state()

        self._raw = detected

        if detected == self._stable:
            # 已经与稳定状态一致：本次"候选变化"作废
            self._pending_since = None
        else:
            if self._pending_since is None:
                # 这是候选变化的**第一拍**，抖动窗口从这里开始算
                self._pending_since = t
                self._raw_changed_at = t
            if (t - self._pending_since) + 1e-9 >= self.bounce_s:
                # 抖动期满，确认状态变化
                was_detected = self._stable
                self._stable = detected
                self._stable_changed_at = t
                self._pending_since = None
                if detected and not was_detected:
                    self._detected_since = t

        # "最后检测到人"必须是**原始电平**还处于"有人"的时刻：
        # 一旦电平已变低，即使去抖窗口还没走完（稳定状态仍是 DETECTED），
        # 也不能再刷新它 —— 否则 seconds_since_motion() 会比真实时间少将近一个
        # 去抖窗口，"久无活动"报警就会迟报。
        effective = self._stable if self._pending_since is None else detected
        if effective:
            if self._last_motion_ts is None or t > self._last_motion_ts:
                self._last_motion_ts = t

        return self._state()

    def _state(self) -> MotionState:
        return MotionState.DETECTED if self._stable else MotionState.IDLE

    # -- 给驱动/业务层用的查询 -------------------------------------------

    @property
    def state(self) -> MotionState:
        """最近一次去抖后的稳定状态。"""
        return self._state()

    @property
    def last_motion_ts(self) -> Optional[float]:
        """最后一次确认"检测到人"的时刻；从未检测到时为 ``None``。"""
        return self._last_motion_ts

    def active_for_s(self, ts: Optional[float] = None) -> float:
        """本次连续活动已持续多少秒（无人时为 0.0）。"""
        if not self._stable or self._last_motion_ts is None:
            return 0.0
        t = self.clock() if ts is None else ts
        return max(0.0, t - self._detected_since)

    def seconds_since_motion(self, ts: Optional[float] = None) -> float:
        """距离最后一次检测到人过去多少秒（业务层判"久无活动"用它）。

        - 从没检测到过人：返回 ``float("inf")``
          ——**不要返回 0**，否则"从没动过"会被误判为"刚刚动过"。
        - 当前电平仍为"有人"：返回 0.0。
        """
        if self._stable:
            return 0.0
        if self._last_motion_ts is None:
            return float("inf")
        t = self.clock() if ts is None else ts
        return max(0.0, t - self._last_motion_ts)


# ==========================================================================
# 第二部分：驱动（硬件访问层）
# ==========================================================================


class HcSr501(Device):
    """HC-SR501 人体红外传感器（PIR）。

    Args:
        pin: BCM 编号引脚（默认 17 = 物理脚 11）。
        active_high: True 表示 OUT 高电平=有人（HC-SR501 默认如此）。
        settle_s: 上电热身期（秒），期间的触发一律忽略。真实部署建议 60。
        bounce_s: 软件去抖时间（秒），默认 0.2。
        mock_period_s: mock 模式下"30 秒有人 / 30 秒无人"的周期，默认 30。
        mock_cycle_s: mock 模式下"有人"持续多久，默认与 ``mock_period_s`` 相同。
        clock: 可注入时钟（默认 :func:`time.monotonic`），测试用假时钟。
        mock_clock: mock 周期性触发用的时钟（默认与 ``clock`` 相同）。
        bus: 总线对象（本驱动用 GPIO，保留以统一构造签名）。
        mock: 模拟模式。**True 时绝不触碰 GPIO**。
        name: 实例名（默认 ``hc_sr501``）。

    Raises:
        DeviceInitError: 真实模式下 GPIO 初始化失败（含排查线索）。
    """

    KIND = DeviceKind.MOTION
    NAME = "hc_sr501"

    def __init__(
        self,
        pin: int = 17,
        active_high: bool = True,
        settle_s: float = DEFAULT_SETTLE_S,
        bounce_s: float = 0.2,
        mock_period_s: float = DEFAULT_MOCK_PERIOD_S,
        mock_cycle_s: Optional[float] = None,
        clock: Callable[[], float] = time.monotonic,
        mock_clock: Optional[Callable[[], float]] = None,
        bus: Any = None,
        mock: bool = False,
        name: str = "",
    ) -> None:
        super().__init__(bus=bus, mock=mock, name=name)
        self.pin = int(pin)
        self.active_high = bool(active_high)
        # mock 演示不模拟"上电热身"：否则启动后第一次读数会是 UNKNOWN，演示脚本会以为坏了。
        # 真实模式必须保留热身期（模块上电约 1 分钟才稳定）。
        self.settle_s = 0.0 if self.mock else float(settle_s)
        self.bounce_s = float(bounce_s)
        self.mock_period_s = float(mock_period_s)
        self.mock_cycle_s = float(
            DEFAULT_MOCK_CYCLE_S if mock_cycle_s is None else mock_cycle_s
        )
        self.clock: Callable[[], float] = clock
        # mock 剧本的计时单独一个时钟：这样"去抖/活动计时"与"周期性有人"
        # 可以各用各的假时钟独立验证（默认与 clock 相同）。
        self._mock_clock: Callable[[], float] = mock_clock or clock

        self._tracker: Optional[MotionTracker] = None
        self._pir: Any = None                 # 真实模式下的 gpiozero 对象
        self._mock_level: int = 0             # mock 模式下"人造"的原始电平
        self._mock_injected: bool = False     # 是否已被人为 inject（优先于周期触发）
        self._mock_started_at: Optional[float] = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def open(self) -> None:
        """初始化 GPIO 并建立去抖器（mock 模式下什么都不做）。"""
        if self._opened:
            return
        # 去抖器必须在 open() 时创建：它的 settle 计时从"设备被打开"那一刻算起
        self._tracker = MotionTracker(
            bounce_s=self.bounce_s,
            clock=self.clock,
            settle_s=self.settle_s,
            active_high=self.active_high,
        )
        if self.mock:
            self._mock_started_at = self._mock_clock()
            self._opened = True
            return
        try:
            from gpiozero import DigitalInputDevice  # type: ignore import-not-found
        except ImportError as exc:
            raise DeviceInitError(
                f"HC-SR501 初始化失败：未安装 gpiozero（{exc}）。"
                "树莓派上执行 `sudo apt install -y python3-gpiozero`；"
                "PC 上开发请用 mock=True。"
                + self._wiring_hint()
            ) from exc
        try:
            # 模块板上已有 3.3V 稳压与电平转换，可直接接 GPIO，无需外部分压。
            self._pir = DigitalInputDevice(
                self.pin, pull_up=False, bounce_time=self.bounce_s
            )
        except Exception as exc:  # noqa: BLE001 - gpiozero 会抛各种运行时错误
            raise DeviceInitError(
                f"HC-SR501 初始化失败（{physical_pin(self.pin)}）：{exc}。"
                + self._wiring_hint()
                + f"5) 是否在 PC 上误用了 mock=False（本机平台：{_platform_hint()}）"
            ) from exc
        self._opened = True

    def _wiring_hint(self) -> str:
        """把最常见的接线错误写成一段可复制的排查清单。"""
        return (
            "排查：1) VCC 是否接在 5V（物理脚 2 或 4）而不是 3.3V"
            "——3.3V 供电时模块工作不稳、探测距离变短；"
            "2) OUT 是否接到 "
            f"{physical_pin(self.pin)}；"
            "3) GND 是否与树莓派共地（不共地必然乱跳）；"
            "4) 上电后是否等了约 1 分钟让模块稳定（可用 settle_s 参数）；"
        )

    def read(self) -> MotionSample:
        """读一次运动状态。

        去抖后的稳定状态 + 驱动自己维护的 ``active_for_s`` 一起返回。
        热身期（``settle_s`` 内）返回 :data:`MotionState.UNKNOWN`
        ——**诚实上报，绝不假装 IDLE**（假装无人会直接引发"久无活动"误报警）。

        Raises:
            DeviceNotReady: 未 ``open()`` 就调用。
            DeviceIOError: 引脚读取失败（上层可重试）。
        """
        self._require_open()
        assert self._tracker is not None  # open() 已保证
        try:
            level = self._read_level()
            state = self._tracker.update(level)
        except Exception as exc:  # noqa: BLE001 - 统一翻译成项目内异常
            self._note_fault(exc)
            raise DeviceIOError(
                f"读取 HC-SR501（{physical_pin(self.pin)}）失败：{exc}。"
                "请检查杜邦线是否松动、模块是否供电正常"
            ) from exc
        self._note_ok()
        return MotionSample(
            device=self.name, state=state, active_for_s=self._tracker.active_for_s()
        )

    def _read_level(self) -> int:
        """读引脚电平。

        - ``mock=True``：先按剧本推进"周期性触发"（见 :meth:`mock_tick`），
          再返回人造电平，**绝不触碰 GPIO**；
        - 真实模式：读 gpiozero 的 ``value``。
        """
        if self.mock:
            self.mock_tick()
            return self._mock_level
        if self._pir is None:  # pragma: no cover - open() 已保证非 None
            raise DeviceIOError("HC-SR501 未初始化（_pir 为空）")
        return 1 if self._pir.value else 0

    @property
    def mock_started_at(self) -> Optional[float]:
        """mock 剧本的起始时刻（测试与排障用）。"""
        return self._mock_started_at

    def close(self) -> None:
        """释放 GPIO（幂等，不抛异常）。"""
        if self._pir is not None:
            try:
                self._pir.close()
            except Exception:  # noqa: BLE001 - 关闭失败不应影响收尾
                pass
            self._pir = None
        self._tracker = None
        self._mock_started_at = None
        self._opened = False

    # ------------------------------------------------------------------
    # 业务层用的查询
    # ------------------------------------------------------------------

    def seconds_since_motion(self) -> float:
        """距离最后一次检测到人过去多少秒（业务层判"久无活动"用它）。

        - 当前仍检测到人 → ``0.0``；
        - 从没检测到过人 → ``float("inf")``（**不是 0**，否则"从没动过"会被
          误判成"刚刚动过"）；
        - 未 ``open()`` 调用 → :class:`DeviceNotReady`。
        """
        self._require_open()
        assert self._tracker is not None
        return self._tracker.seconds_since_motion()

    @property
    def last_motion_ts(self) -> Optional[float]:
        """最后一次确认"检测到人"的时刻（Unix/monotonic 秒，取决于注入的时钟）。"""
        if self._tracker is None:
            return None
        return self._tracker.last_motion_ts

    @property
    def state(self) -> MotionState:
        """最近一次去抖后的稳定状态（未 open 时为 UNKNOWN）。"""
        if self._tracker is None:
            return MotionState.UNKNOWN
        return self._tracker.state

    # ------------------------------------------------------------------
    # mock / 测试专用
    # ------------------------------------------------------------------

    def inject_motion(self, detected: bool) -> None:
        """**仅供 mock / 测试**：人为注入"有人/无人"。

        mock 模式下还支持不加干预的周期性触发（默认 30 秒有人），
        一旦调用本方法，就以注入值为准（周期触发让位）。

        Raises:
            UnsupportedError: 真实模式下调用（防止误把"人造数据"用到真机上）。
        """
        if not self.mock:
            raise UnsupportedError(
                "inject_motion() 只能在 mock=True 时使用（防止误操作真实硬件）"
            )
        self._mock_injected = True
        self._mock_level = 1 if detected else 0

    def mock_tick(self) -> None:
        """mock 模式：按真实时钟推进"周期性触发"的默认剧本。

        规则：打开后每 ``mock_period_s`` 秒为一个周期，周期内前
        ``mock_cycle_s`` 秒视为"有人"，其余时间"无人"。
        若已用过 :meth:`inject_motion`，本方法不再改变电平。
        """
        if not self.mock or self._mock_injected:
            return
        if self._mock_started_at is None:
            self._mock_started_at = self._mock_clock()
        elapsed = max(0.0, self._mock_clock() - self._mock_started_at)
        if self.mock_period_s <= 0:
            self._mock_level = 1
            return
        phase = elapsed % self.mock_period_s
        self._mock_level = 1 if phase < self.mock_cycle_s else 0

    # ------------------------------------------------------------------
    # 文档 / 自检
    # ------------------------------------------------------------------

    def describe(self) -> Dict[str, Any]:
        """接线说明（会被 ``docs`` 生成脚本读取）。"""
        info = super().describe()
        info.update(
            {
                "bus": "GPIO（无 I2C/SPI）",
                "pins": {
                    "vcc": "5V（物理脚 2 或 4）——⚠️ 必须 5V，接 3.3V 会导致误触发",
                    "out": physical_pin(self.pin),
                    "gnd": "GND（物理脚 6/9/14，任意一个）",
                },
                "notes": (
                    f"OUT 为 3.3V 电平，可直连 GPIO；有效电平="
                    f"{'高' if self.active_high else '低'}；"
                    f"软件去抖 {self.bounce_s * 1000:.0f}ms；"
                    f"上电热身期 {self.settle_s:.1f}s（真实部署建议 60s）；"
                    f"模块上两个电位器分别调延时与灵敏度；"
                    f"人离开后 OUT 会保持一段时间，驱动的 seconds_since_motion() "
                    f"已按『最后一次检测到人』计算"
                ),
            }
        )
        return info


def _platform_hint() -> str:
    """给报错信息一句"本机是不是树莓派"的提示（不抛异常）。"""
    try:
        import platform

        return f"{platform.system()} {platform.machine()}"
    except Exception:  # noqa: BLE001 - 提示信息失败不影响主流程
        return "未知平台"


__all__ = ["HcSr501", "MotionTracker", "physical_pin", "DEFAULT_SETTLE_S"]
