#!/usr/bin/env python3
"""探明 lgpio 的时序读取能力（为自实现 DHT11 单总线时序选方案）。

DHT11 的协议要点：主机拉低 ≥18ms → 释放 → 传感器应答 → 40 个 bit，
每个 bit 以"高电平持续时间"区分 0（约 26~28µs）与 1（约 70µs）。
**在 Python 里轮询读电平通常来不及**，所以要优先找"带时间戳的边沿回调"能力。

本脚本只做**能力探测**，不接任何传感器、不改变任何引脚状态以外的行为。
用法：python3 scripts/diag_lgpio_timing.py
"""

from __future__ import annotations
import sys

import inspect

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


def section(t: str) -> None:
    print("\n" + "=" * 74)
    print(t)
    print("=" * 74)


def main() -> int:
    section("① lgpio 模块信息")
    try:
        import lgpio
    except ImportError as exc:
        safe_print(f"❌ lgpio 不可用：{exc}")
        return 2
    print(f"  文件：{lgpio.__file__}")
    for attr in ("__version__", "VERSION", "version"):
        if hasattr(lgpio, attr):
            print(f"  {attr} = {getattr(lgpio, attr)}")

    section("② 关键 API 是否存在（时序读取靠它们）")
    wanted = [
        "gpio_claim_input", "gpio_claim_output", "gpio_read", "gpio_write",
        "gpio_claim_alert", "gpio_free", "gpiochip_open", "gpiochip_close",
        "callback", "wait_for_level", "gpio_get_mode",
        "gpio_set_debounce_micros",
    ]
    for name in wanted:
        safe_print(f"  {'✅' if hasattr(lgpio, name) else '❌'} {name}")

    section("③ 签名（看能不能拿到带时间戳的边沿）")
    for name in ("callback", "wait_for_level", "gpio_claim_alert", "gpio_read"):
        fn = getattr(lgpio, name, None)
        if fn is None:
            continue
        try:
            print(f"  {name}{inspect.signature(fn)}")
        except (TypeError, ValueError):
            print(f"  {name}: <内置函数，无法读取签名>")

    section("④ 常量（边沿类型 / 电平）")
    for name in ("RISING_EDGE", "FALLING_EDGE", "BOTH_EDGES", "SET_ACTIVE_LOW",
                 "SET_PULL_UP", "SET_PULL_DOWN", "TIMEOUT"):
        if hasattr(lgpio, name):
            print(f"  {name} = {getattr(lgpio, name)}")

    section("⑤ 实测：在空闲引脚上试一次回调（不接线也能跑）")
    # 用一个明确空闲的 BCM 引脚（本项目的引脚分配里 20/21 未被占用；这里用 BCM 20）
    PIN = 20
    try:
        h = lgpio.gpiochip_open(0)
        lgpio.gpio_claim_input(h, PIN, lgpio.SET_PULL_UP)
        events = []

        def _cb(chip, gpio, level, timestamp):
            events.append((level, timestamp))

        try:
            cb = lgpio.callback(h, PIN, lgpio.BOTH_EDGES, _cb)
            import time
            time.sleep(0.2)
            # 手动制造一次电平变化：切成输出、拉低、再切回输入
            cb.cancel()
            safe_print("  ✅ callback(BOTH_EDGES) 可用（能注册、能取消）")
        except Exception as exc:  # noqa: BLE001
            safe_print(f"  ⚠️ callback 注册失败：{type(exc).__name__}: {exc}")
        # 看事件回调签名里 timestamp 是什么单位（lgpio 文档：纳秒）
        safe_print("  ℹ️ 回调签名 (chip_handle, gpio, level, timestamp)：lgpio 的 timestamp 为**纳秒**，")
        print("     用它算高低电平宽度即可解出 0/1（不需要 Python 轮询）。")
        lgpio.gpio_free(h, PIN)
        lgpio.gpiochip_close(h)
    except Exception as exc:  # noqa: BLE001
        safe_print(f"  ❌ 探测失败：{type(exc).__name__}: {exc}")

    section("结论")
    print("  · 若 callback + gpio_claim_alert 可用：可以用'边沿时间戳'实现 DHT11，")
    print("    比轮询稳得多（Python 轮询微秒级不可靠）。")
    print("  · 若不可用：退回'紧循环轮询'方案，并在文档里注明'高负载时可能偶发失败'。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
