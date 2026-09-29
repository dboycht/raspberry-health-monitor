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
import random
import unittest
from pathlib import Path

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
    FIFO_DEPTH,
    FIFO_SAMPLE_AVG,
    FIFO_SAMPLE_AVG_BITS,
    FIFO_SAMPLES_PER_READ,
    LED_CURRENT_STEPS,
    MAX30102_ADDRESS,
    MAX30102_PART_ID,
    MAX_I2C_BLOCK_BYTES,
    MODE_RESET,
    MODE_SHUTDOWN,
    MODE_SPO2,
    PART_ID_REGISTERS,
    REG_FIFO_CONFIG,
    REG_FIFO_RD_PTR,
    REG_FIFO_WR_PTR,
    REG_LED1_PA,
    REG_LED2_PA,
    REG_MODE_CONFIG,
    REG_OVF_COUNTER,
    REG_PART_ID,
    REG_PART_ID_ALT,
    REG_REV_ID,
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
    def test_带通去直流不减幅(self) -> None:
        """带通预处理只应去掉直流，脉搏波的幅度要基本保留（否则峰值检测就瞎了）。

        ⚠️ 判据改过（2026-09-29，E52）：旧实现是"两个滑动平均相减"，去直流后均值
        几乎精确为 0，所以当年写的是绝对值 ``< 1.0``；换成真正的 0.7~4Hz 带通后，
        一段 10 秒窗里均值不会精确为 0（不足整周期的截断 + 两端瞬态），实测 4.0，
        而交流 RMS 是 348。**"接近 0"应当相对幅度来判**，绝对阈值只会变成假失败。
        """
        ir, _ = synth_ppg(bpm=60.0)
        ac = _bandpass(ir, 100.0)
        level = _rms(ac)
        self.assertGreater(level, 100.0, "交流分量幅度不该被滤波器吃掉")
        self.assertLess(abs(sum(ac) / len(ac)), 0.05 * level, "去直流后均值应远小于交流幅度")

    def test_带通在呼吸频段至少衰减六倍(self) -> None:
        """★ E52 的正题（滤波器层）：0.3Hz ≈ 18 次/分（呼吸）必须被明显压掉。

        判据用**两个纯音的增益比**，而不是"信号里呼吸成分的绝对值"——
        后者取决于幅度设定，比不出滤波器的本事。实测 0.3Hz 增益 0.049、
        1.1Hz 增益 0.857（比值 0.057）。
        """
        rate = 25.0
        n = 250

        def tone_gain(freq: float) -> float:
            x = [math.sin(2 * math.pi * freq * i / rate) for i in range(n)]
            ac = _bandpass(x, rate)
            core = ac[15 : n - 15]           # 掐掉两端瞬态
            return max(abs(v) for v in core)

        resp = tone_gain(0.3)
        pulse = tone_gain(1.1)
        self.assertLess(resp, pulse * 0.15, f"呼吸频段衰减不足：{resp:.3f} vs {pulse:.3f}")

    def test_大呼吸伪迹下仍能算对心率(self) -> None:
        """★★ E52 的核心回归：呼吸幅度是脉搏的 13 倍、还叠加 4 万计数的漂移时，
        算法**必须仍然报出脉搏的那个心率**（而不是被伪迹带偏）。

        这份合成波形是照着真机实测的处境造的（真机呼吸幅度 ~8000、漂移 ~44000），
        而旧实现（两个滑动平均 + 数波峰）在真机上正是把 66 bpm 报成了 116~126。
        """
        rate = 25.0
        n = 250
        ir = [
            120000.0
            + 600.0 * math.sin(2 * math.pi * 1.2 * i / rate)          # 72 bpm 脉搏
            + 8000.0 * math.sin(2 * math.pi * 0.3 * i / rate)         # 呼吸（13 倍）
            + 40000.0 * (i / n) ** 2                                  # 漂移
            + 100.0 * math.sin(2 * math.pi * 2.7 * i / rate)          # 带内噪声
            for i in range(n)
        ]
        result = analyze_ppg(ir, rate, None, PpgParams())
        self.assertTrue(result.finger_detected, result.reason)
        self.assertIsNotNone(result.heart_rate_bpm, result.reason)
        self.assertAlmostEqual(result.heart_rate_bpm, 72.0, delta=6.0)
        self.assertGreater(result.quality, 0.30, "这种信号应当还能给出可用质量分")

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
        """把质量门槛抬到 0.99：**带内噪声大**的波形达不到，此时只报状态、不给结论。

        注意分层：**纯算法**照实给出心率，但把原因写进 ``reason``；
        由驱动层（``_analyze_buffer``）据此把样本标成 ``ok=False``——
        这样既保留了数值供排查，又不会让坏数据流到报警引擎。

        ⚠️ 造数据的方式改过（2026-09-29，E52）：质量分公式换成
        "信号强度 × 频谱主峰 SNR 分"之后，**一段理想的干净波形能拿满分 1.000**
        （旧公式里带间隔抖动项，永远到不了 1.0），所以不能再拿"干净波形"来测门槛。
        这里换成叠加带内高斯噪声的波形（实测噪声 σ=800 时质量 ≈0.76），
        才是"门槛真的在拦数据"的正确测法。
        """
        rng = random.Random(20260929)
        clean_ir, clean_red = synth_ppg(bpm=60.0)
        ir = [v + rng.gauss(0.0, 4000.0) for v in clean_ir]
        red = [v + rng.gauss(0.0, 4000.0) for v in clean_red]
        strict = PpgParams(quality_min=0.99)
        result = analyze_ppg(ir, 100.0, red, strict)
        self.assertTrue(result.finger_detected)
        self.assertLess(result.quality, 0.99, "这份波形本来就该达不到 0.99")
        self.assertIn("质量", result.reason)

        # 驱动层：关掉自动合成波形，只分析注入的这一段，结论必须 ok=False
        # （注入波形按**器件真实速率**造，见 E51；见 test_inject可以喂真实波形段 的说明）
        #
        # ⚠️ σ 只有 1500：**同一个 σ 在不同采样率下"带内噪声"差很多** ——
        #    100Hz 采样时 0.7~4Hz 只占全带宽的 3%，带通把 97% 的噪声都滤掉了
        #    （所以上面要用 σ=4000）；25Hz 采样时这一带占 26%，σ=1500 就足够把 SNR 压下去。
        dev = Max30102(mock=True, mock_auto_wave=False, quality_min=0.99)
        dev.open()
        rate = dev.analysis_rate
        rng2 = random.Random(20260929)
        ir_dev, red_dev = synth_ppg(bpm=60.0, sample_rate=rate)
        ir_dev = [v + rng2.gauss(0.0, 1500.0) for v in ir_dev]
        red_dev = [v + rng2.gauss(0.0, 1500.0) for v in red_dev]
        dev.inject(ir_dev, red_dev)
        sample = dev.read()
        self.assertFalse(sample.ok, "质量不达标时驱动必须返回 ok=False")
        self.assertTrue(sample.finger_detected)
        self.assertIn("质量", sample.error)
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

    def test_没贴手指的样本必须带awaiting_data标志_E54(self) -> None:
        """★ E54：`ok=False` 有两种含义，必须能区分。

        * "器件坏了"（I2C 异常/超时）⇒ 驱动**抛异常**，采集器计失败；
        * "器件正常、只是这次没测出东西"（没贴手指 / 窗没攒够 / 质量不足）⇒ ``ok=False``
          + **``awaiting_data=True``**，采集器**不许**计失败 —— 否则没人贴手指时
          服务会持续误报 `SENSOR_FAULT`（真机实测：3 轮后黄灯闪 + 蜂鸣 + 两块屏刷报警）。
        """
        dev = Max30102(mock=True, mock_auto_wave=False)
        dev.open()
        try:
            dev.inject([100.0] * 400, [100.0] * 400)      # 直流远低于阈值 ⇒ 没贴手指
            sample = dev.read()
        finally:
            dev.close()
        self.assertFalse(sample.ok)
        self.assertFalse(sample.finger_detected)
        self.assertTrue(sample.awaiting_data, "没贴手指是「器件正常」，不是设备故障")

    def test_质量不足的样本也带awaiting_data标志_E54(self) -> None:
        """质量不足同样是「没有有效结论」，不是故障。

        ⚠️ 波形要**叠带内噪声**：质量分改成"频谱 SNR × 强度"之后，干净的合成波形能拿满分
        （见 `test_质量阈值可调且低质量不给结论` 的说明），不加噪就测不出这条路径。
        """
        dev = Max30102(mock=True, mock_auto_wave=False, quality_min=0.99)
        dev.open()
        try:
            rate = dev.analysis_rate
            rng = random.Random(20260929)
            ir, red = synth_ppg(bpm=60.0, sample_rate=rate)
            ir = [v + rng.gauss(0.0, 1500.0) for v in ir]
            red = [v + rng.gauss(0.0, 1500.0) for v in red]
            dev.inject(ir, red)
            sample = dev.read()
        finally:
            dev.close()
        self.assertFalse(sample.ok)
        self.assertTrue(sample.awaiting_data)

    def test_正常读数的样本不带awaiting_data标志_E54_反向(self) -> None:
        dev = Max30102(mock=True)
        dev.open()
        try:
            sample = dev.read()
            for _ in range(40):          # mock 每次推进 0.5 秒，攒够分析窗（5 秒起）
                if sample.ok:
                    break
                sample = dev.read()
        finally:
            dev.close()
        self.assertTrue(sample.ok, sample.error)
        self.assertFalse(sample.awaiting_data, "正常读数不该被标成「等数据」")

    def test_inject可以喂真实波形段(self) -> None:
        """把 8 秒 60bpm 的合成波形注入 mock 驱动，经过完整驱动链路应算出 60bpm。

        ⚠️ 波形必须按**器件真实速率**造：FIFO 实测只有 `sample_rate/4 = 25` 组/秒
        （4 点平均的代价，`ERROR.md` **E51**）。按 100 Hz 造波形再注入，
        会被按 25 Hz 解读、心率算成约 4 倍 —— 这条测试自己就演示过那个 bug。
        """
        dev = Max30102(mock=True)
        dev.open()
        ir, red = synth_ppg(bpm=60.0, seconds=8.0, sample_rate=dev.analysis_rate)
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

    def test_版本号不对时报错且带线索(self) -> None:
        """★ 用**注入的假总线**制造"版本寄存器读回 0x00"，确定性验证失败路径。

        ⚠️ 为什么不能像早前那样"直接 open(mock=False) 然后断言会抛错"
        （2026-09-24 真机踩到）：那条写法**依赖这台机器碰巧没接传感器**——
        开发机上没接 → 抛错 → 通过；真机上接了 MAX30102 → 打开成功 → 断言失败。
        测试必须能**控制自己的环境**：这里用 MockBus 注入一个错误版本号，
        无论机器上有没有真实硬件，走的都是同一条失败路径。
        """
        from health_monitor.hal.mock_bus import MockBus

        bus = MockBus()
        bus.on_i2c_read = lambda _bus, _addr, _n: bytes([0x00])   # 版本寄存器应为 0x15
        dev = Max30102(mock=False, bus=bus)
        try:
            with self.assertRaises(DeviceInitError) as ctx:
                dev.open()
            text = str(ctx.exception)
            self.assertIn("MAX30102", text)
            self.assertIn("0x21", text, "要指出是哪个寄存器")
            # 线索的关键词按驱动实际文案断言（它给的是"地址不对/器件没焊好/I2C 误码"）
            self.assertTrue(
                any(k in text for k in ("地址", "I2C", "误码")),
                f"要给出可执行的排查线索：{text}",
            )
        finally:
            dev.close()

    def test_真机上能打开则必须能自检(self) -> None:
        """反过来：若这台机器**真的有** MAX30102，打开就必须成功且自检通过。

        这样一台机器上两种结果都有意义：
        没硬件 → 上一条测失败路径；有硬件 → 本条测成功路径。**不再依赖环境碰巧**。

        ⚠️ `OSError` 也要当成"没有器件"（2026-09-25 真机实测，`ERROR.md` E34）：
        器件不在总线上时 smbus2 抛的是裸 `OSError(121, 'Remote I/O error')`。
        `RealBus` 现在会把它翻译成 `DeviceIOError`，但这一层**必须同时容忍裸 OSError** ——
        否则"传感器刚拔掉"就会让整批真机验收测试变红，而问题只是没插好。
        """
        from health_monitor.hal.exceptions import DeviceError

        dev = Max30102(mock=False)
        try:
            dev.open()
        except (DeviceInitError, DeviceError, OSError) as exc:
            self.skipTest(f"本机没有可用的 MAX30102（正常）：{type(exc).__name__}: {exc}")
        try:
            result = dev.self_check()
            self.assertTrue(result["ok"], result.get("detail"))
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
            state["reg"] = data[0] & 0xFF      # 寄存器地址是 8 位（0xFF=PART_ID 备选布局）
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
            # 先关断再复位（E51 补记：只写一次 RESET 不足以从 shutdown/multi-LED 状态恢复采样）
            self.assertEqual(writes[0], (REG_MODE_CONFIG, MODE_SHUTDOWN), "第一步先关断")
            self.assertEqual(writes[1], (REG_MODE_CONFIG, MODE_RESET), "随后才是软复位")
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


# ==========================================================================
# 四、PART_ID 的两种已知布局（E49 回归：别把可用器件误判成"器件可疑"）
# ==========================================================================


class _FakeRegisterFile(MockBus):
    """**有状态的**假 MAX3010x：写进去能读回来，并模拟"软复位位自清"。

    为什么不能只用 :class:`_RegisterProbe`：那个探针"读哪个寄存器由上一次写决定、
    固定回一个值"，装不出"两种 PART_ID 布局"和"写 TEMP_EN 才有温度"这类状态。
    这里用一个字典当寄存器文件，够真、又完全确定性（不碰任何真实硬件）。
    """

    def __init__(self, registers: dict[int, int] | None = None) -> None:
        super().__init__()
        self.defaults = dict(registers or {})
        self.regs = dict(self.defaults)
        self._last = 0
        self.hook_i2c_write(MAX30102_ADDRESS, self._on_write)
        self.hook_i2c(MAX30102_ADDRESS, self._on_read)

    def _on_write(self, _bus: MockBus, data: bytes) -> None:
        if not data:
            return
        self._last = data[0]
        if len(data) < 2:
            return
        value = data[1]
        if self._last == REG_MODE_CONFIG and value & 0x40:
            # 软复位：真器件会在几毫秒内把 RESET 位自己清掉、寄存器回默认值
            self.regs = dict(self.defaults)
            self.regs[REG_MODE_CONFIG] = 0x00
            return
        self.regs[self._last] = value

    def _on_read(self, _bus: MockBus, _data: bytes) -> bytes:
        return bytes([self.regs.get(self._last, 0x00)])


class TestMax30102PartIdLayouts(unittest.TestCase):
    """PART_ID 两种布局都要能开机；两处都没有 0x15 才许报错。

    ⚠️ 这条回归的来历（2026-09-29 真机，`ERROR.md` **E49**）：
    本机模块的 PART_ID 报在 **0xFF**（MAX30105 布局），`0x21` 是 `TEMP_CONFIG`、读回 0x00。
    旧驱动只读 `0x21` ⇒ 明明**可用**的模块被判成"器件可疑"，T7 一直卡在第一步。
    判据**没有放宽**：仍必须真的读到 0x15，只是知道它可能在哪一处。
    """

    def _bus_with_layout(self, part_id_reg: int) -> _FakeRegisterFile:
        regs = {0x21: 0x00, 0xFF: 0x00, 0xFE: 0x03, 0x08: 0x0F}
        regs[part_id_reg] = MAX30102_PART_ID
        return _FakeRegisterFile(regs)

    def test_两种已知布局常量与数据手册口径一致(self) -> None:
        self.assertEqual(PART_ID_REGISTERS, (0x21, 0xFF))
        self.assertEqual(REG_PART_ID, 0x21)
        self.assertEqual(REG_PART_ID_ALT, 0xFF)
        self.assertEqual(REG_REV_ID, 0xFE)
        self.assertEqual(MAX30102_PART_ID, 0x15)

    def test_布局A_版本号在0x21时能开机(self) -> None:
        """MAX30102/MAX30101 布局（原有行为，不能被这次修改弄坏）。"""
        dev = Max30102(bus=self._bus_with_layout(0x21), mock=False)
        try:
            dev.open()
            self.assertTrue(dev._opened)
            self.assertEqual(dev._part_id_reg, 0x21)
        finally:
            dev.close()

    def test_布局B_版本号在0xFF时也能开机_这是E49的正题(self) -> None:
        """★ MAX30105 布局：0x21 读回 0x00，但 0xFF 读回 0x15 ⇒ 必须开机成功。"""
        dev = Max30102(bus=self._bus_with_layout(0xFF), mock=False)
        try:
            dev.open()
            self.assertTrue(dev._opened, "0xFF 布局的可用模块不许被误判成坏件")
            self.assertEqual(dev._part_id_reg, 0xFF, "要记住命中在哪一处布局")
            self.assertEqual(dev._rev_id, 0x03, "REV_ID 应被留痕")
        finally:
            dev.close()

    def test_两处都读不到0x15时报错并同时给出两处读数(self) -> None:
        """报错必须一次说清"两个地址各读到什么"，而不是只甩一个 0x00。"""
        dev = Max30102(bus=_FakeRegisterFile({0x21: 0x00, 0xFF: 0x11}), mock=False)
        try:
            with self.assertRaises(DeviceInitError) as ctx:
                dev.open()
        finally:
            dev.close()
        text = str(ctx.exception)
        self.assertIn("0x21=0x00", text)
        self.assertIn("0xFF=0x11", text, "要把备选布局的读回值也报出来")
        self.assertIn("0x15", text)

    def test_读寄存器0xFF不能被截成0x7F(self) -> None:
        """★ 旧实现把寄存器地址与 **7 位从机地址** 的掩码搞混：``& 0x7F`` 会把 0xFF 变成 0x7F。

        没有这条，支持 0xFF 布局就是假支持（发出去的寄存器号根本不对）。
        """
        bus = MockBus()
        dev = Max30102(bus=bus, mock=False)
        dev._read_reg(REG_PART_ID_ALT)
        self.assertEqual(
            bus.operations("i2c_write")[-1][1],
            (1, MAX30102_ADDRESS, bytes([0xFF])),
            "寄存器地址是 8 位，0xFF 必须原样发出去",
        )

    def test_写寄存器0xFF不能被截成0x7F(self) -> None:
        bus = MockBus()
        dev = Max30102(bus=bus, mock=False)
        dev._write_reg(REG_PART_ID_ALT, 0x01)
        self.assertEqual(
            bus.operations("i2c_write")[-1][1],
            (1, MAX30102_ADDRESS, bytes([0xFF, 0x01])),
        )

    def test_小寄存器地址的字节序没被这次修改弄坏(self) -> None:
        """反向守一手：常见的低地址寄存器仍必须是"寄存器号 + 值"两字节。"""
        bus = MockBus()
        dev = Max30102(bus=bus, mock=False)
        dev._write_reg(REG_SPO2_CONFIG, 0x27)
        dev._read_reg(REG_FIFO_WR_PTR)
        self.assertEqual(
            bus.operations("i2c_write")[0][1],
            (1, MAX30102_ADDRESS, bytes([REG_SPO2_CONFIG, 0x27])),
        )
        self.assertEqual(
            bus.operations("i2c_write")[1][1],
            (1, MAX30102_ADDRESS, bytes([REG_FIFO_WR_PTR])),
        )

    def test_自检在0xFF布局下也必须ok且写明布局(self) -> None:
        dev = Max30102(bus=self._bus_with_layout(0xFF), mock=False)
        try:
            result = dev.self_check()
        finally:
            dev.close()
        self.assertTrue(result["ok"], result.get("detail"))
        self.assertIn("0xFF", result["detail"])
        self.assertIn("MAX30105", result["detail"], "自检报告要写明是哪一种布局")

    def test_自检在两处都没有版本号时必须ok为假(self) -> None:
        dev = Max30102(bus=_FakeRegisterFile({0x21: 0x00, 0xFF: 0x00}), mock=False)
        try:
            result = dev.self_check()
        finally:
            dev.close()
        self.assertFalse(result["ok"])
        self.assertIn("0x21=0x00", result["detail"])
        self.assertIn("0xFF=0x00", result["detail"])

    def test_布局名字映射可读(self) -> None:
        self.assertIn("MAX30102", Max30102.part_id_layout(0x21))
        self.assertIn("MAX30105", Max30102.part_id_layout(0xFF))
        self.assertIn("未识别", Max30102.part_id_layout(None))

    def test_未开机时describe不崩且不带布局字样(self) -> None:
        """describe() 可能在 open() 之前被调用（生成文档时），不能因此报错。"""
        dev = Max30102(mock=True)
        text = dev.describe()["notes"]
        self.assertIn("LED 电流", text)
        self.assertNotIn("PART_ID", text, "还没开机就没有布局可写")


class _SMBusLikeBus(_CountingMockBus):
    """像 `smbus2` 一样：**单次块读超过 32 字节就抛 ValueError**。

    ⚠️ 这就是 E50 的钉子：真机上抛的是
    ``ValueError: Desired block length over 32 bytes``（**不是 OSError**），
    所以它既不会被 `RealBus` 翻译、也不会被驱动的 `except DeviceIOError` 接住 ——
    表现成"每次 read() 都异常、心率永远读不出来"。把这个限制搬进测试总线，
    驱动一旦又"一次读一大坨"，这里就会当场红。
    """

    def i2c_read(self, bus: int, address: int, length: int) -> bytes:
        if length > MAX_I2C_BLOCK_BYTES:
            raise ValueError(
                f"Desired block length over {MAX_I2C_BLOCK_BYTES} bytes"
            )
        return super().i2c_read(bus, address, length)


class TestMax30102FifoChunking(unittest.TestCase):
    """E50 回归：FIFO 必须**分块读**，一次 I2C 事务不许超过 32 字节。

    来历（2026-09-29 真机）：实测这颗器件每秒往 FIFO 推约 25 组（150 字节），
    而旧驱动一次读 `6*count` 字节 ⇒ 必然超限 ⇒ T7 永远读不出心率。
    """

    def test_常量与smbus2的上限一致(self) -> None:
        self.assertEqual(MAX_I2C_BLOCK_BYTES, 32, "SMBus 块读上限就是 32 字节")
        self.assertEqual(FIFO_SAMPLES_PER_READ, 5, "32 // 6 = 5 组（30 字节）")
        self.assertEqual(FIFO_DEPTH, 32)

    def test_一次块读永不超32字节(self) -> None:
        bus = _SMBusLikeBus()
        dev = Max30102(bus=bus, mock=False)
        samples = dev._read_fifo(25)          # 真实一次 read() 的量级
        self.assertEqual(len(samples), 25)
        self.assertTrue(bus.read_lengths, "必须真的读了 FIFO")
        self.assertLessEqual(max(bus.read_lengths), MAX_I2C_BLOCK_BYTES)
        self.assertEqual(bus.read_lengths, [30] * 5, "25 组 = 5 块 × 5 组")

    def test_最后一组不足时按实际长度读(self) -> None:
        bus = _SMBusLikeBus()
        dev = Max30102(bus=bus, mock=False)
        self.assertEqual(len(dev._read_fifo(7)), 7)
        self.assertEqual(bus.read_lengths, [30, 12], "7 组 = 5 组 + 2 组")

    def test_分组边界值_第5组与第6组(self) -> None:
        for count, expected in ((5, [30]), (6, [30, 6]), (10, [30, 30]), (11, [30, 30, 6])):
            bus = _SMBusLikeBus()
            dev = Max30102(bus=bus, mock=False)
            self.assertEqual(len(dev._read_fifo(count)), count)
            self.assertEqual(bus.read_lengths, expected, f"count={count}")

    def test_0组不读任何东西(self) -> None:
        bus = _SMBusLikeBus()
        dev = Max30102(bus=bus, mock=False)
        self.assertEqual(dev._read_fifo(0), [])
        self.assertEqual(bus.read_lengths, [])

    def test_分块后样本顺序与拼接仍然正确(self) -> None:
        """分块不能把"第一组是红光、第二组是红外"的拼接顺序搞乱。"""
        bus = MockBus()

        def on_read(_bus: MockBus, _data: bytes) -> bytes:
            # 5 组 = 30 字节：每组 RED=0x000001, IR=0x000002（18 位）
            return bytes([0x00, 0x00, 0x01, 0x00, 0x00, 0x02] * 5)

        bus.hook_i2c(MAX30102_ADDRESS, on_read)
        dev = Max30102(bus=bus, mock=False)
        samples = dev._read_fifo(5)
        self.assertEqual(samples, [(0x02, 0x01)] * 5, "每组是 (红外, 红光)")


class TestMax30102FifoRate(unittest.TestCase):
    """E51 回归：**分析的时间基数必须是 FIFO 的实际产出速率**（= 配置采样率 ÷ 平均点数）。

    来历（2026-09-29 真机实测）：`SPO2_SR=100` + 4 点平均 ⇒ 实测 FIFO 只有 **24.6 组/秒**；
    关掉平均（AVG=1）⇒ 98.7 组/秒。旧实现拿 `sample_rate=100` 当时间基数，
    心率会被算成约 **4 倍**（72 bpm 显示成约 288），而且**在 mock 里永远看不出来**
    （mock 产生波形与解读波形用的是同一个错误数字，自洽地错下去）。
    """

    def test_平均点数换算(self) -> None:
        self.assertEqual(FIFO_SAMPLE_AVG_BITS, 0b010, "4 点平均的寄存器位")
        self.assertEqual(FIFO_SAMPLE_AVG, 4, "2**2 = 4")

    def test_分析速率是配置采样率的四分之一(self) -> None:
        dev = Max30102(mock=True)
        self.assertEqual(dev.sample_rate, 100, "写进 SPO2_SR 的仍是配置值")
        self.assertAlmostEqual(dev.analysis_rate, 25.0, msg="实测 FIFO ≈ 24.6 组/秒")

    def test_分析窗按FIFO速率算成10秒(self) -> None:
        dev = Max30102(mock=True)
        self.assertEqual(dev._window_samples, 250, "25 组/秒 × 10 秒")
        self.assertAlmostEqual(dev._window_samples / dev.analysis_rate, 10.0)

    def test_描述里同时写明配置速率与实际速率(self) -> None:
        text = Max30102(mock=True).describe()["notes"]
        self.assertIn("100Hz", text)
        self.assertIn("25", text, "要写明 FIFO 实际速率，别让人以为是 100 组/秒")

    def test_算法在真机的25赫兹下仍能算对60bpm(self) -> None:
        """★ 关键：真机只有约 25 组/秒（25 Hz 是能用的下限附近），算法必须仍然准。"""
        rate = 25.0
        n = int(rate * 10)
        ir, red = [], []
        for i in range(n):
            phase = 2.0 * math.pi * 1.0 * (i / rate)      # 60 bpm = 1 Hz
            ir.append(60000 + 600 * math.sin(phase) + 180 * math.sin(2 * phase))
            red.append(58000 + 307 * math.sin(phase) + 92 * math.sin(2 * phase))
        analysis = analyze_ppg(ir, rate, red)
        self.assertTrue(analysis.finger_detected)
        self.assertIsNotNone(analysis.heart_rate_bpm)
        self.assertAlmostEqual(analysis.heart_rate_bpm, 60.0, delta=6.0)

    def test_旧的错误时间基数算不出正确心率_反向验证(self) -> None:
        """把同一段波形按旧的 100 Hz 解读 ⇒ 得不到 60 bpm（说明这个 bug 确实有区分力）。"""
        rate = 25.0
        n = int(rate * 10)
        ir, red = [], []
        for i in range(n):
            phase = 2.0 * math.pi * 1.0 * (i / rate)
            ir.append(60000 + 600 * math.sin(phase) + 180 * math.sin(2 * phase))
            red.append(58000 + 307 * math.sin(phase) + 92 * math.sin(2 * phase))
        wrong = analyze_ppg(ir, 100.0, red)          # 旧实现的时间基数
        hr = wrong.heart_rate_bpm
        self.assertFalse(
            hr is not None and 55.0 <= hr <= 65.0,
            f"拿 100 Hz 解读 25 Hz 的数据仍算出 {hr} bpm ⇒ 这条回归失去区分力",
        )

    def test_mock合成波形也按FIFO速率推进(self) -> None:
        dev = Max30102(mock=True)
        dev.open()
        dev._pull_mock_samples()
        self.assertEqual(len(dev._ir_buf), 12, "25 组/秒 ÷ 2 = 每次推进 12 组（0.5 秒）")


class TestRealCaptureRegression(unittest.TestCase):
    """★★ **真机实测波形回归**（E52）——整套算法最终要能过这一关。

    为什么值得把一份真实采集放进测试夹具：
    这个项目反复吃过"合成波形全绿、真机全红"的亏（E37：合成帧 82 边沿 vs 真机 83）。
    这份 CSV 是 2026-09-29 在树莓派上用 `scripts/vitals_check.py --dump-csv` 抓的
    **真实手指波形**（250 组 @25Hz，10 秒窗；红外直流 101887~145595，**未饱和**）。
    旧算法在它上面报 116~126 bpm、质量 0.00；当前算法报 66.0 bpm、质量 0.84。
    ⇒ 它就是"呼吸伪迹 + 漂移 + 接触噪声"这类真实处境的最小复现。
    """

    @staticmethod
    def _load() -> tuple[list[float], list[float], float]:
        path = Path(__file__).resolve().parent.parent / "fixtures" / "ppg_real_capture.csv"
        ir: list[float] = []
        red: list[float] = []
        rate = 25.0
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("#"):
                if "analysis_rate_hz=" in line:
                    rate = float(line.split("analysis_rate_hz=")[1].split()[0])
                continue
            if line.startswith("index"):
                continue
            parts = line.split(",")
            if len(parts) < 3:          # 跳过空行/尾行，避免夹具尾部换行把测试弄崩
                continue
            ir.append(float(parts[1]))
            red.append(float(parts[2]))
        return ir, red, rate

    def test_夹具文件存在且是250组(self) -> None:
        ir, red, rate = self._load()
        self.assertEqual(len(ir), 250)
        self.assertEqual(len(red), 250)
        self.assertEqual(rate, 25.0)

    def test_真机波形必须算出可用心率(self) -> None:
        ir, red, rate = self._load()
        result = analyze_ppg(ir, rate, red, PpgParams())
        self.assertTrue(result.finger_detected, result.reason)
        self.assertIsNotNone(result.heart_rate_bpm, f"旧算法在这里给不出结论：{result.reason}")
        # 操作者当时静息，且这份波形的带内主峰在 65.9 bpm
        self.assertGreaterEqual(result.heart_rate_bpm, 55.0)
        self.assertLessEqual(result.heart_rate_bpm, 100.0)
        self.assertGreater(result.quality, 0.30, "这份真实波形应当能过质量门槛")
        self.assertFalse(result.reason, result.reason)

    def test_真机波形的血氧落在合理区间(self) -> None:
        ir, red, rate = self._load()
        result = analyze_ppg(ir, rate, red, PpgParams())
        self.assertIsNotNone(result.spo2_percent)
        self.assertGreaterEqual(result.spo2_percent, 85.0)
        self.assertLessEqual(result.spo2_percent, 100.0)

    def test_真机波形不能靠旧的错误时间基数蒙对(self) -> None:
        """把同一份波形按 100 Hz 解读（旧的时间基数）⇒ 得不到 55~100 bpm 的结论。

        这条保证"夹具回归"确实在拦 E51/E52 那类错误，而不是恰好怎么算都对。
        """
        ir, red, _rate = self._load()
        wrong = analyze_ppg(ir, 100.0, red, PpgParams())
        hr = wrong.heart_rate_bpm
        self.assertFalse(
            hr is not None and 55.0 <= hr <= 100.0 and not wrong.reason,
            f"按 100Hz 解读 25Hz 的真实波形仍给出可用结论（{hr} bpm / {wrong.reason}）",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
