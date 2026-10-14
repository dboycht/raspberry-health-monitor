"""★ 门禁：「GPIO 一律走 gpiozero」（2026-10-13 按课程要求收口）。

为什么做成守卫，而不是写进文档
------------------------------
"用哪个库访问 GPIO"是**每加一个器件都要重做一遍的决定**，而写反/写漏
**不会报任何错**：真机照样能点亮（lgpio 也能点），CI 也不会红。
本项目的教训（工作区 `memory/46`、AGENTS 规则 14）是：**发现"承诺 > 机制"时改机制**。
所以这里把口径做成可执行的检查：

1. **不许直接 import ``RPi`` / ``RPi.GPIO``** —— 树莓派 5 上它根本不可用（项目早期踩过）；
2. **直接接 GPIO 的驱动必须用到 ``gpiozero``**（清单见 :data:`GPIO_DRIVERS`）；
3. **直接 import ``lgpio`` 只允许出现在白名单里**，且白名单条目必须
   ①写明原因 ②**仍然真的在 import lgpio**（防"白名单留着不动、实际早已不用"的腐烂）。

判据要点
--------
:func:`scan_source` 是**纯函数**（喂字符串、返回问题清单），所以
``TestScannerItself`` 能用**注入样本**断言"它真的抓得住" ——
**永远绿的守卫比没有守卫更糟**（本项目 2026-10-01 用注入自测抓到过一个"看起来在跑、
其实一直没匹配"的守卫）。
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path
from typing import Dict, List

# ⚠️ 测试必须自包含：不能只依赖 conftest.py 改 sys.path（那是 pytest 专有钩子，
# `python -m unittest discover -s tests` 不会加载它）。
_RPI_DIR = Path(__file__).resolve().parents[2]          # .../rpi
_PROJECT_ROOT = _RPI_DIR.parent                          # 仓库根（含 basic/）
_PKG_DIR = _RPI_DIR / "health_monitor"


#: 必须使用 ``gpiozero`` 的驱动（相对 ``rpi/health_monitor/`` 的路径）。
#: ⚠️ 新加一个"直接接 GPIO 的器件"时**必须**把它的驱动加进来 ——
#: 漏加本身不会报错，但这份清单是评审时唯一能一眼看全的地方（所以刻意写死在测试里）。
GPIO_DRIVERS: Dict[str, str] = {
    "sensors/button.py": "按键（消音/求助、测血氧）",
    "sensors/hc_sr501.py": "HC-SR501 人体红外（PIR）",
    "outputs/led.py": "三色 LED",
    "outputs/buzzer.py": "蜂鸣器",
    "outputs/tft_spi.py": "TFT 彩屏的 DC/RST/背光（含无 spidev 时的软件 SPI 回退）",
}

#: 允许**直接** import ``lgpio`` 的文件 → 原因（必须仍在真的用，否则本文件会失败）。
LGPIO_WHITELIST: Dict[str, str] = {
    "sensors/dht11.py": (
        "DHT11 单总线：gpiozero 2.0.1 起**删掉了 DHT11/DHT22 类**（2026-09-24 真机实测，"
        "ERROR.md E27），只能退回 lgpio 的**内核级边沿时间戳**自实现；"
        "gpiozero 仍是首选（该类存在时优先用它）"
    ),
}

#: 明令禁止的模块（Pi 5 上不可用）。注意只看 import，不扫字符串/注释。
FORBIDDEN_MODULES = {"RPi"}

#: `basic/`（作业版交付物）里也必须用到 gpiozero 的文件。
BASIC_GPIOZERO_REQUIRED = {
    "basic/dht11read.py": "基础版模块化的 DHT11 读取（gpiozero 优先、lgpio 回退）",
    "basic/hw/run.py": "单文件作业版（gpiozero 优先、lgpio 回退）",
}


def imported_modules(source: str) -> set:
    """返回源码里 import 到的**顶层模块名**（`import x` 与 `from x import y` 都算）。"""
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.split(".")[0])
    return names


def scan_source(source: str, rel: str) -> List[str]:
    """检查一份源码是否符合 GPIO 口径；返回问题清单（空 = 通过）。

    :param source: 源码文本
    :param rel: 相对 ``rpi/health_monitor/`` 的路径（用于白名单判定与报错定位）
    """
    problems: List[str] = []
    names = imported_modules(source)

    for module in sorted(names & FORBIDDEN_MODULES):
        problems.append(
            f"{rel}：直接 import 了 {module} —— 树莓派 5 上不可用，"
            "GPIO 一律走 gpiozero（`import gpiozero`）"
        )
    if "lgpio" in names and rel not in LGPIO_WHITELIST:
        problems.append(
            f"{rel}：直接 import 了 lgpio，但它不在白名单里 —— "
            "GPIO 一律走 gpiozero；若确有平台原因，"
            "请把（文件 → 原因）加进本文件的 LGPIO_WHITELIST"
        )
    if rel in GPIO_DRIVERS and "gpiozero" not in names:
        problems.append(
            f"{rel}：是直接接 GPIO 的驱动（{GPIO_DRIVERS[rel]}），但没有 import gpiozero —— "
            "GPIO 一律走 gpiozero"
        )
    return problems


class TestScannerItself(unittest.TestCase):
    """注入自测：**先证明这个扫描器真的抓得住**，再看仓库过不过。"""

    def test_抓到禁止的RPi导入(self) -> None:
        problems = scan_source("import RPi.GPIO as GPIO\n", "outputs/led.py")
        self.assertTrue(problems, "RPi.GPIO 必须被判违规")
        self.assertIn("RPi", " ".join(problems))

    def test_抓到白名单外的lgpio导入(self) -> None:
        problems = scan_source("import lgpio\n", "outputs/led.py")
        self.assertTrue(problems, "非白名单文件 import lgpio 必须被判违规")

    def test_抓到GPIO驱动没import_gpiozero(self) -> None:
        problems = scan_source("import time\n", "outputs/buzzer.py")
        self.assertTrue(problems, "GPIO 驱动不 import gpiozero 必须被判违规")

    def test_白名单文件里的lgpio不算违规(self) -> None:
        self.assertEqual(scan_source("import lgpio\nimport gpiozero\n", "sensors/dht11.py"), [])

    def test_观察ast只认import不认注释(self) -> None:
        """注释里提 lgpio（本项目到处都是这种说明）**不算**违规。"""
        source = "# 这里说明为什么不用 lgpio\nimport gpiozero\nx = 'lgpio 也能点亮'\n"
        self.assertEqual(scan_source(source, "outputs/led.py"), [])

    def test_from导入也算(self) -> None:
        """`from lgpio import ...` 与 `import lgpio` 必须一视同仁。"""
        self.assertTrue(scan_source("from lgpio import gpiochip_open\n", "outputs/led.py"))


class TestRepoFollowsPolicy(unittest.TestCase):
    """真仓库扫描：`rpi/health_monitor/` 下每个 .py 都必须符合口径。"""

    def _sources(self):
        for path in sorted(_PKG_DIR.rglob("*.py")):
            rel = path.relative_to(_PKG_DIR).as_posix()
            yield rel, path.read_text(encoding="utf-8", errors="replace")

    def test_全包无违规(self) -> None:
        problems: List[str] = []
        for rel, text in self._sources():
            problems.extend(scan_source(text, rel))
        self.assertEqual(problems, [], "\n".join(problems))

    def test_清单里的驱动文件都存在(self) -> None:
        for rel in sorted(GPIO_DRIVERS):
            with self.subTest(rel=rel):
                self.assertTrue((_PKG_DIR / rel).is_file(), f"{rel} 不在仓库里（清单该更新了）")

    def test_白名单条目仍然真的在用lgpio(self) -> None:
        """防腐烂：白名单留着不动、而文件其实已经不用 lgpio 了 —— 必须删掉那一行。"""
        for rel, reason in sorted(LGPIO_WHITELIST.items()):
            with self.subTest(rel=rel):
                path = _PKG_DIR / rel
                self.assertTrue(path.is_file(), f"{rel} 不在仓库里（白名单该更新了）")
                self.assertIn("lgpio", imported_modules(path.read_text(encoding="utf-8", errors="replace")),
                              f"{rel} 已不再 import lgpio ⇒ 请从 LGPIO_WHITELIST 里删掉它（原因：{reason}）")

    def test_GPIO驱动都真的用了gpiozero(self) -> None:
        for rel, why in sorted(GPIO_DRIVERS.items()):
            with self.subTest(rel=rel):
                text = (_PKG_DIR / rel).read_text(encoding="utf-8", errors="replace")
                self.assertIn("gpiozero", imported_modules(text), f"{rel}（{why}）必须走 gpiozero")


class TestBasicDeliverablesUseGpiozero(unittest.TestCase):
    """`basic/` 是**要交给老师的作业版**，同样按口径要求：必须用到 gpiozero。"""

    def test_作业版文件都import了gpiozero(self) -> None:
        for rel, why in sorted(BASIC_GPIOZERO_REQUIRED.items()):
            with self.subTest(rel=rel):
                path = _PROJECT_ROOT / rel
                self.assertTrue(path.is_file(), f"{rel} 不在仓库里（清单该更新了）")
                names = imported_modules(path.read_text(encoding="utf-8", errors="replace"))
                self.assertIn("gpiozero", names, f"{rel}（{why}）必须 import gpiozero")

    def test_基础版DHT11后端顺序是gpiozero优先(self) -> None:
        """顺序写反不会有任何报错 ⇒ 这里直接读源码钉住它（行为级见 basic/tests）。"""
        path = _PROJECT_ROOT / "basic" / "dht11read.py"
        text = path.read_text(encoding="utf-8", errors="replace")
        self.assertIn("for factory in (GpiozeroBackend, LgpioBackend):", text,
                      "顺序必须是 (GpiozeroBackend, LgpioBackend) —— 先 gpiozero、后 lgpio")


if __name__ == "__main__":
    sys.exit(0 if unittest.main(verbosity=2) else 1)
