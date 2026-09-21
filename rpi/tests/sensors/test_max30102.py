"""MAX30102 驱动测试 —— 覆盖"纯算法 + 驱动契约 + 失败路径"三层。

要点（照抄 ``test_button.py`` 范本）：
1. **纯算法用合成波形测**：不接硬件、不 sleep，喂 10 秒 60bpm 的波形就断言心率；
2. **mock 模式断言"绝不创建真实硬件对象"**（``_bus is None``，不会去开 /dev/i2c-1）；
3. **失败路径要测**：未 open 就读、I2C 读失败、数据不足、超量程，
   以及"没贴手指不许报心率"这条安全底线；
4. **初始化序列用 MockBus 当探针**：断言驱动真的往 0x57 写对了寄存器。
"""

from __future__ import annotations

import math
import unittest

from health_monitor.hal import (
    ConfigError,
    DataInvalidError,
    DeviceIOError,
    DeviceInitError,
    DeviceKind,
    DeviceNotReady,
    MockBus,
    UnsupportedError,
    VitalSignsSample,
    create_device,
)
from health_monitor.sensors.max30102 import (
    FIFO_CONFIG_VALUE,
    LED_CURRENT_STEPS,
    MAX30102_ADDRESS,
    MAX30102_PART_ID,
    MODE_SPO2,
    REG_FIFO_CONFIG,
    REG_FIFO_RD_PTR,
    REG_FIFO_WR_PTR,
    REG_LED1_PA,
    REG_LED2_PA,
    REG_MODE_CONFIG,
    REG_OVF_COUNTER,
    REG_PART_ID,
    REG_SPO2_CONFIG,
    Max30102,
    PpgParams,
    _bandpass,
    _rms,
    _to_18bit,
    analyze_ppg,
)

# ==========================================================================
# 测试用合成波形（不碰硬件）：PPG 的"直流 + 基波 + 二次谐波"
# ==========================================================================


def synth_ppg(
    bpm: float = 60.0,
    seconds: float = 10.0,
    sample_rate: float = 100.0,
    dc: float = 60000.0,
    amp: float = 600.0,
    red_dc: float = 58000.0,
    red_amp: float = 307.0,
    noise_ratio: float = 0.0,
) -> tuple[list[int], list[int]]:
    """合成一段心率为 ``bpm`` 的 PPG 波形，返回 ``(红外, 红光)`` 两组整数采样。

    为什么这么造：真实 PPG 的交流成分主要是**基波 + 二次谐波**（主峰 + 重搏波前的小峰），
    直流分量则由 LED 电流、皮肤厚度决定。它不接触任何硬件，
    因此测试可以在毫秒级跑完，且**每次结果完全一致**（可复现）。
    """
    freq = bpm / 60.0
    count = int(seconds * sample_rate)
    ir: list[int] = []
    red: list[int] = []
    for i in range(count):
        t = i / sample_rate
        phase = 2.0 * math.pi * freq * t
        noise = noise_ratio * dc * math.sin(2.0 * math.pi * 37.0 * t)
        ir.append(int(dc + amp * math.sin(phase) + 0.3 * amp * math.sin(2 * phase) + noise))
        red.append(
            int(red_dc + red_amp * math.sin(phase) + 0.3 * red_amp * math.sin(2 * phase) + noise)
        )
    return ir, red


# ==========================================================================
# 一、纯算法：预处理 + analyze_ppg
# ==========================================================================


class TestPpgPreprocess(unittest.TestCase):
    def test_移动平均去直流不减幅(self) -> None:
        """带通预处理只应去掉直流，脉搏波的幅度要基本保留（否则峰值检测就瞎了）。"""
        ir, _ = synth_ppg(bpm=60.0)
        ac = _bandpass(ir, 100.0)
        self.assertLess(abs(sum(ac) / len(ac)), 1.0, "去直流后均值应接近 0")
        self.assertGreater(_rms(ac), 100.0, "交流分量幅度不该被滤波器吃掉")

    def test_空数据不崩(self) -> None:
        self.assertEqual(_bandpass([], 100.0), [])

    def test_固定数据交流为0(self) -> None:
        ac = _bandpass([60000] * 500, 100.0)
        self.assertLess(_rms(ac), 1e-6, "恒定不变（没有脉搏波）的信号，交流分量必须是 0")


class TestPpgAlgorithm(unittest.TestCase):
    def test_合成波形能算出60bpm(self) -> None:
        """喂 10 秒、周期 1.2 秒（60bpm）的合成 IR 波形，心率应落在 55~65 bpm。"""
        ir, red = synth_ppg(bpm=60.0, seconds=10.0)
        result = analyze_ppg(ir, 100.0, red)
        self.assertTrue(result.finger_detected)
        self.assertIsNotNone(result.heart_rate_bpm, f"未算出心率：{result.reason}")
        self.assertLess(abs(result.heart_rate_bpm - 60.0), 6.0)
        self.assertGreaterEqual(result.beats, 5)

    def test_不同心率都算得准(self) -> None:
        """48/72/96/120 bpm 四种波形都要落在 ±5% 内（防"只对 60 调参"）。"""
        for bpm in (48.0, 72.0, 96.0, 120.0):
            with self.subTest(bpm=bpm):
                ir, red = synth_ppg(bpm=bpm)
                result = analyze_ppg(ir, 100.0, red)
                self.assertIsNotNone(result.heart_rate_bpm, result.reason)
                self.assertLess(abs(result.heart_rate_bpm - bpm) / bpm, 0.05)

    def test_叠加噪声仍能算准(self) -> None:
        """叠加 0.2% 的高频噪声（模拟手指微动/电源干扰）仍应给出 55~65 bpm。"""
        ir, red = synth_ppg(bpm=60.0, noise_ratio=0.002)
        result = analyze_ppg(ir, 100.0, red)
        self.assertIsNotNone(result.heart_rate_bpm, result.reason)
        self.assertLess(abs(result.heart_rate_bpm - 60.0), 6.0)

    def test_血氧落在合理区间(self) -> None:
        ir, red = synth_ppg(bpm=60.0)
        result = analyze_ppg(ir, 100.0, red)
        self.assertIsNotNone(result.spo2_percent, result.reason)
        self.assertGreater(result.spo2_percent, 85.0)
        self.assertLessEqual(result.spo2_percent, 100.0)

    def test_没有红光样本时不给血氧(self) -> None:
        """只有红外也要能给心率；血氧必须老实留 None，不许瞎补一个。"""
        ir, _ = synth_ppg(bpm=60.0)
        result = analyze_ppg(ir, 100.0)
        self.assertIsNotNone(result.heart_rate_bpm)
        self.assertIsNone(result.spo2_percent)

    def test_空数据返回无结论(self) -> None:
        result = analyze_ppg([], 100.0)
        self.assertFalse(result.finger_detected)
        self.assertIsNone(result.heart_rate_bpm)
        self.assertIsNone(result.spo2_percent)
        self.assertEqual(result.beats, 0)

    def test_全0数据不崩且不给心率(self) -> None:
        result = analyze_ppg([0] * 1000, 100.0)
        self.assertIsNone(result.heart_rate_bpm)
        self.assertFalse(result.finger_detected)

    def test_超量程数据不崩且不给心率(self) -> None:
        """ADC 满量程塞满（例如对着强光）：不许抛异常，也不许报心率。"""
        result = analyze_ppg([2 ** 18 - 1] * 1000, 100.0)
        self.assertIsNone(result.heart_rate_bpm)
        self.assertIsNotNone(result.reason)

    def test_没贴手指不给心率(self) -> None:
        """红外直流只有 100 计数 = 没贴手指：心率必须为 None（0bpm 会被误判成"心率过低"）。"""
        result = analyze_ppg([100] * 1000, 100.0)
        self.assertFalse(result.finger_detected)
        self.assertIsNone(result.heart_rate_bpm)
        self.assertIsNone(result.spo2_percent)

    def test_采样时长不足不给结论(self) -> None:
        ir, red = synth_ppg(bpm=60.0, seconds=1.0)  # 只有 1 秒
        result = analyze_ppg(ir, 100.0, red)
        self.assertIsNone(result.heart_rate_bpm)
        self.assertTrue(result.finger_detected, "手指状态应当照报")
        self.assertIn("不足", result.reason)

    def test_采样率为0不崩(self) -> None:
        ir, _ = synth_ppg(bpm=60.0)
        result = analyze_ppg(ir, 0.0)
        self.assertIsNone(result.heart_rate_bpm)

    def test_样本数不匹配时不给血氧(self) -> None:
        """红光与红外长度不一致（上层拼错了）时应放弃血氧，而不是索引越界。"""
        ir, red = synth_ppg(bpm=60.0)
        result = analyze_ppg(ir, 100.0, red[:500])
        self.assertIsNotNone(result.heart_rate_bpm)
        self.assertIsNone(result.spo2_percent)

    def test_质量阈值可调且低质量不给结论(self) -> None:
        """把质量门槛抬到 0.99：干净波形也达不到，此时只报状态、不给结论。

        注意分层：**纯算法**照实给出心率，但把原因写进 ``reason``；
        由驱动层（``_analyze_buffer``）据此把样本标成 ``ok=False``——
        这样既保留了数值供排查，又不会让坏数据流到报警引擎。
        """
        ir, red = synth_ppg(bpm=60.0)
        strict = PpgParams(quality_min=0.99)
        result = analyze_ppg(ir, 100.0, red, strict)
        self.assertTrue(result.finger_detected)
        self.assertIn("质量", result.reason)

        # 驱动层：关掉自动合成波形，只分析注入的这一段，结论必须 ok=False
        dev = Max30102(mock=True, mock_auto_wave=False, quality_min=0.99)
        dev.open()
        dev.inject(ir, red)
        sample = dev.read()
        self.assertFalse(sample.ok, "质量不达标时驱动必须返回 ok=False")
        self.assertTrue(sample.finger_detected)
        self.assertIn("质量", sample.error)
        self.assertLess(abs(sample.heart_rate_bpm - 60.0), 6.0, "数值仍保留，便于排查")
        dev.close()


# ==========================================================================
# 二、驱动：构造参数与生命周期
# ==========================================================================


class TestMax30102Driver(unittest.TestCase):
    def test_未open就read必须报错(self) -> None:
        dev = Max30102(mock=True)
        with self.assertRaises(DeviceNotReady):
            dev.read()

    def test_类属性与注册表一致(self) -> None:
        self.assertEqual(Max30102.NAME, "max30102")
        self.assertEqual(Max30102.KIND, DeviceKind.VITAL)
        self.assertEqual(MAX30102_ADDRESS, 0x57)
        self.assertEqual(MAX30102_PART_ID, 0x15)

    def test_参数非法要抛ConfigError(self) -> None:
        with self.assertRaises(ConfigError):
            Max30102(led_current="99mA", mock=True)
        with self.assertRaises(ConfigError):
            Max30102(sample_rate=123, mock=True)
        with self.assertRaises(ConfigError):
            Max30102(address=0x80, mock=True)
        with self.assertRaises(ConfigError):
            Max30102(quality_min=1.5, mock=True)

    def test_默认参数符合注册表提示(self) -> None:
        dev = Max30102(mock=True)
        self.assertEqual(dev.address, 0x57)
        self.assertEqual(dev.i2c_bus, 1)
        self.assertEqual(dev.led_current, "7.6mA")
        self.assertEqual(dev.sample_rate, 100)
        self.assertIn("7.6mA", LED_CURRENT_STEPS)

    def test_mock模式不创建真实硬件对象(self) -> None:
        with Max30102(mock=True) as dev:
            self.assertTrue(dev.mock)
            self.assertIsNone(dev._bus, "mock 模式下不允许创建 RealBus（会去打开 /dev/i2c-1）")
            dev.read()  # 读一次也不该因此建出真实总线
            self.assertIsNone(dev._bus)

    def test_mock模式连续读数落在合理区间(self) -> None:
        """每次 read() 推进 0.5 秒合成波形，读到第 11 次（5.5 秒）应给出真实合理值。"""
        dev = Max30102(mock=True)
        dev.open()
        sample = None
        for _ in range(11):
            sample = dev.read()
        self.assertIsInstance(sample, VitalSignsSample)
        self.assertTrue(sample.finger_detected)
        self.assertTrue(sample.ok, sample.error)
        self.assertLess(abs(sample.heart_rate_bpm - 60.0), 6.0)
        self.assertGreater(sample.spo2_percent, 90.0)
        self.assertLessEqual(sample.spo2_percent, 100.0)
        self.assertEqual(sample.device, "max30102")
        dev.close()

    def test_数据不足时ok为False且心率是None(self) -> None:
        """刚开就只读一次：必须 ok=False、心率 None，不能拿半截数据硬算。"""
        dev = Max30102(mock=True)
        dev.open()
        sample = dev.read()
        self.assertFalse(sample.ok)
        self.assertIsNone(sample.heart_rate_bpm)
        self.assertIsNone(sample.spo2_percent)
        self.assertTrue(sample.finger_detected)
        self.assertIsNotNone(sample.error)
        dev.close()

    def test_inject可以喂真实波形段(self) -> None:
        """把 8 秒 60bpm 的合成波形注入 mock 驱动，经过完整驱动链路应算出 60bpm。"""
        dev = Max30102(mock=True)
        dev.open()
        ir, red = synth_ppg(bpm=60.0, seconds=8.0)
        dev.inject(ir, red)
        sample = dev.read()
        self.assertTrue(sample.ok, sample.error)
        self.assertLess(abs(sample.heart_rate_bpm - 60.0), 6.0)
        self.assertGreater(sample.spo2_percent, 85.0)
        dev.close()

    def test_inject长度不一致要报错(self) -> None:
        dev = Max30102(mock=True)
        dev.open()
        with self.assertRaises(ConfigError):
            dev.inject([60000] * 100, [58000] * 50)
        dev.close()

    def test_真实模式下inject必须被拒绝(self) -> None:
        dev = Max30102(mock=False)
        with self.assertRaises(UnsupportedError):
            dev.inject([60000] * 1000)
        with self.assertRaises(UnsupportedError):
            dev.set_mock_fault()

    def test_注入故障时抛IOError并留痕(self) -> None:
        dev = Max30102(mock=True)
        dev.open()
        dev.set_mock_fault()
        with self.assertRaises(DeviceIOError):
            dev.read()
        status = dev.status()
        self.assertEqual(status["fault_count"], 1, "失败必须计数，静默失败是一级缺陷")
        self.assertIn("注入的故障", status["last_error"])
        # 故障只影响一次，下一次应当恢复正常
        self.assertTrue(dev.read().finger_detected)
        dev.close()

    def test_close可重复调用(self) -> None:
        dev = Max30102(mock=True)
        dev.open()
        dev.read()
        dev.close()
        dev.close()  # 幂等，不应抛异常
        self.assertFalse(dev._opened)
        self.assertEqual(dev._ir_buf, [], "close 后应清空采样缓冲")

    def test_describe写清物理脚号(self) -> None:
        dev = Max30102(mock=True)
        desc = dev.describe()
        self.assertEqual(desc["name"], "max30102")
        self.assertEqual(desc["kind"], "vital")
        self.assertIn("0x57", desc["bus"])
        self.assertIn("物理脚 5", desc["pins"]["scl"])
        self.assertIn("物理脚 3", desc["pins"]["sda"])
        self.assertIn("3.3V", desc["pins"]["vin"])

    def test_status计数正确(self) -> None:
        dev = Max30102(mock=True)
        dev.open()
        self.assertEqual(dev.status()["read_count"], 0)
        dev.read()
        dev.read()
        status = dev.status()
        self.assertEqual(status["read_count"], 2)
        self.assertEqual(status["fault_count"], 0)
        self.assertEqual(status["driver"], "Max30102")
        self.assertTrue(status["opened"])
        dev.close()

    def test_self_check在mock下可用(self) -> None:
        dev = Max30102(mock=True)
        result = dev.self_check()  # 内部会自己 open/read
        self.assertIn("ok", result)
        self.assertIn("detail", result)
        self.assertTrue(result["ok"], result["detail"])

    def test_open可重复调用(self) -> None:
        dev = Max30102(mock=True)
        dev.open()
        dev.open()  # 不应重置状态或抛异常
        self.assertTrue(dev._opened)
        dev.close()

    def test_真实模式初始化失败抛DeviceInitError且带线索(self) -> None:
        """PC 上（没装 smbus2/没接 I2C）打开真实模式必须给出可读的排查线索。"""
        dev = Max30102(mock=False)
        try:
            dev.open()
        except DeviceInitError as exc:
            text = str(exc)
            self.assertIn("MAX30102", text)
            self.assertTrue("i2cdetect" in text or "3.3V" in text)
        except Exception as exc:  # noqa: BLE001 - 真树莓派上可能真的成功，跳过
            self.skipTest(f"本机环境不支持真实 I2C：{type(exc).__name__}")
        finally:
            dev.close()

    def test_装配方式与注册表一致(self) -> None:
        """``create_device('max30102')`` 必须能构造出未初始化的实例（注册表契约）。"""
        dev = create_device("max30102", mock=True)
        self.assertIsInstance(dev, Max30102)
        self.assertFalse(dev.status()["opened"])
        self.assertTrue(dev.mock)


# ==========================================================================
# 三、真实硬件访问："到底往 0x57 发了什么字节"（用 MockBus 当探针）
# ==========================================================================


class _RegisterProbe:
    """一个"记得住寄存器"的 MockBus 包装：记录写入、并让 PART_ID 读回 0x15。"""

    def __init__(self) -> None:
        self.bus = MockBus()
        self.writes: list[tuple[int, int]] = []
        self._last_reg = -1
        self.bus.hook_i2c_write(MAX30102_ADDRESS, self._on_write)
        self.bus.hook_i2c(MAX30102_ADDRESS, self._on_read)

    def _on_write(self, _bus: MockBus, data: bytes) -> None:
        if not data:
            return
        reg = data[0] & 0x7F
        self._last_reg = reg
        if len(data) >= 2:
            self.writes.append((reg, data[1]))

    def _on_read(self, _bus: MockBus, _data: bytes) -> bytes:
        # 读哪个寄存器由上一次写决定；PART_ID 必须回 0x15，否则驱动会判"器件不对"
        return bytes([MAX30102_PART_ID if self._last_reg == REG_PART_ID else 0x00])

    def value_of(self, reg: int) -> int:
        """最后一次写入某个寄存器的值。"""
        values = [v for r, v in self.writes if r == reg]
        return values[-1] if values else -1

    @property
    def regs(self) -> list[int]:
        return [reg for reg, _ in self.writes]


class _CountingMockBus(MockBus):
    """记录"每一次读请求了多少字节"的 MockBus。

    为什么需要它：MockBus 的读钩子签名是 ``func(bus, payload)``，**看不到请求长度**，
    所以想验证"驱动只要了 3 字节"就必须在总线这一层记一笔。
    """

    def __init__(self) -> None:
        super().__init__()
        self.read_lengths: list[int] = []

    def i2c_read(self, bus: int, address: int, length: int) -> bytes:
        self.read_lengths.append(length)
        return super().i2c_read(bus, address, length)


class TestMax30102I2CProtocol(unittest.TestCase):
    """不接真器件，用 MockBus 记录驱动实际写出的寄存器，验证初始化序列与 FIFO 解析。"""

    def _probe(self) -> tuple[Max30102, _RegisterProbe]:
        probe = _RegisterProbe()
        return Max30102(bus=probe.bus, mock=False), probe

    def test_初始化序列写对寄存器(self) -> None:
        dev, probe = self._probe()
        dev._opened = True  # 跳过 open() 里的复位等待，直接验证 _init_sequence
        dev._init_sequence()
        regs = probe.regs
        self.assertIn(REG_FIFO_WR_PTR, regs, "要清 FIFO 写指针")
        self.assertIn(REG_FIFO_RD_PTR, regs, "要清 FIFO 读指针")
        self.assertIn(REG_OVF_COUNTER, regs, "要清溢出计数")
        self.assertIn(REG_FIFO_CONFIG, regs)
        self.assertIn(REG_SPO2_CONFIG, regs)
        self.assertIn(REG_LED1_PA, regs)
        self.assertIn(REG_LED2_PA, regs)
        self.assertEqual(probe.value_of(REG_MODE_CONFIG), MODE_SPO2, "必须进 SpO2 模式（红光+红外）")
        self.assertEqual(probe.value_of(REG_LED1_PA), LED_CURRENT_STEPS["7.6mA"])
        self.assertEqual(probe.value_of(REG_LED2_PA), LED_CURRENT_STEPS["7.6mA"])
        self.assertEqual(probe.value_of(REG_FIFO_WR_PTR), 0x00)
        self.assertEqual(probe.value_of(REG_FIFO_RD_PTR), 0x00)
        self.assertEqual(probe.value_of(REG_OVF_COUNTER), 0x00)
        self.assertEqual(
            probe.value_of(REG_FIFO_CONFIG) & 0xE0,
            FIFO_CONFIG_VALUE & 0xE0,
            "FIFO 平均点数位要与常量表一致（4 点平均 = 0b010）",
        )
        self.assertEqual(probe.value_of(REG_SPO2_CONFIG) & 0x60, 0x20, "ADC 量程应为 4096nA")
        self.assertEqual(probe.value_of(REG_SPO2_CONFIG) & 0x1C, 0x04, "采样率应为 100Hz")

    def test_进入SpO2模式的写操作在最后(self) -> None:
        dev, probe = self._probe()
        dev._opened = True
        dev._init_sequence()
        self.assertEqual(probe.writes[-1], (REG_MODE_CONFIG, MODE_SPO2))

    def test_版本号不对必须报错(self) -> None:
        """PART_ID 读回不是 0x15 → 器件/地址不对，必须抛 DeviceInitError 而不是硬跑。"""
        bus = MockBus().set_i2c_reply(MAX30102_ADDRESS, b"\x00")  # 永远回 0x00
        dev = Max30102(bus=bus, mock=False)
        dev._opened = True
        with self.assertRaises(DeviceInitError) as ctx:
            dev._init_sequence()
        self.assertIn("0x15", str(ctx.exception))

    def test_读FIFO按6字节一组解析18位(self) -> None:
        """SpO2 模式下每个采样点 = **6 字节**：先红光 3 字节，后红外 3 字节。"""
        frame = bytes([
            0x02, 0x03, 0x04,   # SLOT1 = 红光 0x020304（18 位以内）
            0x03, 0x02, 0x01,   # SLOT2 = 红外 0x030201
        ])
        bus = _CountingMockBus().hook_i2c(
            MAX30102_ADDRESS, lambda _b, _d, f=frame: f[: bus.read_lengths[-1]]
        )
        dev = Max30102(bus=bus, mock=False)
        samples = dev._read_fifo(1)
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0], (0x030201, 0x020304), "(红外, 红光)")
        self.assertEqual(bus.read_lengths[-1], 6, "读 1 组样本必须正好要 6 字节")
        self.assertEqual(
            bus.operations("i2c_write")[-1][1],
            (1, MAX30102_ADDRESS, bytes([0x07])),
            "必须从 FIFO_DATA(0x07) 读",
        )

    def test_18位拼装会掩掉无效高位(self) -> None:
        """ADC 只有 18 位：高 6 位是无效的，必须掩掉，否则心率/血氧全乱（判据可执行）。"""
        self.assertEqual(_to_18bit(bytes([0xFC, 0x03, 0x02])), 0x000302)
        self.assertEqual(_to_18bit(bytes([0x03, 0xFF, 0xFF])), 0x03FFFF)
        self.assertEqual(_to_18bit(bytes([0x00, 0x00, 0x00])), 0)
        with self.assertRaises(DataInvalidError):
            _to_18bit(bytes([0x00, 0x00]))

    def test_读FIFO多组样本按序拼接(self) -> None:
        """连读 2 组：第 1 组在前、第 2 组在后，顺序不能乱（乱序=心率算反）。"""
        frame = bytes([
            0x00, 0x00, 0x0A,   # 第 1 组红光 = 10
            0x00, 0x00, 0x14,   # 第 1 组红外 = 20
            0x00, 0x00, 0x1E,   # 第 2 组红光 = 30
            0x00, 0x00, 0x28,   # 第 2 组红外 = 40
        ])
        bus = _CountingMockBus().hook_i2c(
            MAX30102_ADDRESS, lambda _b, _d, f=frame: f[: bus.read_lengths[-1]]
        )
        dev = Max30102(bus=bus, mock=False)
        samples = dev._read_fifo(2)
        self.assertEqual(samples, [(20, 10), (40, 30)])
        self.assertEqual(bus.read_lengths[-1], 12, "2 组样本要 12 字节")

    def test_FIFO字节数不对要报错(self) -> None:
        """钩子只回 3 字节但驱动要 6 字节：MockBus 会先报长度不符，驱动不许静默截断。"""
        bus = MockBus().set_i2c_reply(MAX30102_ADDRESS, b"\x00\x00\x00")
        dev = Max30102(bus=bus, mock=False)
        with self.assertRaises(DeviceIOError):
            dev._read_fifo(1)

    def test_完整open序列在探针总线上跑通(self) -> None:
        """把复位/等 RESET 清零/初始化整条链路真跑一遍（用探针总线，不接真器件）。

        这条测试的价值：``open()`` 里"等 RESET 位清零"的循环很容易写成死等，
        用"复位位立刻清零 + PART_ID 回 0x15"的假器件跑一次，就能确认
        初始化顺序与异常处理都是通的。
        """
        bus = MockBus()
        state = {"reg": -1, "mode": 0x00}
        writes: list[tuple[int, int]] = []
        reads: list[int] = []

        def on_write(_bus: MockBus, data: bytes) -> None:
            state["reg"] = data[0] & 0x7F
            if len(data) >= 2:
                writes.append((state["reg"], data[1]))
                if state["reg"] == REG_MODE_CONFIG:
                    state["mode"] = data[1]

        def on_read(_bus: MockBus, _data: bytes) -> bytes:
            reads.append(state["reg"])
            if state["reg"] == REG_PART_ID:
                return bytes([MAX30102_PART_ID])
            if state["reg"] == REG_MODE_CONFIG:
                # 模拟"复位已在几毫秒内完成"：bit6 读回 0
                return bytes([state["mode"] & ~0x40])
            return b"\x00"

        bus.hook_i2c_write(MAX30102_ADDRESS, on_write)
        bus.hook_i2c(MAX30102_ADDRESS, on_read)
        dev = Max30102(bus=bus, mock=False)
        dev.open()
        try:
            self.assertTrue(dev._opened)
            self.assertEqual(writes[0], (REG_MODE_CONFIG, 0x40), "第一步必须是软复位")
            mode_writes = [v for r, v in writes if r == REG_MODE_CONFIG]
            self.assertEqual(mode_writes[-1], MODE_SPO2, "最后一步是进 SpO2 模式")
            self.assertIn(REG_PART_ID, reads, "初始化时必须读版本号确认器件在线")
            self.assertIn(REG_MODE_CONFIG, reads, "复位后必须回读确认 RESET 位已清零")
        finally:
            dev.close()
        self.assertFalse(dev._opened)
        led_writes = [v for r, v in writes if r == REG_LED1_PA]
        self.assertEqual(led_writes[-1], 0x00, "close() 应当把 LED 关掉（省电、不发烫）")

    def test_写寄存器带寄存器地址(self) -> None:
        """I2C 写必须是"寄存器地址 + 数据"两字节，顺序不能反。"""
        bus = MockBus()
        dev = Max30102(bus=bus, mock=False)
        dev._write_reg(REG_LED1_PA, 0x26)
        ops = bus.operations("i2c_write")
        self.assertEqual(ops[-1][1], (1, MAX30102_ADDRESS, bytes([REG_LED1_PA, 0x26])))


if __name__ == "__main__":
    unittest.main(verbosity=2)
