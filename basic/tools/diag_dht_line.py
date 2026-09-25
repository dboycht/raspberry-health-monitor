#!/usr/bin/env python3
"""DHT11 数据线诊断：把"上拉/下拉/浮空"三种电平组合**翻译成结论**。

为什么需要它
------------
"读不到温度"这件事有四五种完全不同的原因（没供电 / 线插错列 / 没共地 / 裸传感器缺上拉 /
器件坏了）。直接看代码只会看到一句 `只捕获到 0 个边沿`，**这句话对定位原因几乎没有区分力**。

本脚本换一个角度：**把数据脚当成一根普通输入线来回测**。
- 先给内部上拉，读 50 次 → 看线能不能被拉高；
- 再给内部下拉，读 50 次 → 看线能不能被拉低；
- 最后浮空，读 50 次 → 看线是否悬空乱跳。

三种组合对应完全不同的故障（判据见 `_interpret`），而且**不需要示波器**。

⚠️ 判据的边界（本项目踩过的教训，见 ERROR.md E30）
------------------------------------------------
"上拉=1、下拉=0"**只能证明"线上没有强驱动"**，它**不能**区分
"空脚"和"接着但不应答的模块"。所以脚本**不会**据此下"模块已接好"的结论，
只会说"线上没有器件在驱动"——**结论必须有区分力，否则就不是证据**。

用法::

    python3 basic/tools/diag_dht_line.py              # 三态电平（默认 GPIO4）
    python3 basic/tools/diag_dht_line.py --read 5     # 顺便连读 5 次真实数据
    python3 basic/tools/diag_dht_line.py --pin 17     # 换成另一个数据脚
    python3 basic/tools/diag_dht_line.py --json       # 机器可读输出

⚠️ 运行前请先停掉 `run.py`（同一个 GPIO 不能被两个进程同时占用）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

BASIC_DIR = Path(__file__).resolve().parents[1]
if str(BASIC_DIR.parent) not in sys.path:
    sys.path.insert(0, str(BASIC_DIR.parent))

from basic.dht11read import Dht11Error, Dht11Reader  # noqa: E402
from basic.pins import describe_pin  # noqa: E402
from basic import wire_spec  # noqa: E402
from basic.console import safe_text  # noqa: E402

#: 每种电平配置读多少次（取"高电平次数"作为判据）
DEFAULT_SAMPLES = 50


def _section(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def _interpret(pull_up_high: int, pull_down_high: int, floating_high: int, samples: int) -> Tuple[str, str]:
    """把三种电平组合翻译成 ``(结论级别, 中文结论)``。

    级别：``ok`` / ``warn`` / ``bad`` / ``unknown``。判据（全部可执行）：

    ===================  ==================  ==========================================
    上拉                 下拉                 含义
    ===================  ==================  ==========================================
    高（≈全部）           低（≈0）           线上没有器件强驱动（空脚，或器件不应答）
    低（≈0）             低（≈0）           线被**拉死到地**：DATA 接到 GND / 短路
    高（≈全部）           高（≈全部）         线被**拉死到 3.3V**：DATA 接到 VCC 那一列
    乱跳                  乱跳                 线太长/接触不良（天线效应）
    ===================  ==================  ==========================================
    """
    high_threshold = int(samples * 0.9)
    low_threshold = int(samples * 0.1)

    if pull_up_high <= low_threshold and pull_down_high <= low_threshold:
        return "bad", (
            "线被**拉死到地**：数据脚一直是低电平。\n"
            "    排查：① DATA 是不是接到了 GND 那一列（面包板每列纵向导通，最容易插错）；\n"
            "          ② 模块内部短路（换模块）；③ 杜邦线内部短路。"
        )
    if pull_up_high >= high_threshold and pull_down_high >= high_threshold:
        return "bad", (
            "线被**拉死到 3.3V**：数据脚一直是高电平。\n"
            "    排查：① DATA 插到了 3.3V（脚 1/17）同一列；② DATA 与 VCC 短路（拔掉模块再测一次对照）。"
        )
    if pull_up_high >= high_threshold and pull_down_high <= low_threshold:
        return "ok", (
            "线上**没有器件在强驱动**（上拉能拉高、下拉能拉低）。\n"
            "    这是「数据线接对了、但没有人在应答」的形态。它**不能**证明模块已经接好 ——\n"
            "    空脚与「接着但不应答的模块」读数完全一样（判据必须有区分力）。\n"
            "    请配合下面的 --read 连读，以及万用表量 VCC 是否 3.3V。"
        )
    return "warn", (
        "读数不稳定（既不是稳定高、也不是稳定低）。\n"
        "    排查：① 杜邦线过长或接触不良（换短线、换面包板孔位）；\n"
        "          ② 附近有强干扰源；③ 电源不稳（换电源适配器）。"
    )


def diag_levels(pin: int, samples: int = DEFAULT_SAMPLES) -> Optional[Dict[str, object]]:
    """三态电平测量；返回结果字典（``None`` = 缺少 lgpio，无法测量）。"""
    _section(f"① 数据线三态电平（GPIO{pin} = {describe_pin(pin)}）")
    try:
        import lgpio
    except ImportError as exc:
        print(safe_text(f"  ❌ 缺少 lgpio：{exc}"))
        print("     树莓派上装：sudo apt install -y python3-lgpio")
        print("     在电脑上跑不了这个检查（电脑没有 GPIO）—— 这是预期行为，不是脚本坏了。")
        return None

    handle = lgpio.gpiochip_open(0)
    result: Dict[str, object] = {"pin": pin, "samples": samples}
    try:
        # 先释放一次：如果上一次运行留下了占用，这里会给出明确报错
        try:
            lgpio.gpio_free(handle, pin)
        except Exception:  # noqa: BLE001 - 没占用是正常情况
            pass

        readings: Dict[str, int] = {}
        for label, flags in (("pull_up", lgpio.SET_PULL_UP), ("pull_down", lgpio.SET_PULL_DOWN), ("floating", 0)):
            try:
                lgpio.gpio_free(handle, pin)
            except Exception:  # noqa: BLE001
                pass
            try:
                lgpio.gpio_claim_input(handle, pin, flags)
            except Exception as exc:  # noqa: BLE001 - 最常见：引脚被别的进程占着
                print(safe_text(f"  ❌ 无法申请引脚 GPIO{pin}：{type(exc).__name__}: {exc}"))
                print("     最常见原因：`run.py` 还在跑（同一个 GPIO 只能被一个进程用）——先按 Ctrl+C 停掉它。")
                return None
            time.sleep(0.05)
            samples_list = [lgpio.gpio_read(handle, pin) for _ in range(samples)]
            high = int(sum(samples_list))
            readings[label] = high
            text = {"pull_up": "内部上拉", "pull_down": "内部下拉", "floating": "浮空"}[label]
            print(f"  {text:<6}：高电平 {high}/{samples} 次")
    finally:
        try:
            lgpio.gpio_free(handle, pin)
        except Exception:  # noqa: BLE001
            pass
        lgpio.gpiochip_close(handle)

    level, conclusion = _interpret(readings["pull_up"], readings["pull_down"], readings["floating"], samples)
    result.update({"readings": readings, "level": level, "conclusion": conclusion.strip()})
    mark = {"ok": "✅", "warn": "⚠️", "bad": "❌", "unknown": "❓"}[level]
    print(safe_text(f"\n  {mark} 结论：{conclusion}"))
    return result


def diag_read(pin: int, times: int = 5, interval_s: float = None) -> List[Dict[str, object]]:
    """连读若干次真实数据（走与 `run.py` 完全相同的驱动代码）。"""
    interval = wire_spec.RECOMMENDED_INTERVAL_S if interval_s is None else interval_s
    _section(f"② 连读 {times} 次（每次间隔 {interval:g} 秒，与 run.py 同一条代码路径）")
    reader = Dht11Reader(pin=pin, min_interval_s=max(interval, wire_spec.MIN_INTERVAL_S))
    results: List[Dict[str, object]] = []
    try:
        backend = reader.open()
        print(f"  后端：{backend}　（{reader.describe()}）")
        for index in range(1, times + 1):
            result = reader.read()
            stamp = time.strftime("%H:%M:%S")
            if result.ok:
                text = f"温度: {result.temperature_c:.1f} ℃，湿度: {result.humidity_percent:.0f} %"
            else:
                text = f"失败：{result.note}"
            print(f"  [{stamp}] #{index}　{text}")
            results.append({"index": index, "ok": result.ok, "temperature_c": result.temperature_c,
                            "humidity_percent": result.humidity_percent, "note": result.note})
            if index < times:
                time.sleep(interval)
    except Dht11Error as exc:
        print(safe_text(f"  ❌ 打不开 DHT11：{exc}"))
        results.append({"index": 0, "ok": False, "note": str(exc)})
    finally:
        reader.close()

    ok_count = sum(1 for item in results if item.get("ok"))
    print()
    if ok_count:
        print(safe_text(f"  ✅ {ok_count}/{len(results)} 次读到有效数据 —— 器件与接线是通的。"))
    else:
        print(safe_text("  ❌ 一次都没读到。按顺序排查（每步都有判据，别跳步）："))
        print("     ① 万用表量模块 VCC 对 GND 是否约 3.3V（不是 5V）")
        print(f"     ② 万用表蜂鸣档量模块 DATA ↔ 树莓派物理脚 {wire_spec.DATA_PHYSICAL} 是否导通")
        print("     ③ 万用表蜂鸣档量模块 GND ↔ 树莓派 GND 是否导通（共地）")
        print(f"     ④ 裸四针传感器：DATA ↔ 3.3V 之间应量到 "
              f"{wire_spec.PULLUP_OHM_RANGE[0] // 1000}kΩ~{wire_spec.PULLUP_OHM_RANGE[1] // 1000}kΩ")
        print("     ⑤ 换一个 DHT11 模块再试（器件本身坏是最常见的一种）")
    return results


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="DHT11 数据线诊断（三态电平 + 连读）")
    parser.add_argument("--pin", type=int, default=wire_spec.DATA_BCM,
                        help=f"DHT11 数据脚 BCM 编号（默认 {wire_spec.DATA_BCM}）")
    parser.add_argument("--samples", type=int, default=DEFAULT_SAMPLES, help="每种电平配置的采样次数")
    parser.add_argument("--read", type=int, default=0, metavar="N", help="顺便连读 N 次真实数据（0 = 不读）")
    parser.add_argument("--no-levels", action="store_true", help="跳过三态电平测量（只连读）")
    parser.add_argument("--json", action="store_true", help="最后打印 JSON（机器可读）")
    args = parser.parse_args(argv)

    print("=" * 72)
    print("DHT11 数据线诊断（基础版）")
    print("=" * 72)
    print(f"数据脚：GPIO{args.pin} = {describe_pin(args.pin)}")
    print(f"期望接线：VCC → 3.3V（脚 {'/'.join(map(str, wire_spec.V33_PHYSICAL))}）、"
          f"GND → 脚 {wire_spec.GND_RECOMMENDED}、DATA → 脚 {wire_spec.DATA_PHYSICAL}")
    print(safe_text("⚠️ 运行前请先停掉 run.py（同一个 GPIO 不能被两个进程同时用）"))

    payload: Dict[str, object] = {"pin": args.pin}
    levels = None if args.no_levels else diag_levels(args.pin, args.samples)
    if levels is not None:
        payload["levels"] = levels
    if args.read > 0:
        payload["reads"] = diag_read(args.pin, args.read)

    if args.json:
        print()
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    # 退出码：三态电平被拉死（bad）或指定了连读但一次都没成功 → 1
    failed = False
    if levels is not None and levels.get("level") == "bad":
        failed = True
    reads = payload.get("reads")
    if isinstance(reads, list) and reads and not any(item.get("ok") for item in reads):
        failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
