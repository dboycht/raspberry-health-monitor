#!/usr/bin/env python3
"""树莓派上线助手：**随时跑一下，它告诉你"现在到哪一步了、下一步做什么"**。

为什么要它
----------
把树莓派接上电脑这件事有 6 个环节（开热点 → 树莓派连上 → 开 SSH → 装公钥 →
固定 IP → 我这边能连），任何一环没成就卡住，而**现象往往一样**（"连不上"）。
本脚本把这些环节逐条探测出来，直接给出**下一条命令**，不用来回问。

用法（在**电脑**上跑，Windows 也行）::

    python rpi/scripts/pi_ready.py                 # 自动探测并给出下一步
    python rpi/scripts/pi_ready.py --ip 192.168.137.3   # 已知树莓派 IP 时直接测它

它只做**只读探测**：ping、TCP 22 探测、解析主机名、读本机网络配置。
不会改你的网络设置，也不会登录树莓派。
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import platform
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple
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

#: 候选网段（按可能性排序）：
#: 192.168.137 = Windows 移动热点默认；192.168.50 = 本项目文档里的网线直连；其他为常见家用网段
CANDIDATE_PREFIXES = ("192.168.137", "192.168.50", "192.168.1", "192.168.0", "10.42.0")
HOSTNAMES = ("health-pi.local", "raspberrypi.local", "pi.local")


def run(cmd: List[str], timeout: float = 8.0) -> Tuple[int, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace")
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except Exception as exc:  # noqa: BLE001
        return 1, f"{type(exc).__name__}: {exc}"


def wireless_adapters() -> List[str]:
    """本机无线网卡名（用 Get-NetAdapter 的 NdisPhysicalMedium，**不靠网卡名字猜**）。

    早前版本用正则匹配网卡名里的 "WLAN/Wi-Fi"，在中文系统上会漏判
    （网卡名可能叫"无线网络连接"或被系统重命名）—— 判据应当来自适配器属性。
    """
    code, text = run(["powershell", "-NoProfile", "-Command",
                      "Get-NetAdapter | Where-Object { $_.NdisPhysicalMedium -eq 9 -or "
                      "$_.PhysicalMediaType -match '802\\.11|Native 802\\.11' } | "
                      "ForEach-Object { $_.Name }"])
    names = [line.strip() for line in text.splitlines() if line.strip()] if code == 0 else []
    # 兜底：名字里带无线关键词
    if not names:
        for _, alias in local_ipv4():
            if re.search(r"WLAN|Wi-?Fi|Wireless|无线", alias, re.I):
                names.append(alias)
    return names


def local_ipv4() -> List[Tuple[str, str]]:
    """本机 IPv4 地址与网卡名（排除回环与链路本地）。"""
    out: List[Tuple[str, str]] = []
    code, text = run(["powershell", "-NoProfile", "-Command",
                      "Get-NetIPAddress -AddressFamily IPv4 | "
                      "Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } | "
                      "ForEach-Object { \"$($_.IPAddress)|$($_.InterfaceAlias)\" }"])
    if code == 0:
        for line in text.splitlines():
            if "|" in line:
                ip, _, alias = line.strip().partition("|")
                out.append((ip, alias))
    return out


def tcp_open(ip: str, port: int = 22, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def resolve(name: str) -> Optional[str]:
    try:
        return socket.gethostbyname(name)
    except OSError:
        return None


def sweep(prefix: str, port: int = 22, limit: int = 254, progress: bool = True) -> List[str]:
    """扫一个 /24 网段的 22 端口（只做 TCP 连接尝试，不发任何登录请求）。

    ⚠️ 必须**边扫边打印**：254 个地址在慢网络上要十几秒，
    不打印进度就会被误判成"卡死"（本地实测吃过这个亏）。
    """
    found: List[str] = []
    started = time.time()
    for last in range(1, limit + 1):
        ip = f"{prefix}.{last}"
        if tcp_open(ip, port, timeout=0.25):
            found.append(ip)
            if progress:
                print(f"\r    命中：{ip:<16}", flush=True)
        if progress and last % 64 == 0:
            print(f"\r    已扫 {last}/{limit}（{time.time() - started:.0f}s，命中 {len(found)} 个）",
                  end="", flush=True)
    if progress:
        print(f"\r    扫完 {prefix}.0/24：{len(found)} 个 22 端口开放{'（' + ', '.join(found) + '）' if found else ''}    ")
    return found


def ssh_key_ready() -> bool:
    key = Path.home() / ".ssh" / "id_ed25519_pi_health"
    return key.exists() and key.with_suffix(".pub").exists()


def step_line(index: int, ok: bool, title: str, detail: str = "") -> str:
    mark = "✅" if ok else "❌"
    text = f"{mark} [{index}/6] {title}"
    if detail:
        text += f"　{detail}"
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description="树莓派上线助手（只读探测 + 给出下一步）")
    parser.add_argument("--ip", default="", help="已知树莓派 IP（给了就直接测它）")
    parser.add_argument("--prefix", default="", help="只扫这个 /24 网段，例如 192.168.137")
    parser.add_argument("--full", action="store_true", help="扫本机全部门段（慢；默认只扫热点与直连网段）")
    parser.add_argument("--no-sweep", action="store_true", help="不扫网段（只测已知 IP 与主机名）")
    args = parser.parse_args()

    print("=" * 78)
    print("树莓派上线助手　（" + ("Windows" if platform.system() == "Windows" else platform.system()) + "）")
    print("=" * 78)

    ips = local_ipv4()
    print("\n【本机网络】")
    for ip, alias in ips:
        print(f"  {ip:<16} {alias}")
    if not ips:
        print("  （没有可用 IPv4 地址：先连上网，或开热点）")

    # ---- 第 1 步：本机有没有无线网卡（热点的前提；不必已连接）----
    wifi_aliases = wireless_adapters()
    hotspot_ip = [ip for ip, _ in ips if ip.startswith("192.168.137.")]

    print("\n【逐环节探测】")
    print(step_line(1, bool(wifi_aliases) or bool(hotspot_ip), "电脑有无线网卡（开热点用）",
                    f"无线网卡：{', '.join(wifi_aliases) or '未发现'}"
                    + (f"；热点网卡 IP {hotspot_ip[0]}" if hotspot_ip else "")))

    # ---- 第 2 步：树莓派在不在网里 ----
    targets: List[str] = []
    if args.ip:
        targets.append(args.ip)
    for name in HOSTNAMES:
        ip = resolve(name)
        if ip and ip not in targets:
            targets.append(ip)
    found_by_sweep: List[str] = []
    if not args.no_sweep:
        # ⚠️ 只扫**树莓派可能出现的**网段：
        #   - 192.168.137.x = Windows 移动热点默认网段
        #   - 192.168.50.x  = 本项目文档里的网线直连网段
        #   本机自带的 VMware 虚拟网段（192.168.209/126）永远不会有树莓派，
        #   扫它们纯属浪费（每个 /24 要 60+ 秒，实测直接把探测跑成"卡死"）。
        my_prefixes = {".".join(ip.split(".")[:3]) for ip, _ in ips}
        wanted = [p for p in ("192.168.137", "192.168.50") if p in my_prefixes]
        if args.prefix:
            wanted = [args.prefix]
        elif args.full:
            wanted = sorted(my_prefixes)
        if not wanted:
            print("  … 本机没有热点网段（192.168.137.x）或直连网段（192.168.50.x）→ 跳过扫描")
            print("     先开热点（Win+I → 网络和 Internet → 移动热点）或插网线并设 IP，再跑一次")
        for prefix in wanted:
            print(f"  … 扫 {prefix}.0/24 的 22 端口（每个地址 0.25 秒超时）")
            found_by_sweep.extend(sweep(prefix))
        if args.full:
            print("  （--full 模式：以上是全部本机网段）")
    found = [ip for ip in targets if tcp_open(ip)] + [ip for ip in found_by_sweep if ip not in targets]
    print(step_line(2, bool(found), "找到树莓派（22 端口可连）",
                    f"候选：{', '.join(found) if found else '无'}；"
                    f"主机名解析：{', '.join(f'{n}->{resolve(n)}' for n in HOSTNAMES)}"))

    # ---- 第 3 步：SSH 服务是否就绪（能建 TCP 就算开着）----
    ssh_up = bool(found)
    print(step_line(3, ssh_up, "树莓派 SSH 服务在监听",
                    "能连上 22 端口" if ssh_up else "→ 树莓派上执行：sudo systemctl enable --now ssh"))

    # ---- 第 4 步：本机密钥是否就绪 ----
    key_ok = ssh_key_ready()
    print(step_line(4, key_ok, "本机专用密钥已生成",
                    "~/.ssh/id_ed25519_pi_health" if key_ok else "→ 让我生成（或见 docs/09 手动生成）"))

    # ---- 第 5 步：免密登录是否成功 ----
    login_ok = False
    if found:
        code, out = run(["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
                         "-o", "ConnectTimeout=5", f"pi@{found[0]}", "echo PI_OK; hostname"], timeout=15)
        login_ok = "PI_OK" in out
        if login_ok:
            print(step_line(5, True, "免密登录成功", out.strip().splitlines()[-1][:60]))
        else:
            print(step_line(5, False, "免密登录未成功（公钥还没装）",
                            "→ 电脑上执行：type $env:USERPROFILE\\.ssh\\id_ed25519_pi_health.pub | "
                            f"ssh pi@{found[0]} \"mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys\""))
    else:
        print(step_line(5, False, "免密登录未成功（还没找到树莓派）"))

    # ---- 第 6 步：项目代码在不在树莓派上 ----
    project_ok = False
    if login_ok:
        code, out = run(["ssh", "-o", "BatchMode=yes", f"pi@{found[0]}",
                         "test -d ~/raspberry-health-monitor/rpi && echo HAVE_PROJECT || echo NO_PROJECT"], timeout=15)
        project_ok = "HAVE_PROJECT" in out
        print(step_line(6, project_ok, "树莓派上有项目代码",
                        "~/raspberry-health-monitor" if project_ok
                        else "→ git clone https://github.com/dboycht/raspberry-health-monitor.git"
                             "（或用 scp 从电脑传过去）"))
    else:
        print(step_line(6, False, "树莓派上有项目代码（待第 5 步成功后再看）"))

    # ---- 结论与下一步 ----
    print("\n" + "=" * 78)
    if login_ok:
        safe_print("🎉 全通了！告诉我「连上了」，我就开始跑只读体检并给你逐器件结论。")
        print(f"   我这边要用的别名：把 C:\\Users\\{Path.home().name}\\.ssh\\config 里 pi-health 的 "
              f"HostName 改成 {found[0]}（我可以自己改，你说一声即可）")
    elif found:
        print("已经能看到树莓派，但还进不去。下一步（在**电脑**上执行，会问一次树莓派密码）：")
        print(f"  type $env:USERPROFILE\\.ssh\\id_ed25519_pi_health.pub | ssh pi@{found[0]} "
              "\"mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys\"")
        print(f"  然后验证：ssh -o BatchMode=yes pi@{found[0]} \"hostname; uname -m\"")
    else:
        print("还没发现树莓派。按顺序做（二选一）：")
        print("\n  【方案 A：电脑开热点（推荐，电脑照常上网）】")
        print("   1) Win+I → 网络和 Internet → 移动热点 → 共享『宽带连接 2』")
        print("      网络名称 pi-debug / 密码≥12位 / 频段先试 5GHz（不行换 2.4GHz）/ 网络类型 WPA2")
        print("   2) 树莓派接显示器，WiFi 选 pi-debug 连上；执行 sudo systemctl enable --now ssh")
        print("   3) 再跑一次本脚本：python rpi/scripts/pi_ready.py")
        print("\n  【方案 B：网线直连电脑】")
        safe_print("   1) 网线插电脑网口 ↔ 树莓派网口")
        print("   2) 电脑设 IP：New-NetIPAddress -InterfaceAlias \"以太网\" -IPAddress 192.168.50.2 -PrefixLength 24")
        print("   3) 树莓派：sudo nmcli connection modify \"Wired connection 1\" ipv4.method manual "
              "ipv4.addresses 192.168.50.3/24 && sudo nmcli connection up \"Wired connection 1\"")
        print("   4) 再跑一次本脚本：python rpi/scripts/pi_ready.py")
        print("\n  详细步骤与排错：D:\\code\\DeepSeekHarness\\raspberry-health-monitor\\docs\\09-远程调试通路.md")
    print("=" * 78)
    return 0 if login_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
