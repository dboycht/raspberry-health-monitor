"""抓一帧 DHT11 的**原始边沿时序**，把每一位的宽度打出来。

为什么（2026-09-25 真机）：驱动稳定报「只解出 39/40 个 bit」，而同一时刻
`diag_dht_bits.py` 数到的是**满帧 83 个边沿**（高电平段 42 个）。
83 个边沿、42 个高电平段，理论上应当能配出 **41 个 (高,低) 对**：
    1 个应答 + 40 个数据位 = 41 ⇒ 丢掉应答后正好 40 位。
而驱动只解出 39 位 ⇒ 要么有 1 个宽度被判无效（>200µs 被丢弃），
要么配对的循环少走了一轮。本脚本把**每一位的宽度**打印出来，一眼就能看出来。

判据：
    宽度>200µs 的位（被驱动丢弃）  ⇒ 采样窗口/时序抖动问题
    配对总数 < 41                  ⇒ 解码循环的 off-by-one
"""
from __future__ import annotations

import contextlib
import sys
import time

import lgpio
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

sys.path.insert(0, ".")

P7, P11, P13 = 4, 17, 27
ALL = (P7, P11, P13)
MAX_BIT_WIDTH_US = 200.0
RESPONSE_MIN_US = 60.0     # 应答高电平约 80µs；数据"1"约 70µs，靠位置区分而不是宽度


def free(handle: int, bcm: int) -> None:
    with contextlib.suppress(Exception):
        lgpio.gpio_free(handle, bcm)


def capture(handle: int, bcm: int) -> list[tuple[int, int]]:
    edges: list[tuple[int, int]] = []
    cb = None
    try:
        free(handle, bcm)
        lgpio.gpio_claim_output(handle, bcm, 0)
        lgpio.gpio_write(handle, bcm, 0)
        time.sleep(0.020)
        free(handle, bcm)
        lgpio.gpio_claim_alert(handle, bcm, lgpio.BOTH_EDGES, lgpio.SET_PULL_UP)
        cb = lgpio.callback(handle, bcm, lgpio.BOTH_EDGES,
                            lambda _chip, _gpio, level, ts: edges.append((level, ts)))
        time.sleep(0.040)
    finally:
        if cb is not None:
            with contextlib.suppress(Exception):
                cb.cancel()
        free(handle, bcm)
    return edges


def main() -> int:
    handle = lgpio.gpiochip_open(0)
    print("=" * 92)
    print("抓一帧原始时序，逐位打印宽度")
    print("=" * 92)
    try:
        # 用脚 11 给模块供电（用户的三根线里有一根是 +）、脚 13 当 GND
        for bcm in ALL:
            free(handle, bcm)
        lgpio.gpio_claim_output(handle, P11, 1)
        lgpio.gpio_write(handle, P11, 1)
        lgpio.gpio_claim_output(handle, P13, 0)
        lgpio.gpio_write(handle, P13, 0)
        time.sleep(0.5)

        edges = capture(handle, P7)
        for bcm in ALL:
            free(handle, bcm)

        highs = [ts for level, ts in edges if level == 1]
        print(f"\n边沿总数 {len(edges)}　高电平段 {len(highs)}")
        print(f"前 6 个边沿：{[(lv, ts) for lv, ts in edges[:6]]}")

        pairs: list[float] = []
        i = 1
        while i + 1 < len(edges):
            level, ts_a = edges[i]
            nxt, ts_b = edges[i + 1]
            if level == 1 and nxt == 0:
                pairs.append((ts_b - ts_a) / 1000.0)
                i += 2
            else:
                i += 1
        print(f"配对出的高电平段数：{len(pairs)}（理论 41 = 应答 1 + 数据 40）")
        safe_print("逐位宽度（µs）：")
        for index, width in enumerate(pairs):
            tag = "应答" if index == 0 else f"#{index:02d}"
            flag = "  ← 超过 200µs，会被驱动丢弃" if width >= MAX_BIT_WIDTH_US else ""
            print(f"  {tag:>5}: {width:8.1f}{flag}")
        data = pairs[1:]
        valid = [w for w in data if 0.0 < w < MAX_BIT_WIDTH_US]
        safe_print(f"\n丢掉应答后：{len(data)} 个候选位；其中有效（<200µs）：{len(valid)} 个")
        print("判据：")
        safe_print("  · 候选位 40 个、有效 39 个 ⇒ 某一位宽度异常（多半是采样窗口/信号质量）")
        safe_print("  · 候选位只有 39 个        ⇒ 解码循环少配一对（off-by-one，改代码即可）")
    finally:
        for bcm in ALL:
            free(handle, bcm)
        lgpio.gpiochip_close(handle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
