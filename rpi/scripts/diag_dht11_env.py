#!/usr/bin/env python3
"""诊断树莓派上的 DHT11 读取链路：gpiozero 到底有没有 DHT11 支持。

背景（2026-09-24 真机发现）
--------------------------
本项目 `sensors/dht11.py` 原先走 `from gpiozero import DHT11`，
但树莓派（Debian 13 / gpiozero 2.0.1）报：

    cannot import name 'DHT11' from 'gpiozero'

而 DHT11 是**课程任务 H（温湿度测量）的主角**，必须能读。
本脚本把"这台机器上到底有什么可用"查清楚，再决定修法：
  ① 官方 apt 包是否含 DHT11（可能被拆到单独包）；
  ② gpiozero 有哪些可用的输入类；
  ③ 系统里是否存在其他 DHT 实现（Adafruit_DHT / dht11 等）；
  ④ lgpio 是否就绪（备选方案：自己按单总线时序读）。

用法（在树莓派上）::

    python3 scripts/diag_dht11_env.py
"""

from __future__ import annotations

import os
import subprocess
import sys

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


def section(title: str) -> None:
    print("\n" + "=" * 74)
    print(title)
    print("=" * 74)


def run(cmd: list[str]) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=20,
                           encoding="utf-8", errors="replace")
        return ((p.stdout or "") + (p.stderr or "")).strip()
    except Exception as exc:  # noqa: BLE001
        return f"<执行失败：{type(exc).__name__}: {exc}>"


def main() -> int:
    section("① gpiozero：版本 / 是否有 DHT11 / 有哪些类")
    try:
        import gpiozero
        print(f"  版本：{getattr(gpiozero, '__version__', '未知')}")
        print(f"  路径：{os.path.dirname(gpiozero.__file__)}")
        names = sorted(n for n in dir(gpiozero) if n[:1].isupper())
        print(f"  是否有 DHT11：{hasattr(gpiozero, 'DHT11')}")
        print(f"  是否有 DHT22：{hasattr(gpiozero, 'DHT22')}")
        print(f"  导出类（{len(names)} 个）：{', '.join(names)}")
    except ImportError as exc:
        safe_print(f"  ❌ gpiozero 不可用：{exc}")

    section("② gpiozero 包里与 DHT 相关的文件 / 子模块")
    try:
        import gpiozero
        base = os.path.dirname(gpiozero.__file__)
        hits = [f for f in os.listdir(base) if "dht" in f.lower()]
        print(f"  顶层含 dht 的文件：{hits or '（无）'}")
        for sub in ("input_devices", "sensors"):
            path = os.path.join(base, sub)
            if os.path.isdir(path):
                files = [f for f in os.listdir(path) if "dht" in f.lower()]
                print(f"  {sub}/ 含 dht 的文件：{files or '（无）'}")
        # 直接找类定义（有时存在但没导出到 __init__）
        grep = run(["grep", "-rl", "class DHT11", base])
        print(f"  源码里定义 class DHT11 的文件：{grep or '（没找到）'}")
    except Exception as exc:  # noqa: BLE001
        print(f"  检查失败：{type(exc).__name__}: {exc}")

    section("③ 系统里有别的 DHT 实现吗")
    for mod in ("Adafruit_DHT", "dht11", "adafruit_dht", "pigpio"):
        try:
            __import__(mod)
            safe_print(f"  ✅ {mod} 可用")
        except ImportError as exc:
            safe_print(f"  ❌ {mod} 不可用（{exc}）")

    section("④ lgpio / 内核接口（自实现单总线时序的前提）")
    try:
        import lgpio
        safe_print("  ✅ lgpio 可用（可用它按微秒级时序直接读 DHT11）")
        print(f"     版本：{getattr(lgpio, '__version__', '未知')}")
    except ImportError as exc:
        safe_print(f"  ❌ lgpio 不可用：{exc}")
    for dev in ("/dev/gpiomem", "/dev/gpiochip0", "/dev/gpiochip4"):
        safe_print(f"  {'✅' if os.path.exists(dev) else '❌'} {dev}")

    section("⑤ apt 包信息（看是不是被拆包 / 有没有 python3-dht 之类）")
    print(run(["dpkg", "-l"]) .split("\n")[0] if False else "")
    listing = run(["bash", "-lc", "dpkg -l | grep -iE 'gpiozero|gpiod|dht|lgpio' || true"])
    print("  " + "\n  ".join(listing.splitlines()) if listing else "  （无匹配包）")
    apt = run(["bash", "-lc", "apt-cache search dht 2>/dev/null | head -8 || true"])
    print("  apt 搜索 dht 的结果：")
    print("  " + ("\n  ".join(apt.splitlines()) if apt else "（无）"))

    section("结论提示")
    print("  · 若 ① 显示没有 DHT11：说明该版本 gpiozero 已不含 DHT 支持，")
    print("    需要 ① 换 API（新的 gpiozero 可能改名）或 ② 自己按单总线时序实现；")
    print("  · 若 ④ 的 lgpio 可用：可自实现（本项目已有 lgpio 的 GPIO 抽象可复用）；")
    print("  · 无论如何，DHT11 是课程任务 H 的主角，**必须能读**，不能只靠模拟数据。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
