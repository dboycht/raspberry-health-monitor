"""决定性验证：把三根线的角色在**软件里**摆成"正确接法"，再读一次 DHT11。

现场事实（2026-09-25 真机，换模块后实测）
------------------------------------------
* 物理脚 7（GPIO4）：内部上拉 20/20、内部下拉 20/20、浮空 20/20（三态全高）
* 把脚 7 驱动到 0 ⇒ 能拉低（0/20）；驱动到 1 后释放 + 内部下拉 ⇒ 自己弹回 20/20
  ⇒ **线上有一个上拉电阻在往 3.3V 顶**（下拉拉不住），这正是 DHT11 模块
     "DATA ↔ VCC 上拉"的样子 ⇒ 数据线确实挂在脚 7 上，模块也接到了脚 7。
* 之前读到过"83 个边沿 / 39 位" ⇒ 模块**能工作**，只是供电方式不对。

推论：**模块的 `+` 接在了 GPIO4（脚 7）上** —— 它靠数据脚偷电（所以数据线被顶高、
供电不稳、帧解不全）。

验证办法（不动硬件）
--------------------
用户在脚 7 / 11 / 13 上各接了一根线，其中两根是模块的 `+` 与 `-`。逐个假设：

    假设 A：脚 11 = +（3.3V，我们输出 1）、脚 13 = -（GND，我们输出 0），读脚 7
    假设 B：脚 13 = +、脚 11 = -，读脚 7

哪一个能读出温湿度，就说明模块的 + 在那一根上 —— 然后按结论改线即可。
"""
from __future__ import annotations

import contextlib
import sys
import time

import lgpio

sys.path.insert(0, ".")

from health_monitor.sensors.dht11 import Dht11  # noqa: E402

P7, P11, P13 = 4, 17, 27
ALL = (P7, P11, P13)


def free(handle: int, bcm: int) -> None:
    with contextlib.suppress(Exception):
        lgpio.gpio_free(handle, bcm)


def read_once(data_bcm: int) -> tuple[bool, str]:
    dev = Dht11(pin=data_bcm, retries=1, min_interval_s=2.0)
    try:
        dev.open()
        temp, humid = dev._read_lgpio_raw()
        return True, f"温度 {temp}℃ / 湿度 {humid}%"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {str(exc)[:80]}"
    finally:
        with contextlib.suppress(Exception):
            dev.close()


def main() -> int:
    handle = lgpio.gpiochip_open(0)
    print("=" * 90)
    print("软件里摆成「正确接法」，验证模块的 + 到底接在哪根线上")
    print("=" * 90)
    hits = []
    try:
        for label, plus_bcm, gnd_bcm in (
            ("假设 A：脚 11 = +、脚 13 = -", P11, P13),
            ("假设 B：脚 13 = +、脚 11 = -", P13, P11),
        ):
            for bcm in ALL:
                free(handle, bcm)
            lgpio.gpio_claim_output(handle, plus_bcm, 1)
            lgpio.gpio_write(handle, plus_bcm, 1)      # 给模块供电
            lgpio.gpio_claim_output(handle, gnd_bcm, 0)
            lgpio.gpio_write(handle, gnd_bcm, 0)       # 给模块接地
            time.sleep(0.5)                            # 上电稳定
            print(f"\n{label}，读脚 7：")
            for i in range(3):
                ok, note = read_once(P7)
                print(f"  第 {i + 1} 次：{'★★★ ' + note if ok else note}")
                if ok:
                    hits.append((label, note))
                time.sleep(2.1)
            for bcm in ALL:
                free(handle, bcm)
    finally:
        for bcm in ALL:
            free(handle, bcm)
        lgpio.gpiochip_close(handle)

    print("\n" + "=" * 90)
    if hits:
        print("结论：模块是好的！正确接法如下（断电后照这个改）：")
        for label, note in hits[:1]:
            print(f"  实测读数：{note}（{label}）")
        print("""
      模块 +（中间那针）  →  物理脚 1（3.3V）
      模块 -（丝印 -）    →  物理脚 6（GND）
      模块 S（丝印 S）    →  物理脚 7（GPIO4）

  也就是说：**现在接在脚 11 / 13 上的那两根里，有一根是 +、一根是 -**，
  只要把 + 挪到脚 1、- 挪到脚 6，把留在脚 7 的那根当作 S 就行。
""")
    else:
        print("两种假设都没读出数。请把三根线从**模块侧**拔下来，逐根量通断（断线很常见），")
        print("并确认模块 + ↔ - 之间有 3.3V（红表笔在 +）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
