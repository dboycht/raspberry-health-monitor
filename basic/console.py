#!/usr/bin/env python3
"""控制台输出的编码安全网（基础版专用）。

为什么需要（2026-09-25 在中文 Windows 上实测踩到，见仓库 `ERROR.md` E32）
------------------------------------------------------------------------
Python 在中文 Windows 上的 `sys.stdout.encoding` 是 **GBK（cp936）**——即使控制台
已经 `chcp 65001` 也一样。此时 `print("✅ 通过")` 会直接抛：

    UnicodeEncodeError: 'gbk' codec can't encode character '\\u2705'

**注意这不是"打印得难看"，而是程序崩掉**：`basic/tools/selfcheck.py` 跑完 10 项检查、
明明全通过，却在最后一行打印 `✅` 时抛异常 ⇒ 退出码 1 ⇒
`rpi/scripts/validate.py` 的第 10 项把它判成"基础版自检未通过"；
交给老师的同学在 Windows 上运行也会看到一坨 traceback。
树莓派（Debian，UTF-8 locale）永远碰不到这个问题，所以它一直是"只在开发机上炸"的暗雷。

判据很简单：**凡是可能进不了"当前输出编码"的字符，打印前都要过一遍 :func:`safe_text`。**
"当前输出编码"取 `sys.stdout.encoding`，取不到就当 UTF-8（树莓派/管道/CI 都是这样）。
"""

from __future__ import annotations

import sys
from typing import Dict, Optional

#: 常用符号的 ASCII 替身：既保证不抛异常，也保证**信息不丢**
_LABELS: Dict[str, str] = {
    "\u2705": "[OK]",      # ✅
    "\u274c": "[X]",       # ❌
    "\u26a0": "[!]",       # ⚠
    "\ufe0f": "",          # 变体选择符（⚠️ 的第二个码位）
    "\u2192": "->",        # →
    "\u2190": "<-",        # ←
    "\u2265": ">=",        # ≥
    "\u2264": "<=",        # <=
    "\u00b5": "u",         # µ
    "\u03a9": "ohm",       # Ω
    "\u2103": "degC",      # ℃
    "\u00d7": "x",         # ×
}


def output_encoding() -> str:
    """当前 stdout 用的编码名（取不到就按 UTF-8 处理，绝不因为取不到而崩）。"""
    enc = getattr(sys.stdout, "encoding", None)
    if not enc:
        return "utf-8"
    try:
        "".encode(enc)
    except LookupError:       # 编码名本身不认识
        return "utf-8"
    return enc


def safe_text(text: str, encoding: Optional[str] = None) -> str:
    """把 `text` 转成"当前输出编码一定打得出来"的形式。

    * UTF-8 环境（树莓派 / CI / 管道）：**原样返回**，一个字符都不改；
    * GBK 之类的窄编码：把进不去的字符换成 ASCII 替身（✅→`[OK]`），
      没有替身的换成 `?` —— 保证是"能读懂的降级"，而不是异常或空白。
    """
    enc = encoding or output_encoding()
    try:
        text.encode(enc)
        return text
    except UnicodeEncodeError:
        pass

    out = []
    for ch in text:
        try:
            ch.encode(enc)
            out.append(ch)
        except UnicodeEncodeError:
            out.append(_LABELS.get(ch, "?"))
    return "".join(out)


def safe_print(*args, **kwargs) -> None:
    """`print` 的安全版：所有参数先过 :func:`safe_text`（窄编码控制台上不崩）。

    为什么要有它：`rpi/scripts/` 下有 20 多个诊断/验收工具，原来是**直接**
    `print("✅ …")`；在中文 Windows（GBK 控制台）上会抛 `UnicodeEncodeError` 把
    整个脚本崩掉（2026-09-25 真机实测：`check_docs.py` 因此被误报成"文档自检失败"，
    见 `ERROR.md` E32/E35）。有了它，这些工具只需把 `print(` 换成 `safe_print(`。
    """
    print(*(safe_text(str(arg)) for arg in args), **kwargs)


__all__ = ["safe_text", "safe_print", "output_encoding"]
