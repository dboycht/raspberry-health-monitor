"""诊断"只解出 39/40 bit"：数边沿、看缺的是哪一位、两种排布各多试几次。

背景（2026-09-25 真机）：换模块后 DHT11 **已经应答**（不再是"0 个边沿"），
但报 `数据位不足：只解出 39/40 个 bit` —— 差 1 位。

一帧 DHT11 的理论边沿数 = 起始低 + 应答低 + 应答高 + 40 位 ×（低+高）+ 收尾 = **83**
（本项目的 `_read_lgpio_raw` 里就是这么判的）。常见成因：
    · 上拉偏弱 / 线太长 ⇒ 最后一个边沿（收尾下降沿）来晚了或没被采到；
    · 采样的 40ms 窗口不够 ⇒ 干脆提早取消回调（本项目该窗口是 40ms，DHT11 约 4ms，够）；
    · 模块供电不足（例如还在从数据线偷电）⇒ 时序拉长。

本脚本：对"两种可能排布"各连读 5 次，打印**每次的边沿数**与**解出的位数/结果**。
判据：
    · 边沿数稳定在 83 附近、只是偶尔 39 位 ⇒ 上拉/线材问题（加 4.7kΩ~10kΩ 上拉、换短线）
    · 边沿数明显少于 83 ⇒ 模块没被正确供电（回到"针脚顺序"那条线排查）
"""
from __future__ import annotations

import contextlib
import sys
import time

import lgpio

sys.path.insert(0, ".")

from health_monitor.sensors.dht11 import Dht11  # noqa: E402

P7, P11, P13 = 4, 17, 27


def free(handle: int, bcm: int) -> None:
    with contextlib.suppress(Exception):
        lgpio.gpio_free(handle, bcm)


def count_edges(handle: int, bcm: int) -> tuple[int, int]:
    """手动跑一次单总线时序，返回 (边沿数, 高电平脉冲数)。**只为诊断，不改驱动。**"""
    edges: list[tuple[int, int]] = []
    cb = None
    try:
        free(handle, bcm)
        lgpio.gpio_claim_output(handle, bcm, 0)
        time.sleep(0.020)                      # 起始信号（≥18ms）
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
    highs = [ts for level, ts in edges if level == 1]
    return len(edges), len(highs)


def try_read(data_bcm: int, times: int = 5) -> list[str]:
    out: list[str] = []
    for _ in range(times):
        dev = Dht11(pin=data_bcm, retries=1, min_interval_s=2.0)
        try:
            dev.open()
            temp, humid = dev._read_lgpio_raw()
            out.append(f"OK 温度 {temp}℃ / 湿度 {humid}%")
        except Exception as exc:  # noqa: BLE001
            out.append(f"{type(exc).__name__}: {str(exc)[:70]}")
        finally:
            with contextlib.suppress(Exception):
                dev.close()
        time.sleep(2.1)                        # DHT11 硬件要求两次读取间隔 ≥2s
    return out


def main() -> int:
    handle = lgpio.gpiochip_open(0)
    print("=" * 92)
    print("DHT11「只解出 39/40 位」诊断")
    print("=" * 92)
    try:
        print("\n【1】纯数边沿（不给模块供电，只看它现在会回几个边沿）")
        edges, highs = count_edges(handle, P7)
        print(f"  脚 7 当 DATA：边沿 {edges} 个（高电平段 {highs} 个）")
        print("  理论值：一帧 83 个边沿（起始 1 + 应答 2 + 40×2 + 收尾 1）")
        if edges == 0:
            print("  ⇒ 0 个边沿 = 传感器完全没应答（供电/接线还没对）")
        elif edges < 70:
            print("  ⇒ 边沿偏少：供电不足或线材问题")
        else:
            print("  ⇒ 边沿数接近满帧：模块在正常工作，问题多半在**上拉/线长**造成的最后一位丢失")

        for label, plus_bcm, gnd_bcm in (
            ("原样（脚13 当 +、脚11 当 -）", P13, None),
            ("换位（脚11 当 +、脚13 当 -）", P11, None),
        ):
            print(f"\n【2】{label}：连读 5 次（每次间隔 2.1 秒）")
            for bcm in (P7, P11, P13):
                free(handle, bcm)
            lgpio.gpio_claim_output(handle, plus_bcm, 1)
            time.sleep(0.4)
            for i, result in enumerate(try_read(P7), start=1):
                print(f"  第 {i} 次：{result}")
            free(handle, plus_bcm)
    finally:
        for bcm in (P7, P11, P13):
            free(handle, bcm)
        lgpio.gpiochip_close(handle)

    print("""
怎么读这些结果
--------------
· 有 OK ⇒ 直接去跑 `python3 -m health_monitor selfcheck --real` 看 DHT11 那行；
· 全是「39/40 位」 ⇒ **加一个 4.7kΩ~10kΩ 上拉电阻**（DATA ↔ 3.3V），并换短杜邦线；
· 全是「0 个边沿」 ⇒ 供电/针脚顺序还没对（先量 模块 + ↔ - 是否 3.3V）。
""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
