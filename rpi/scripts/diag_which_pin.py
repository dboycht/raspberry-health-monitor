"""三态全高的终极分辨：数据线到底是接在**脚 7** 上，还是接在**脚 1（3.3V）**上？

背景（2026-09-25 真机）：用户说接的是 1（3.3V）/ 6（GND）/ 7（DATA），
但实测物理脚 7 是「上拉 25/25、下拉 25/25、浮空 25/25」= **三态全高**。
三态全高有两种成因，本脚本一次分开：

    ① 数据线其实接在 **脚 1（3.3V 电源）** 上
       ⇒ 脚 1 读起来会像"被钉在 3.3V"（上拉/下拉都读到 1，因为它被 3.3V 电源撑着），
         而脚 7 之所以也读到高，是因为……不，脚 7 不会有这种表现，
         所以这一条只看"脚 1 上有没有挂着东西"就够。
    ② 数据线接在脚 7，但模块把它**强驱动**到高（模块反插/损坏）

判据（其中唯一"驱动"动作只有 1 毫秒，电流被模块自身限流，风险极低）
--------------------------------------------------------------
实验 A：读脚 1（物理脚 1）—— 如果它读起来"像被 3.3V 撑着"（下拉也读到高），
        说明这根线是**接到了电源**。正常情况：脚 1 是 3.3V，本来就是高，读出来都是 1，
        所以这条**只能证明它确实是 3.3V**，不能证明线挂在哪 —— 于是实验 B 才是关键。
实验 B：让脚 7 输出 0（拉低）1 毫秒：
        · 若数据线真接在脚 1（3.3V）上 ⇒ 脚 7 上什么都没有，输出 0 毫无影响，
          脚 7 释放后立刻回到"上拉=高"；
        · 若数据线接在脚 7 上且被模块强驱动 ⇒ 脚 7 输出低时会被 3.3V 对抗
          （读数仍可能是高），释放后也仍是高。
        两者的区别体现在**脚 7 能不能被自己拉低**。
实验 C：把脚 7 当输入、脚 11 与 13 也当输入，逐个上拉，看哪个脚"跟着动"。
"""
from __future__ import annotations

import contextlib
import time

import lgpio

CANDIDATES = [
    (1, None, "3.3V 电源脚（读它一定全是 1，属正常）"),
    (6, None, "GND（读它一定全是 0，属正常）"),
    (7, 4, "DHT11 DATA（用户说接在这里）"),
    (11, 17, "空脚（对照）"),
    (13, 27, "空脚（对照）"),
    (15, 22, "空脚（对照）"),
]


def free(handle: int, bcm: int) -> None:
    with contextlib.suppress(Exception):
        lgpio.gpio_free(handle, bcm)


def read3(handle: int, bcm: int, samples: int = 20) -> tuple[int, int, int]:
    out = []
    for flags in (lgpio.SET_PULL_UP, lgpio.SET_PULL_DOWN, 0):
        free(handle, bcm)
        lgpio.gpio_claim_input(handle, bcm, flags)
        time.sleep(0.04)
        out.append(sum(lgpio.gpio_read(handle, bcm) for _ in range(samples)))
        free(handle, bcm)
    return out[0], out[1], out[2]


def main() -> int:
    handle = lgpio.gpiochip_open(0)
    print("=" * 94)
    print("分辨：数据线接在脚 7 上，还是接在脚 1（3.3V）上？")
    print("=" * 94)
    try:
        print(f"\n{'物理脚':<6}{'BCM':<6}{'上拉':<8}{'下拉':<8}{'浮空':<8}说明")
        print("-" * 94)
        for physical, bcm, label in CANDIDATES:
            if bcm is None:
                print(f"{physical:<6}{'—':<6}{'（电源/地脚，不测）':<24}{label}")
                continue
            up, down, floating = read3(handle, bcm)
            print(f"{physical:<6}{bcm:<6}{up:<8}{down:<8}{floating:<8}{label}")

        print("\n【实验 B】脚 7 能不能被自己拉低（输出 0 一毫秒）")
        free(handle, 4)
        lgpio.gpio_claim_output(handle, 4, 0)
        time.sleep(0.001)
        free(handle, 4)
        lgpio.gpio_claim_input(handle, 4, lgpio.SET_PULL_DOWN)
        time.sleep(0.02)
        after_low = sum(lgpio.gpio_read(handle, 4) for _ in range(20))
        free(handle, 4)
        print(f"  输出过 0 之后、用内部下拉读：{after_low}/20")
        print("    0/20  ⇒ 脚 7 能被拉低（数据线上没有强驱动）")
        print("    20/20 ⇒ 脚 7 被**强驱动到高**（模块反插/损坏，或线接在 3.3V 上）")
    finally:
        for _, bcm, _ in CANDIDATES:
            if bcm is not None:
                free(handle, bcm)
        lgpio.gpiochip_close(handle)

    print("""
怎么读
------
· 脚 7 = 上拉/下拉/浮空 **全 1** 且实验 B 也是 20/20
    ⇒ 数据线**挂在 3.3V 上**：它其实插在**脚 1**（或与脚 1 同列/同一条电轨）。
      改动：把数据线从脚 1 挪到**脚 7**。判据 = 挪完后脚 7 变成「上拉=满、下拉=0」。
· 脚 7 = 全 1，但实验 B 读到 0/20
    ⇒ 脚 7 自己能拉低，说明那根线不在 7 上 ⇒ 与上面同一条结论。
· 脚 11/13/15 里有哪个不是「上拉=满、下拉=0」
    ⇒ 数据线可能挂在那个脚上（那就是"插错排"）。
""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
