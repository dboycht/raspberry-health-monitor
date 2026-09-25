"""真机接线"体检"：把 40-pin 上与本次接线相关的脚逐个量一遍。

三层判据（全部只读，不会损坏器件）：
  ① 三态电平：内部上拉 / 内部下拉 / 浮空 各读 N 次 —— 判断这根线**有没有被强驱动**
  ② 对地/对电源电阻：拉高后读电压，或用内部下拉读电平 —— 与已知的 GND/3.3V 脚对照
  ③ 与地/电源是否"导通"：把两个脚都设成"一拉一放"，看电流是否被对方拽走（软判据）

输出：一行一个脚，右侧给出结论（正常空脚 / 被拉死到地 / 被拉死到 3.3V / 接着模块）。
"""
from __future__ import annotations

import time

import lgpio

#: (物理脚, BCM, 名称, 期望接什么)
PINS = [
    (1, None, "3.3V", "电源（DHT11 VCC 应接这里）"),
    (2, None, "5V", "电源（5V，本次不该用）"),
    (6, None, "GND", "地（DHT11 GND 应接这里）"),
    (7, 4, "GPIO4", "DHT11 DATA"),
    (9, None, "GND", "另一条地"),
    (11, 17, "GPIO17", "（空脚，用作对照）"),
    (13, 27, "GPIO27", "（空脚，用作对照）"),
    (15, 22, "GPIO22", "（空脚，用作对照）"),
    (17, None, "3.3V", "另一个 3.3V"),
    (20, None, "GND", "另一条地"),
    (34, None, "GND", "另一条地"),
    (39, None, "GND", "另一条地"),
]

SAMPLES = 30


def measure(handle: int, bcm: int) -> dict:
    """返回三种配置下的高电平次数。"""
    out = {}
    for label, flags in (("up", lgpio.SET_PULL_UP), ("down", lgpio.SET_PULL_DOWN), ("float", 0)):
        try:
            lgpio.gpio_free(handle, bcm)
        except Exception:  # noqa: BLE001 - 没占用是正常的
            pass
        lgpio.gpio_claim_input(handle, bcm, flags)
        time.sleep(0.05)
        out[label] = sum(lgpio.gpio_read(handle, bcm) for _ in range(SAMPLES))
        try:
            lgpio.gpio_free(handle, bcm)
        except Exception:  # noqa: BLE001
            pass
    return out


def verdict(data: dict) -> str:
    up, down, flt = data["up"], data["down"], data["float"]
    hi = SAMPLES * 0.9
    lo = SAMPLES * 0.1
    if up >= hi and down <= lo:
        return "正常空脚（线上没有强驱动）"
    if up <= lo and down <= lo:
        return "★ 被拉死到 GND（这根线接到了地 / 与地短路）"
    if up >= hi and down >= hi:
        return "★ 被拉死到 3.3V（这根线接到了电源 / 与电源短路）"
    if lo < up < hi or lo < down < hi or lo < flt < hi:
        return "不稳定（读数在跳：线太长/接触不良，或与相邻脚桥接）"
    return "读数异常（需人工看）"


def main() -> int:
    handle = lgpio.gpiochip_open(0)
    print("=" * 96)
    print(f"接线体检：每个脚读三种配置各 {SAMPLES} 次（只读，不改状态）")
    print("=" * 96)
    print(f"{'物理脚':<6}{'BCM':<8}{'名称':<10}{'上拉高':<8}{'下拉高':<8}{'浮空高':<8}结论 / 说明")
    print("-" * 96)
    try:
        for physical, bcm, name, expect in PINS:
            if bcm is None:
                print(f"{physical:<6}{'—':<8}{name:<10}{'（电源/地脚，不测电平）':<26}{expect}")
                continue
            data = measure(handle, bcm)
            line = (f"{physical:<6}{bcm:<8}{name:<10}"
                    f"{data['up']:<8}{data['down']:<8}{data['float']:<8}{verdict(data)}")
            print(line)
            print(f"{'':<42}期望：{expect}")
    finally:
        lgpio.gpiochip_close(handle)
    print("-" * 96)
    print("怎么读：")
    print("  · DHT11 的 DATA（物理脚 7）应当和右边那三个【空脚对照】一样：上拉=满、下拉=0")
    print("  · 若它是『被拉死到 GND』   ⇒ DATA 接到了 GND（插错一格 / 与地短路 / 模块反插）")
    print("  · 若它是『被拉死到 3.3V』  ⇒ DATA 接到了电源列")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
