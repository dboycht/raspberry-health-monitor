"""最终判据：脚 7 是被"强驱动到高"，还是自己能拉低。

上一版实验 B 的写法有缺陷：`gpio_claim_output(handle, pin, 0)` 的第三个参数只是
**初值**，并不能保证把线拉到低（lgpio 的语义是"声明输出 + 设初值"）。
本版用 **claim_output 之后显式 `gpio_write(0)` 并保持 50ms**，同时用另一路（同脚回读）
看它到底降没降 —— 降不下去 = 线被强驱动/短接到 3.3V。

判据：
    · 驱动 0 之后读回 0  ⇒ 线能被拉低（**数据线不在脚 7 上**，或线上没有强驱动）
    · 驱动 0 之后读回 1  ⇒ **有东西以比 GPIO 更强的力量顶在 3.3V**
                          （典型：数据线与 3.3V 电轨短接；或模块反插）
"""
from __future__ import annotations

import contextlib
import time

import lgpio

DATA = 4          # 物理脚 7
DRIVE_MS = 50


def free(handle: int, bcm: int) -> None:
    with contextlib.suppress(Exception):
        lgpio.gpio_free(handle, bcm)


def drive_and_readback(handle: int, bcm: int, level: int) -> tuple[int, int]:
    """把脚驱动到 level，保持 DRIVE_MS，期间回读；返回 (驱动期间高电平次数, 释放后高电平次数)。"""
    free(handle, bcm)
    lgpio.gpio_claim_output(handle, bcm, level)
    lgpio.gpio_write(handle, bcm, level)          # ★ 显式驱动（这是上一版漏掉的一步）
    time.sleep(0.05)
    during = sum(lgpio.gpio_read(handle, bcm) for _ in range(20))
    free(handle, bcm)
    lgpio.gpio_claim_input(handle, bcm, lgpio.SET_PULL_DOWN)
    time.sleep(0.02)
    after = sum(lgpio.gpio_read(handle, bcm) for _ in range(20))
    free(handle, bcm)
    return during, after


def main() -> int:
    handle = lgpio.gpiochip_open(0)
    print("=" * 88)
    print("脚 7（GPIO4）能不能被拉低")
    print("=" * 88)
    try:
        during0, after0 = drive_and_readback(handle, DATA, 0)
        print(f"  驱动 0 期间回读：{during0}/20　释放后（内部下拉）：{after0}/20")
        print("    驱动 0 却仍读 1 ⇒ 有线以更强的力量把它顶在 3.3V（短接到电源 / 模块反插）")
        print("    驱动 0 读回 0   ⇒ 线能被拉低 ⇒ 数据线很可能**不在脚 7 上**")
        during1, after1 = drive_and_readback(handle, DATA, 1)
        print(f"  驱动 1 期间回读：{during1}/20　释放后（内部下拉）：{after1}/20")
    finally:
        free(handle, DATA)
        lgpio.gpiochip_close(handle)

    print("""
结论怎么用
----------
情况甲：驱动 0 期间仍读 1
    ⇒ 脚 7 上挂着一根**接到 3.3V 的线**（或与 3.3V 电轨/脚 1 同列）。
      请检查：数据线是不是插在**脚 1**那一列/那一个（面包板同一列是连通的）；
      把它换到**脚 7**（脚 6 的隔壁、奇数排第 4 个洞）。
情况乙：驱动 0 读回 0
    ⇒ 脚 7 本身是好的、线上没有强驱动 ⇒ **数据线不在脚 7 上**（插到别处了），
      或者那根线断了。请把三根线逐根从模块侧拔下来、单独量通断。
""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
