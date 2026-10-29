"""★ 门禁：接线图必须与配置/驱动同步（不许"图是旧的，代码是新的"）。

为什么需要这个门禁
------------------
接线图是**给人在工位上照着插线用的**。它一旦落后于 ``devices.json``，
同学就会照着旧图把线插错地方 —— 而**任何单测都不会因此变红**
（图只是文档，不参与运行）。

本项目已经真实发生过：
* `hardware/01-引脚分配表.md` / `02-接线图.md` / `树莓派接线总表.md` 至今仍写着
  **红色 LED（GPIO12 / 物理脚 32）**，而 ``devices.json`` 的 ``status_led.pins``
  早已只剩 green / yellow（红灯随 T3 阶段撤掉）；
* 同三份文档里**没有「测血氧」按键**（``spo2_button.pin=13``），而它从 2026-09-30 起就在跑。

⇒ 所以接线图**不手写**：由 ``hardware/tools/gen_wiring_svg.py`` 从
``devices.json`` + 各驱动 ``describe()`` 生成；本文件把"生成物是否最新"做成可执行判据。

判据
----
1. 生成脚本能跑通，输出两张 SVG；
2. **已提交的 SVG 与"现在重新渲染"的结果逐字节一致**（``--check`` 模式）；
3. SVG 里**真的出现了**配置里每个在用器件（防止"图还在、器件没了"）；
4. 已删除的器件**不许**再出现在图里（红 LED / HC-SR04 / 蓝牙音箱）。

第 3、4 条是**内容级**的：只比字节相等的话，脚本若把某器件整个漏掉，
重画一张同样"过期一致"的图也能通过。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Dict, List

# ⚠️ 测试必须自包含（不能只依赖 conftest.py 的 sys.path，那是 pytest 专有钩子）。
_RPI_DIR = Path(__file__).resolve().parents[2]          # .../rpi
_PROJECT_ROOT = _RPI_DIR.parent                          # 仓库根
_GEN = _PROJECT_ROOT / "hardware" / "tools" / "gen_wiring_svg.py"
_GEN_SCH = _PROJECT_ROOT / "hardware" / "tools" / "gen_schematic.py"
_HW = _PROJECT_ROOT / "hardware"
_SCHEMATIC = "原理图.svg"


def _run_gen(args: List[str], script: Path = None) -> subprocess.CompletedProcess:
    """跑生成脚本；强制 UTF-8 IO（中文 Windows 上默认是 cp936，见 ERROR.md E32）。"""
    import os

    env = dict(os.environ)
    env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
    return subprocess.run(
        [sys.executable, str(script or _GEN), *args],
        cwd=str(_PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )


def _svg(name: str) -> str:
    return (_HW / name).read_text(encoding="utf-8")


def _config_devices() -> Dict[str, dict]:
    raw = json.loads((_RPI_DIR / "config" / "devices.json").read_text(encoding="utf-8"))
    return raw["devices"]


class TestWiringSvgIsCurrent(unittest.TestCase):
    """生成物必须是最新的（这是本文件的主判据）。"""

    def test_两张SVG存在(self) -> None:
        for name in ("接线图-引脚.svg", "接线图-原理.svg"):
            path = _HW / name
            self.assertTrue(path.exists(), f"缺少接线图 {path}")
            self.assertGreater(path.stat().st_size, 3000, f"{name} 太小，疑似空文件")

    def test_SVG与重新渲染的结果逐字节一致(self) -> None:
        """★ 核心判据：改了 devices.json / describe() 却忘了重画图 ⇒ 这里红。

        修复方式写在失败信息里（不要让学生去猜）。
        """
        proc = _run_gen(["--check"])
        self.assertEqual(
            proc.returncode, 0,
            "接线图已过期，请重新生成：\n"
            "    python hardware/tools/gen_wiring_svg.py\n"
            f"脚本输出：\n{proc.stdout}\n{proc.stderr}",
        )

    def test_生成脚本可复跑且产出稳定(self) -> None:
        """连跑 --check 两次都要通过（防止生成器带随机/时间戳之类的非确定性）。"""
        first = _run_gen(["--check"])
        second = _run_gen(["--check"])
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)

    def test_SVG一律是LF线尾(self) -> None:
        """★ 跨平台假红的钉子（2026-10-27 实测踩到）。

        现象：生成器原来用 ``Path.write_text``，在 Windows 上把 ``\\n`` 写成 ``\\r\\n``；
        Linux CI 用**同样的输入**会生成 LF ⇒ 两边字节不同 ⇒ ``--check`` 在 CI 上红、
        在本机绿。这正是本项目 E32/E33 那一族"只在这台机器上假红"的坑。

        判据：仓库里的 SVG 必须 **0 个 CRLF**。（写入侧已固定 ``newline="\\n"``。）
        """
        for name in ("接线图-引脚.svg", "接线图-原理.svg"):
            raw = (_HW / name).read_bytes()
            self.assertEqual(raw.count(b"\r\n"), 0,
                             f"{name} 含 CRLF：Windows 与 CI 生成的字节会不一致，--check 会假红")
            self.assertGreater(raw.count(b"\n"), 10, f"{name} 行数异常")


class TestWiringSvgContent(unittest.TestCase):
    """内容级判据：图里**真的**画了现在的器件，且**没有**已删除的器件。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.pins_svg = _svg("接线图-引脚.svg")
        cls.sch_svg = _svg("接线图-原理.svg")
        cls.both = cls.pins_svg + cls.sch_svg
        cls.devices = _config_devices()

    def test_每个在用器件都出现在图里(self) -> None:
        """配置里启用的器件，其名字必须出现在两张图之一。"""
        expected = {
            "ambient": "DHT11",
            "motion": "HC-SR501",
            "display": "LCD1602",
            "tft": "TFT",
            "alarm_buzzer": "蜂鸣器",
            "status_led": "LED",
            "sos_button": "求救",
            "spo2_button": "血氧",
            # vitals 默认 enabled=false（按需测量），但器件在图上必须写明，
            # 否则照着图接线的人会漏掉心率血氧模块。
            "vitals": "MAX30102",
        }
        missing = [
            f"{name}（{label}）" for name, label in expected.items()
            if label not in self.both
        ]
        self.assertEqual(missing, [], f"这些器件没有画进接线图：{missing}")

    def test_已删除的器件不许再画进器件卡(self) -> None:
        """红线：这些器件已从代码里删干净，图里再给它们画卡片就是误导。

        * 红色 LED —— ``devices.json`` 的 ``status_led.pins`` 只有 green/yellow；
        * HC-SR04 —— 2026-10-02 废止删除（ECHO 为 5V 电平）；
        * 蓝牙音箱 —— 2026-10-02 废止删除（板上无可发声输出）；
        * TMP36 / MCP3002 —— 2026-09-28 取消、2026-10-01 删除。

        ⚠️ 只检查**引脚图的器件卡区**与原理图的**卡片区**，不检查原理图底部的备注 —
        —— 那里刻意写了一句"已废止并删除的器件（不再接线）"，是为了让答辩时被问到
        "为什么没有 XX" 有个现成答案。**禁止提到它们**反而是错的。
        """
        # 引脚图里卡从 "MAX30102 心率血氧" 开始，之前都是排针本体
        for ghost in ("HC-SR04", "蓝牙音箱", "TMP36", "MCP3002", "红色 LED", "红 LED"):
            self.assertNotIn(ghost, self.pins_svg,
                             f"引脚图里还画着已删除的器件：{ghost}")
            self.assertNotIn(ghost, self.sch_svg.split("⚠ 两条电源红线")[0],
                             f"原理图的器件卡里还画着已删除的器件：{ghost}")

    def test_原理图备注写明了已删除器件(self) -> None:
        """反过来：备注里**必须**有这句，否则答辩被问"为什么没接 XX"说不清。"""
        self.assertIn("已废止并删除的器件", self.sch_svg)

    def test_今天配置里的引脚号必须出现在引脚图上(self) -> None:
        """逐个核对：配置里实际使用的 BCM，图上的文字必须提到它。

        注意：这张图是**给物理脚号用的**，所以同时检查物理脚号 ——
        同学插线看物理脚，写代码看 BCM（见 hal/pins.py 的文档串）。
        """
        from health_monitor.hal.pins import bcm_to_physical

        required = ["ambient", "motion", "alarm_buzzer", "sos_button",
                    "spo2_button", "status_led", "tft"]
        missing_devices = [n for n in required if n not in self.devices]
        self.assertEqual(missing_devices, [],
                         f"devices.json 里缺少这些器件：{missing_devices}"
                         "（若确实已删除，请同步改本测试与 hardware/ 文档）")

        claims = [
            self.devices["ambient"]["params"]["pin"],
            self.devices["motion"]["params"]["pin"],
            self.devices["alarm_buzzer"]["params"]["pin"],
            self.devices["sos_button"]["params"]["pin"],
            self.devices["spo2_button"]["params"]["pin"],
            self.devices["tft"]["params"]["dc_pin"],
            self.devices["tft"]["params"]["reset_pin"],
        ]
        claims += list(self.devices["status_led"]["params"]["pins"].values())

        missing = []
        for bcm in claims:
            phys = bcm_to_physical(bcm)
            if f"GPIO{bcm}" not in self.pins_svg or f">{phys}<" not in self.pins_svg:
                missing.append(f"GPIO{bcm}（物理脚 {phys}）")
        self.assertEqual(missing, [], f"引脚图里没有这些引脚：{missing}")

    def test_LED只有绿黄两色(self) -> None:
        """红灯已撤：图上若还写"红"就会让人多插一根线。"""
        led = self.devices["status_led"]["params"]["pins"]
        self.assertEqual(sorted(led), ["green", "yellow"],
                         "配置里的 LED 颜色变了 —— 请同步检查接线图与 hardware/ 文档")
        self.assertNotIn("LED red", self.pins_svg)

    def test_SVG是合法XML且含标题(self) -> None:
        """结构级：能被解析（否则浏览器里就是一片空白，且没人会注意到）。"""
        import xml.etree.ElementTree as ET

        for name in ("接线图-引脚.svg", "接线图-原理.svg"):
            path = _HW / name
            try:
                root = ET.parse(str(path)).getroot()
            except ET.ParseError as exc:  # noqa: PERF203
                self.fail(f"{name} 不是合法 XML：{exc}")
            self.assertTrue(root.tag.endswith("svg"), f"{name} 的根元素不是 <svg>")
            title = root.find("{http://www.w3.org/2000/svg}title")
            self.assertIsNotNone(title, f"{name} 缺少 <title>")

    def test_图里没有脚本或外链(self) -> None:
        """交付/打印用：不许有 <script>，也不许引用外部资源（断网也要能看）。"""
        for name in ("接线图-引脚.svg", "接线图-原理.svg"):
            text = _svg(name)
            self.assertNotIn("<script", text, f"{name} 含脚本")
            self.assertNotRegex(text, r'href="(?!#)', f"{name} 引用了外部资源")


class TestSchematicIsCurrent(unittest.TestCase):
    """★ 端口对端口原理图（2026-10-27 按用户要求新增）。

    用户口径：「我要原理图……就是那种端口对端口的」。所以这张图必须
    **同时画出两端**：树莓派 40 个物理脚 + 器件自己的引脚名（VIN/SDA/OUT…），
    每根线从 Pi 的脚连到器件的引脚。只画"器件卡片写着 VCC→脚2"不算原理图。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.svg = (_HW / _SCHEMATIC).read_text(encoding="utf-8")
        cls.devices = _config_devices()

    def test_原理图存在且不过期(self) -> None:
        self.assertTrue((_HW / _SCHEMATIC).exists(), "缺少 原理图.svg")
        proc = _run_gen(["--check"], script=_GEN_SCH)
        self.assertEqual(
            proc.returncode, 0,
            "原理图已过期，请重新生成：\n"
            "    python hardware/tools/gen_schematic.py\n"
            f"脚本输出：\n{proc.stdout}\n{proc.stderr}",
        )

    def test_树莓派侧画出了全部40个物理脚(self) -> None:
        """端口对端口的前提：Pi 这一端要有「端口」。少画脚号就等于少了端点。"""
        missing = []
        for phys in range(1, 41):
            # pin numbers are rendered as their own <text> element
            if f">{phys}</text>" not in self.svg:
                missing.append(phys)
        self.assertEqual(missing, [], f"原理图缺少这些物理脚号：{missing}")

    def test_器件侧画出了自己的引脚名(self) -> None:
        """每个器件都要有它**自己的**引脚名，否则连线没有终点。"""
        expected = {
            "MAX30102": ["VIN", "GND", "SDA", "SCL", "INT"],
            "LCD1602": ["VCC", "GND", "SDA", "SCL"],
            "DHT11": ["VCC", "DATA", "GND"],
            "HC-SR501": ["VCC", "OUT", "GND"],
            "TFT": ["VCC", "GND", "SCK", "SDA", "CS", "DC", "RST"],
            "蜂鸣器": ["+"],
        }
        drawn = set()
        import re
        for m in re.finditer(r">([^<>]+)</text>", self.svg):
            drawn.add(m.group(1))
        missing = {k: [p for p in pins if p not in drawn]
                   for k, pins in expected.items()}
        missing = {k: v for k, v in missing.items() if v}
        self.assertEqual(missing, {}, f"这些器件的引脚名没画出来：{missing}")

    def test_每个配置引脚都作为网标出现(self) -> None:
        """★ 核心判据：配置里的 BCM 必须都能在图上找到对应网名（GPIOxx）。"""
        claims = [
            self.devices["ambient"]["params"]["pin"],
            self.devices["motion"]["params"]["pin"],
            self.devices["alarm_buzzer"]["params"]["pin"],
            self.devices["sos_button"]["params"]["pin"],
            self.devices["spo2_button"]["params"]["pin"],
            self.devices["tft"]["params"]["dc_pin"],
            self.devices["tft"]["params"]["reset_pin"],
        ] + list(self.devices["status_led"]["params"]["pins"].values())

        missing = [f"GPIO{b}" for b in claims if f"GPIO{b}" not in self.svg]
        self.assertEqual(missing, [], f"原理图里没有这些引脚的网标：{missing}")

    def test_I2C与SPI的固定功能网标在图上(self) -> None:
        """总线脚不在 devices.json 里（内核固定功能），必须由图画出来。"""
        for net in ("SDA1", "SCL1", "SCLK", "MOSI", "CE1"):
            self.assertIn(net, self.svg, f"原理图缺少总线网标 {net}")

    def test_限流电阻按代码要求画出(self) -> None:
        """蜂鸣器 + 绿/黄 LED = 恰好 3 个限流电阻（红灯已撤，不该有第 4 个）。"""
        self.assertEqual(self.svg.count(">220R<"), 3,
                         "限流电阻数量与预期不符（器件增减时请同步更新本断言）")

    def test_已删除器件不许出现(self) -> None:
        for ghost in ("HC-SR04", "蓝牙音箱", "TMP36", "MCP3002"):
            self.assertNotIn(ghost, self.svg, f"原理图里还画着已删除的器件：{ghost}")
        self.assertNotIn("GPIO12", self.svg, "原理图里还有已撤销的红色 LED（GPIO12）")

    def test_原理图是黑白线稿(self) -> None:
        """用户选定"传统电气原理图风格"：不许有大面积彩色填充（打印用）。"""
        import re

        fills = re.findall(r'fill="(#[0-9a-fA-F]{6})"', self.svg)
        colourful = [f for f in fills
                     if f.lower() not in ("#ffffff", "#111111", "#333333",
                                          "#8a8a8a", "#d8d8d8")]
        self.assertEqual(colourful[:5], [], f"原理图出现彩色填充：{set(colourful)}")

    def test_原理图是LF线尾(self) -> None:
        raw = (_HW / _SCHEMATIC).read_bytes()
        self.assertEqual(raw.count(b"\r\n"), 0, f"{_SCHEMATIC} 含 CRLF（CI 会假红）")


class TestNoVolatileInput(unittest.TestCase):
    """★ 渲染不许含"会自己变"的东西（2026-10-29 实测踩到，CI #124 因此红）。

    **现象**：三张图都印了生成日期，而门禁 ``--check`` 是"重新渲染 + 逐字节比对"。
    于是**第二天 CI 必红** —— 尽管图上每个引脚都对。本机当时没重跑 ``--check``，
    所以表现为"本机绿、隔天 CI 红"。

    **根因**：门禁回答的是"图与**代码**是否同步"，而日期是**时间**的函数，
    不是代码的函数。把时间写进被比对的内容 ⇒ 判据自带过期时间。

    **结论**：生成时间由 git 记录（``git log -- hardware/原理图.svg``），
    不允许进入渲染内容；版本号可以，因为它只在人改它时才变。
    """

    def test_渲染结果里没有日期或时间戳(self) -> None:
        import re

        for name in ("接线图-引脚.svg", "接线图-原理.svg", "原理图.svg"):
            text = (_HW / name).read_text(encoding="utf-8")
            found = re.findall(r"\b(?:19|20)\d{2}-\d{2}-\d{2}\b", text)
            self.assertEqual(
                found[:3], [],
                f"{name} 里出现了日期时间戳 —— --check 会在第二天假红。"
                "生成时间请交给 git 记录。",
            )
            self.assertNotIn("生成日期", text, f"{name} 里出现了「生成日期」")

    def test_渲染是日期无关的(self) -> None:
        """★ 直接注入"未来日期"，断言渲染结果逐字节不变。

        只断言"图里没有日期字符串"不够 —— 时间还可能以别的形式影响输出。
        """
        import os

        probe = (
            "import sys, datetime;"
            "sys.path.insert(0, 'rpi'); sys.path.insert(0, 'hardware/tools');"
            "import gen_wiring_svg as G, gen_schematic as S;"
            "before = dict(G.render_all()); before.update(S.render_all());"
            "datetime.date = type('D', (datetime.date,),"
            " {'today': classmethod(lambda c: c(2030, 12, 31))});"
            "after = dict(G.render_all()); after.update(S.render_all());"
            "print('SAME' if before == after else 'DIFF: ' + "
            "','.join(k for k in before if before[k] != after[k]))"
        )
        env = dict(os.environ)
        env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
        proc = subprocess.run([sys.executable, "-c", probe], cwd=str(_PROJECT_ROOT),
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=env)
        out = (proc.stdout or "").strip()
        if not out:
            # 探针跑不起来（语法错/导入失败）也要说清楚 —— 否则只会看到一句无信息的
            # "SAME not found in ''"，分不清是"渲染变了"还是"探针本身坏了"。
            self.fail("日期无关性探针没有输出，可能生成器改坏了语法：\n"
                      + (proc.stderr or "")[-800:])
        self.assertIn("SAME", out, f"渲染结果随日期变化（{out}）—— --check 会隔天假红。")


class TestContractWithDocs(unittest.TestCase):
    """接线图与既有文字文档的关系：图是"照着接线"的主入口，必须被索引进。"""

    def test_hardware_README登记了三张图(self) -> None:
        readme = (_HW / "README.md").read_text(encoding="utf-8")
        for name in ("接线图-引脚.svg", "接线图-原理.svg", "原理图.svg"):
            self.assertIn(name, readme, f"hardware/README.md 没有登记 {name}")

    def test_生成脚本自己写明了来源(self) -> None:
        """脚本要能自解释：读的是哪个文件、产出什么。"""
        src = _GEN.read_text(encoding="utf-8")
        self.assertIn("devices.json", src)
        self.assertIn("describe()", src)

    def test_中文只允许出现在字符串字面量里(self) -> None:
        """项目纪律的**本脚本口径**：非 ASCII 只许是"要被画进 SVG 的中文文案"。

        为什么不是"整个文件纯 ASCII"：这个脚本的**唯一职责**就是产出中文接线图，
        中文必然要写在源码里。真正的风险是"注释/变量名/路径里混进中文"——
        那在 GBK 控制台上可能打不出来，且容易在复制粘贴时变成乱码。

        判据：**标识符与运算符**（NAME / OP / NUMBER）里不许出现非 ASCII。
        字符串字面量（含 f-string 的各段碎片）与注释一律放行 —— 注释本来就允许中文。
        """
        import io
        import tokenize

        src = _GEN.read_text(encoding="utf-8")
        checked = {tokenize.NAME, tokenize.OP, tokenize.NUMBER}
        bad: List[str] = []
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type in checked and not tok.string.isascii():
                bad.append(f"第 {tok.start[0]} 行：{tok.string!r}")

        self.assertEqual(bad, [], "脚本里出现字符串以外的非 ASCII（标识符/运算符？）：\n"
                                  + "\n".join(bad[:10]))


if __name__ == "__main__":
    unittest.main()
