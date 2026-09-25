#!/usr/bin/env python3
"""项目验证脚本：把"体检 + 单测 + 演示 + 配置一致性 + 基础版 + Node 工具"一次跑完。

用途：**每次提交前跑一遍**，或每周合练时确认大家的改动没有互相破坏。

用法（在 ``rpi/`` 目录下）::

    python scripts/validate.py              # 全部 11 项检查
    python scripts/validate.py --quick      # 跳过单测/演示/基础版（只做静态检查）
    python scripts/validate.py --real       # 额外提示哪些检查需要真机

退出码：0 = 全部通过；1 = 有检查失败（打印失败项）。

⚠️ **本文件里的子进程一律走 `run_python()` / `run_node()`**（强制子进程 UTF-8 + 容错解码，
Node 还额外处理本机 TLS 解密代理的证书链问题）：
中文 Windows 上 Python 默认按 GBK 打印，父进程若直接用 `encoding="utf-8"` 抓输出
会抛 `UnicodeDecodeError` 并把**三项真实通过的检查假报成失败**（2026-09-25 实测，见 ERROR.md E32）。
新增任何子进程调用时请沿用这两个入口 —— `tests/scripts/test_validate_encoding.py`
会扫描本文件的 AST，发现"绕过它直接 `subprocess.run`"就报错。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, Tuple

RPI_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = RPI_DIR.parent
if str(RPI_DIR) not in sys.path:
    sys.path.insert(0, str(RPI_DIR))


# ---------------------------------------------------------------------------
# 子进程统一入口：**本文件所有子进程都必须走它**
# ---------------------------------------------------------------------------
# 为什么需要（2026-09-25 在中文 Windows 上实测踩到，详见 ERROR.md E32）：
# 本脚本用 `encoding="utf-8"` 抓子进程输出，但**子进程按什么编码打印，是由它自己决定的**：
# Python 在中文 Windows 上的 `sys.stdout.encoding` 是 **GBK/cp936**（本机实测，
# 即使控制台 `chcp` 是 65001 也一样），于是子进程吐出 GBK 字节、父进程按 UTF-8 解，
# `subprocess.run` 内部解码抛 `UnicodeDecodeError`，`stdout` 变成 **None** ⇒
# 文档一致性 / 演示 / 基础版**三项被假报为"失败"**，而它们单独跑全是 PASS。
# 树莓派（Debian，UTF-8 locale）从来不会暴露这个问题 —— 典型的"只在这台机器上假红"。
# 修法：给每个子进程强制 `PYTHONIOENCODING=utf-8`（只影响本次子进程，不污染本会话），
# 然后用容错解码兜底，保证**任何编码下都不会再抛 UnicodeDecodeError**。

#: 强制子进程用 UTF-8 打印（Python 3.7+ 认这个变量；被 `-X utf8` 之外的一切 locale 因素覆盖）
CHILD_IO_ENV = {
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUTF8": "1",
}

#: 兜底解码顺序：先按声明的 UTF-8 严格解；失败说明子进程没听环境变量（例如非 Python 程序）
_FALLBACK_ENCODINGS = ("utf-8", "gbk", "cp1252")


def child_env() -> dict:
    """返回"给子进程用的环境变量"：在**当前环境**的副本上强制 UTF-8 IO。"""
    import os

    env = dict(os.environ)
    env.update(CHILD_IO_ENV)
    return env


def _decode(raw) -> str:
    """把子进程的原始输出解成 str：先严格 UTF-8，失败再按常见本地编码兜底。

    最后一档 `errors="replace"` 是**刻意的安全网**：即使拿到的是混合编码，
    也只会出现几个替换字符，而不是让整项检查崩成"失败"。
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    for enc in _FALLBACK_ENCODINGS:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


#: Node 报"证书链不受信任"时的错误码 —— 本机装了 TLS 解密代理（Steam++）就会出现，
#: 且 Node 自带 CA 列表不认 Windows 证书存储里的自签根证书。见 ERROR.md E33。
_NODE_CERT_ERROR_CODES = (
    "UNABLE_TO_VERIFY_LEAF_SIGNATURE",
    "SELF_SIGNED_CERT_IN_CHAIN",
    "DEPTH_ZERO_SELF_SIGNED_CERT",
    "UNABLE_TO_GET_ISSUER_CERT",
    "CERT_UNTRUSTED",
)


def _looks_like_node_cert_error(text: str) -> bool:
    """子进程输出里有没有"证书链不受信任"的指纹（Node 的报错文案）。"""
    lowered = text.lower()
    return any(code.lower() in lowered for code in _NODE_CERT_ERROR_CODES) or (
        "unable to verify the first certificate" in lowered
    )


def _node_supports_system_ca() -> bool:
    """当前 Node 是否支持 `--use-system-ca`（v22.15 / v23 起；取不到就当不支持）。"""
    try:
        raw = subprocess.run([shutil.which("node") or "node", "--version"], capture_output=True)
    except OSError:  # pragma: no cover - 没有 node 时由调用方给出更清楚的提示
        return False
    match = re.match(r"v(\d+)\.(\d+)\.", _decode(raw.stdout).strip())
    if not match:
        return False
    major, minor = int(match.group(1)), int(match.group(2))
    if major >= 23:
        return True
    return major == 22 and minor >= 15


def run_node(args: List[str], cwd: Path | str):
    """跑一个 Node 子进程，并**自动绕开本机的 TLS 解密代理**。

    本机（以及任何装了 Steam++ / 抓包工具的机器）上，Node 自带 CA 列表不认
    Windows 证书存储里的自签根证书 ⇒ `fetch()` 报 `UNABLE_TO_VERIFY_LEAF_SIGNATURE`，
    而文档里写的命令是 `node scripts/check_ci.cjs`（没有 `--use-system-ca`）——
    也就是"照着文档敲必然失败"（ERROR.md E33）。
    `scripts/check_ci.cjs` 自己会兜底重试，这里再做一层：**只在真的报证书错时才带
    `--use-system-ca` 重跑一次**，所以没有代理的机器（树莓派、CI）走的是原样命令。

    ⚠️ 绝不使用 `rejectUnauthorized=false`：那会关掉校验、把真问题一起藏起来。
    """
    exe = shutil.which("node")
    if not exe:
        return subprocess.CompletedProcess(args=args, returncode=127, stdout="", stderr="node not found")

    def _run(extra: List[str]):
        proc = subprocess.run([exe, *extra, *args], cwd=str(cwd), capture_output=True)
        return subprocess.CompletedProcess(
            args=proc.args,
            returncode=proc.returncode,
            stdout=_decode(proc.stdout),
            stderr=_decode(proc.stderr),
        )

    first = _run([])
    combined = (first.stdout or "") + "\n" + (first.stderr or "")
    if first.returncode != 0 and _looks_like_node_cert_error(combined) and _node_supports_system_ca():
        second = _run(["--use-system-ca"])
        if second.returncode == 0:
            return second
        return second  # 仍然失败：把带 --use-system-ca 的那次输出交给调用方
    return first


def run_python(args: List[str], cwd: Path | str, env_extra: dict | None = None):
    """跑一个子进程（通常是"另一个 Python 脚本"），**强制 UTF-8 且保证解码不炸**。

    返回值刻意与 `subprocess.CompletedProcess` 同形（有 `returncode` / `stdout` / `stderr`），
    `stdout` / `stderr` 是**已经解好的 str**，调用方可以直接 `.splitlines()`。
    """
    env = child_env()
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, *args],
        cwd=str(cwd),
        capture_output=True,
        env=env,
    )
    return subprocess.CompletedProcess(
        args=proc.args,
        returncode=proc.returncode,
        stdout=_decode(proc.stdout),
        stderr=_decode(proc.stderr),
    )


#: "一项检查失败了，但连一句判据都没读到"时的提示 —— 用来把"假失败"当场指出来
_NO_OUTPUT_HINT = (
    "（未读到任何输出：可能是子进程输出编码与解码不一致 —— 见 ERROR.md E32；"
    "请手工重跑上面那条命令确认）"
)

#: 常用符号的 ASCII 替身（打印前过 `safe_text`，见下）
_ASCII_LABELS = {
    "\u2705": "[OK]",      # ✅
    "\u274c": "[X]",       # ❌
    "\u26a0": "[!]",       # ⚠
    "\ufe0f": "",          # 变体选择符（⚠️ 的第二个码位）
}


def safe_text(text: str) -> str:
    """把 `text` 转成"父进程自己的 stdout 一定打得出来"的形式。

    **为什么父进程也要管**（同一根因 E32 的另一半，修第一半时才暴露）：
    子进程输出修成 UTF-8 之后，父进程拿到了带 `✅` 的字符串，可父进程自己的
    `sys.stdout.encoding` 仍是 **GBK** ⇒ `print(f"...{detail}")` 抛
    `UnicodeEncodeError`，整个验证脚本崩在**打印报告**这一步，比原来更糟。

    判据：输出编码是 UTF-8（树莓派 / CI / 管道）时**原样返回**；窄编码时把打不出的
    字符换成 ASCII 替身。**只影响终端显示，不影响任何判据**（判据看的是 returncode 与文本内容）。
    """
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        text.encode(enc)
        return text
    except (UnicodeEncodeError, LookupError):
        pass

    out = []
    for ch in text:
        try:
            ch.encode(enc)
            out.append(ch)
        except (UnicodeEncodeError, LookupError):
            out.append(_ASCII_LABELS.get(ch, "?"))
    return "".join(out)


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
    selftest = run_python([str(RPI_DIR / "scripts" / "check_docs.py"), "--self-test"], cwd=RPI_DIR)
    if selftest.returncode != 0:
        detail = [ln for ln in (selftest.stdout or "").splitlines() if ln.strip().startswith("❌")]
        return False, "文档检查器自测失败（守卫可能已失效）：\n      " + "\n      ".join(detail[:5])

    proc = run_python([str(RPI_DIR / "scripts" / "check_docs.py")], cwd=RPI_DIR)
    if proc.returncode == 0:
        lines = [ln for ln in (proc.stdout or "").splitlines() if ln.startswith("✅")]
        return True, (lines[0] if lines else "文档自检通过") + "（含检查器注入自测）"
    detail = [ln for ln in (proc.stdout or "").splitlines() if ln.strip().startswith("-")]
    return False, "文档自检失败：\n      " + ("\n      ".join(detail[:10]) or _NO_OUTPUT_HINT)


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
    has_pytest = run_python(["-c", "import pytest"], cwd=RPI_DIR).returncode == 0
    if has_pytest:
        cmd = ["-m", "pytest", "tests", "-q"]
    else:
        cmd = ["-m", "unittest", "discover", "-s", "tests", "-v"]
    proc = run_python(cmd, cwd=RPI_DIR)

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
    if not summary and not verdict:
        # 连"Ran N tests"都没读到 ⇒ 很可能不是测试真的失败，而是**输出根本没读到**
        return False, f"测试失败：{_NO_OUTPUT_HINT}\n      " + "\n      ".join(lines[-12:])
    tail = "\n      ".join(lines[-12:])
    return False, f"测试失败（{summary or '无统计'}）：\n      {tail}"


def check_demo() -> Tuple[bool, str]:
    """跑一遍演示剧本（验证整机链路：采集 → 判定 → 下发）。"""
    # 演示里会 print 大量内容，这里只关心退出码
    proc = run_python(["-m", "health_monitor", "demo"], cwd=RPI_DIR)
    if proc.returncode == 0:
        lines = [ln for ln in (proc.stdout or "").splitlines() if ln.startswith("演示结束")]
        return True, lines[0] if lines else "演示脚本运行成功"
    tail = "\n      ".join((proc.stderr or "").strip().splitlines()[-8:])
    return False, f"演示失败（退出码 {proc.returncode}）：\n      " + (tail or _NO_OUTPUT_HINT)


def check_basic_version() -> Tuple[bool, str]:
    """基础版（``basic/``，课程作业 H 的最小可用版）自检 + 单测。

    为什么要并进提交前检查：基础版是**另一条独立可交付的路径**
    （一个文件夹拷走就能交作业），它坏掉不会让主项目单测变红——
    2026-09-24 就真的出过一次"基础版回放把演示数据删了"却没人发现。
    这里跑两个**不依赖硬件**的命令：`basic/tools/selfcheck.py` 与 `basic/tests`。
    """
    root = RPI_DIR.parent
    selfcheck = run_python([str(root / "basic" / "tools" / "selfcheck.py")], cwd=root)
    if selfcheck.returncode != 0:
        tail = "\n      ".join((selfcheck.stdout or "").strip().splitlines()[-6:])
        return False, f"基础版自检未通过：\n      " + (tail or _NO_OUTPUT_HINT)
    summary = ""
    for line in (selfcheck.stdout or "").splitlines():
        if line.startswith("结果："):
            summary = line.strip()
    # 单测。（pytest 的路径规则不需要额外配置：basic/tests/conftest.py 自己加了 sys.path）
    #
    # ⚠️ **没有 pytest 也必须能跑**（2026-09-25 真机实测，ERROR.md E35）：树莓派上没装 pytest
    # （README 明说"核心零第三方依赖"，板子上不该被强制装它），原来这里写死 `-m pytest`
    # ⇒ 真机上第 10 项恒红，而那 124 项单测用标准库 unittest 跑是**全绿**的。
    # 这与 check_tests() 的策略保持一致：先试 pytest，没有就退回 unittest。
    if run_python(["-c", "import pytest"], cwd=root).returncode == 0:
        tests = run_python(["-m", "pytest", "basic/tests", "-q"], cwd=root)
        runner = "pytest"
    else:
        # `basic/tests/__init__.py` 必须存在，unittest 才把该目录当可导入包
        # （缺那个空文件时 discover 会报 "Start directory is not importable"）
        tests = run_python(["-m", "unittest", "discover", "-s", "basic/tests"], cwd=root)
        runner = "unittest"
    combined = (tests.stdout or "") + "\n" + (tests.stderr or "")
    if tests.returncode != 0:
        tail = "\n      ".join(combined.strip().splitlines()[-8:])
        return False, f"基础版单测失败（{runner}）：\n      " + (tail or _NO_OUTPUT_HINT)
    passed = ""
    # ⚠️ unittest 把摘要写在 **stderr**（"Ran N tests… / OK"），只读 stdout 会得到空串，
    #    于是报告里显示成一句没有信息量的"单测 OK"（2026-09-25 真机实测，E35）。
    for line in reversed(combined.splitlines()):
        if "passed" in line or "failed" in line or line.startswith("Ran ") or line.startswith("OK"):
            passed = line.strip()
            break
    return True, f"{summary or '基础版自检通过'}；单测 {passed or 'OK'}（{runner}）"


def check_node_scripts() -> Tuple[bool, str]:
    """主机侧 Node 脚本的自检（``scripts/selftest.cjs``，**离线、不联网**）。

    为什么要在提交前跑它：``scripts/`` 下是"只在开发机上用、不参与运行"的工具
    （查 CI 状态、打印发布材料、隐私扫描），它们坏掉**不会让任何单测变红**——
    2026-09-25 就真出过一次：文档写着 `node scripts/check_ci.cjs`，
    而这台机器上直接跑会因 TLS 证书链报错（见 `ERROR.md` E33）。

    ⚠️ **不带 `--online`**：提交前检查不该依赖 github.com 是否可达，
    否则"没网"会变成一次红构建 —— 正是本文件要消灭的那类假失败。
    """
    selftest = REPO_ROOT / "scripts" / "selftest.cjs"
    if not selftest.exists():
        return False, f"找不到 Node 自检脚本：{selftest}"
    if not shutil.which("node"):
        return False, "没找到 node（主机侧工具需要 Node；装了之后重跑）"

    proc = run_node([str(selftest)], cwd=REPO_ROOT)
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    if proc.returncode == 0:
        summary = ""
        for line in reversed(stdout.splitlines()):
            if line.startswith("selftest OK"):
                summary = line.strip()
                break
        return True, summary or "Node 脚本自检通过"

    detail = [
        ln for ln in (stdout + "\n" + stderr).splitlines()
        if ln.strip().startswith("-") or "FAIL" in ln
    ]
    if not detail:
        # 没抓到"FAIL"行也要给东西：把尾部原样带出来（否则只看到一句"失败"，无从下手）
        tail = "\n      ".join((stdout + "\n" + stderr).strip().splitlines()[-12:])
        return False, "Node 脚本自检失败：\n      " + (tail or _NO_OUTPUT_HINT)
    return False, "Node 脚本自检失败：\n      " + "\n      ".join(detail[:12])


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
        Check("Node 脚本自检", check_node_scripts),
    ]
    if not args.quick:
        checks.append(Check("单元测试", check_tests))
        checks.append(Check("端到端演示", check_demo))
        checks.append(Check("基础版（basic/）", check_basic_version))

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
        # ⚠️ 必须过 safe_text：中文 Windows 上 sys.stdout.encoding 是 GBK，
        #    检查详情里可能带 ✅/❌，直接 print 会把**整个验证脚本**崩在报告这一步（E32）。
        print(safe_text(f"[{mark}] {check.name}：{detail}"))
    print("-" * 78)
    if failures:
        print(f"结果：{failures} 项失败（共 {len(checks)} 项）—— 请先修掉再提交")
    else:
        print(safe_text(f"结果：全部 {len(checks)} 项通过 ✅"))
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
