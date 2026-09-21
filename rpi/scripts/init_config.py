#!/usr/bin/env python3
"""初始化本机配置：把 ``config/devices.example.json`` 复制为 ``config/devices.json``。

为什么需要它：``devices.json`` 是**每台机器自己的引脚配置**（可能被同学改过），
所以它按惯例**不入库**（见 .gitignore），仓库里只放 ``devices.example.json``。
第一次拉代码的人跑一次本脚本即可拿到可运行的配置。

用法::

    python scripts/init_config.py            # 若已存在则不覆盖
    python scripts/init_config.py --force    # 覆盖（会先备份成 devices.json.bak）
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

RPI_DIR = Path(__file__).resolve().parents[1]
CONFIG_DIR = RPI_DIR / "config"
EXAMPLE = CONFIG_DIR / "devices.example.json"
TARGET = CONFIG_DIR / "devices.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="初始化 rpi/config/devices.json")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的配置（先备份）")
    args = parser.parse_args()

    if not EXAMPLE.exists():
        print(f"ERROR: 找不到示例配置 {EXAMPLE}")
        return 1

    if TARGET.exists() and not args.force:
        print(f"配置已存在，未改动：{TARGET}")
        print("如需覆盖请加 --force（会先备份成 devices.json.bak）")
        return 0

    if TARGET.exists():
        backup = TARGET.with_suffix(".json.bak")
        shutil.copy2(TARGET, backup)
        print(f"已备份旧配置 -> {backup}")

    shutil.copy2(EXAMPLE, TARGET)
    print(f"已生成配置：{TARGET}")
    print()
    print("下一步：")
    print("  1) 按你的实际接线修改该文件里的引脚/地址（params 段）")
    print("  2) 跑体检：python -m health_monitor selfcheck --mock")
    print("  3) 看冲突：python scripts/validate.py --quick")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
