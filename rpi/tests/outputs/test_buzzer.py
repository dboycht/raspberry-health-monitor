"""蜂鸣器驱动测试。

覆盖：
1. **防长鸣**安全设计：单次 ``send()`` 总时长上限 10 秒，超出自动截断并计数；
2. **可注入 sleep**：注入假 sleep，单测瞬时完成、不等真实秒数；
3. ``beep_on`` / ``beep_off`` / ``silence`` 的动作与计数；
4. 错误路径：未 open 抛 ``DeviceNotReady``、未知指令抛 ``UnsupportedError``、
   失败抛 ``AlarmDispatchError`` 且**出错前先静音**。
"""

from __future__ import annotations

import unittest

from health_monitor.hal import (
    AlarmDispatchError,
    BeepCommand,
    DeviceInitError,
    DeviceKind,
    DeviceNotReady,
    Sample,
    SpeakCommand,
    UnsupportedError,
)
from health_monitor.outputs.buzzer import (
    DEFAULT_MAX_TOTAL_MS,
    BeepStep,
    Buzzer,
    plan_beeps,
    total_ms,
)


class FakeSleep:
    """假 sleep：只记录被"睡"了多久，绝不真的等。"""

    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)

    @property
    def total_s(self) -> float:
        return sum(self.calls)

    @property
    def total_ms(self) -> int:
        return int(round(self.total_s * 1000))


# ==========================================================================
# 一、纯逻辑：plan_beeps（节拍规划）
# ==========================================================================


class TestPlanBeeps(unittest.TestCase):
    def test_单响不需要静音(self) -> None:
        steps = plan_beeps(times=1, on_ms=200, off_ms=200)
        self.assertEqual(steps, [BeepStep(on_ms=200, off_ms=0)])
        self.assertEqual(total_ms(steps), 200)

    def test_三响的总时长含两次间隔(self) -> None:
        steps = plan_beeps(times=3, on_ms=200, off_ms=200)
        self.assertEqual(len(steps), 3)
        self.assertEqual(steps[0], BeepStep(on_ms=200, off_ms=200))
        self.assertEqual(steps[-1], BeepStep(on_ms=200, off_ms=0))
        self.assertEqual(total_ms(steps), 3 * 200 + 2 * 200)

    def test_次数为零不响(self) -> None:
        self.assertEqual(plan_beeps(times=0, on_ms=200, off_ms=200), [])
        self.assertEqual(plan_beeps(times=-3, on_ms=200, off_ms=200), [])

    def test_单响时长为零不响(self) -> None:
        self.assertEqual(plan_beeps(times=5, on_ms=0, off_ms=200), [])
        self.assertEqual(plan_beeps(times=5, on_ms=-100, off_ms=200), [])

    def test_超总时长上限被截断(self) -> None:
        """这是"防吵人"的核心：10 秒上限。100 响 × 1s 必须被截断。"""
        steps = plan_beeps(times=100, on_ms=1000, off_ms=500, max_total_ms=10_000)
        self.assertLessEqual(total_ms(steps), 10_000)
        self.assertLess(len(steps), 100)
        # 10 秒预算：每响 1000ms + 间隔 500ms，最后一响无间隔（截断处的静音也省掉）
        # → 能完整放下 6 响：1000*6 + 500*5 = 8500ms；第 7 响会超（10000 > 预算）。
        self.assertEqual(len(steps), 6)
        self.assertEqual(total_ms(steps), 8_500)

    def test_截断只切在完整节拍边界上(self) -> None:
        """绝不出现"响到一半被掐断"（半响听起来像故障）。"""
        steps = plan_beeps(times=100, on_ms=1000, off_ms=500, max_total_ms=10_000)
        for step in steps[:-1]:
            self.assertEqual(step.on_ms, 1000, "中间每一响都必须是完整的 1000ms")
        self.assertEqual(steps[-1].off_ms, 0, "最后一响后面不需要静音")

    def test_单响就超总上限时夹到上限(self) -> None:
        """单响时长 > 总上限时，夹到总量上限；报警"要么响、要么不响"，绝不静默丢弃。"""
        steps = plan_beeps(
            times=1, on_ms=60_000, off_ms=0, max_total_ms=10_000, max_on_ms=60_000
        )
        self.assertEqual(steps, [BeepStep(on_ms=10_000, off_ms=0)])

    def test_间隔过大时也至少响一次(self) -> None:
        """off_ms 被传成天量数字时，不能整条报警静默消失。"""
        steps = plan_beeps(times=3, on_ms=200, off_ms=60_000, max_total_ms=10_000)
        self.assertEqual(steps, [BeepStep(on_ms=200, off_ms=0)])

    def test_单响上限先于总量上限生效(self) -> None:
        steps = plan_beeps(times=1, on_ms=9999, off_ms=0, max_on_ms=1000, max_total_ms=10_000)
        self.assertEqual(steps, [BeepStep(on_ms=1000, off_ms=0)])

    def test_极短鸣叫不被误截断(self) -> None:
        steps = plan_beeps(times=5, on_ms=50, off_ms=50, max_total_ms=10_000)
        self.assertEqual(len(steps), 5)
        self.assertEqual(total_ms(steps), 5 * 50 + 4 * 50)


# ==========================================================================
# 二、驱动：Buzzer
# ==========================================================================


class TestBuzzerDriver(unittest.TestCase):
    def test_未open就send必须报错(self) -> None:
        bz = Buzzer(pin=18, mock=True)
        with self.assertRaises(DeviceNotReady):
            bz.send(BeepCommand())

    def test_未open就read必须报错(self) -> None:
        with self.assertRaises(DeviceNotReady):
            Buzzer(pin=18, mock=True).read()

    def test_mock模式可用且不碰硬件(self) -> None:
        sleep = FakeSleep()
        with Buzzer(pin=18, mock=True, sleep=sleep) as bz:
            self.assertTrue(bz.mock)
            self.assertEqual(bz.KIND, DeviceKind.AUDIO)
            self.assertIsNone(bz._dev, "mock 模式下不允许创建真实 GPIO 对象")
            self.assertIsInstance(bz.read(), Sample)

    def test_send按次数与节奏鸣叫(self) -> None:
        sleep = FakeSleep()
        bz = Buzzer(pin=18, mock=True, sleep=sleep)
        bz.open()
        bz.send(BeepCommand(times=3, on_ms=200, off_ms=100))
        self.assertEqual(bz.status()["beep_count"], 3)
        self.assertEqual(sleep.calls, [0.2, 0.1, 0.2, 0.1, 0.2])
        self.assertFalse(bz.status()["sounding"], "鸣叫结束后必须回到静音")
        bz.close()

    def test_超上限时截断并计数(self) -> None:
        """100 响 × 1 秒：必须截断到 10 秒内，并留下"被截断过"的痕迹。"""
        sleep = FakeSleep()
        bz = Buzzer(pin=18, mock=True, sleep=sleep)
        bz.open()
        bz.send(BeepCommand(times=100, on_ms=1000, off_ms=500))
        self.assertLessEqual(sleep.total_ms, DEFAULT_MAX_TOTAL_MS)
        self.assertEqual(bz.status()["truncated_count"], 1)
        self.assertEqual(bz.status()["planned_steps"], 6)
        bz.close()

    def test_未超上限不计数截断(self) -> None:
        bz = Buzzer(pin=18, mock=True, sleep=FakeSleep())
        bz.open()
        bz.send(BeepCommand(times=2, on_ms=100, off_ms=100))
        self.assertEqual(bz.status()["truncated_count"], 0)
        bz.close()

    def test_单次send总时长上限默认10秒(self) -> None:
        self.assertEqual(DEFAULT_MAX_TOTAL_MS, 10_000)
        bz = Buzzer(mock=True)
        self.assertEqual(bz.max_total_ms, 10_000)
        self.assertEqual(bz.status()["max_total_ms"], 10_000)
        bz.close()

    def test_上限可配置(self) -> None:
        sleep = FakeSleep()
        bz = Buzzer(pin=18, mock=True, max_total_ms=500, sleep=sleep)
        bz.open()
        bz.send(BeepCommand(times=10, on_ms=500, off_ms=500))
        self.assertLessEqual(sleep.total_ms, 500)
        bz.close()

    def test_不认识的指令抛UnsupportedError(self) -> None:
        bz = Buzzer(pin=18, mock=True, sleep=FakeSleep())
        bz.open()
        with self.assertRaises(UnsupportedError):
            bz.send(SpeakCommand(text="你好"))
        self.assertEqual(bz.status()["fault_count"], 1)
        bz.close()

    def test_beep_on_off与silence(self) -> None:
        bz = Buzzer(pin=18, mock=True, sleep=FakeSleep())
        bz.open()
        bz.beep_on()
        self.assertTrue(bz.status()["sounding"])
        bz.beep_off()
        self.assertFalse(bz.status()["sounding"])
        bz.beep_on()
        bz.silence()                       # 消音：业务层"静音"按钮用
        self.assertFalse(bz.status()["sounding"])
        self.assertEqual(bz.status()["beep_count"], 2)
        bz.close()

    def test_未open时beep_on也要报错(self) -> None:
        bz = Buzzer(pin=18, mock=True, sleep=FakeSleep())
        with self.assertRaises(DeviceNotReady):
            bz.beep_on()

    def test_GPIO操作失败抛AlarmDispatchError并先静音(self) -> None:
        """出错时最糟的情况是"异常了还在叫"，所以必须先静音再抛。"""
        bz = Buzzer(pin=18, mock=True, sleep=FakeSleep())
        bz.open()

        class BoomDev:
            def __init__(self) -> None:
                self.off_called = False

            def on(self) -> None:
                raise OSError("模拟 GPIO 写入失败")

            def off(self) -> None:
                self.off_called = True

        dev = BoomDev()
        bz._dev = dev                       # 注入一个"一开就炸"的假器件
        with self.assertRaises(AlarmDispatchError) as ctx:
            bz.send(BeepCommand(times=1, on_ms=100, off_ms=0))
        self.assertIn("蜂鸣器", str(ctx.exception))
        self.assertTrue(dev.off_called, "抛异常前必须先尝试静音")
        self.assertFalse(bz.status()["sounding"])
        self.assertEqual(bz.status()["fault_count"], 1)
        bz.close()

    def test_真实模式GPIO失败抛DeviceInitError且带接线线索(self) -> None:
        bz = Buzzer(pin=18, mock=False)
        try:
            bz.open()
        except DeviceInitError as exc:
            text = str(exc)
            self.assertIn("蜂鸣器", text)
            self.assertIn("GPIO18", text)
            self.assertIn("220", text, "必须提示串限流电阻")
        except Exception as exc:  # noqa: BLE001 - 本机能开 GPIO 则跳过
            self.skipTest(f"本机环境不支持真实 GPIO：{type(exc).__name__}")

    def test_describe与status有内容(self) -> None:
        bz = Buzzer(pin=18, mock=True, sleep=FakeSleep())
        bz.open()
        desc = bz.describe()
        self.assertEqual(desc["name"], "buzzer")
        self.assertIn("GPIO18", desc["pins"]["signal"])
        self.assertIn("物理脚 12", desc["pins"]["signal"])
        self.assertIn("有源", desc["notes"])
        self.assertIn("10.0s", desc["notes"], "必须写清防长鸣上限")
        st = bz.status()
        self.assertEqual(st["driver"], "Buzzer")
        self.assertEqual(st["pin"], 18)
        self.assertEqual(st["fault_count"], 0)
        bz.close()

    def test_close可重复调用且幂等(self) -> None:
        bz = Buzzer(pin=18, mock=True, sleep=FakeSleep())
        bz.open()
        bz.close()
        bz.close()
        self.assertFalse(bz._opened)

    def test_open幂等(self) -> None:
        bz = Buzzer(pin=18, mock=True, sleep=FakeSleep())
        bz.open()
        bz.open()
        self.assertTrue(bz._opened)
        bz.close()

    def test_read计数与status一致(self) -> None:
        bz = Buzzer(pin=18, mock=True, sleep=FakeSleep())
        bz.open()
        bz.read()
        bz.read()
        self.assertEqual(bz.status()["read_count"], 2)
        bz.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
