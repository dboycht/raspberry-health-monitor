#!/usr/bin/env python3
"""项目验证脚本：把"体检 + 单测 + 演示 + 配置一致性"一次跑完。

用途：**每次提交前跑一遍**，或每周合练时确认大家的改动没有互相破坏。

用法（在 ``rpi/`` 目录下）::

    python scripts/validate.py              # 全部检查
    python scripts/validate.py --quick      # 跳过单测（只做体检/配置/演示）
    python scripts/validate.py --real       # 额外提示哪些检查需要真机

退出码：0 = 全部通过；1 = 有检查失败（打印失败项）。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, Tuple

RPI_DIR = Path(__file__).resolve().parents[1]
if str(RPI_DIR) not in sys.path:
    sys.path.insert(0, str(RPI_DIR))


class Check:
    """一项检查。"""

    def __init__(self, name: str, func: Callable[[], Tuple[bool, str]]) -> None:
        self.name = name
        self.func = func


def check_python_version() -> Tuple[bool, str]:
    ok = sys.version_info >= (3, 9)
    return ok, f"Python {sys.version.split()[0]}（要求 >= 3.9）"


def check_imports() -> Tuple[bool, str]:
    """核心模块必须能导入（避免语法错误/循环导入）。"""
    try:
        import health_monitor  # noqa: F401
        from health_monitor.service import Runtime  # noqa: F401
        from health_monitor.net.web import WebApi  # noqa: F401

        return True, f"health_monitor {health_monitor.__version__} 导入正常"
    except Exception as exc:  # noqa: BLE001
        return False, f"导入失败：{type(exc).__name__}: {exc}"


def check_config() -> Tuple[bool, str]:
    """配置文件必须存在且校验通过。"""
    try:
        from health_monitor.core.config import load_config

        cfg = load_config(None)
        enabled = cfg.enabled_devices()
        return True, f"配置有效：{len(enabled)} 个设备启用（{', '.join(d.name for d in enabled)}）"
    except Exception as exc:  # noqa: BLE001
        return False, f"配置错误：{exc}"


def check_drivers() -> Tuple[bool, str]:
    """注册表里每个驱动都要能加载并构造（缺实现会在这里暴露）。"""
    from health_monitor.hal import MANIFEST, create_device

    failed: List[str] = []
    for name in sorted(MANIFEST):
        try:
            create_device(name, mock=True)
        except Exception as exc:  # noqa: BLE001
            failed.append(f"{name}（{type(exc).__name__}: {exc}）")
    if failed:
        return False, f"{len(failed)} 个驱动无法构造：\n      " + "\n      ".join(failed)
    return True, f"{len(MANIFEST)} 个驱动全部可构造"


def check_pin_conflicts() -> Tuple[bool, str]:
    """★ 配置里的独占 GPIO 不能撞脚（真实发生过：PIR 与按钮抢 GPIO17）。"""
    from health_monitor.core.config import DEFAULT_CONFIG
    from health_monitor.hal import find_conflicts

    exclusive = {
        "dht11": ("pin",), "hc_sr501": ("pin",), "hc_sr04": ("trig_pin", "echo_pin"),
        "buzzer": ("pin",), "button": ("pin",),
    }
    claims: List[Tuple[str, int]] = []
    for dev_name, item in DEFAULT_CONFIG["devices"].items():
        if not item.get("enabled", True):
            continue
        params = item.get("params", {})
        for key in exclusive.get(item["driver"], ()):
            if key in params:
                claims.append((f"{dev_name}.{key}", int(params[key])))
        if item["driver"] == "led":
            for color, pin in (params.get("pins") or {}).items():
                claims.append((f"{dev_name}.led.{color}", int(pin)))
    conflicts = find_conflicts(claims)
    if conflicts:
        return False, "存在 GPIO 撞脚：\n      " + "\n      ".join(conflicts)
    return True, f"{len(claims)} 个独占引脚无冲突"


def check_docs() -> Tuple[bool, str]:
    """文档一致性：悬空引用 / docs 索引与实体不等 / 报警码三方不一致。

    这类问题**人眼查不出**（"少一条索引""链接指向已改名的文件"），
    但会让新同学按文档找不到东西，所以放进提交前检查。

    ⚠️ 先跑**注入自测**再跑全量检查：检查器自己"静默失效"过一次
    （正则只允许 ASCII，而本项目文档名全是中文 ⇒ 一个都匹配不到，却一直报通过）。
    自测会造一条假悬空引用、断言它真的被抓到，失败时归因清晰。
    """
    selftest = subprocess.run(
        [sys.executable, str(RPI_DIR / "scripts" / "check_docs.py"), "--self-test"],
        cwd=str(RPI_DIR), capture_output=True, text=True, encoding="utf-8",
    )
    if selftest.returncode != 0:
        detail = [ln for ln in (selftest.stdout or "").splitlines() if ln.strip().startswith("❌")]
        return False, "文档检查器自测失败（守卫可能已失效）：\n      " + "\n      ".join(detail[:5])

    proc = subprocess.run(
        [sys.executable, str(RPI_DIR / "scripts" / "check_docs.py")],
        cwd=str(RPI_DIR), capture_output=True, text=True, encoding="utf-8",
    )
    if proc.returncode == 0:
        lines = [ln for ln in (proc.stdout or "").splitlines() if ln.startswith("✅")]
        return True, (lines[0] if lines else "文档自检通过") + "（含检查器注入自测）"
    detail = [ln for ln in (proc.stdout or "").splitlines() if ln.strip().startswith("-")]
    return False, "文档自检失败：\n      " + "\n      ".join(detail[:10])


def check_selfcheck() -> Tuple[bool, str]:
    """全设备模拟体检（assemble → open → self_check）。"""
    from health_monitor.hal import snapshot

    report = snapshot(mock=True)
    bad = [
        f"{name}（{entry.get('error') or entry.get('self_check', {}).get('detail')}）"
        for name, entry in report["devices"].items()
        if not entry.get("loaded") or not entry.get("self_check", {}).get("ok")
    ]
    if bad:
        return False, f"{len(bad)} 个设备自检失败：\n      " + "\n      ".join(bad)
    return True, f"{len(report['devices'])} 个设备自检全部通过（模拟模式）"


def check_tests() -> Tuple[bool, str]:
    """跑单元测试（用 pytest；没有 pytest 就退回 unittest）。

    ⚠️ 统计数字**从日志文件里 grep**，而不是取 stdout 的最后一行。
    为什么（2026-09-22 在树莓派上实测踩到）：本项目很多测试会往 stdout 打中文日志
    （LED/语音/报警），**unittest 的 `Ran N tests / OK` 汇总行会被这些日志挤到中间**，
    取"最后一行"就变成打印了 "[语音] 播报：监护系统已启动"——看起来像没跑测试。
    判据应当来自**报告本身**（`Ran N tests` / `OK` / `FAILED`），而不是最后一行。
    """
    has_pytest = subprocess.run(
        [sys.executable, "-c", "import pytest"], capture_output=True
    ).returncode == 0
    if has_pytest:
        cmd = [sys.executable, "-m", "pytest", "tests", "-q"]
    else:
        cmd = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]
    proc = subprocess.run(cmd, cwd=str(RPI_DIR), capture_output=True, text=True, encoding="utf-8")

    combined = "\n".join(part or "" for part in (proc.stdout, proc.stderr))
    lines = combined.splitlines()

    def pick(pattern: str) -> str:
        import re

        for line in reversed(lines):
            if re.search(pattern, line):
                return line.strip()
        return ""

    summary = pick(r"\bRan \d+ tests?\b") or pick(r"\d+ passed")
    verdict = pick(r"^OK\b") or pick(r"^FAILED\b") or pick(r"\d+ failed")

    if proc.returncode == 0:
        detail = "；".join(part for part in (summary, verdict) if part) or "全部通过"
        return True, f"测试通过：{detail}"
    tail = "\n      ".join(lines[-12:])
    return False, f"测试失败（{summary or '无统计'}）：\n      {tail}"


def check_demo() -> Tuple[bool, str]:
    """跑一遍演示剧本（验证整机链路：采集 → 判定 → 下发）。"""
    # 演示里会 print 大量内容，这里只关心退出码
    proc = subprocess.run(
        [sys.executable, "-m", "health_monitor", "demo"],
        cwd=str(RPI_DIR), capture_output=True, text=True, encoding="utf-8",
    )
    if proc.returncode == 0:
        lines = [ln for ln in (proc.stdout or "").splitlines() if ln.startswith("演示结束")]
        return True, lines[0] if lines else "演示脚本运行成功"
    return False, f"演示失败（退出码 {proc.returncode}）：\n      " + "\n      ".join((proc.stderr or "").strip().splitlines()[-8:])


def main() -> int:
    parser = argparse.ArgumentParser(description="树莓派健康监护项目验证脚本")
    parser.add_argument("--quick", action="store_true", help="跳过单测与演示（只做静态检查）")
    parser.add_argument("--real", action="store_true", help="提示哪些检查需要真机执行")
    args = parser.parse_args()

    checks: List[Check] = [
        Check("Python 版本", check_python_version),
        Check("核心模块导入", check_imports),
        Check("配置文件", check_config),
        Check("驱动注册表", check_drivers),
        Check("引脚冲突", check_pin_conflicts),
        Check("设备自检（模拟）", check_selfcheck),
        Check("文档一致性", check_docs),
    ]
    if not args.quick:
        checks.append(Check("单元测试", check_tests))
        checks.append(Check("端到端演示", check_demo))

    print("=" * 78)
    print("树莓派健康监护项目 · 验证报告")
    print("=" * 78)
    failures = 0
    for check in checks:
        try:
            ok, detail = check.func()
        except Exception as exc:  # noqa: BLE001 - 检查本身出错也算失败
            ok, detail = False, f"检查本身异常：{type(exc).__name__}: {exc}"
        mark = "PASS" if ok else "FAIL"
        if not ok:
            failures += 1
        print(f"[{mark}] {check.name}：{detail}")
    print("-" * 78)
    if failures:
        print(f"结果：{failures} 项失败（共 {len(checks)} 项）—— 请先修掉再提交")
    else:
        print(f"结果：全部 {len(checks)} 项通过 ✅")
    print()
    print("需要真机执行的检查（本脚本无法代劳）：")
    print("  python -m health_monitor selfcheck --real     # 真实硬件体检（需要树莓派 + 接线）")
    print("  i2cdetect -y 1                                # 应看到 0x57（MAX30102）与 0x27（LCD）")
    print("  ls -l /dev/spidev0.*                          # 存在说明 SPI 已开")
    print("  python -m health_monitor serve --real         # 正式运行，手机端联调")
    if args.real:
        print()
        print("按 --real 提示：以上命令请在树莓派上执行；PC 上只能验证 mock 路径。")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
