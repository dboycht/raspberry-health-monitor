"""命令行入口。

用法（在 ``rpi/`` 目录下）::

    python -m health_monitor selfcheck --mock           # 全体驱动体检（不接硬件）
    python -m health_monitor selfcheck --real           # 体检真实硬件（树莓派上跑）
    python -m health_monitor serve --mock               # 模拟模式起服务（PC 上演示用）
    python -m health_monitor serve --real --port 8080   # 树莓派上正式运行
    python -m health_monitor demo                       # 一键跑完整个报警链路演示
    python -m health_monitor status --mock              # 打印设备/配置状态

⚠️ 默认是 **mock 模式**：不带 ``--real`` 时绝不访问真实 I2C/GPIO，
这样在 PC 上也能安全地跑通全部代码路径。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from typing import Any, Dict, List, Optional

from .core.config import ConfigError, load_config
from .hal.registry import DRIVER_DOCS, MANIFEST, get_spec
from .service import Runtime, build_runtime


def _setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


# --------------------------------------------------------------------------
# selfcheck
# --------------------------------------------------------------------------


def cmd_selfcheck(args: argparse.Namespace) -> int:
    """体检：逐个**已启用的设备** 装配 → open → 自检，输出表格化的报告。

    ⚠️ 必须按**配置里的设备名**（`vitals` / `ambient` / `display`…）遍历，
    不能按 `MANIFEST` 里的**驱动名**（`max30102` / `dht11` / `lcd1602`…）——
    两者在本项目里**名字不同**（配置用"器官名"、MANIFEST 用"型号名"），
    按驱动名查 `device_report()` 永远查不到 ⇒ 全部显示 `skip 未在配置中启用`，
    真机体检等于什么都没测，却打印"装配失败/自检失败：0 项"
    （2026-09-25 真机实测踩到：DHT11 明明接着，体检报告却把它标成 skip）。
    """
    mock = not args.real
    runtime = build_runtime(args.config, mock=mock, store_path="", dispatcher_enabled=False)
    errors = runtime.open()
    report = runtime.device_report()

    #: 配置里每个设备的驱动名（用于诊断"这类驱动根本没配置"的情况）
    configured_drivers = {cfg.name: cfg.driver for cfg in runtime.config.enabled_devices()}
    #: 已经用掉的驱动名（剩下的说明"实现了但没启用/没接线"）
    used_drivers = set(configured_drivers.values())

    width = max([len(name) for name in report] + [12])
    print("=" * 78)
    print(f"设备体检报告（mock={mock}）  {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 78)
    print(f"{'设备名':<{width}}  {'驱动':<12}  {'装配':<6}  {'自检':<6}  说明")
    print("-" * 78)
    failures = 0

    # ① 先列**配置里启用的设备**（这才是真正要验的东西）
    for name in sorted(configured_drivers):
        driver = configured_drivers[name]
        if name in report:
            entry = report[name]
            check = entry.get("self_check", {})
            ok = "OK" if check.get("ok") else "FAIL"
            if not check.get("ok"):
                failures += 1
            assemble = "OK" if name not in errors else "FAIL"
            if name in errors:
                failures += 1
            detail = str(errors.get(name) or check.get("detail") or "")[:34]
        else:
            # 装配就失败了（构造期异常）：如实算失败，不吞
            assemble, ok = "FAIL", "-"
            detail = str(runtime.assembly_errors.get(name, "未装配（原因未记录）"))[:34]
            failures += 1
        print(f"{name:<{width}}  {driver:<12}  {assemble:<6}  {ok:<6}  {detail}")

    # ② 再列**实现了但这次没配置/没启用**的驱动（信息行，不算失败）
    for driver in sorted(set(MANIFEST) - used_drivers):
        spec = get_spec(driver)
        print(f"{driver:<{width}}  {spec.kind.value:<12}  {'skip':<6}  {'-':<6}  未在配置中启用")

    print("-" * 78)
    print(f"装配失败/自检失败：{failures} 项")
    print("提示：未接硬件的器件在 mock 模式下失败 = 驱动实现有问题；")
    print("      真实模式下失败 = 接线/地址/权限问题（报告里已写出排查线索）。")
    runtime.close()
    if not mock and failures:
        return 1
    return 0


# --------------------------------------------------------------------------
# serve
# --------------------------------------------------------------------------


def cmd_serve(args: argparse.Namespace) -> int:
    """启动完整服务（采集 + 报警 + HTTP API），前台运行，Ctrl+C 退出。"""
    mock = not args.real
    runtime = build_runtime(args.config, mock=mock, store_path=args.store, dispatcher_enabled=not args.quiet)
    errors = runtime.open()
    if errors:
        print(f"⚠️  {len(errors)} 个设备打开失败（服务继续运行，但相关功能不可用）：")
        for name, err in errors.items():
            print(f"   - {name}: {err}")
    try:
        runtime.start_http(host=args.host, port=args.port, token=args.token)
    except OSError as exc:
        print(f"❌ 端口 {args.port} 无法绑定：{exc}")
        print("   可能已有实例在运行；换端口用 --port，或先关掉旧进程（按端口找 PID，别杀错 node 进程）。")
        runtime.close()
        return 2

    print("=" * 78)
    print(f"树莓派健康监护服务已启动（版本 {runtime.version}，mock={mock}）")
    print(f"  接口地址：http://<树莓派IP>:{args.port}/api/v1/current")
    print(f"  健康检查：http://127.0.0.1:{args.port}/api/v1/health")
    print(f"  设备数量：{len(runtime.devices)}（输入 {len(runtime.inputs)} / 输出 {len(runtime.outputs)}）")
    if runtime.assembly_errors:
        print(f"  装配警告：{runtime.assembly_errors}")
    print("  模式说明：" + ("模拟数据（无硬件）" if mock else "真实硬件采集"))
    print("  按 Ctrl+C 退出")
    print("=" * 78)
    runtime.start_background()
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\n正在停止服务…")
    finally:
        runtime.close()
    print("已退出。")
    return 0


# --------------------------------------------------------------------------
# demo
# --------------------------------------------------------------------------


def cmd_demo(args: argparse.Namespace) -> int:
    """一键演示：模拟数据走完"采集 → 判断 → 报警 → 恢复"全链路。"""
    from .demo import run_demo

    return run_demo(with_http=args.http, port=args.port)


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    """打印当前配置与设备状态（不启动服务）。"""
    mock = not args.real
    runtime = build_runtime(args.config, mock=mock, store_path="", dispatcher_enabled=False)
    runtime.open()
    info = runtime.status()
    print(json.dumps(info, ensure_ascii=False, indent=2))
    runtime.close()
    return 0


# --------------------------------------------------------------------------
# drivers（给同学看的"任务清单"）
# --------------------------------------------------------------------------


def cmd_drivers(args: argparse.Namespace) -> int:
    """列出所有驱动、负责人、状态（团队分工用）。"""
    print(f"{'驱动名':<12} {'大类':<10} {'中文名':<28} {'题包':<6} 负责人")
    print("-" * 78)
    for name in sorted(MANIFEST):
        spec = get_spec(name)
        doc = DRIVER_DOCS.get(name, {})
        owner = doc.get("owner") or "（待认领）"
        print(f"{name:<12} {spec.kind.value:<10} {spec.label:<28} {spec.option:<6} {owner}")
    print("-" * 78)
    print("说明：负责人一栏在 health_monitor/hal/registry.py 的 DRIVER_DOCS 里填写。")
    return 0


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="health_monitor",
        description="树莓派居家老人健康监护系统（课程设计）",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="打印调试日志")
    parser.add_argument("--config", default=None, help="配置文件路径（默认 config/devices.json）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("selfcheck", help="设备体检（装配 + 自检）")
    p_check.add_argument("--real", action="store_true", help="访问真实硬件（树莓派上才用）")
    p_check.set_defaults(func=cmd_selfcheck)

    p_serve = sub.add_parser("serve", help="启动完整服务（含 HTTP API）")
    p_serve.add_argument("--real", action="store_true", help="访问真实硬件")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8080)
    p_serve.add_argument("--token", default="", help="可选：设置后请求需带 X-Auth-Token 头")
    p_serve.add_argument("--store", default="data/history.db", help="历史库路径；传空字符串表示不落库")
    p_serve.add_argument("--quiet", action="store_true", help="不发声/不显示（只采集与上报）")
    p_serve.set_defaults(func=cmd_serve)

    p_demo = sub.add_parser("demo", help="一键跑通全链路演示（模拟数据）")
    p_demo.add_argument("--http", action="store_true", help="演示时同时起 HTTP 服务")
    p_demo.add_argument("--port", type=int, default=8080)
    p_demo.set_defaults(func=cmd_demo)

    p_status = sub.add_parser("status", help="打印状态")
    p_status.add_argument("--real", action="store_true")
    p_status.set_defaults(func=cmd_status)

    p_drivers = sub.add_parser("drivers", help="列出驱动与负责人（分工用）")
    p_drivers.set_defaults(func=cmd_drivers)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"❌ 配置错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
