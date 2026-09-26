"""最终判定：脚 7 被钉在 0V —— 到底是「线/模块拽住」还是「板子这边短路」。

用户 2026-09-25 已经按接线表接好（+ → 脚1、- → 脚6、S → 脚7），现象不变：
    脚 7 内部上拉 = 0/25、内部下拉 = 0/25、浮空 = 0/25，
    而且**再挂一个内部弱上拉也顶不动**（脚 11 上拉时脚 7 仍 0/25）。

于是只剩两种可能，本脚本用两个实验分开：

实验 A「脚 1 / 脚 6 是否真的带电」
    只用**读**：跑 `python3 scripts/diag_pin_levels.py --adc` 之类会误伤，
    所以这里用"整机状态"侧写：读取 `/proc/device-tree` 无关，
    改为**提示人工**用万用表量 脚1-脚6 ≈ 3.3V（脚本给出判据）。

实验 B「拔掉 S（数据线）再量脚 7」—— **决定性**
    · 拔掉后脚 7 变成"上拉=满、下拉=0" ⇒ 是**线或模块**把线拽住的（换模块/换线）
    · 拔掉后脚 7 仍然全 0 ⇒ 是**板子这一侧**（脚 7 被短路到地：面包板同列、排针桥接、或 GPIO 已损坏）
"""
from __future__ import annotations

import contextlib
import time

import sys
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

PINS = [(7, 4, "DHT11 DATA（S 那根）"), (11, 17, "空脚对照"), (13, 27, "空脚对照")]
SAMPLES = 25


def free(handle: int, bcm: int) -> None:
    with contextlib.suppress(Exception):
        lgpio.gpio_free(handle, bcm)


def triple(handle: int, bcm: int) -> tuple[int, int, int]:
    out = []
    for flags in (lgpio.SET_PULL_UP, lgpio.SET_PULL_DOWN, 0):
        free(handle, bcm)
        lgpio.gpio_claim_input(handle, bcm, flags)
        time.sleep(0.05)
        out.append(sum(lgpio.gpio_read(handle, bcm) for _ in range(SAMPLES)))
        free(handle, bcm)
    return out[0], out[1], out[2]


def verdict(up: int, down: int) -> str:
    if up >= SAMPLES * 0.9 and down <= SAMPLES * 0.1:
        return "正常（上拉=满、下拉=0）"
    if up <= 1 and down <= 1:
        return "★ 被钉在 0V（有东西把它拉到地）"
    return "异常（读数在跳）"


def main() -> int:
    handle = lgpio.gpiochip_open(0)
    print("=" * 86)
    print(f"脚 7 最终判定（每种配置读 {SAMPLES} 次）")
    print("=" * 86)
    try:
        for physical, bcm, label in PINS:
            up, down, floating = triple(handle, bcm)
            print(f"  物理脚 {physical:>2} (BCM {bcm:>2}) {label:<22}"
                  f"上拉 {up:>2}/{SAMPLES}　下拉 {down:>2}/{SAMPLES}　浮空 {floating:>2}/{SAMPLES}　{verdict(up, down)}")
    finally:
        for _, bcm, _ in PINS:
            free(handle, bcm)
        lgpio.gpiochip_close(handle)

    safe_print("""
判定方法（30 秒，做一个就够）
----------------------------
把模块的 **S（数据）那根线从物理脚 7 上拔下来**，然后重跑本脚本：

  · 脚 7 变成「上拉=满、下拉=0」 ⇒ 是**线或模块**把线拽住的
        → 先换一根杜邦线；还不行就**换模块**（你现在这个很可能已经坏了）
  · 脚 7 仍然「上拉=0、下拉=0」 ⇒ 是**板子这一侧**被短路到地
        → 检查面包板（脚 7 那一列是不是连到了地轨）、排针有没有桥接；
          把杜邦线从面包板拔掉、直接量排针，再跑一次
""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
