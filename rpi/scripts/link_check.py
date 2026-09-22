#!/usr/bin/env python3
"""网线直连验收：确认"电脑 ↔ 树莓派"这根线真的通了，并给出下一步。

为什么单独写一个
----------------
直连时"连不上"有三种完全不同的原因，**现象却一样**（都只是"ping 不通/SSH 失败"）：
1. **电脑侧没设静态 IP**（网卡还停在 169.254.x.x 的自动地址）；
2. **树莓派侧没设同段 IP**（还在等 DHCP）；
3. **物理链路没起来**（线没插好/线是坏的/网口灯不亮）。

本脚本按这三层逐条给判据，一眼就能看出卡在哪层。
它只做**只读探测**（读网卡状态、ping、TCP 22），不改任何设置。

用法（在**电脑**上跑）::

    cd rpi
    python scripts/link_check.py                 # 看电脑侧配好没、链路起来没
    python scripts/link_check.py --ip 192.168.50.3   # 树莓派设好 IP 后直接测它
"""

from __future__ import annotations

import argparse
import platform
import socket
import subprocess
import sys
from typing import List, Tuple

PC_IP = "192.168.50.2"
PI_IP = "192.168.50.3"
PREFIX = "192.168.50"
#: 有线网卡名候选（本机实测叫「以太网」；写多个是为了换机器也能用）
WIRED_NAMES = ("以太网", "Ethernet", "以太网 2", "Ethernet 2")


def run(cmd: List[str], timeout: float = 10.0) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace")
        return (proc.stdout or "") + (proc.stderr or "")
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"


def ps(command: str) -> str:
    return run(["powershell", "-NoProfile", "-Command", command])


def find_wired_adapter() -> Tuple[str, str, str]:
    """返回 (网卡名, 状态, 速度)。"""
    for name in WIRED_NAMES:
        out = ps(f"Get-NetAdapter -Name '{name}' -ErrorAction SilentlyContinue | "
                 f"ForEach-Object {{ \"$($_.Name)|$($_.Status)|$($_.LinkSpeed)\" }}")
        for line in out.splitlines():
            if "|" in line:
                parts = line.strip().split("|")
                if len(parts) >= 3:
                    return parts[0], parts[1], parts[2]
    return "", "", ""


def adapter_ipv4(name: str) -> List[str]:
    out = ps(f"Get-NetIPAddress -InterfaceAlias '{name}' -AddressFamily IPv4 -ErrorAction SilentlyContinue | "
             f"ForEach-Object {{ $_.IPAddress }}")
    return [line.strip() for line in out.splitlines() if line.strip()]


def ping(ip: str, count: int = 2, timeout_ms: int = 900) -> bool:
    if platform.system() == "Windows":
        out = run(["ping", "-n", str(count), "-w", str(timeout_ms), ip], timeout=10)
        return "TTL=" in out.upper() or "ttl=" in out
    out = run(["ping", "-c", str(count), "-W", "1", ip], timeout=10)
    return " 0% packet loss" in out


def tcp_open(ip: str, port: int = 22, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="网线直连验收（只读探测）")
    parser.add_argument("--ip", default=PI_IP, help=f"树莓派 IP（默认 {PI_IP}）")
    parser.add_argument("--pc-ip", default=PC_IP, help=f"电脑该网卡应有的 IP（默认 {PC_IP}）")
    args = parser.parse_args()

    print("=" * 78)
    print("网线直连验收　（目标：电脑侧 " + args.pc_ip + " ↔ 树莓派 " + args.ip + "）")
    print("=" * 78)
    problems: List[str] = []

    # ---- 第 1 层：物理链路 ----
    name, status, speed = find_wired_adapter()
    if not name:
        print("❌ 找不到有线网卡（名字不是 '以太网'？跑 Get-NetAdapter 看看）")
        return 2
    link_up = status.lower() == "up"
    print(f"\n【第 1 层 · 物理链路】网卡「{name}」状态={status} 速率={speed}")
    if link_up:
        print("   ✅ 链路已建立（网口灯应常亮/闪）")
    else:
        problems.append(
            f"链路没起来。做这两件事：\n"
            f"     1) 网线两端插紧（电脑网口 ↔ 树莓派网口），看**网口指示灯**是否亮；\n"
            f"     2) 换一根网线试（劣质线/水晶头松是最常见原因）；\n"
            f"     3) 树莓派要**开机**（关机状态下网口也可能亮灯，别被误导）。"
        )
        print("   ❌ 链路未建立")

    # ---- 第 2 层：电脑侧 IP ----
    ips = adapter_ipv4(name) if name else []
    print(f"\n【第 2 层 · 电脑侧 IP】当前：{', '.join(ips) or '（无）'}")
    has_pc_ip = args.pc_ip in ips
    if has_pc_ip:
        print(f"   ✅ 已是 {args.pc_ip}")
    else:
        apipa = [ip for ip in ips if ip.startswith("169.254.")]
        if apipa:
            print(f"   ⚠️ 现在只有自动地址 {apipa[0]}（说明没设静态 IP）")
        problems.append(
            "电脑侧要设静态 IP（**需要管理员 PowerShell**）：\n"
            f'     New-NetIPAddress -InterfaceAlias "{name}" -IPAddress {args.pc_ip} -PrefixLength 24'
        )
        print("   ❌ 还没设静态 IP")

    # ---- 第 3 层：树莓派侧 IP 与连通性 ----
    print(f"\n【第 3 层 · 能不能通到树莓派 {args.ip}】")
    if not link_up or not has_pc_ip:
        print("   ⏭ 前两层没过，先别测这层（测了也一定不通，容易误判成树莓派的问题）")
    else:
        ping_ok = ping(args.ip)
        ssh_ok = tcp_open(args.ip)
        print(f"   ping：{'✅ 通' if ping_ok else '❌ 不通'}　"
              f"22 端口：{'✅ 开' if ssh_ok else '❌ 关闭/不通'}")
        if ssh_ok:
            print("   🎉 树莓派可达且 SSH 在监听！下一步装公钥：")
            print(f'     type $env:USERPROFILE\\.ssh\\id_ed25519_pi_health.pub | ssh pi@{args.ip} '
                  '"mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"')
        elif ping_ok:
            problems.append(
                "能 ping 通但 22 端口不通 ⇒ 树莓派上 SSH 没开。在树莓派上执行：\n"
                "     sudo systemctl enable --now ssh"
            )
        else:
            problems.append(
                f"完全不通 ⇒ 树莓派侧还没设同段 IP。在树莓派桌面右上角：\n"
                f"     右键网络图标 → 高级选项 → 编辑连接 → 有线连接 → IPv4 设置 → 手动：\n"
                f"       地址 {args.ip}　子网掩码 255.255.255.0　网关留空\n"
                f"     保存后重连一次；或命令行：sudo nmcli connection modify \"Wired connection 1\" "
                f"ipv4.method manual ipv4.addresses {args.ip}/24 && sudo nmcli connection up \"Wired connection 1\""
            )

    # ---- 结论 ----
    print("\n" + "=" * 78)
    if not problems:
        print("✅ 三层全通。告诉我「直连通了」，我就装公钥并开始跑体检。")
    else:
        print("下一步该做的事：")
        for index, item in enumerate(problems, start=1):
            print(f"  {index}) {item}")
    print("=" * 78)
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
