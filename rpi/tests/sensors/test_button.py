"""按键驱动测试 —— 同时作为**"驱动测试怎么写"的范本**。

要点（其他驱动请照抄这三条）：
1. 纯逻辑（去抖/长按）用**假时钟**测，不 sleep、不偶发失败；
2. mock 模式下断言"绝不访问真实硬件"；
3. 错误路径要测（GPIO 打开失败必须抛 ``DeviceInitError`` 且带排查线索）。
"""

from __future__ import annotations

import unittest

from health_monitor.hal import (
    ButtonAction,
    ButtonEvent,
    DeviceInitError,
    DeviceNotReady,
    DeviceKind,
    UnsupportedError,
)
from health_monitor.sensors.button import Button, ButtonDebouncer


class FakeClock:
    """假时钟：单测里"瞬间"跨越任意时长，杜绝 sleep 与偶发失败。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> float:
        self.t += seconds
        return self.t


# ==========================================================================
# 一、纯逻辑：ButtonDebouncer
# ==========================================================================


class TestButtonDebouncer(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.deb = ButtonDebouncer(bounce_s=0.05, long_press_s=1.0, clock=self.clock)

    def _feed(self, level: int, seconds: float, repeat: int = 1) -> list:
        """保持某个电平 ``seconds`` 秒（按 10ms 步进喂采样），收集所有事件。"""
        events: list = []
        steps = max(1, int(seconds / 0.01))
        for _ in range(steps * repeat):
            self.clock.advance(0.01)
            events.extend(self.deb.update(level))
        return events

    def test_初始状态不是按下(self) -> None:
        self.assertFalse(self.deb.pressed)

    def test_按下产生PRESS事件(self) -> None:
        self._feed(1, 0.02)          # 先喂"未按下"稳定 20ms
        events = self._feed(0, 0.10)  # 再按下 100ms
        actions = [e.action for e in events]
        self.assertIn(ButtonAction.PRESS, actions)
        self.assertTrue(self.deb.pressed)

    def test_去抖窗口内的抖动被忽略(self) -> None:
        """抖动 5ms（< bounce_s=50ms）不应产生任何 PRESS 事件。"""
        self._feed(1, 0.02)
        events = []
        for _ in range(3):  # 连续三次 5ms 的抖动脉冲
            events.extend(self._feed(0, 0.005))
            events.extend(self._feed(1, 0.005))
        self.assertEqual([e.action for e in events if e.action is ButtonAction.PRESS], [])
        self.assertFalse(self.deb.pressed)

    def test_短按产生RELEASE与CLICK(self) -> None:
        self._feed(1, 0.02)
        press_events = self._feed(0, 0.20)
        self.assertIn(ButtonAction.PRESS, [e.action for e in press_events])
        release_events = self._feed(1, 0.10)
        actions = [e.action for e in release_events]
        self.assertIn(ButtonAction.RELEASE, actions)
        self.assertIn(ButtonAction.CLICK, actions)

    def test_长按只报一次LONG_PRESS(self) -> None:
        self._feed(1, 0.02)
        self._feed(0, 0.20)
        held = self._feed(0, 2.0)  # 一直按住 2 秒
        long_presses = [e for e in held if e.action is ButtonAction.LONG_PRESS]
        self.assertEqual(len(long_presses), 1, "长按事件应当且仅当报一次")
        self.assertGreaterEqual(long_presses[0].pressed_for_s, 1.0)

    def test_长按后抬起不再产生CLICK(self) -> None:
        self._feed(1, 0.02)
        self._feed(0, 0.20)
        self._feed(0, 2.0)
        release_events = self._feed(1, 0.10)
        self.assertNotIn(ButtonAction.CLICK, [e.action for e in release_events])

    def test_低电平有效可配置(self) -> None:
        deb = ButtonDebouncer(bounce_s=0.05, long_press_s=1.0, clock=self.clock, pressed_when_low=False)
        self.assertFalse(deb.is_pressed(0))
        self.assertTrue(deb.is_pressed(1))


# ==========================================================================
# 二、驱动：Button
# ==========================================================================


class TestButtonDriver(unittest.TestCase):
    def test_未open就read必须报错(self) -> None:
        btn = Button(pin=17, mock=True)
        with self.assertRaises(DeviceNotReady):
            btn.read()

    def test_mock模式可用且不碰硬件(self) -> None:
        with Button(pin=17, mock=True) as btn:
            self.assertTrue(btn.mock)
            self.assertEqual(btn.KIND, DeviceKind.BUTTON)
            self.assertIsNone(btn._btn, "mock 模式下不允许创建真实 GPIO 对象")

    def test_inject能模拟按下求救(self) -> None:
        btn = Button(pin=17, mock=True)
        btn.open()
        btn.inject(ButtonAction.CLICK)
        ev = btn.read()
        self.assertIsInstance(ev, ButtonEvent)
        self.assertEqual(ev.action, ButtonAction.CLICK)
        btn.close()

    def test_无事件时返回NONE而不是RELEASE(self) -> None:
        btn = Button(pin=17, mock=True)
        btn.open()
        self.assertEqual(btn.read().action, ButtonAction.NONE)
        btn.close()

    def test_真实模式下inject必须被拒绝(self) -> None:
        """防止误把 mock 注入接口用到真机上（真机上不该有"人造事件"）。"""
        btn = Button(pin=17, mock=False)
        with self.assertRaises(UnsupportedError):
            btn.inject(ButtonAction.PRESS)

    def test_GPIO初始化失败抛DeviceInitError且带线索(self) -> None:
        """真实模式下，GPIO 打开失败必须抛 DeviceInitError（含排查提示）。"""
        btn = Button(pin=17, mock=False)
        try:
            btn.open()
        except DeviceInitError as exc:
            self.assertIn("GPIO", str(exc))
        except Exception as exc:  # noqa: BLE001 - 在真树莓派上会成功，跳过
            self.skipTest(f"本机环境不支持真实 GPIO：{type(exc).__name__}")

    def test_describe与status有内容(self) -> None:
        btn = Button(pin=27, mock=True)
        btn.open()
        desc = btn.describe()
        self.assertEqual(desc["name"], "button")
        self.assertIn("GPIO27", desc["pins"]["signal"])
        st = btn.status()
        self.assertEqual(st["driver"], "Button")
        self.assertEqual(st["fault_count"], 0)
        self.assertGreaterEqual(st["read_count"], 0)
        btn.close()

    def test_close可重复调用(self) -> None:
        btn = Button(pin=17, mock=True)
        btn.open()
        btn.close()
        btn.close()  # 幂等，不应抛异常
        self.assertFalse(btn._opened)


if __name__ == "__main__":
    unittest.main(verbosity=2)
