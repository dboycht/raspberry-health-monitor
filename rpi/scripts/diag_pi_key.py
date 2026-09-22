#!/usr/bin/env python3
"""诊断"公钥装了但免密登录仍失败"——并按结果给出一条**能直接成功**的安装命令。

为什么要单独诊断
----------------
"免密登录失败"至少有 4 种原因，**现象完全一样**（都是 `Permission denied (publickey)`），
但修法完全不同：
1. **公钥文件带 CRLF 换行**（Windows 常见）⇒ 写进 `authorized_keys` 的那一行末尾多一个
   `\\r`，sshd 解析失败 ⇒ **公钥格式无效**（最常见，本脚本重点查这个）；
2. 公钥**没真的写进去**（重定向/引号被 PowerShell 拆坏，写成了空文件或写错路径）；
3. 树莓派的 `~/.ssh` 或 `authorized_keys` **权限不对**（sshd 会直接忽略它）；
4. `sshd_config` 里 `PubkeyAuthentication no` 或 `AuthorizedKeysFile` 被改过（少见）。

用法（在**电脑**上跑）::

    cd rpi
    python scripts/diag_pi_key.py
    python scripts/diag_pi_key.py --ip 192.168.50.3 --user pi
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

KEY_NAME = "id_ed25519_pi_health"


def pub_path() -> Path:
    return Path.home() / ".ssh" / f"{KEY_NAME}.pub"


def inspect_local(pub: Path) -> dict:
    """分析本机公钥文件的字节（重点：有没有 CR、有没有 BOM、末尾空行）。"""
    raw = pub.read_bytes()
    return {
        "path": str(pub),
        "size": len(raw),
        "has_cr": b"\r" in raw,                       # CRLF 的元凶
        "has_bom": raw.startswith(b"\xef\xbb\xbf"),
        "crlf_count": raw.count(b"\r\n"),
        "trailing_newlines": len(raw) - len(raw.rstrip(b"\n")),
        "text": raw.decode("utf-8", "replace").strip(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="诊断公钥安装失败的原因")
    parser.add_argument("--ip", default="192.168.50.3")
    parser.add_argument("--user", default="pi")
    args = parser.parse_args()

    pub = pub_path()
    print("=" * 78)
    print("公钥安装诊断")
    print("=" * 78)

    if not pub.exists():
        print(f"❌ 公钥不存在：{pub}")
        return 2

    info = inspect_local(pub)
    print(f"\n【本机公钥文件】{info['path']}")
    print(f"  大小 {info['size']} 字节；含 CR(\\r)：{'是 ⚠️' if info['has_cr'] else '否'}"
          f"；含 BOM：{'是 ⚠️' if info['has_bom'] else '否'}"
          f"；CRLF 个数：{info['crlf_count']}；末尾换行数：{info['trailing_newlines']}")
    print(f"  内容：{info['text'][:100]}…")

    fix_needed = info["has_cr"] or info["has_bom"]
    if fix_needed:
        print("\n🔎 结论：本机公钥带 CR/BOM —— **这就是免密失败的最可能原因**。")
        print("   sshd 读 authorized_keys 时，一行末尾多出的 \\r 会让公钥解析失败。")
    else:
        print("\n🔎 本机公钥格式看起来是干净的（无 CR/BOM）；若仍失败，原因多半在树莓派侧"
              "（公钥没写进去 / 权限不对）。")

    # 核心：给出一条**不依赖文件**的安装命令（把公钥内联进远程命令，避免任何换行符问题）
    key_line = info["text"].replace("\r", "").replace("\n", " ").strip()
    remote = (
        'mkdir -p ~/.ssh && chmod 700 ~/.ssh && '
        f'grep -v "dsh-agent@pc-for-raspberry-health-monitor" ~/.ssh/authorized_keys 2>/dev/null > ~/.ssh/ak.tmp; '
        f'mv ~/.ssh/ak.tmp ~/.ssh/authorized_keys 2>/dev/null; '
        f'echo "{key_line}" >> ~/.ssh/authorized_keys && '
        'chmod 600 ~/.ssh/authorized_keys && '
        'echo KEY_INSTALLED && wc -l < ~/.ssh/authorized_keys'
    )
    print("\n" + "=" * 78)
    print("👉 在电脑上执行这一条（会问一次树莓派密码）——它是**修复版**：")
    print("   ① 不进管道、不读文件，公钥直接内联，绕开 CRLF/编码问题；")
    print("   ② 会先删掉之前写坏的同一把公钥，再写入干净的；")
    print("   ③ 顺手把权限设成 700/600（权限不对 sshd 会忽略）。")
    print("=" * 78)
    print(f'\nssh {args.user}@{args.ip} \'{remote}\'\n')
    print("然后再验证（应该不再问密码）：")
    print(f'ssh -o BatchMode=yes {args.user}@{args.ip} "echo PI_OK; hostname; uname -m; date"')
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
