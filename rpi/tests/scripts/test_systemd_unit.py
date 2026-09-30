"""守卫：随仓库发布的 **systemd 单元文件**必须真的能用（2026-10-01，ERROR.md E74）。

## 为什么要它

`rpi/deploy/health-monitor.service` 是这个项目**正规的部署路径**（配合 `install-service.sh`），
但此前**从没被验证过**。本轮用 `systemd-analyze verify` 一查就抓到：

    Unknown key 'After' in section [Service], ignoring.

`After=dev-i2c-1.device` 被写在 **`[Service]`** 段里，而 `After=` 只在 **`[Unit]`** 有效
⇒ **被静默忽略** ⇒ 注释里承诺的"等 /dev/i2c-1 就绪再启动"**根本没生效**。

⚠️ **这条的教训**：systemd 对"键放错段"的处理是**忽略 + 一行告警**，
既不是启动失败、也不是报错退出 ⇒ 不主动查就永远发现不了
（和 E68/E70/E74 是同一族：**声明与实际不一致，而没有任何东西会响**）。

本文件把这个检查固化下来：**不依赖 systemd**（CI 上也能跑），用 configparser 看"段 + 键"。
"""

from __future__ import annotations

import configparser
import shlex
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
UNIT = ROOT / "rpi" / "deploy" / "health-monitor.service"

#: 只在 `[Unit]` 段合法的键（放错段会被 systemd **静默忽略**）
UNIT_ONLY = {"description", "documentation", "after", "before", "wants", "requires",
             "binds to", "part of", "conflicts", "onfailure"}
#: 只在 `[Service]` 段合法的键
SERVICE_ONLY = {"execstart", "execstop", "user", "group", "supplementarygroups",
                "restart", "restartsec", "type", "standardoutput", "standarderror",
                "syslogidentifier", "workingdirectory", "environment"}


def parse_unit(text: str) -> dict[str, dict[str, str]]:
    """把单元文件解析成 `{段名: {小写键: 值}}`（**纯函数**，便于注入自测）。

    ⚠️ 存**值**而不是只存键：`StandardOutput=journal` 这类断言要看值
    （第一版只存了键集合，跑起来 `set.get` 当场报 AttributeError）。
    """
    parser = configparser.ConfigParser(strict=False, allow_no_value=True)
    parser.read_string(text)
    return {section.lower(): {k.lower(): (v or "") for k, v in parser[section].items()}
            for section in parser.sections()}


def misplaced_keys(sections: dict[str, dict[str, str]]) -> list[str]:
    """返回"放错段"的键（空 = 合规）。"""
    problems: list[str] = []
    for key in sorted(set(sections.get("unit", {})) & SERVICE_ONLY):
        problems.append(f"[Unit] 段里的 `{key}` 不是 [Unit] 的键（systemd 会忽略它）")
    for key in sorted(set(sections.get("service", {})) & UNIT_ONLY):
        problems.append(f"[Service] 段里的 `{key}` 不是 [Service] 的键（systemd 会忽略它）")
    return problems


class TestDetectorWorks(unittest.TestCase):
    """★ 先证明判据真的会响 —— 用**内存里的坏样本**，不碰真文件。"""

    def test_能抓到放错段的键(self) -> None:
        bad = "[Unit]\nDescription=x\n\n[Service]\nExecStart=/bin/true\nAfter=dev-i2c-1.device\n"
        problems = misplaced_keys(parse_unit(bad))
        self.assertTrue(problems, "注入的坏样本没被抓到 ⇒ 判据是坏的")
        self.assertIn("after", " ".join(problems).lower())

    def test_合规样本不误报(self) -> None:
        good = ("[Unit]\nDescription=x\nAfter=network-online.target\n"
                "Wants=network-online.target\n\n"
                "[Service]\nType=simple\nExecStart=/bin/true\nUser=pi\nRestart=always\n")
        self.assertEqual(misplaced_keys(parse_unit(good)), [])


class TestShippedUnitIsUsable(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(UNIT.exists(), f"找不到单元文件：{UNIT}")
        self.text = UNIT.read_text(encoding="utf-8")
        self.sections = parse_unit(self.text)

    def test_没有放错段的键(self) -> None:
        self.assertEqual(
            misplaced_keys(self.sections), [],
            "systemd 对放错段的键是**忽略**（不是报错）⇒ 承诺的行为会静默失效。",
        )

    def test_必备键都在(self) -> None:
        service = self.sections.get("service", {})
        self.assertIn("execstart", service)
        self.assertIn("workingdirectory", service)
        self.assertIn("user", service)
        self.assertIn("wantedby", self.sections.get("install", {}))
        # 日志进 journald（否则长期运行会无限长 —— 项目自带的正规部署路径）
        self.assertEqual(service.get("standardoutput"), "journal")

    def test_ExecStart的参数能被真正的解析器接受(self) -> None:
        """★ 不启动服务、也不碰硬件：只把 ExecStart 的参数喂给真正的 argparse。"""
        import sys
        sys.path.insert(0, str(ROOT / "rpi"))
        from health_monitor.main import build_parser

        lines = [ln for ln in self.text.split("\n") if ln.startswith("ExecStart=")]
        self.assertEqual(len(lines), 1, "ExecStart 应当只有一行")
        argv = shlex.split(lines[0].split("=", 1)[1])
        self.assertEqual(argv[:3], ["/usr/bin/python3", "-m", "health_monitor"],
                         "ExecStart 必须以 `python3 -m health_monitor` 起头")
        ns = build_parser().parse_args(argv[3:])
        self.assertTrue(getattr(ns, "real", False), "ExecStart 应当带 --real（真机部署）")
        self.assertTrue(getattr(ns, "port", None), "ExecStart 应当指定 --port")

    def test_等i2c设备就绪这件事写在正确的段里(self) -> None:
        """把 E74 那个具体缺陷钉死：`After=dev-i2c-1.device` 必须在 `[Unit]`。"""
        self.assertIn("after", self.sections.get("unit", set()))
        self.assertNotIn("after", self.sections.get("service", set()),
                         "After= 写在 [Service] 里会被 systemd 忽略（E74 原样复发）")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
