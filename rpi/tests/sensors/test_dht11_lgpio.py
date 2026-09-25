"""DHT11 **自实现单总线解码**的测试（用合成时序，不需要真传感器）。

背景（2026-09-24 真机实测）
--------------------------
Debian 13 的 ``python3-gpiozero 2.0.1`` **删除了 DHT11/DHT22**，
所以本项目自己用 lgpio 的**纳秒级边沿时间戳**实现单总线读取。
DHT11 每个 bit 靠"高电平持续时间"区分：约 26~28µs = 0，约 70µs = 1（阈值取 50µs）。

这类"时序解码"代码如果只靠插上真传感器试，会非常难调（错了只看到"校验和不符"）。
所以这里**用合成的边沿时间戳**直接喂给解码逻辑：

1. 造出 DHT11 的完整帧（应答脉冲 + 40 bit + 校验和）；
2. 用假的 lgpio 模块把合成边沿按纳秒时间戳"回放"进回调；
3. 断言解出来的温湿度与原始一致。

同时覆盖三类失败路径：**传感器不应答**、**数据位不足**、**校验和不符**。
"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

RPI_DIR = Path(__file__).resolve().parents[2]
if str(RPI_DIR) not in sys.path:
    sys.path.insert(0, str(RPI_DIR))

from health_monitor.hal.exceptions import DeviceIOError  # noqa: E402
from health_monitor.sensors.dht11 import Dht11  # noqa: E402

#: 合成时序用的时长（微秒）——按 DHT11 数据手册的典型值
T_LOW_BIT_US = 26.0      # "0" 的高电平宽度
T_HIGH_BIT_US = 70.0     # "1" 的高电平宽度
T_BIT_LOW_US = 50.0      # 每个 bit 之间固定的低电平宽度
T_RESPONSE_US = 80.0     # 应答信号：低 80µs + 高 80µs


def build_frame(temperature: int, humidity: int, checksum: int | None = None) -> list[tuple[int, int]]:
    """按 DHT11 协议造一串 ``(level, timestamp_ns)`` 边沿。

    帧结构：主机拉低（起始）→ 传感器应答（低 80µs、高 80µs）→ 40 bit（MSB 先出）
    → 校验和（前 4 字节之和的低 8 位）。

    ``checksum`` 可显式指定（测试"校验和不符"时用）。
    """
    data = [humidity, 0, temperature, 0]
    data.append(sum(data) & 0xFF if checksum is None else (checksum & 0xFF))

    bits: list[int] = []
    for byte in data:
        for shift in range(7, -1, -1):
            bits.append((byte >> shift) & 1)

    edges: list[tuple[int, int]] = []
    ts = 0.0  # 纳秒

    def add(level: int, us: float) -> None:
        nonlocal ts
        edges.append((level, int(ts)))
        ts += us * 1000.0

    # 应答：低 → 高
    add(0, T_RESPONSE_US)
    add(1, T_RESPONSE_US)
    # 40 bit：每 bit = 低电平(50µs) + 高电平(26 或 70µs)
    # 边沿是"电平变化"：每个 bit 产生 一段低（下降沿）与 一段高（上升沿）
    for bit in bits:
        add(0, T_BIT_LOW_US)                             # 下降沿（进入低电平）
        add(1, T_HIGH_BIT_US if bit else T_LOW_BIT_US)   # 上升沿（高电平宽度 = 该 bit）
    # ⚠️ **收尾低电平**：真实 DHT11 传完第 40 位会把总线拉回低电平，
    #    少了这个下降沿，最后一个 bit 就**没有下降沿来结束它** → 也会少一位。
    add(0, T_BIT_LOW_US)
    # ⚠️⚠️ **帧尾"上拉回空闲"的上升沿**（2026-09-25 真机实测补上，见 ERROR.md E37）：
    #    最后一位结束后，从机上拉把总线重新拉高（实测时间戳：…低 3749.1 → 高 3802.9 µs）。
    #    它**不是数据位**，但会让整帧变成 **83 个边沿 / 42 个上升沿 + 41 个下降沿**；
    #    解码器若"从下标 1 开始逐对扫"，应答的配对就会错位一格 ⇒ 只解出 39/40 位。
    #    修法是"取最后 41 对"，这一行合成边沿就是那条断言的靶子（去掉它必须仍然能解）。
    add(1, T_BIT_LOW_US)
    return edges


class FakeCallback:
    def __init__(self, fn):
        self._fn = fn
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class FakeLgpio(types.ModuleType):
    """把 lgpio 的调用记下来，并在注册回调时**回放**合成边沿。

    这样测试跑的是真实的解码代码路径（含"应答后的第一个下降沿"这种边界），
    而不是另写一份解码逻辑（那样测不到真代码）。
    """

    BOTH_EDGES = 3
    RISING_EDGE = 1
    FALLING_EDGE = 2
    SET_PULL_UP = 32

    def __init__(self, edges: list[tuple[int, int]] | None) -> None:
        super().__init__("lgpio")
        self.edges = edges
        self.calls: list[str] = []
        self.callback_cancelled = False

    # 这些方法只要"能被调用且不抛"即可
    def gpiochip_open(self, chip: int):  # noqa: ANN201
        self.calls.append(f"gpiochip_open({chip})")
        return 99

    def gpiochip_close(self, handle) -> None:  # noqa: ANN001
        self.calls.append("gpiochip_close")

    def gpio_free(self, handle, pin) -> None:  # noqa: ANN001
        self.calls.append(f"gpio_free({pin})")

    def gpio_claim_output(self, handle, pin, level) -> None:  # noqa: ANN001
        self.calls.append(f"gpio_claim_output({pin},{level})")

    def gpio_claim_alert(self, handle, pin, eflags, lflags=0) -> None:  # noqa: ANN001
        self.calls.append(f"gpio_claim_alert({pin},{eflags},{lflags})")

    def callback(self, handle, pin, edge, fn):  # noqa: ANN001
        self.calls.append(f"callback({pin},{edge})")
        if self.edges is not None:
            for level, ts in self.edges:
                fn(handle, pin, level, ts)      # 立刻回放（同步），无需真的等 40ms
        inner = FakeCallback(fn)
        outer = self

        class _Cb(FakeCallback):
            def cancel(self) -> None:
                outer.callback_cancelled = True
                inner.cancel()

        return _Cb(fn)


def make_sensor(edges: list[tuple[int, int]] | None, *, retries: int = 1) -> Dht11:
    """造一个走 lgpio 后端的 Dht11（不打开真实 GPIO）。

    ``min_interval_s`` 必须 ≥1s（驱动会校验，这是 DHT11 的硬件要求），
    但这里直接调 ``_read_lgpio_raw()``，不经过缓存判断，所以不会真的等。
    """
    sensor = Dht11(pin=4, retries=retries, min_interval_s=2.0)
    fake = FakeLgpio(edges)
    sensor._lgpio = fake
    sensor._lgpio_handle = 99
    sensor._backend = "lgpio"
    sensor._opened = True
    sensor._sleep = lambda _s: None      # 不要让重试真的睡
    sensor._fake_lgpio = fake            # type: ignore[attr-defined]
    return sensor


class TestDecodeSuccess(unittest.TestCase):
    """正常帧要能解出正确数值。"""

    def test_典型读数(self) -> None:
        sensor = make_sensor(build_frame(temperature=25, humidity=58))
        temp, humid = sensor._read_lgpio_raw()
        self.assertEqual(humid, 58.0)
        self.assertEqual(temp, 25.0)

    def test_边界值(self) -> None:
        for temp, humid in ((0, 20), (50, 90), (23, 45), (37, 61)):
            with self.subTest(temp=temp, humid=humid):
                sensor = make_sensor(build_frame(temperature=temp, humidity=humid))
                got_temp, got_humid = sensor._read_lgpio_raw()
                self.assertEqual((got_temp, got_humid), (float(temp), float(humid)))

    def test_全0与全1字节都能解(self) -> None:
        """0x00 与 0xFF 的位模式最容易暴露移位方向写反。"""
        for temp, humid in ((0, 0), (0, 255)):
            # 湿度 255 超量程但**解码层不管量程**（量程校验是纯函数的事）
            with self.subTest(temp=temp, humid=humid):
                sensor = make_sensor(build_frame(temperature=temp, humidity=humid & 0xFF))
                got = sensor._read_lgpio_raw()
                self.assertEqual(got[1], float(humid & 0xFF))

    def test_读完会取消回调并释放引脚(self) -> None:
        """★ 不留悬空回调：否则下一次读取会拿到上一次的边沿。"""
        sensor = make_sensor(build_frame(25, 58))
        sensor._read_lgpio_raw()
        fake = sensor._fake_lgpio        # type: ignore[attr-defined]
        self.assertTrue(fake.callback_cancelled, "必须调用 cb.cancel()")
        self.assertIn("gpio_free(4)", fake.calls, "必须释放引脚")
        self.assertIn("gpio_claim_alert(4,3,32)", fake.calls, "应以 BOTH_EDGES + 上拉注册")


class TestDecodeFailures(unittest.TestCase):
    """三类失败路径都要给出可读的错误信息。"""

    def test_完全没有边沿时报没应答(self) -> None:
        sensor = make_sensor([])
        with self.assertRaises(DeviceIOError) as ctx:
            sensor._read_lgpio_raw()
        msg = str(ctx.exception)
        self.assertIn("没应答", msg)
        self.assertIn("0 个边沿" if "0 个边沿" in msg else "没有任何边沿", msg)
        self.assertIn("上拉", msg, "错误信息要给出排查线索")

    def test_边沿过少时报疑似未应答并报出数量(self) -> None:
        """边沿数明显低于一帧（约 83 个）时，判据是"疑似未应答"，且必须报出数量。"""
        frame = build_frame(25, 58)
        sensor = make_sensor(frame[:20])
        with self.assertRaises(DeviceIOError) as ctx:
            sensor._read_lgpio_raw()
        msg = str(ctx.exception)
        self.assertIn("未应答", msg)
        self.assertIn("20 个边沿", msg, "要说清抓到多少边沿，便于判断是没接还是时序问题")

    def test_帧尾那个空闲上升沿不会让解码错位(self) -> None:
        """★★★ 2026-09-25 真机回归（ERROR.md E37）：帧尾多一个"上拉回空闲"的上升沿。

        真实抓包：一帧 **83 个边沿 / 42 个上升沿 / 41 个下降沿**，
        最后那个上升沿（…低 3749.1 → 高 3802.9 µs）不是数据位。
        老写法"从下标 1 开始逐对扫、丢掉第一个高电平段"会因此错位一格：
        只配出 40 对、丢掉一对后只剩 **39 位** ⇒ 永远解不出（真机上就是这个症状）。

        判据（两条都要）：
        1. `build_frame()`（**已包含帧尾空闲上升沿**）能解出正确温湿度；
        2. 把那个上升沿去掉（合成帧回到 82 边沿）**也一样能解** —— 解码不许依赖它。
        """
        sensor = make_sensor(build_frame(25, 58))
        self.assertEqual(sensor._read_lgpio_raw(), (25.0, 58.0))

        frame_without_idle_rise = build_frame(25, 58)[:-1]      # 去掉帧尾上升沿
        sensor2 = make_sensor(frame_without_idle_rise)
        self.assertEqual(sensor2._read_lgpio_raw(), (25.0, 58.0),
                         "解码不许依赖帧尾那个空闲上升沿（两种帧都要能解）")

    def test_边沿不够时仍报未应答(self) -> None:
        """边沿数不到一帧的 95%（< 79）⇒ "疑似未应答"，且必须报出数量。"""
        frame = build_frame(25, 58)
        sensor = make_sensor(frame[:60])
        with self.assertRaises(DeviceIOError) as ctx:
            sensor._read_lgpio_raw()
        message = str(ctx.exception)
        self.assertIn("60 个边沿", message)
        self.assertIn("上拉", message)

    def test_宽度超限的位会被丢弃并如实报不足(self) -> None:
        """某一位宽度 >200µs（噪声/中断延迟）必须被丢弃，且如实报"位不足"。"""
        frame = list(build_frame(25, 58))
        # 把某个"上升沿"的时间戳往前挪 1ms ⇒ 它的高电平宽度变成约 1000µs（超限）
        level, ts = frame[31]
        frame[31] = (level, ts - 1_000_000)
        sensor = make_sensor(frame)
        with self.assertRaises(DeviceIOError) as ctx:
            sensor._read_lgpio_raw()
        self.assertIn("bit", str(ctx.exception))

    def test_校验和不符(self) -> None:
        """★ 校验和是 DHT11 唯一的数据自检手段：不符必须报错，不能"凑合用"。"""
        sensor = make_sensor(build_frame(25, 58, checksum=0x00))   # 故意给错的校验和
        with self.assertRaises(DeviceIOError) as ctx:
            sensor._read_lgpio_raw()
        self.assertIn("校验和", str(ctx.exception))

    def test_应答残留不会被当成数据位(self) -> None:
        """★ 回归测试：应答的高电平（约 80µs）比任何数据位都宽，
        早前的实现把它当成第 1 个 bit，导致 40 位错位、真机上报"校验和不符"。
        这里断言正常帧能解出**正好 40 个 bit 且校验通过**。"""
        sensor = make_sensor(build_frame(26, 60))
        temp, humid = sensor._read_lgpio_raw()
        self.assertEqual((temp, humid), (26.0, 60.0))

    def test_重试后仍失败会带上后端名(self) -> None:
        sensor = make_sensor([], retries=2)
        with self.assertRaises(DeviceIOError) as ctx:
            sensor._read_hardware()
        msg = str(ctx.exception)
        self.assertIn("后端 lgpio", msg, "报错要写清用的哪个后端，便于排查")
        self.assertIn("连续 2 次", msg)


class TestMockUnaffected(unittest.TestCase):
    """mock 模式必须完全不受后端改动影响。"""

    def test_mock不碰lgpio也不碰gpiozero(self) -> None:
        with Dht11(pin=4, mock=True) as sensor:
            self.assertIsNone(sensor._lgpio)
            self.assertIsNone(sensor._sensor)
            self.assertEqual(sensor._backend, "none")
            sample = sensor.read()
            self.assertTrue(sample.ok)
            self.assertIsNotNone(sample.temperature_c)
            self.assertIsNotNone(sample.humidity_percent)


if __name__ == "__main__":
    unittest.main(verbosity=2)
