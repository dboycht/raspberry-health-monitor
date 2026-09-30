"""「按需测血氧」状态机测试（2026-09-30 用户要求的「测血氧开关」）。

为什么值得单独立一组：
* 这是本项目**第一个"服务主动叫人做事"**的功能：蜂鸣器叫人 → 等按键 → 测量 → 报结果。
  它跨了 采集 / 定时 / 交互 / 两块屏 / 蜂鸣 五件事，必须有确定性的机器判据；
* 它**刻意不报警** —— "请老人配合量一下"是提示，不是"出事了"。混进报警流水会污染
  `/api/v1/alarms` 与报警历史，所以"全程不产生报警"本身就是要钉死的判据；
* 它还牵出一个真 bug（`ERROR.md` **E61**）：去抖器产出的按键事件**不带设备名**，
  系统里有第二个按键时业务层分不清"要测血氧"和"求救"。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_RPI_DIR = Path(__file__).resolve().parents[2]
if str(_RPI_DIR) not in sys.path:
    sys.path.insert(0, str(_RPI_DIR))

from health_monitor.core.config import AppConfig  # noqa: E402
from health_monitor.hal.models import ButtonAction, ButtonEvent  # noqa: E402
from health_monitor.playback import PlaybackRuntime  # noqa: E402
from health_monitor.sensors.button import Button  # noqa: E402
from health_monitor.service import (  # noqa: E402
    SPO2_FAIL_LINES,
    SPO2_MEASURE_LINES,
    SPO2_PROMPT_LINES,
    _median,
)

REMIND_S = 120.0
TIMEOUT_S = 30.0
MEASURE_S = 10.0

DEMO = {
    "thresholds": {
        "no_motion_timeout_s": 60,
        "repeat_cooldown_s": 0,
        "spo2_remind_interval_s": REMIND_S,
        "spo2_remind_timeout_s": TIMEOUT_S,
        "spo2_measure_s": MEASURE_S,
    },
    "devices": {
        "vitals": {"driver": "max30102", "read_interval_s": 1.0},
        "display": {"driver": "lcd1602", "read_interval_s": 1.0},
        "tft": {"driver": "tft_spi", "read_interval_s": 2.0,
                "params": {"controller": "st7735", "spi_device": 1}},
        "alarm_buzzer": {"driver": "buzzer", "read_interval_s": 1.0},
        "sos_button": {"driver": "button", "read_interval_s": 0.2},
        "spo2_button": {"driver": "button", "read_interval_s": 0.2},
    },
}


class _Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = float(t)

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += float(seconds)


class _Base(unittest.TestCase):
    """公共装置：假时钟 + 两块屏 + 蜂鸣器 + 两个按键。"""

    #: 子类可改成 False ⇒ 关掉「测血氧」功能（验证总开关）
    spo2_enabled = True

    def setUp(self) -> None:
        devices = {name: dict(cfg) for name, cfg in DEMO["devices"].items()}
        if not self.spo2_enabled:
            devices["spo2_button"] = dict(devices["spo2_button"], enabled=False)
        config = {
            "thresholds": dict(DEMO["thresholds"]),
            "devices": devices,
        }
        self.clock = _Clock()
        self.rt = PlaybackRuntime(
            AppConfig.from_dict(config),
            clock=self.clock,
            sleep=lambda _s: None,
            verbose_outputs=False,
        )
        self.rt.open()
        self.addCleanup(self.rt.close)

    # ---------- 读屏 / 读蜂鸣 / 按键 ----------

    def _lcd(self) -> tuple:
        return tuple(self.rt.devices["display"].read().lines)

    def _tft(self) -> tuple:
        return tuple(self.rt.devices["tft"].read().lines)

    def _beeps(self) -> int:
        return int(self.rt.devices["alarm_buzzer"].total_beeps)

    def _press_spo2(self, action: ButtonAction = ButtonAction.CLICK) -> None:
        self.rt.sim("spo2_button").press(action)

    def _press_and_tick(self, who: str = "spo2_button",
                        action: ButtonAction = ButtonAction.CLICK,
                        advance: float = 0.3) -> None:
        """注入一次按键，然后跑一帧。

        ⚠️ **必须把时钟推过按键的读取周期（0.2s）**：采集器按 ``read_interval_s``
        节流，时钟不动的话第二帧根本不会去读那个按键，事件就一直躺在驱动队列里。
        （2026-09-30 写这组测试时踩到：所有"按键类"断言全灭，而"叫人"类断言全过 ——
        因为后者不需要按键。**测试装置的坑也会伪装成被测代码的 bug**。）
        """
        self.rt.sim(who).press(action)
        self._tick(advance=advance)

    def _tick(self, advance: float = 0.0) -> None:
        if advance:
            self.clock.advance(advance)
        self.rt.tick()

    def _reach_prompt(self) -> None:
        """走到"正在叫人"这一步。"""
        self.rt.tick()
        self.clock.advance(REMIND_S)
        self.rt.tick()
        self.assertEqual(self._lcd()[0], SPO2_PROMPT_LINES[0].ljust(16),
                         "到点应当已经在叫人（前置条件没成立，后面的断言就不成立）")


class TestReminder(_Base):
    """叫人这一步：到点才叫，叫在两块屏上，且把屏占住。"""

    def test_没到点不叫人(self) -> None:
        self.rt.tick()
        self.assertNotIn("SPO2", " ".join(self._lcd()))
        self.assertEqual(self._beeps(), 0)

    def test_到点叫人并且鸣两声(self) -> None:
        self.rt.tick()
        self.clock.advance(REMIND_S)
        self.rt.tick()
        self.assertIn(SPO2_PROMPT_LINES[0], self._lcd()[0])
        self.assertIn(SPO2_PROMPT_LINES[1], self._lcd()[1])
        self.assertEqual(self._beeps(), 2, "叫人的暗号是 2 声")

    def test_叫人时两块屏都显示(self) -> None:
        self._reach_prompt()
        self.assertIn(SPO2_PROMPT_LINES[1], self._tft()[1], "彩屏也要提示（主人看彩屏）")

    def test_提示期间彩屏不被信息页顶掉(self) -> None:
        """★ 与报警同理：正在等人的提示不能被轮播冲掉。"""
        self._reach_prompt()
        for _ in range(4):
            self._tick(advance=7.0)          # 累计 28s < 30s 超时
        self.assertIn(SPO2_PROMPT_LINES[0], self._tft()[0], "彩屏被信息页顶掉了")
        self.assertIn(SPO2_PROMPT_LINES[0], self._lcd()[0], "LCD 被调试面板顶掉了")

    def test_超时没按键就放弃而且不报警(self) -> None:
        """★ 这是**提示**不是**报警**：放弃了也不能进报警流水。"""
        self._reach_prompt()
        self._tick(advance=TIMEOUT_S + 1.0)
        self.assertEqual(self.rt.engine.active_alarms(), {})
        self.assertEqual(self.rt.recent_events(), [], "放弃一轮不该产生任何报警事件")

    def test_本轮过后会重新排队下一轮(self) -> None:
        self._reach_prompt()
        self._tick(advance=TIMEOUT_S + 1.0)      # 放弃
        before = self._beeps()
        self._tick(advance=REMIND_S)             # 再等一个间隔
        self.assertGreater(self._beeps(), before, "过了间隔应当再叫一次")


class TestMeasure(_Base):
    """测量这一步：按键进测量、屏上提示"轻贴"、到点报结果。"""

    def test_按键后进入测量并提示轻贴(self) -> None:
        """★ 屏上那句 `TOUCH LIGHTLY` **就是 E60 的产品化处置**（按紧会让血氧偏低）。"""
        self._reach_prompt()
        self._press_and_tick()
        self.assertIn(SPO2_MEASURE_LINES[0], self._lcd()[0])
        self.assertIn(SPO2_MEASURE_LINES[1], self._lcd()[1])
        self.assertIn(SPO2_MEASURE_LINES[1], self._tft()[1])

    def test_主动按键不必等叫人(self) -> None:
        """没到提醒时间也能自己按着测（"我想现在测一下"）。"""
        self.rt.tick()
        self._press_and_tick()
        self.assertIn(SPO2_MEASURE_LINES[0], self._lcd()[0])

    def test_测到读数就把数值报出来(self) -> None:
        self._reach_prompt()
        self._press_and_tick()
        self.rt.set_vitals(heart_rate=72.0, spo2=97.0)
        self._tick(advance=MEASURE_S + 1.0)
        text = " ".join(self._lcd())
        self.assertIn("72", text)
        self.assertIn("97", text)
        self.assertIn("SPO2", text)

    def test_没测到就如实说没测到并鸣五声(self) -> None:
        """★ 兼作 `ERROR.md` **E62** 的回归：模拟器"没贴手指"时给的样本是
        ``ok=True`` 但 ``hr/spo2`` 都是 ``None`` —— 只信 ``ok`` 标记就会
        ``float(None)`` **把 tick() 整条链打断**。

        所以这里既断言"如实说没测到"，也顺便保证**不抛异常**（真抛了这条就红）。
        """
        self._reach_prompt()
        self._press_and_tick()
        self.rt.set_vitals(finger=False)          # 手指没贴好（ok=True 但值为 None）
        before = self._beeps()
        self._tick(advance=MEASURE_S + 1.0)
        self.assertIn(SPO2_FAIL_LINES[0], self._lcd()[0])
        self.assertIn(SPO2_FAIL_LINES[1], self._lcd()[1])
        self.assertEqual(self._beeps() - before, 5, "失败的暗号是 5 声")

    def test_值为None的样本不能把服务弄崩(self) -> None:
        """★ E62 的更直接一条：服务必须**活着**跑完测量窗口。"""
        self._reach_prompt()
        self._press_and_tick()
        self.rt.set_vitals(finger=False)
        for _ in range(6):
            self._tick(advance=2.0)               # 会跨过测量窗口的截止点
        self.assertEqual(self.rt.engine.active_alarms(), {})
        self.assertNotIn("Traceback", " ".join(self._lcd()))

    def test_报的是窗口中位数而不是瞬时值(self) -> None:
        """★ 真机教训（2026-09-30 第一次验收）：取"最后一次读数"时，
        第二轮报出 **HR 111 bpm**（恰好越过 ``hr_max=110``）⇒ 当场触发一次真的
        ``hr_too_high`` 报警。改成窗口**中位数**（与 `vitals_check.py` 同一口径）后，
        单个瞬时尖峰削不掉结论。
        """
        self._reach_prompt()
        self._press_and_tick()
        for hr in (70.0, 71.0, 111.0, 70.0, 70.0):   # 中间夹一个尖峰
            self.rt.set_vitals(heart_rate=hr, spo2=96.0)
            self._tick(advance=1.0)
        self._tick(advance=MEASURE_S)                # 收尾
        text = " ".join(self._lcd())
        self.assertIn("70", text, f"应当报中位数 70 左右：{text!r}")
        self.assertNotIn("111", text, f"瞬时尖峰不该成为结论：{text!r}")


    def test_测量中再按一下可以提前结束(self) -> None:
        self._reach_prompt()
        self._press_and_tick()
        self.rt.set_vitals(heart_rate=70.0, spo2=96.0)
        self._tick(advance=2.0)
        self._press_spo2()                        # 提前结束
        self._tick(advance=0.3)
        self.assertNotIn(SPO2_MEASURE_LINES[0], self._lcd()[0], "应当已经结束测量")

    def test_测量全程不产生报警(self) -> None:
        self._reach_prompt()
        self._press_and_tick()
        self.rt.set_vitals(heart_rate=70.0, spo2=96.0)
        self._tick(advance=MEASURE_S + 1.0)
        self.assertEqual(self.rt.engine.active_alarms(), {})
        self.assertEqual(self.rt.recent_events(), [])


class TestSwitch(_Base):
    """总开关：配置里 `spo2_button.enabled=false` ⇒ 整个功能关掉。"""

    spo2_enabled = False

    def test_关掉后到点也不叫人(self) -> None:
        self.rt.tick()
        self._tick(advance=REMIND_S * 3)
        self.assertNotIn("SPO2", " ".join(self._lcd()))
        self.assertEqual(self._beeps(), 0)

    def test_关掉后按键也没反应(self) -> None:
        """按键被排除在配置外时，事件根本不会进来（也不该进测量）。"""
        self.rt.tick()
        dev = self.rt.sim("spo2_button")
        self.assertIsNone(dev, "未启用的器件不该被装配出来")


class TestTwoButtonsDoNotConfuse(unittest.TestCase):
    """★ E61：两个按键必须分得清"谁按的"。"""

    def test_去抖器产出的事件会被补上设备名(self) -> None:
        """`ButtonDebouncer` 不认识设备名 ⇒ 事件出了驱动必须**盖章**。"""
        btn = Button(pin=5, mock=True, name="spo2_button")
        raw = ButtonEvent(ts=1.0, action=ButtonAction.CLICK, pressed_for_s=0.0)
        self.assertEqual(raw.device, "", "前置条件：去抖器产出的事件本来就没有设备名")
        stamped = btn._stamp(raw)  # noqa: SLF001 - 正是在测这个私有补救点
        self.assertEqual(stamped.device, "spo2_button")
        self.assertIs(stamped.action, ButtonAction.CLICK)

    def test_已经带设备名的事件不会被覆盖(self) -> None:
        btn = Button(pin=5, mock=True, name="spo2_button")
        stamped = btn._stamp(ButtonEvent(ts=1.0, device="sos_button", action=ButtonAction.CLICK))
        self.assertEqual(stamped.device, "sos_button")


class TestTwoButtonsInService(_Base):
    """★ E61 的行为侧：按「测血氧」键**绝不能**变成消音或求救。"""

    def test_测血氧按键不产生求救报警(self) -> None:
        self.rt.tick()
        self._press_and_tick()
        self.assertEqual(self.rt.recent_events(), [], "按测血氧键冒出报警事件了")
        self.assertEqual(self.rt.engine.active_alarms(), {})

    def test_测血氧按键把报警消音了才算错(self) -> None:
        self.rt.sos()                       # 造一个报警（sos 会解除静音）
        self.rt.tick()
        self.assertFalse(self.rt.dispatcher.is_silenced(self.clock()))
        self._press_and_tick()
        self.assertFalse(
            self.rt.dispatcher.is_silenced(self.clock()),
            "「测血氧」按键把报警消音了 —— 它擅自落进了 sos_button 的分支（E61）",
        )

    def test_求救按键仍然照常消音(self) -> None:
        """反向钉子：别为了新按键把老按键的功能弄坏。"""
        self.rt.sos()
        self.rt.tick()
        self.rt.sim("sos_button").press(ButtonAction.CLICK)
        self._tick(advance=0.3)
        self.assertTrue(self.rt.dispatcher.is_silenced(self.clock()),
                        "sos_button 短按应当照旧消音")

    def test_求救按键长按仍然照常求助(self) -> None:
        self.rt.tick()
        before = len(self.rt.recent_events())
        self.rt.sim("sos_button").press(ButtonAction.LONG_PRESS)
        self._tick(advance=0.3)
        self.assertGreater(len(self.rt.recent_events()), before, "长按应当产生 SOS")


class TestMedian(unittest.TestCase):
    """`_median` 纯函数（测量结果的口径就靠它）。"""

    def test_奇数个取中间(self) -> None:
        self.assertEqual(_median([3.0, 1.0, 2.0]), 2.0)

    def test_偶数个取两者平均(self) -> None:
        self.assertEqual(_median([1.0, 2.0, 3.0, 4.0]), 2.5)

    def test_空列表返回零而不是抛异常(self) -> None:
        self.assertEqual(_median([]), 0.0)

    def test_单个尖峰不影响结果(self) -> None:
        self.assertEqual(_median([70.0, 70.0, 70.0, 111.0, 70.0]), 70.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
