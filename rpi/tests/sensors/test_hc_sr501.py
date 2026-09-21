"""HC-SR501 人体红外驱动测试。

要点（照抄 ``test_button.py`` 的三条纪律）：
1. 纯逻辑（去抖 / 活动计时）用**假时钟**测，不 sleep、不偶发失败；
2. mock 模式下断言"绝不创建真实 GPIO 对象"；
3. 错误路径要测（未 open 就 read 抛 ``DeviceNotReady``；真实模式失败抛 ``DeviceInitError``）。
"""

from __future__ import annotations

import unittest

from health_monitor.hal import (
    DeviceInitError,
    DeviceKind,
    DeviceNotReady,
    MotionSample,
    MotionState,
    UnsupportedError,
)
from health_monitor.sensors.hc_sr501 import HcSr501, MotionTracker


class FakeClock:
    """假时钟：单测里"瞬间"跨越任意时长，杜绝 sleep 与偶发失败。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> float:
        self.t += seconds
        return self.t

    def feed(self, tracker: MotionTracker, level: int, seconds: float, step: float = 0.05) -> list:
        """保持某个电平 ``seconds`` 秒（按 ``step`` 步进喂采样），返回每次的状态。"""
        states = []
        steps = max(1, int(round(seconds / step)))
        for _ in range(steps):
            self.advance(step)
            states.append(tracker.update(level))
        return states


# ==========================================================================
# 一、纯逻辑：MotionTracker
# ==========================================================================


class TestMotionTracker(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.tracker = MotionTracker(
            bounce_s=0.2, clock=self.clock, settle_s=0.0, active_high=True
        )

    # -- 去抖 ------------------------------------------------------------

    def test_首采样作为基准不产生误触发(self) -> None:
        self.assertEqual(self.tracker.update(1), MotionState.DETECTED)
        self.assertTrue(self.tracker._stable)

    def test_去抖窗口内的抖动被忽略(self) -> None:
        """3 次 50ms 的短脉冲（< bounce_s=200ms）不应被确认为"检测到人"。"""
        self.tracker.update(0)                      # 基准：无人
        self.assertFalse(self.tracker._stable)
        for _ in range(3):
            self.clock.feed(self.tracker, 1, 0.05)
            self.clock.feed(self.tracker, 0, 0.05)
        self.assertFalse(self.tracker._stable, "抖动必须被去抖窗口挡掉")
        self.assertEqual(self.tracker.state, MotionState.IDLE)

    def test_超过去抖窗口才被确认(self) -> None:
        self.tracker.update(0)
        self.clock.advance(0.05)
        self.assertEqual(self.tracker.update(1), MotionState.IDLE)  # 刚跳变，尚未确认
        self.clock.advance(0.1)
        self.assertEqual(self.tracker.update(1), MotionState.IDLE)  # 0.15s < 0.2s
        self.clock.advance(0.1)
        self.assertEqual(self.tracker.update(1), MotionState.DETECTED)  # 0.25s >= 0.2s

    def test_低电平有效可配置(self) -> None:
        tracker = MotionTracker(bounce_s=0.2, clock=self.clock, settle_s=0.0, active_high=False)
        self.assertTrue(tracker.is_detected(0))
        self.assertFalse(tracker.is_detected(1))
        self.assertEqual(tracker.update(0), MotionState.DETECTED)

    # -- 热身期 ----------------------------------------------------------

    def test_热身期内一律UNKNOWN且不猜(self) -> None:
        """上电初期模块会乱跳；热身期必须如实上报 UNKNOWN，绝不假装 IDLE。"""
        clock = FakeClock()
        tracker = MotionTracker(bounce_s=0.2, clock=clock, settle_s=1.0)
        states = clock.feed(tracker, 1, 0.9)
        self.assertTrue(all(s is MotionState.UNKNOWN for s in states))
        self.assertIsNone(tracker.last_motion_ts, "热身期的噪声不能当成'检测到人'")

    def test_热身期结束后才开始判定(self) -> None:
        clock = FakeClock()
        tracker = MotionTracker(bounce_s=0.0, clock=clock, settle_s=1.0)
        clock.feed(tracker, 0, 1.1)
        self.assertIsNotNone(tracker.update(0))
        self.assertNotEqual(tracker.state, MotionState.UNKNOWN)

    # -- 活动时长 / 久无活动 ----------------------------------------------

    def test_active_for_s随时钟增长(self) -> None:
        self.tracker.update(0)
        self.clock.feed(self.tracker, 1, 0.5)
        # 首个"与稳定状态不一致"的采样在 t=0.05，去抖 0.2s 后于 t=0.25 确认，
        # 因此喂 0.5s 高电平后 active_for_s 约为 0.25s（从**确认**时刻起算）。
        self.assertGreaterEqual(self.tracker.active_for_s(), 0.2)
        self.clock.feed(self.tracker, 1, 1.0)
        self.assertGreaterEqual(self.tracker.active_for_s(), 1.2)

    def test_无人时active_for_s为0(self) -> None:
        self.clock.feed(self.tracker, 0, 1.0)
        self.assertEqual(self.tracker.active_for_s(), 0.0)

    def test_seconds_since_motion在有人时为0(self) -> None:
        self.clock.feed(self.tracker, 1, 1.0)
        self.assertEqual(self.tracker.seconds_since_motion(), 0.0)

    def test_seconds_since_motion反映最后一次检测到人(self) -> None:
        """人离开后 OUT 仍会保持高电平一段（模块延时），驱动要按"最后检测到人"算。

        语义：``last_motion_ts`` = 最后一次**读到家用电平为"有人"**的时刻。
        """
        self.tracker.update(0)
        self.clock.feed(self.tracker, 1, 0.5)     # 有人：最后一次"检测到人"在 t≈0.50
        self.clock.feed(self.tracker, 0, 0.3)     # 人离开（t≈0.55 起为低），去抖后确认无人
        self.assertEqual(self.tracker.state, MotionState.IDLE)
        self.assertIsNotNone(self.tracker.last_motion_ts)
        # "最后一次检测到人" = 最后一拍**电平为有人**的采样（此时无模块延时，故等于 0.3s）
        self.assertAlmostEqual(self.tracker.seconds_since_motion(), 0.3, places=2)
        self.clock.feed(self.tracker, 0, 1.0)     # 再过 1 秒，仍然没有动静
        self.assertAlmostEqual(self.tracker.seconds_since_motion(), 1.3, places=1)

    def test_从未检测到人时返回无穷大而不是0(self) -> None:
        """返回 0 会让"从没动过"被误判成"刚刚动过"（久无活动报警永远不会触发）。"""
        self.assertEqual(self.tracker.seconds_since_motion(), float("inf"))

    def test_模块保持高电平时活动计时继续累加(self) -> None:
        """这是 HC-SR501 的器件特性：人走了 OUT 还是高，驱动应认为"人还在"。"""
        self.clock.feed(self.tracker, 1, 3.0)
        self.assertEqual(self.tracker.state, MotionState.DETECTED)
        self.assertGreaterEqual(self.tracker.active_for_s(), 2.5)
        self.assertEqual(self.tracker.seconds_since_motion(), 0.0)


# ==========================================================================
# 二、驱动：HcSr501
# ==========================================================================


class TestHcSr501Driver(unittest.TestCase):
    @staticmethod
    def _settle(pir: HcSr501, clock: FakeClock, seconds: float = 0.3) -> MotionSample:
        """模拟"业务层周期性轮询"，让状态变化真正被去抖确认。

        去抖需要**两拍**：第一拍是"与稳定状态不一致"的采样（以此建立候选窗口），
        第二拍要等 ``bounce_s`` 之后。真实运行中业务层每 100~500ms 轮询一次，
        天然满足；单测里必须显式补上第一拍（并在需要时补第二拍）。
        """
        clock.advance(seconds)
        sample = pir.read()          # 第一拍：建立"候选变化"窗口
        clock.advance(seconds)
        return pir.read()            # 第二拍：跨过去抖窗口，确认变化

    def test_未open就read必须报错(self) -> None:
        pir = HcSr501(pin=17, mock=True)
        with self.assertRaises(DeviceNotReady):
            pir.read()

    def test_未open时seconds_since_motion也要报错(self) -> None:
        pir = HcSr501(pin=17, mock=True)
        with self.assertRaises(DeviceNotReady):
            pir.seconds_since_motion()

    def test_mock模式可用且不碰硬件(self) -> None:
        with HcSr501(pin=17, mock=True) as pir:
            self.assertTrue(pir.mock)
            self.assertEqual(pir.KIND, DeviceKind.MOTION)
            self.assertIsNone(pir._pir, "mock 模式下不允许创建真实 GPIO 对象")
            self.assertIsInstance(pir.read(), MotionSample)

    def test_inject_motion能模拟有人与无人(self) -> None:
        pir = HcSr501(pin=17, mock=True, settle_s=0.0, bounce_s=0.0)
        pir.open()
        pir.inject_motion(True)
        self.assertTrue(pir.read().detected)
        pir.inject_motion(False)
        self.assertFalse(pir.read().detected)
        pir.close()

    def test_真实模式下inject必须被拒绝(self) -> None:
        """防止把"人造数据"用到真机上。"""
        pir = HcSr501(pin=17, mock=False)
        with self.assertRaises(UnsupportedError):
            pir.inject_motion(True)

    def test_未注入时按周期默认触发(self) -> None:
        """mock 默认剧本：每个周期开头一段"有人"，其余时间"无人"。

        注意：去抖用的是 tracker 时钟，而"剧本"用 mock_clock，两个都要推进
        （真实运行中它们都是同一个走动的时钟）。
        """
        tracker_clock = FakeClock()
        mock_clock = FakeClock()
        pir = HcSr501(pin=17, mock=True, settle_s=0.0, bounce_s=0.2,
                      mock_period_s=10.0, mock_cycle_s=2.0,
                      clock=tracker_clock, mock_clock=mock_clock)
        pir.open()
        self.assertTrue(pir.read().detected, "周期开头应为'有人'")
        mock_clock.advance(5.0)           # 进入周期中段 → 剧本变为"无人"
        self.assertFalse(self._settle(pir, tracker_clock).detected, "周期中段应为'无人'")
        mock_clock.advance(5.0)           # 进入下一个周期的开头 → 又"有人"
        self.assertTrue(self._settle(pir, tracker_clock).detected)
        pir.close()

    def test_read返回的state与tracker一致(self) -> None:
        clock = FakeClock()
        pir = HcSr501(pin=17, mock=True, settle_s=0.0, bounce_s=0.2, clock=clock)
        pir.open()
        pir.inject_motion(False)
        sample = pir.read()
        self.assertEqual(sample.state, MotionState.IDLE)
        self.assertFalse(sample.detected)
        clock.advance(0.3)
        pir.inject_motion(True)
        sample = self._settle(pir, clock)   # 第一拍建立候选，间隔 0.3s 后第二拍确认
        self.assertEqual(sample.state, MotionState.DETECTED)
        self.assertTrue(sample.detected)
        pir.close()

    def test_读取失败抛DeviceIOError并留痕(self) -> None:
        """mock=False 但注入一个"读就炸"的假 GPIO：验证异常翻译与留痕。

        （用 mock=False + 假 _pir，而不是 monkeypatch mock 模式的方法——
        mock 模式下驱动**故意不读** _read_level，patched 方法根本不会被调用。）
        """
        from health_monitor.hal import DeviceIOError

        class BoomPir:
            @property
            def value(self) -> int:
                raise OSError("模拟引脚读取失败")

        pir = HcSr501(pin=17, mock=False, settle_s=0.0)
        pir._opened = True              # 跳过真实 GPIO 初始化（本机可能没 gpiozero）
        pir._tracker = MotionTracker(bounce_s=0.0, settle_s=0.0)
        pir._pir = BoomPir()

        with self.assertRaises(DeviceIOError) as ctx:
            pir.read()
        self.assertIn("模拟引脚读取失败", str(ctx.exception))
        self.assertEqual(pir.status()["fault_count"], 1)
        self.assertIn("模拟引脚读取失败", pir.status()["last_error"] or "")

    def test_真实模式GPIO失败抛DeviceInitError且带线索(self) -> None:
        pir = HcSr501(pin=17, mock=False)
        try:
            pir.open()
        except DeviceInitError as exc:
            text = str(exc)
            self.assertIn("HC-SR501", text)
            self.assertIn("5V", text, "必须提示 5V 供电这条最常见的接线错误")
        except Exception as exc:  # noqa: BLE001 - 本机若有 gpiozero 且能开 GPIO 则跳过
            self.skipTest(f"本机环境不支持真实 GPIO：{type(exc).__name__}")

    def test_describe与status有内容(self) -> None:
        pir = HcSr501(pin=17, mock=True)
        pir.open()
        desc = pir.describe()
        self.assertEqual(desc["name"], "hc_sr501")
        self.assertIn("GPIO17", desc["pins"]["out"])
        self.assertIn("物理脚 11", desc["pins"]["out"])
        self.assertIn("5V", desc["pins"]["vcc"])
        self.assertIn("3.3V", desc["notes"], "OUT 为 3.3V 可直连这条必须写清楚")
        st = pir.status()
        self.assertEqual(st["driver"], "HcSr501")
        self.assertEqual(st["fault_count"], 0)
        self.assertGreaterEqual(st["read_count"], 0)
        pir.close()

    def test_close可重复调用(self) -> None:
        pir = HcSr501(pin=17, mock=True)
        pir.open()
        pir.close()
        pir.close()
        self.assertFalse(pir._opened)

    def test_open幂等不重复创建(self) -> None:
        pir = HcSr501(pin=17, mock=True)
        pir.open()
        tracker = pir._tracker
        pir.open()
        self.assertIs(pir._tracker, tracker, "重复 open() 不应重建去抖器（否则热身期会被重置）")
        pir.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
