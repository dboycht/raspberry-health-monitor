#!/usr/bin/env python3
"""装公钥：把本机专用公钥追加到树莓派的 ``~/.ssh/authorized_keys``（免密登录）。

**为什么要你自己跑**：这一步需要**树莓派的登录密码**。
密码应当由你亲手输入（交互式提示），**不要**写进任何命令或文件——
本项目的一条纪律就是"凭据不落盘、不进日志、不进仓库"。

用法（在**电脑**上跑）::

    cd rpi
    python scripts/install_pi_key.py                    # 默认 pi@192.168.50.3
    python scripts/install_pi_key.py --user dboy        # 用户名不是 pi 时
    python scripts/install_pi_key.py --ip 192.168.50.3 --check   # 只检查是否已装好

它做三件事：
1. 优先用 ``ssh-copy-id``（Git 自带，最省事）；
2. 没有就退回 PowerShell 等效命令；
3. 跑完**立即验证**免密登录是否真的成功（不许"看起来装了"就算完）。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

KEY_NAME = "id_ed25519_pi_health"


def normalized_pubkey(pub: Path) -> str:
    """返回**规范化后的公钥行**：去掉 CR/BOM、去掉多余空行，末尾恰好一个换行。

    ⚠️ 为什么必须做（2026-09-22 实测踩到）：Windows 写出的 `.pub` 是 **CRLF**，
    末尾那个 `\\r` 被一起写进 `authorized_keys` 后，**sshd 解析该行失败**，
    表现为"公钥明明装上了却仍然 Permission denied (publickey)"。
    这类失败极难自查（文件看起来完全正常），所以这里统一规范化，
    并且**安装时不再走管道/读文件**，直接把公钥内联进远程命令。
    """
    text = pub.read_bytes().decode("utf-8", "replace")
    return text.replace("\r", "").replace("\ufeff", "").strip() + "\n"


def key_dir() -> Path:
    return Path.home() / ".ssh"


def have_ssh_copy_id() -> str:
    """找**能在 Windows 上直接执行**的 ssh-copy-id。

    ⚠️ 坑（2026-09-22 实测）：Windows 上 `shutil.which("ssh-copy-id")` 会命中
    **Git 自带的那个** `C:\\Program Files\\Git\\usr\\bin\\ssh-copy-id`，
    但它是个 **Bash shell 脚本**（无扩展名），Windows 直接执行会报
    `OSError: [WinError 193] %1 不是有效的 Win32 应用程序`。
    所以判据必须是"它是不是可执行文件"：Windows 上只认 `.exe`/`.cmd`/`.bat`，
    Bash 脚本一律不用（退回 PowerShell 等效命令）。
    """
    if sys.platform.startswith("win"):
        for ext in (".exe", ".cmd", ".bat"):
            found = shutil.which(f"ssh-copy-id{ext}")
            if found:
                return found
        return ""
    return shutil.which("ssh-copy-id") or ""


def check_login(user: str, ip: str) -> bool:
    """验证免密登录（BatchMode：不许提示密码，能过才算真的装好了）。"""
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
           "-o", "ConnectTimeout=6", f"{user}@{ip}", "echo PI_KEY_OK; hostname; uname -m; date"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=25,
                              encoding="utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        print(f"  验证命令异常：{type(exc).__name__}: {exc}")
        return False
    out = (proc.stdout or "") + (proc.stderr or "")
    if "PI_KEY_OK" in out:
        lines = [ln for ln in out.strip().splitlines() if ln.strip()]
        print("  ✅ 免密登录成功：")
        for line in lines[1:4]:
            print(f"     {line}")
        return True
    print("  ❌ 免密登录仍未成功：")
    for line in out.strip().splitlines()[-3:]:
        print(f"     {line}")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="把本机公钥装到树莓派（免密登录）")
    parser.add_argument("--ip", default="192.168.50.3", help="树莓派 IP（默认 192.168.50.3）")
    parser.add_argument("--user", default="pi", help="树莓派用户名（默认 pi）")
    parser.add_argument("--check", action="store_true", help="只检查免密登录是否已可用")
    args = parser.parse_args()

    pub = key_dir() / f"{KEY_NAME}.pub"
    priv = key_dir() / KEY_NAME

    print("=" * 78)
    print(f"装公钥：{args.user}@{args.ip}")
    print("=" * 78)

    if not pub.exists():
        print(f"❌ 找不到公钥 {pub}")
        print("   → 先生成：ssh-keygen -t ed25519 -f \"$env:USERPROFILE\\.ssh\\{KEY_NAME}\" -N '\"\"' -C dsh-agent")
        return 2
    print(f"本机公钥：{pub}")
    print(f"公钥内容：{pub.read_text(encoding='utf-8').strip()[:80]}…\n")

    if args.check:
        return 0 if check_login(args.user, args.ip) else 1

    print("【第一步】检查是否已经装过（能免密就直接结束）")
    if check_login(args.user, args.ip):
        print("\n🎉 已经装好了，不需要再装。")
        return 0

    print("\n【第二步】把公钥追加到树莓派")
    print("⚠️ 接下来会提示输入**树莓派的登录密码**（输入时不显示，是正常的）。")
    print("   密码只在你和树莓派之间传输，本脚本不记录它。\n")
    input("按回车开始（或 Ctrl+C 取消）…")

    copy_id = have_ssh_copy_id()
    if copy_id:
        print(f"用 ssh-copy-id（{copy_id}）…")
        cmd = [copy_id, "-i", str(priv), f"{args.user}@{args.ip}"]
    else:
        print("用**内联公钥**方式安装（不进管道、不读文件 → 绕开 CRLF/编码坑）…")
        key_line = normalized_pubkey(pub).strip()
        remote = (
            'mkdir -p ~/.ssh && chmod 700 ~/.ssh && '
            'grep -v "dsh-agent@pc-for-raspberry-health-monitor" ~/.ssh/authorized_keys 2>/dev/null '
            '> ~/.ssh/ak.tmp; mv ~/.ssh/ak.tmp ~/.ssh/authorized_keys 2>/dev/null; '
            f'echo "{key_line}" >> ~/.ssh/authorized_keys && '
            'chmod 600 ~/.ssh/authorized_keys && echo KEY_INSTALLED'
        )
        cmd = ["ssh", f"{args.user}@{args.ip}", remote]

    print("  执行中（会提示输入树莓派密码）…")
    try:
        proc = subprocess.run(cmd, timeout=300)
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 执行失败：{type(exc).__name__}: {exc}")
        return 1
    if proc.returncode != 0:
        print(f"⚠️ 命令退出码 {proc.returncode}（下面用**实际验证**判定，退出码不作数）")

    print("\n【第三步】验证免密登录（这一步才算数）")
    ok = check_login(args.user, args.ip)
    print()
    if ok:
        print("=" * 78)
        print("🎉 装好了。接下来我会：")
        print("   1) 改 ~/.ssh/config 里 pi-health 的 HostName 指向这台树莓派；")
        print("   2) 跑只读体检（设备节点 / 依赖 / i2cdetect / 逐器件）；")
        print("   3) 把项目代码传上去（或 git clone）并跑 hardware_test.py。")
        print("=" * 78)
        return 0
    print("❌ 还是不行。排错顺序：")
    print("   1) 密码是否正确（树莓派桌面登录用的那个）；")
    print("   2) 用户名是否正确（不是 pi 就加 --user 你的名字）；")
    print("   3) 树莓派上 ~/.ssh 权限：chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys；")
    print("   4) 看树莓派 sshd 日志：sudo journalctl -u ssh -n 20")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
