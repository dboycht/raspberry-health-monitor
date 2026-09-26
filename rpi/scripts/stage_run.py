#!/usr/bin/env python3
"""**分级验收用的临时服务**：只开本级器件、起一个后台服务，验完一键还原配置。

为什么要它（2026-09-26 起，配合 `docs/14-分步实施路线图.md`）
------------------------------------------------------------
阶梯式推进的口径是"**一次只加一个器件**"：接 T2（蜂鸣器 + LED）时，
`vitals`（MAX30102）/`body_temp`（TMP36）/`motion`（PIR）根本还没接 ——
如果直接 `serve --real`，启动时会满屏"打不开器件"的噪音，
真正的信息（本级器件好不好）被淹掉。

本脚本做三件事：
1. **备份**板子上的 `config/devices.json`（只在第一次备份，用 `*.stage-bak`）；
2. 把**除了 `--only` 点名的器件之外**全部临时置 `enabled=false`，起一个后台服务
   （日志落盘、PID 与端口落盘）；
3. `--stop` 时停服务并**把配置原样还原**（配置是板子本机的、不入库，必须还原）。

⚠️ 纪律
-------
* 只改板子的 `config/devices.json`（本机文件），**不碰** `devices.local.json`；
* **判活一律用健康探针**（`GET /api/v1/health`），**绝不用 `os.kill(pid, 0)`** ——
  见下条血泪；
* 停服务前**必须先验活**：PID 会被系统回收复用，"PID 还活着"不能证明那是我们的服务。

🔴 **本项目最贵的一次"自作聪明"（2026-09-26，两次把开发用的 Harness 搞崩）**
Windows 上 `os.kill(pid, 0)` **不是探活，而是杀进程**：CPython 对非
`CTRL_C_EVENT`/`CTRL_BREAK_EVENT` 的信号一律执行 ``TerminateProcess(handle, sig)``，
于是"探活"会把目标进程真的杀掉（退出码 0）。本脚本第一版写了
``pid_alive() → os.kill(pid, 0)``，而单测里有一句 ``pid_alive(os.getpid())`` ——
**pytest 进程把自己杀了**：命令零输出、Harness 看到子进程凭空消失、整轮被打断。
⇒ 判据：**Windows 上永远不要用 `os.kill` 做存活判断**；要判"服务还在不在"就**问它自己**
（健康探针）。这正是本项目早已写下的纪律（`rules/01` §8.6 / `memory/22` §7：
「验活而不是验身份」），我第一版把它违反了。

用法::

    cd ~/raspberry-health-monitor/rpi
    python3 scripts/stage_run.py --only ambient,display,alarm_buzzer,status_led
    python3 scripts/stage_run.py --status
    python3 scripts/stage_run.py --stop

退出码：0 成功；1 环境问题（已有实例在跑 / 配置缺失 / 名字写错）；2 起服务失败。
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

RPI_DIR = Path(__file__).resolve().parents[1]

# 让 `basic.console.safe_print` 可导入（打印 ✅/❌/⚠/⇒ 时在窄编码控制台上自动降级）
# ⚠️ 为什么（ERROR.md E32/E41）：中文 Windows（GBK/cp936）控制台上 `print` 直接打印
#    这些符号会抛 UnicodeEncodeError 把**整个脚本**崩掉；本脚本主要跑在树莓派（UTF-8），
#    导入失败就退回内置 print（行为与过去一致）。守卫
#    `tests/scripts/test_validate_encoding.py` 会扫描"裸 print 打印致命字符"并当场报错。
try:
    from pathlib import Path  # noqa: E402,F811
except ImportError:  # pragma: no cover
    Path = None
ROOT = Path(__file__).resolve().parents[2] if Path is not None else None
if ROOT is not None and str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    from basic.console import safe_print  # noqa: E402
except ImportError:  # pragma: no cover - 只在 basic 不可用时
    safe_print = print

CONFIG_PATH = RPI_DIR / "config" / "devices.json"
STAGES_PATH = RPI_DIR / "config" / "stages.json"
BACKUP_PATH = RPI_DIR / "config" / "devices.json.stage-bak"
STATE_PATH = RPI_DIR / "config" / ".stage-run.json"
LOG_PATH = Path("/tmp/health-monitor-stage.log")
DEFAULT_PORT = 8090


def parse_only(text: Optional[str]) -> List[str]:
    """解析 ``--only ambient,display``（去空白、去重、保持顺序）。"""
    if not text:
        return []
    seen: List[str] = []
    for piece in str(text).split(","):
        name = piece.strip()
        if name and name not in seen:
            seen.append(name)
    return seen


def load_stages(path: Optional[Path] = None) -> Dict[str, Any]:
    """读分级快照（`config/stages.json`）：**"每一级开哪些器件"的单一来源**。

    为什么是这一个文件而不是 14 个小文件：级与级之间的差别只是"多开一个器件"，
    分散成 14 份必然互相漂移（本项目刚在"板上漏同步一个目录"上踩过同类坑）。

    Raises:
        FileNotFoundError / ValueError: 文件不在或结构不对（**大声报错**，不静默放行）。
    """
    target = Path(path) if path else STAGES_PATH
    data = json.loads(target.read_text(encoding="utf-8"))
    stages = data.get("stages")
    if not isinstance(stages, dict) or not stages:
        raise ValueError(f"{target} 里没有可用的 stages 字段")
    return data


def sorted_stage_names(stages: Dict[str, Any]) -> List[str]:
    """按**数字序**排级别（T2 在 T10 前面，别按字符串排成 T1、T10、T2）。"""
    def key(name: str) -> Tuple[int, str]:
        digits = "".join(ch for ch in str(name) if ch.isdigit())
        return (int(digits) if digits else 10**6, str(name))
    return sorted(stages, key=key)


def stage_devices(data: Dict[str, Any], name: str) -> List[str]:
    """取某一级的器件列表。

    Raises:
        KeyError: 没有这一级（消息里带上可用级别，避免"什么都没开却看起来正常"）。
    """
    stages = data.get("stages") or {}
    key = str(name).strip().upper()
    if key not in stages:
        raise KeyError(f"没有 {key} 这一级；可用：{', '.join(sorted_stage_names(stages))}")
    return [str(x) for x in (stages[key].get("devices") or [])]


def stage_info(data: Dict[str, Any], name: str) -> Dict[str, Any]:
    """取某一级的完整快照（title/status/devices/note…）。"""
    stages = data.get("stages") or {}
    key = str(name).strip().upper()
    if key not in stages:
        raise KeyError(f"没有 {key} 这一级；可用：{', '.join(sorted_stage_names(stages))}")
    info = dict(stages[key])
    info["stage"] = key
    return info


def select_enabled(devices: Dict[str, Any], only: List[str]) -> Tuple[List[str], List[str], List[str]]:
    """算出"改完之后哪些 enabled=true"。

    Returns:
        ``(enabled_names, unknown, disabled_names)``：
        - `enabled_names`：本次要开的（= only 与配置的交集，保持**配置顺序**）；
        - `unknown`：`--only` 里配置中不存在的名字（**必须报错**，否则会静默什么都不开）；
        - `disabled_names`：本次被临时关掉的。
    """
    known = list(devices)
    unknown = [name for name in only if name not in known]
    enabled = [name for name in known if name in set(only)] if only else list(known)
    disabled = [name for name in known if name not in set(enabled)]
    return enabled, unknown, disabled


def pid_exists(pid: int) -> Optional[bool]:
    """这个 PID 是否活着：**只有 POSIX 才用 `os.kill(pid, 0)`**；Windows 返回 ``None``（未知）。

    ⚠️⚠️ Windows 上 `os.kill(pid, 0)` **会真的杀掉目标进程**（见模块文档的血泪条），
    所以这里刻意在 Windows 直接返回"未知"，由 :func:`service_alive` 的健康探针判定。
    """
    if pid <= 0:
        return False
    if os.name == "nt":                 # pragma: no cover - 只在 Windows 走到
        return None                     # 未知：绝不调 os.kill
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:             # pragma: no cover - 属于别的用户
        return True
    return True


def read_state() -> Dict[str, Any]:
    """读 PID/端口状态文件（没有或坏了都返回空字典，**不抛**）。"""
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_state(pid: int, port: int) -> None:
    STATE_PATH.write_text(json.dumps({"pid": pid, "port": port}), encoding="utf-8")


def clear_state() -> None:
    try:
        STATE_PATH.unlink()
    except OSError:
        pass


def api_get(port: int, path: str, timeout: float = 3.0) -> str:
    """取一段接口内容（失败返回一行说明，**不抛**）。"""
    url = f"http://127.0.0.1:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - 只连本机
            body = resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as exc:
        return f"(请求 {path} 失败：{type(exc).__name__})"
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return body[:400]
    return json.dumps(data, ensure_ascii=True)[:600]


def service_alive(port: int, timeout: float = 1.0) -> bool:
    """服务是否真的活着：**问它自己的健康接口**（不看 PID、不看端口是否被占）。"""
    return api_get(port, "/api/v1/health", timeout=timeout).startswith("{")


def apply_config(only: List[str]) -> Tuple[List[str], List[str], List[str]]:
    """备份 + 写临时配置。返回 :func:`select_enabled` 的三元组。"""
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    devices = raw.get("devices") or {}
    if not only:
        return list(devices), [], []
    if not BACKUP_PATH.exists():
        BACKUP_PATH.write_text(CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    enabled, unknown, disabled = select_enabled(devices, only)
    if unknown:
        return enabled, unknown, disabled
    for name, dev in devices.items():
        dev["enabled"] = name in set(enabled)
    CONFIG_PATH.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return enabled, unknown, disabled


def restore_config() -> bool:
    """把配置还原（有备份才还原）。返回是否真的还原了。"""
    if not BACKUP_PATH.exists():
        return False
    CONFIG_PATH.write_text(BACKUP_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    BACKUP_PATH.unlink()
    return True


def do_stop(quiet: bool = False) -> int:
    """停服务 + 还原配置（幂等）。

    ⚠️ **先验活再动手**：健康探针不通 ⇒ 认为服务已经没了，只清状态文件，
    **绝不对"PID 文件里那个号"发信号**（PID 会被系统回收，Windows 上 `os.kill` 是真杀，
    误杀别人的进程后果严重）。
    """
    state = read_state()
    pid = state.get("pid")
    port = int(state.get("port") or DEFAULT_PORT)
    alive = service_alive(port)

    if alive and isinstance(pid, int) and pid > 0:
        try:
            os.kill(pid, signal.SIGTERM)   # 已用健康探针确认"就是我们那个服务"
        except OSError as exc:  # pragma: no cover
            safe_print(f"[!] 停止 PID {pid} 失败：{exc}（可手动：kill {pid}）")
        # ⚠️ **要等它真的停下来**：SIGTERM 只是"请求退出"，服务还要跑收尾（关器件/关库）。
        #    不等就返回的话，紧接着 `--only` 起新实例会撞 `Address already in use`
        #    （2026-09-26 实测踩到：老实例还在收尾，新实例绑 8090 直接退出码 2）。
        deadline = time.time() + 10.0
        while time.time() < deadline and service_alive(port, timeout=0.5):
            time.sleep(0.3)
        if service_alive(port, timeout=0.5):
            safe_print(f"[!] 端口 {port} 仍有服务响应（可能还没退干净）—— 等几秒再启，或换 --port")
        elif not quiet:
            safe_print(f"[OK] 已停止服务（PID {pid}，端口 {port}）")
    elif alive:
        safe_print(f"[!] 端口 {port} 上有服务在跑，但状态文件里没有 PID —— 不是本脚本起的，不动它")
    elif pid:
        if not quiet:
            safe_print(f"[..] 端口 {port} 没有服务响应（PID {pid} 可能已退出或是回收后的别的进程）⇒ 只清状态")
    clear_state()

    if restore_config() and not quiet:
        safe_print("[OK] 配置已从 config/devices.json.stage-bak 还原")
    elif not quiet:
        safe_print("[..] 没有备份文件，配置保持原样")
    return 0


def do_status(port: int) -> int:
    """打印服务与接口摘要（不启动、不修改）。"""
    state = read_state()
    pid = state.get("pid")
    port = int(state.get("port") or port)
    alive = service_alive(port)
    safe_print(f"服务：{'运行中（PID %s，端口 %s）' % (pid, port) if alive else '未运行（健康探针无响应）'}")
    safe_print(f"读数：{api_get(port, '/api/v1/current')}")
    safe_print(f"报警：{api_get(port, '/api/v1/alarms?limit=3')}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="分级验收用的临时服务（只开本级器件）")
    parser.add_argument("--only", default=None, help="只开这些设备（配置里的设备名，逗号分隔）")
    parser.add_argument("--stage", default=None, metavar="Tn",
                        help="按分级快照开器件，例如 --stage T2（快照见 config/stages.json）")
    parser.add_argument("--list-stages", action="store_true", help="列出所有分级快照与状态")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"端口（默认 {DEFAULT_PORT}）")
    parser.add_argument("--wait", type=float, default=8.0, help="启动后等几秒再打摘要（默认 8）")
    parser.add_argument("--stop", action="store_true", help="停服务并还原配置")
    parser.add_argument("--status", action="store_true", help="只看状态，不启动")
    args = parser.parse_args(argv)

    if args.list_stages:
        try:
            data = load_stages()
        except (OSError, ValueError) as exc:
            safe_print(f"[X] 读不了 {STAGES_PATH}：{type(exc).__name__}: {exc}")
            return 1
        safe_print("分级快照（config/stages.json）：")
        for name in sorted_stage_names(data["stages"]):
            info = data["stages"][name]
            mark = "[已验]" if info.get("status") == "verified" else "[计划]"
            safe_print(f"  {mark} {name:<4} {info.get('title','')}")
            safe_print(f"          器件：{', '.join(info.get('devices') or []) or '（无，走 basic/）'}")
        return 0

    if args.stop:
        return do_stop()
    if args.status:
        return do_status(args.port)

    if args.only and args.stage:
        safe_print("[X] --only 与 --stage 只能给一个（--stage 是快照，--only 是临时指定）")
        return 1

    only = parse_only(args.only)
    if args.stage:
        try:
            data = load_stages()
            info = stage_info(data, args.stage)
        except (OSError, ValueError, KeyError) as exc:
            safe_print(f"[X] 取分级快照失败：{exc}")
            return 1
        only = list(info.get("devices") or [])
        status = info.get("status", "?")
        safe_print(f"[{args.stage}] {info.get('title','')}（{status}）")
        if info.get("note"):
            safe_print(f"    备注：{info['note']}")
        if not only:
            safe_print(f"[X] {args.stage} 没有器件集合（T0 走 basic/，不在 rpi 的配置开关里）")
            return 1
        if status != "verified" and not info.get("devices"):
            safe_print("[X] 该级的器件集合还没定义")
            return 1

    if not CONFIG_PATH.exists():
        safe_print(f"[X] 找不到 {CONFIG_PATH}（先跑 python3 scripts/init_config.py）")
        return 1

    state = read_state()
    old_port = int(state.get("port") or DEFAULT_PORT)
    if service_alive(old_port):
        safe_print(f"[X] 已经有一个 stage 服务在跑（端口 {old_port}）。先停掉：")
        safe_print("    python3 scripts/stage_run.py --stop")
        return 1

    enabled, unknown, disabled = apply_config(only)
    if unknown:
        safe_print(f"[X] --only 里有配置中不存在的设备名：{unknown}")
        safe_print("    可用名字：python3 scripts/hardware_test.py --list")
        return 1
    safe_print(f"[OK] 临时配置：开 {len(enabled)} 个 -> {', '.join(enabled) or '(无)'}")
    if disabled:
        safe_print(f"     （临时关掉 {len(disabled)} 个：{', '.join(disabled)}）")

    LOG_PATH.write_text("", encoding="utf-8")
    log_file = LOG_PATH.open("a", encoding="utf-8")
    env = dict(os.environ)
    env.setdefault("PYTHONUNBUFFERED", "1")
    proc = subprocess.Popen(  # noqa: S603 - 固定命令，无外部输入
        [sys.executable, "-m", "health_monitor", "serve", "--real", "--port", str(args.port)],
        cwd=str(RPI_DIR), stdout=log_file, stderr=subprocess.STDOUT, env=env,
        # ⚠️ 必须**另开一个会话**：本脚本常通过 SSH 跑，SSH 一断，同一会话里的子进程会收到
        #    SIGHUP 被带走 —— 而分级验收恰恰需要"服务留着、人去做拔插动作"。
        start_new_session=True,
    )
    write_state(proc.pid, args.port)
    safe_print(f"[OK] 已启动：PID {proc.pid}，端口 {args.port}，日志 {LOG_PATH}")

    deadline = time.time() + max(0.0, args.wait)
    while time.time() < deadline:
        if service_alive(args.port):
            break
        if proc.poll() is not None:
            break
        time.sleep(0.5)

    safe_print("-" * 72)
    safe_print(f"读数：{api_get(args.port, '/api/v1/current')}")
    safe_print(f"报警：{api_get(args.port, '/api/v1/alarms?limit=3')}")
    if proc.poll() is not None:
        safe_print(f"[X] 服务已退出（退出码 {proc.returncode}），看日志：tail -30 {LOG_PATH}")
        return 2
    safe_print("-" * 72)
    safe_print("验完记得：python3 scripts/stage_run.py --stop   # 停服务 + 还原配置")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
