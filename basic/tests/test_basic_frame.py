#!/usr/bin/env python3
"""DHT11 单总线**解码**的测试：用合成时序喂进解码器，断言解出原文。

为什么这么测（这是本项目最重要的一条测试经验）：
真机上解码错了只会看到一句"校验和不符"，查起来极慢；而**合成报文 → 解码 → 断言**
能在毫秒级暴露"错位一位""少一个边沿"这类问题。
所以这里的用例都是**不碰硬件**的：自己按协议造边沿序列。
"""

from __future__ import annotations

import unittest

from basic.dht11read import (
    BIT_THRESHOLD_US,
    Dht11Error,
    GpiozeroBackend,
    LgpioBackend,
    MockBackend,
    check_range,
    decode_frame,
    synth_frame,
)


class FakeClock:
    """假时钟：让"时间流逝"在测试里瞬时发生（不用真的 sleep 10 秒）。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.value = float(start)

    def __call__(self) -> float:
        return self.value


class TestDecodeSyntheticFrame(unittest.TestCase):
    def test_合成帧能解回原文(self):
        """★ 主用例：合成 (25.0℃, 58.0%) → 解码 → 必须一模一样。"""
        edges = synth_frame(25.0, 58.0)
        temperature, humidity = decode_frame(edges)
        self.assertAlmostEqual(temperature, 25.0, places=1)
        self.assertAlmostEqual(humidity, 58.0, places=1)

    def test_合成帧的形状与真机一致(self):
        """合成帧必须与 lgpio 回调在真机上看到的形状一致：**82 个边沿 / 41 个高电平段**。

        判据来自协议：应答高（1 段）+ 40 个数据位（40 段），每段后面跟一个低电平，
        序列从"应答的上升沿"开始（回调在主机释放总线之后才注册）。
        这条断言能防止"合成得太顺手"——例如多补一个收尾边沿，
        那样测出来的"能解码"在真机上并不成立。
        """
        edges = synth_frame(25.0, 58.0)
        self.assertEqual(len(edges), 82)
        self.assertEqual(edges[0][0], 1)                 # 第一个边沿是应答的上升沿
        self.assertEqual(edges[-1][0], 0)                # 最后一个边沿是最后一位的下降沿
        highs = sum(1 for level, _ in edges if level == 1)
        self.assertEqual(highs, 41)                      # 应答 + 40 个数据位

    def test_多组数值都正确(self):
        for temperature, humidity in ((0.0, 20.0), (26.0, 45.0), (50.0, 90.0), (23.5, 61.0)):
            with self.subTest(temperature=temperature, humidity=humidity):
                got_t, got_h = decode_frame(synth_frame(temperature, humidity))
                self.assertAlmostEqual(got_t, temperature, places=1)
                self.assertAlmostEqual(got_h, humidity, places=1)

    def test_应答脉冲必须按位置丢掉(self):
        """应答高电平（约 80µs）与数据位"1"（约 70µs）几乎同宽，只能按位置丢。

        做法：把合成帧里的应答脉冲宽度改成和数据位"1"一样（70µs）——
        若解码器是靠"宽度阈值过滤应答"，这么改就会错位；靠位置丢则仍然正确。
        """
        edges = synth_frame(25.0, 58.0)
        narrowed = [(1, edges[0][1] + 70_000)] + edges[1:]
        temperature, humidity = decode_frame(narrowed)
        self.assertAlmostEqual(temperature, 25.0, places=1)
        self.assertAlmostEqual(humidity, 58.0, places=1)

    def test_一帧边界处的宽度下标(self):
        """边界：最后一对边沿也要参与解码（下标是 len-2 / len-1），否则少一位。

        判据：同一个合成帧，**原样解码成功**、**砍掉最后两个边沿就报"数据位不足"**。
        """
        edges = synth_frame(25.0, 58.0)
        self.assertAlmostEqual(decode_frame(edges)[0], 25.0, places=1)
        with self.assertRaises(Dht11Error) as ctx:
            decode_frame(edges[:-2])
        self.assertIn("数据位不足", str(ctx.exception))

    def test_没有边沿说明传感器没应答(self):
        with self.assertRaises(Dht11Error) as ctx:
            decode_frame([])
        self.assertIn("没有应答", str(ctx.exception))

    def test_边沿太少也报没应答(self):
        with self.assertRaises(Dht11Error) as ctx:
            decode_frame(synth_frame(25.0, 58.0)[:20])
        self.assertIn("没有应答", str(ctx.exception))

    def test_校验和不符要报错(self):
        """把最后一个数据位取反 → 校验和必然对不上，**必须报错**。

        ⚠️ 这里刻意"取反"而不是"设成 1"：若原始位本来就是 1，设成 1 等于没改，
        测试会变成一条空断言（"没改也能过"）——这类假测试比没有测试更危险。
        """
        edges = synth_frame(25.0, 58.0)
        self.assertEqual(edges[0][0], 1)
        self.assertEqual(edges[1][0], 0)
        self.assertEqual(len(edges) % 2, 0)          # 一帧以"下降沿"结束
        high_index = len(edges) - 2                  # 最后一个高电平段的起点
        level, start_ns = edges[high_index]
        self.assertEqual(level, 1)
        width_us = (edges[high_index + 1][1] - start_ns) / 1000.0
        flipped_us = 70.0 if width_us < BIT_THRESHOLD_US else 26.0
        edges[high_index] = (1, start_ns)
        edges[high_index + 1] = (0, start_ns + int(flipped_us * 1000))
        with self.assertRaises(Dht11Error) as ctx:
            decode_frame(edges)
        self.assertIn("校验和不符", str(ctx.exception))

    def test_超宽脉冲会被过滤掉(self):
        """噪声造成的超宽高电平段要被丢弃（而不是解成一个错误的 bit）。

        ⚠️ 注意注入方式：噪声段必须**真的占掉时间**（宽度算得出来），
        不能把后一个边沿的时间戳往前挪（那会算出负宽度，属于伪造时序）。
        """
        edges = synth_frame(25.0, 58.0)
        ack_end = edges[1][1]                       # 应答结束的下降沿时刻
        noisy = edges[:2] + [(1, ack_end), (0, ack_end + 500_000)] + edges[2:]
        temperature, humidity = decode_frame(noisy)
        self.assertAlmostEqual(temperature, 25.0, places=1)
        self.assertAlmostEqual(humidity, 58.0, places=1)


class TestRangeCheck(unittest.TestCase):
    def test_正常读数通过(self):
        self.assertIsNone(check_range(25.0, 58.0))

    def test_超量程返回原因(self):
        self.assertIn("温度", check_range(60.0, 58.0) or "")
        self.assertIn("湿度", check_range(25.0, 5.0) or "")

    def test_边界值算通过(self):
        self.assertIsNone(check_range(0.0, 20.0))
        self.assertIsNone(check_range(50.0, 90.0))


class TestMockBackend(unittest.TestCase):
    def test_合成数据在量程内(self):
        backend = MockBackend()
        temperature, humidity = backend.read_once()
        self.assertIsNone(check_range(temperature, humidity))

    def test_指定值原样返回(self):
        backend = MockBackend(explicit=(25.0, 58.0))
        self.assertEqual(backend.read_once(), (25.0, 58.0))

    def test_数据随时间缓慢变化而不是一条直线(self):
        """用**注入的假时钟**推进 10 秒，合成值必须变化（不靠"本次运行跑得够久"碰运气）。"""
        clock = FakeClock()
        backend = MockBackend(seed=1, clock=clock)
        first = backend.read_once()
        clock.value += 10.0
        later = backend.read_once()
        self.assertNotEqual(first, later)

    def test_合成值可复现(self):
        """同一个 seed + 同一段假时间 → 同一串数值（否则单测会偶发失败）。"""
        clock_a, clock_b = FakeClock(), FakeClock()
        backend_a, backend_b = MockBackend(seed=7, clock=clock_a), MockBackend(seed=7, clock=clock_b)
        for _ in range(3):
            self.assertEqual(backend_a.read_once(), backend_b.read_once())
            clock_a.value += 7.0
            clock_b.value += 7.0

    def test_close幂等(self):
        backend = MockBackend()
        backend.close()
        backend.close()


class TestBackendAvailability(unittest.TestCase):
    """后端可用性：**如实反映本机情况**，不假装能用。"""

    def test_lgpio后端在电脑上不可用(self):
        # 开发机（Windows）没有 lgpio：构造应当失败，且失败原因可读
        try:
            LgpioBackend(4)
        except Exception as exc:  # noqa: BLE001 - 期望就是 ImportError 之类
            self.assertTrue(str(exc) or type(exc).__name__)
        else:  # pragma: no cover - 在有 lgpio 的树莓派上会走到这里
            self.skipTest("本机装有 lgpio（树莓派），跳过'不可用'断言")

    def test_gpiozero没有DHT11类时如实报错(self):
        from basic.dht11read import Dht11Reader

        # 强制走"真硬件"路径：如果本机 gpiozero 已删掉 DHT11（新系统就是如此），
        # open() 必须抛出带排查命令的 Dht11Error，**不能静默降级成 mock**。
        reader = Dht11Reader(pin=4, mock=False)
        try:
            backend = reader.open()
        except Dht11Error as exc:
            self.assertIn("没有可用的 DHT11 读取后端", str(exc))
            self.assertIn("--mock", str(exc))
        else:  # pragma: no cover - 只有在真有 GPIO 的机器上才会成功
            self.assertIn(backend, ("lgpio", "gpiozero"))
        finally:
            reader.close()


if __name__ == "__main__":
    unittest.main()
