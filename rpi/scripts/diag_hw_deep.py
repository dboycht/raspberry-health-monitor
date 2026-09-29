#!/usr/bin/env python3
"""精细诊断：为什么"能扫到地址"却"读不到数据"。

两个具体问题：
  ① MAX3010x：i2cdetect 能看到 0x57，但"版本号"读不对；
  ② TMP36+MCP3002：SPI 能打开，但 ADC 读数全是 None。

⚠️ **关于 ① 的重要更正（2026-09-29 真机，`ERROR.md` E49）**：
早前把"0x21 读到 0x00"当成"器件可疑/假片"，**是误判**。MAX3010x 系有**两套已知布局**：

    MAX30102/MAX30101：0x21 = PART_ID（应读回 0x15）
    MAX30105（SparkFun 库口径）：0xFF = PART_ID（应读回 0x15），0x21 是 TEMP_CONFIG

本机模块是后者。**有区分力的判据**：往 0x21 写 0x01 再读 0x1F/0x20 ——
若立刻报出一个像室温的温度，说明 0x21 是 TEMP_EN（MAX30105 布局），版本号在 0xFF。
所以本脚本**两个地址都读**，并且能区分"器件可疑"与"布局不同"。

本脚本的 MAX3010x 部分会**写** 0x21（只为验 TEMP_EN，取证实用）与恢复原始值；
TMP36/MCP3002 部分只读。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# 让 `basic.console.safe_print` 可导入（打印 ✅/❌/⚠ 时在窄编码控制台上自动降级）
# ⚠️ 为什么（2026-09-25 真机实测，ERROR.md E32/E35）：中文 Windows / GBK 控制台上
#    `print` 直接打印这些符号时会抛 UnicodeEncodeError 把**整个脚本**崩掉；这些工具主要跑在
#    树莓派（UTF-8）上，导入失败就退回内置 print（行为与过去一致）。
try:
    from pathlib import Path  # noqa: E402
except ImportError:  # pragma: no cover - Path 是标准库，理论上不会失败
    Path = None
ROOT = Path(__file__).resolve().parents[2] if Path is not None else None
if ROOT is not None and str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    from basic.console import safe_print  # noqa: E402
except ImportError:  # pragma: no cover - 只在 basic 不可用时
    safe_print = print

RPI_DIR = Path(__file__).resolve().parents[1]
if str(RPI_DIR) not in sys.path:
    sys.path.insert(0, str(RPI_DIR))

#: PART_ID 的两处已知地址（与 `health_monitor/sensors/max30102.py` 同源）
PART_ID_REGISTERS = (0x21, 0xFF)
PART_ID_VALUE = 0x15


def section(t: str) -> None:
    print("\n" + "=" * 74)
    print(t)
    print("=" * 74)


def diag_max30102() -> None:
    section("① MAX3010x（I2C 0x57）")
    try:
        from smbus2 import SMBus
    except ImportError:
        safe_print("  ❌ 未安装 smbus2")
        return

    bus = SMBus(1)
    candidates = [0x57, 0x56, 0x55, 0x58]
    print("  逐个地址读两处版本号寄存器（0x15 = MAX3010x 系；0x11 = MAX30105 早期批次）：")
    safe_print("    [!] 0x21 读 0x00 **不等于**坏件：MAX30105 布局下 0x21 是 TEMP_CONFIG")
    hits = []
    for addr in candidates:
        for attempt in range(1, 4):
            try:
                values = [(reg, bus.read_byte_data(addr, reg)) for reg in PART_ID_REGISTERS]
                text = "、".join(f"0x{reg:02X}=0x{v:02X}" for reg, v in values)
                hit = [reg for reg, v in values if v in (PART_ID_VALUE, 0x11)]
                if hit:
                    hits.append((addr, hit[0]))
                    layout = "MAX30102/MAX30101 布局" if hit[0] == 0x21 else "MAX30105 布局"
                    safe_print(f"    0x{addr:02X} 第{attempt}次：{text}　✅ 命中 {layout}")
                else:
                    safe_print(f"    0x{addr:02X} 第{attempt}次：{text}　⚠️ 两处都不是 0x15")
                break
            except OSError as exc:
                if attempt == 3:
                    print(f"    0x{addr:02X}：无应答（{exc.__class__.__name__}）")
                time.sleep(0.05)

    print("\n  对照：同一总线上别的器件（LCD 在 0x27）读一下，验证总线本身没问题：")
    try:
        # PCF8574 无"寄存器"概念，用 SMBus 的 quick 命令探测是否 ACK
        bus.write_quick(0x27)
        safe_print("    0x27：✅ 应答（说明 I2C 总线与上拉是好的）")
    except OSError as exc:
        safe_print(f"    0x27：❌ 也不应答（{exc}）→ 问题可能在总线本身")

    print("\n  再试读几个已知寄存器（看是不是「只有版本号读不到」）：")
    for reg, name in (
        (0x00, "INTR_STATUS_1（bit0=POWER_READY）"),
        (0x04, "FIFO_WR_PTR"),
        (0x07, "FIFO_DATA"),
        (0xFE, "REV_ID（MAX3010x 实测 0x03）"),
    ):
        try:
            value = bus.read_byte_data(0x57, reg)
            print(f"    reg 0x{reg:02X} ({name}) = 0x{value:02X}")
        except OSError as exc:
            print(f"    reg 0x{reg:02X} ({name}) 读失败：{exc}")

    if not hits:
        print("\n  两处版本号都没命中 —— 再做一次**有区分力**的实验：0x21 是 TEMP_EN 吗？")
        safe_print("    （写 0x21=0x01 后若报出像室温的温度 -> 是 MAX30105 布局，版本号该去 0xFF 读）")
        try:
            original = bus.read_byte_data(0x57, 0x21)
            bus.write_byte_data(0x57, 0x21, 0x01)
            time.sleep(0.3)
            raw_int = bus.read_byte_data(0x57, 0x1F)
            raw_frac = bus.read_byte_data(0x57, 0x20)
            celsius = raw_int + (raw_frac & 0x0F) * 0.0625
            print("    写 0x21=0x01 后：0x1F=0x%02X、0x20=0x%02X -> %.2f C" % (raw_int, raw_frac, celsius))
            if 5.0 <= celsius <= 45.0:
                safe_print("    ✅ 落在室温区间 ⇒ **0x21 是 TEMP_CONFIG**，器件用的 MAX30105 布局")
                safe_print("       （版本号在 0xFF；旧版脚本只查 0x21 会把它误判成『器件可疑』）")
            else:
                safe_print("    ⚠️ 不是室温 ⇒ 0x21 不像 TEMP_EN，需按『器件可疑』继续排查")
            bus.write_byte_data(0x57, 0x21, original)  # 复原，不留下副作用
            print(f"    已把 0x21 复原成 0x{original:02X}")
        except OSError as exc:
            print(f"    写/读 0x21 失败：{exc}")

    print("\n  结论提示：")
    safe_print("    · 任一处读到 0x15 -> 器件是 MAX3010x 系，**可用**（布局不同不是故障）")
    print("    · 若 0x27 应答而 0x57 所有寄存器都读 0x00/无应答 → 器件供电或接触问题")
    print("    · 若两处都不是 0x15、且 0x21 写 1 也读不出室温 → 才怀疑兼容片/坏片")
    print("    · 若读值随机跳变 → 线太长/无上拉/电源噪声（I2C 建议 ≤30cm 且共用 4.7k 上拉）")
    bus.close()


def diag_tmp36_mcp3002() -> None:
    section("② TMP36 + MCP3002（SPI0.0，CE0）")
    try:
        import spidev
    except ImportError:
        safe_print("  ❌ 未安装 spidev")
        return

    spi = spidev.SpiDev()
    try:
        spi.open(0, 0)
        spi.max_speed_hz = 1_000_000
        spi.mode = 0
    except Exception as exc:  # noqa: BLE001
        safe_print(f"  ❌ 打开 SPI0.0 失败：{exc}")
        return

    print("  读 CH0 与 CH1 各 5 次（MCP3002 单端模式；raw 0~1023）：")
    for channel in (0, 1):
        # MCP3002 单端：起始位1 + SGL/DIFF=1 + ODD/SIGN=channel + 填充 → 0x60 | (channel<<5)
        cmd = [0x60 | (channel << 5), 0x00]
        raws = []
        for _ in range(5):
            resp = spi.xfer2(cmd)
            raw = ((resp[0] & 0x03) << 8) | resp[1]
            raws.append(raw)
            time.sleep(0.02)
        volts = [r / 1023.0 * 3.3 for r in raws]
        print(f"    CH{channel}: raw={raws}　电压≈{[round(v, 3) for v in volts]} V")
        if all(r == 0 for r in raws):
            safe_print("      ⚠️ 全是 0：MCP3002 没回应（CS/CLK/DIN/DOUT 接线或供电）")
        elif all(r == 1023 for r in raws):
            safe_print("      ⚠️ 全是 1023：输入饱和（CH 接到 3.3V 了？或 TMP36 供电接了 5V）")
        elif channel == 0:
            temp = [(r / 1023.0 * 3.3 - 0.5) * 100 for r in raws]
            safe_print(f"      → 按 TMP36 公式换算温度：{[round(t, 1) for t in temp]} ℃")
            safe_print("      （室温应 20~30 ℃；若约 50 ℃ 说明公式没用 0.5V 偏移）")
    spi.close()
    print("\n  结论提示：")
    print("    · CH0 与 CH1 都恒定相同值 → 很可能 MCP3002 没工作（先查 CS=物理脚24 与供电）")
    print("    · CH0 有变化但 TMP36 没接 → 那是悬空引脚在飘（接上 TMP36 中脚到 CH0）")
    print("    · 别忘了 TMP36 与 MCP3002 必须**共地**、都用 3.3V")


def diag_dht11() -> None:
    section("③ DHT11（GPIO4 = 物理脚 7）")
    from health_monitor.sensors.dht11 import Dht11

    dev = Dht11(pin=4, retries=3, min_interval_s=2.0)
    try:
        dev.open()
        print(f"  open 成功，后端 = {dev._backend}")
        for attempt in range(1, 4):
            try:
                temp, humid = dev._read_lgpio_raw()
                safe_print(f"  第{attempt}次读取：温度={temp}℃ 湿度={humid}%　✅ 成功")
            except Exception as exc:  # noqa: BLE001
                print(f"  第{attempt}次读取失败：{type(exc).__name__}: {str(exc)[:160]}")
            if attempt < 3:
                time.sleep(2.2)
    except Exception as exc:  # noqa: BLE001
        safe_print(f"  ❌ open 失败：{type(exc).__name__}: {exc}")
    finally:
        dev.close()
    print("\n  结论提示：")
    print("    · 三次都「未应答」→ DHT11 没接好（VCC=3.3V、DATA=物理脚7、GND 共地、裸件要 4.7k 上拉）")
    print("    · 偶发成功 → 线太长或上拉偏弱，缩短线/换 4.7k 上拉")


def main() -> int:
    print("=" * 74)
    print("精细诊断：能扫到地址 ≠ 能读到数据")
    print("=" * 74)
    diag_max30102()
    diag_tmp36_mcp3002()
    diag_dht11()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
