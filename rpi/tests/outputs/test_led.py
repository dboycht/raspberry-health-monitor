"""LED 状态指示驱动测试。

重点覆盖"**任何时刻只有一种状态灯亮**"这条显示安全要求：
``send()`` 必须先熄灭其他颜色再点亮目标色 —— 否则红绿同亮会被看成黄色，
把"报警"读成"注意"，是会耽误救援的显示缺陷。
"""

from __future__ import annotations

import unittest

from health_monitor.hal import (
    AlarmDispatchError,
    BeepCommand,
    DeviceInitError,
    DeviceKind,
    DeviceNotReady,
    LightCommand,
    Sample,
    UnsupportedError,
)
from health_monitor.outputs.led import DEFAULT_PINS, LOGICAL_OFF, Led


class FakeSleep:
    """假 sleep：只记录时长，绝不真的等（闪烁测试因此瞬时完成）。"""

    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)

    @property
    def total_s(self) -> float:
        return sum(self.calls)


def make_led(**kwargs) -> Led:
    """造一个已 open 的 mock LED（并注入假 sleep）。"""
    kwargs.setdefault("mock", True)
    kwargs.setdefault("sleep", FakeSleep())
    led = Led(**kwargs)
    led.open()
    return led


def lit_colors(led: Led) -> list:
    """当前点亮的颜色列表（逻辑电平为 1 的颜色）。"""
    return sorted(c for c in led.pins if led.level(c) == 1)


class TestLedConstruction(unittest.TestCase):
    def test_默认引脚映射符合接线表(self) -> None:
        self.assertEqual(DEFAULT_PINS, {"green": 22, "yellow": 23, "red": 24})

    def test_可扩展蓝色(self) -> None:
        led = make_led(pins={"green": 22, "yellow": 23, "red": 24, "blue": 25})
        led.send(LightCommand(color="blue"))
        self.assertEqual(led.current_color, "blue")
        self.assertEqual(lit_colors(led), ["blue"])
        led.close()

    def test_颜色名大小写不敏感(self) -> None:
        led = make_led()
        led.send(LightCommand(color="RED"))
        self.assertEqual(led.current_color, "red")
        led.close()

    def test_空颜色名被拒绝(self) -> None:
        with self.assertRaises(UnsupportedError):
            Led(pins={"": 22}, mock=True)

    def test_空引脚表被拒绝(self) -> None:
        with self.assertRaises(UnsupportedError):
            Led(pins={}, mock=True)


class TestLedMock(unittest.TestCase):
    def test_未open就send必须报错(self) -> None:
        led = Led(mock=True, sleep=FakeSleep())
        with self.assertRaises(DeviceNotReady):
            led.send(LightCommand(color="green"))

    def test_未open就read必须报错(self) -> None:
        with self.assertRaises(DeviceNotReady):
            Led(mock=True, sleep=FakeSleep()).read()

    def test_未open就blink必须报错(self) -> None:
        with self.assertRaises(DeviceNotReady):
            Led(mock=True, sleep=FakeSleep()).blink("green")

    def test_mock模式可用且不碰硬件(self) -> None:
        with Led(mock=True, sleep=FakeSleep()) as led:
            self.assertTrue(led.mock)
            self.assertEqual(led.KIND, DeviceKind.LIGHT)
            self.assertEqual(led._devs, {}, "mock 模式下不允许创建真实 GPIO 对象")
            self.assertIsInstance(led.read(), Sample)

    def test_open后所有灯全灭(self) -> None:
        led = Led(mock=True, sleep=FakeSleep())
        led.open()
        self.assertEqual(lit_colors(led), [])
        self.assertEqual(led.current_color, LOGICAL_OFF)
        led.close()

    def test_current_color初始为off(self) -> None:
        led = make_led()
        self.assertEqual(led.current_color, "off")
        self.assertEqual(led.status()["current_color"], "off")
        led.close()

    # -- 核心：先熄其他色 -------------------------------------------------

    def test_send先熄灭其他颜色再点亮目标色(self) -> None:
        """只允许一种颜色亮：红灯亮起时绿灯必须已经灭掉。"""
        led = make_led()
        led.send(LightCommand(color="green"))
        self.assertEqual(lit_colors(led), ["green"])

        led.send(LightCommand(color="red"))
        self.assertEqual(lit_colors(led), ["red"], "点红灯时绿灯必须已灭")
        self.assertEqual(led.level("green"), 0)
        self.assertEqual(led.level("yellow"), 0)
        self.assertEqual(led.current_color, "red")
        led.close()

    def test_任何时刻都不会红绿同亮(self) -> None:
        """遍历所有颜色：每一步都必须"恰好一种颜色亮"。"""
        led = make_led()
        for color in ("green", "yellow", "red", "yellow", "green", "red", "off"):
            led.send(LightCommand(color=color))
            lit = lit_colors(led)
            expected = [] if color == "off" else [color]
            self.assertEqual(lit, expected, f"color={color} 时点亮的应是 {expected}，实际 {lit}")
        led.close()

    def test_off会熄灭全部(self) -> None:
        led = make_led()
        led.send(LightCommand(color="red"))
        led.send(LightCommand(color="off"))
        self.assertEqual(lit_colors(led), [])
        self.assertEqual(led.current_color, "off")
        led.close()

    def test_all_off可单独调用(self) -> None:
        led = make_led()
        led.send(LightCommand(color="yellow"))
        led.all_off()
        self.assertEqual(lit_colors(led), [])
        led.all_off()                       # 幂等
        self.assertEqual(led.current_color, "off")
        led.close()

    def test_先熄后点的顺序被真正执行(self) -> None:
        """用调用顺序断言顺序（不是只看最终电平）：最后一次"点亮"必须是目标色。"""

        class Recorder(Led):
            def __init__(self, **kw) -> None:
                super().__init__(**kw)
                self.trace: list = []

            def _set(self, color: str, on: bool) -> None:
                self.trace.append((color, on))
                super()._set(color, on)

        led = Recorder(mock=True, sleep=FakeSleep())
        led.open()
        led.send(LightCommand(color="green"))
        led.trace.clear()
        led.send(LightCommand(color="red"))
        # 最后一条必须是点亮 red；此前不得出现"点亮 green"（说明没先熄）
        self.assertEqual(led.trace[-1], ("red", True))
        self.assertNotIn(("green", True), led.trace)
        self.assertIn(("green", False), led.trace, "必须先熄灭绿灯")
        led.close()

    # -- 错误路径 --------------------------------------------------------

    def test_未知颜色抛UnsupportedError并列出可用颜色(self) -> None:
        led = make_led()
        with self.assertRaises(UnsupportedError) as ctx:
            led.send(LightCommand(color="purple"))
        text = str(ctx.exception)
        self.assertIn("purple", text)
        for color in ("green", "yellow", "red", "off"):
            self.assertIn(color, text, "报错必须列出可用颜色")
        self.assertEqual(led.status()["fault_count"], 1)
        led.close()

    def test_不认识的指令抛UnsupportedError(self) -> None:
        led = make_led()
        with self.assertRaises(UnsupportedError):
            led.send(BeepCommand(times=1))
        self.assertEqual(led.status()["fault_count"], 1)
        led.close()

    def test_GPIO操作失败抛AlarmDispatchError(self) -> None:
        led = make_led()

        def boom(color: str, on: bool) -> None:
            raise OSError("模拟 GPIO 写入失败")

        led._set = boom  # type: ignore[method-assign]
        with self.assertRaises(AlarmDispatchError) as ctx:
            led.send(LightCommand(color="red"))
        self.assertIn("red", str(ctx.exception))
        self.assertEqual(led.status()["fault_count"], 1)
        led.close()

    def test_真实模式GPIO失败抛DeviceInitError且带限流电阻线索(self) -> None:
        led = Led(mock=False, sleep=FakeSleep())
        try:
            led.open()
        except DeviceInitError as exc:
            text = str(exc)
            self.assertIn("LED", text)
            self.assertIn("220", text, "必须提示串限流电阻")
        except Exception as exc:  # noqa: BLE001 - 本机能开 GPIO 则跳过
            self.skipTest(f"本机环境不支持真实 GPIO：{type(exc).__name__}")


class TestLedBlink(unittest.TestCase):
    def test_闪烁用可注入sleep而不真的等待(self) -> None:
        sleep = FakeSleep()
        led = make_led(sleep=sleep, blink_hz=1.0, blink_duration_s=1.0)
        rounds = led.blink("red")
        # 1Hz → 半周期 0.5s；1 秒能走 1 个"灭→亮"完整轮次
        self.assertEqual(rounds, 1)
        self.assertGreater(sleep.total_s, 0.0)
        self.assertAlmostEqual(sleep.total_s, 1.0, places=3)
        led.close()

    def test_闪烁结束后停在亮态(self) -> None:
        """报警不能被"闪灭"吞掉：闪完必须保持点亮。"""
        led = make_led(sleep=FakeSleep(), blink_hz=2.0, blink_duration_s=1.0)
        led.send(LightCommand(color="red", blink=True))
        self.assertEqual(led.current_color, "red")
        self.assertEqual(led.level("red"), 1)
        self.assertEqual(lit_colors(led), ["red"])
        self.assertGreaterEqual(led.status()["blink_count"], 1)
        led.close()

    def test_blink_hz与时长决定轮数(self) -> None:
        led = make_led(sleep=FakeSleep(), blink_hz=1.0, blink_duration_s=3.0)
        self.assertEqual(led.blink("yellow"), 3)
        self.assertEqual(led.status()["blink_count"], 3)
        led.close()

    def test_blink未点亮目标色时报错(self) -> None:
        led = make_led()
        with self.assertRaises(UnsupportedError):
            led.blink("purple")
        led.close()

    def test_非法频率被纠正为默认1Hz(self) -> None:
        led = make_led(blink_hz=0)
        self.assertEqual(led.blink_hz, 1.0)
        led.close()

    def test_blink期间其他颜色保持熄灭(self) -> None:
        led = make_led(sleep=FakeSleep(), blink_hz=2.0, blink_duration_s=1.0)
        led.send(LightCommand(color="green"))
        led.send(LightCommand(color="red", blink=True))
        self.assertEqual(led.level("green"), 0)
        self.assertEqual(led.level("yellow"), 0)
        led.close()


class TestLedStatusAndLifecycle(unittest.TestCase):
    def test_status暴露current_color与电平(self) -> None:
        """Sample 是 frozen dataclass 且契约不许加字段 → 额外信息放在 status()。"""
        led = make_led()
        led.send(LightCommand(color="yellow"))
        st = led.status()
        self.assertEqual(st["driver"], "Led")
        self.assertEqual(st["current_color"], "yellow")
        self.assertEqual(st["levels"]["yellow"], 1)
        self.assertEqual(st["levels"]["red"], 0)
        self.assertEqual(st["pins"], {"green": 22, "yellow": 23, "red": 24})
        led.close()

    def test_read返回基类Sample且计数(self) -> None:
        led = make_led()
        sample = led.read()
        self.assertIsInstance(sample, Sample)
        self.assertEqual(sample.device, "led")
        self.assertTrue(sample.ok)
        led.read()
        self.assertEqual(led.status()["read_count"], 2)
        led.close()

    def test_describe写清物理脚号与限流电阻(self) -> None:
        led = make_led()
        desc = led.describe()
        self.assertEqual(desc["name"], "led")
        self.assertIn("GPIO22", desc["pins"]["green"])
        self.assertIn("物理脚 15", desc["pins"]["green"])
        self.assertIn("物理脚 16", desc["pins"]["yellow"])
        self.assertIn("物理脚 18", desc["pins"]["red"])
        self.assertIn("220", desc["pins"]["green"])
        self.assertIn("先熄灭其他颜色", desc["notes"])
        led.close()

    def test_available_colors含off(self) -> None:
        led = make_led()
        self.assertEqual(led.available_colors, ["green", "red", "yellow", "off"])
        led.close()

    def test_close全灭且幂等(self) -> None:
        led = make_led()
        led.send(LightCommand(color="red"))
        led.close()
        led.close()                          # 幂等，不抛异常
        self.assertFalse(led._opened)
        self.assertEqual(led.current_color, "off")
        self.assertEqual(led.level("red"), 0)

    def test_close后再send必须报错(self) -> None:
        led = make_led()
        led.close()
        with self.assertRaises(DeviceNotReady):
            led.send(LightCommand(color="green"))

    def test_active_low可配置(self) -> None:
        led = make_led(active_low=True)
        self.assertTrue(led.active_low)
        self.assertTrue(led.describe()["notes"].find("低") >= 0)
        led.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
