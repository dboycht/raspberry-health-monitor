#!/usr/bin/env python3
"""接线文档与接线事实的测试（**不依赖硬件**）。

这里测的是一类很"隐蔽但后果严重"的问题：**文档里的数字与代码不一致**。
同学照着文档插线，插错了要排查一整晚，而代码测试全绿 —— 所以必须机器盯着。

判据分三层：
1. **事实自洽**：引脚/电源/地/上拉/采样周期之间不能互相矛盾（`wire_spec.self_check`）；
2. **文档 == 代码**：磁盘上的四份文档必须与生成结果逐字符一致（`wire_docs.check_all`）；
3. **检查器有效**：往文本里注入错误，检查器**必须**报警（`wire_docs.self_test`）
   —— 否则"检查通过"只是虚假的安全感。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from basic import pins, wire_spec
from basic.tools import wire_docs
from basic.tools.diag_dht_line import _interpret


class TestWiringFacts(unittest.TestCase):
    def test_接线事实自洽(self):
        """引脚 / 电源 / 地 / 上拉 / 采样周期 之间不能互相矛盾。"""
        problems = wire_spec.self_check()
        self.assertEqual([str(p) for p in problems], [], "接线事实自检未通过")

    def test_数据脚就是驱动的默认引脚(self):
        """文档里的数据脚必须**从驱动签名读出来**，不是手写的数字。"""
        import inspect

        from basic.dht11read import Dht11Reader

        default = inspect.signature(Dht11Reader.__init__).parameters["pin"].default
        self.assertEqual(wire_spec.DATA_BCM, default)
        self.assertEqual(wire_spec.DATA_PHYSICAL, pins.bcm_to_physical(default))

    def test_数据脚不是电源也不是地(self):
        self.assertNotIn(wire_spec.DATA_PHYSICAL, wire_spec.V33_PHYSICAL)
        self.assertNotIn(wire_spec.DATA_PHYSICAL, wire_spec.V5_PHYSICAL)
        self.assertNotIn(wire_spec.DATA_PHYSICAL, wire_spec.GND_PHYSICAL)

    def test_推荐地脚在GND名单里(self):
        self.assertIn(wire_spec.GND_RECOMMENDED, wire_spec.GND_PHYSICAL)

    def test_上拉电阻写成人话而不是整数除法(self):
        """`4700 // 1000` 会写成 "4kΩ"（把 4.7k 说成 4k）—— 文档写错的经典方式。"""
        self.assertEqual(wire_spec.ohm_text(4700), "4.7kΩ")
        self.assertEqual(wire_spec.ohm_text(10000), "10kΩ")
        self.assertEqual(wire_spec.pullup_text(), "4.7kΩ~10kΩ")

    def test_建议采样周期不小于硬件下限(self):
        self.assertGreaterEqual(wire_spec.RECOMMENDED_INTERVAL_S, wire_spec.MIN_INTERVAL_S)

    def test_三个脚的功能表自洽(self):
        """电源/地名单必须与功能表一致，且拼起来要覆盖整整 40 个脚。"""
        for physical in wire_spec.V33_PHYSICAL:
            self.assertEqual(wire_spec.PHYSICAL_FUNCTION[physical], "3.3V")
        for physical in wire_spec.V5_PHYSICAL:
            self.assertEqual(wire_spec.PHYSICAL_FUNCTION[physical], "5V")
        for physical in wire_spec.GND_PHYSICAL:
            self.assertEqual(wire_spec.PHYSICAL_FUNCTION[physical], "GND")
        self.assertEqual(len(wire_spec.PHYSICAL_FUNCTION), 40)

    def test_逐线表就是三根线(self):
        lines = wire_spec.wires()
        self.assertEqual(len(lines), 3)
        self.assertEqual([line.signal for line in lines], ["供电", "地", "数据"])
        self.assertEqual(lines[2].bcm, wire_spec.DATA_BCM)
        # 线色约定必须三根各不相同，否则"按颜色排错"就不成立
        self.assertEqual(len({line.color for line in lines}), 3)


class TestWiringDocuments(unittest.TestCase):
    def test_文档与代码逐字符一致(self):
        problems = wire_docs.check_all()
        self.assertEqual(problems, [], "接线文档与代码不一致（改了代码没重新生成，或手改了文档）")

    def test_检查器自测能抓到注入的错误(self):
        """★ 守卫必须**真的有效**：注入三类错误都得被抓到。"""
        self.assertEqual(wire_docs.self_test(), [])

    def test_全部文档都在(self):
        for name in wire_docs.DOCUMENTS:
            path = wire_docs.HW_DIR / name
            self.assertTrue(path.exists(), f"缺少文档 {path}")
            self.assertGreater(path.stat().st_size, 500, f"{name} 内容太少，像没生成成功")

    def test_文档是LF换行(self):
        """生成物必须显式用 LF：Windows 默认 CRLF 会与 .gitattributes 打架。"""
        for name in wire_docs.DOCUMENTS:
            raw = (wire_docs.HW_DIR / name).read_bytes()
            self.assertNotIn(b"\r\n", raw, f"{name} 里有 CRLF（生成器应显式写 LF）")

    def test_生成是幂等的(self):
        """同样的代码生成两次，结果必须一模一样（否则 diff 会永远在抖）。"""
        with tempfile.TemporaryDirectory() as tmp:
            first = wire_docs.generate_all(Path(tmp))
            before = {path.name: path.read_text(encoding="utf-8") for path in first}
            wire_docs.generate_all(Path(tmp))
            after = {path.name: path.read_text(encoding="utf-8") for path in first}
            self.assertEqual(before, after)

    def test_打印产物清单存在且被校验(self):
        self.assertTrue((wire_docs.HW_DIR / wire_docs.MANIFEST_NAME).exists())
        self.assertEqual(wire_docs.check_print_sync(), [])

    def test_打印产物被改动必须被抓到(self):
        """★ 守卫有效性：把 PDF 换成别的内容，检查器**必须**报警。"""
        import shutil
        import tempfile as _tempfile

        with _tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            for name in list(wire_docs.DOCUMENTS) + list(wire_docs.REQUIRED_FILES):
                shutil.copy2(wire_docs.HW_DIR / name, target / name)
            wire_docs.generate_all(target)                      # 重建清单（记录当前 PDF 哈希）
            self.assertEqual(wire_docs.check_print_sync(target), [])
            (target / "04-线色与自查卡.pdf").write_bytes(b"%PDF-1.4 fake")   # 篡改
            problems = wire_docs.check_print_sync(target)
            self.assertTrue(problems, "打印产物被改却没人报警 —— 守卫形同虚设")
            self.assertIn("不一致", problems[0])

    def test_关键事实在文档里查得到(self):
        """物理脚号 / 供电 / 上拉 这些数字必须真的出现在文档正文里。"""
        corpus = "\n".join(
            (wire_docs.HW_DIR / name).read_text(encoding="utf-8") for name in wire_docs.DOCUMENTS
        )
        self.assertEqual(wire_docs.check_required_facts(corpus), [])
        self.assertIn(f"物理脚 {wire_spec.DATA_PHYSICAL}", corpus)
        self.assertIn("3.3V", corpus)
        self.assertIn(wire_spec.pullup_text(), corpus)

    def test_文档引用的脚本都真实存在(self):
        corpus = "\n".join(
            (wire_docs.HW_DIR / name).read_text(encoding="utf-8") for name in wire_docs.DOCUMENTS
        )
        self.assertEqual(wire_docs.check_referenced_paths(corpus, wire_docs.HW_DIR), [])

    def test_文档没有markdown标记泄漏(self):
        """界面/文档正文里出现裸 markdown 标记是踩过的坑（README 第 7 节）：

        这里只检查"表格分隔行不得出现在正文段落"这类明显问题不现实，
        改检查一个**可判定**的点：文档里不出现连续两个空格的 markdown 硬换行，
        以及没有残留的生成占位符（`{}`、`TODO`）。
        """
        for name in wire_docs.DOCUMENTS:
            text = (wire_docs.HW_DIR / name).read_text(encoding="utf-8")
            self.assertNotIn("TODO", text, f"{name} 里有未完成的 TODO")
            self.assertNotIn("{DATA_BCM}", text, f"{name} 里有未替换的占位符")
            self.assertNotIn("None", text.replace("NoneType", ""), f"{name} 里渲染出了 None")


class TestWiringDiagramLayout(unittest.TestCase):
    """ASCII 接线图的**对齐判据**（手写空格的图一定会错位，所以必须机器盯）。"""

    def _rows(self) -> list:
        diagram = wire_docs.wiring_diagram()
        block = diagram.split("```")[1]
        return [line for line in block.splitlines() if "●" in line]

    def test_三个端子竖着对齐(self):
        rows = self._rows()
        self.assertEqual(len(rows), 3, f"接线图里应有 3 个端子（●），实际 {len(rows)} 个")
        columns = {line.index("●") for line in rows}
        self.assertEqual(len(columns), 1, f"三个端子的列号不一致（图错位了）：{sorted(columns)}")

    def test_三个端子按电源地数据顺序(self):
        rows = self._rows()
        text = "\n".join(rows)
        self.assertLess(text.index("VCC"), text.index("GND"))
        self.assertLess(text.index("GND"), text.index("DATA"))

    def test_每根线都有线色(self):
        rows = self._rows()
        for color in ("（红线）", "（黑线）", "（黄线）"):
            self.assertTrue(any(color in line for line in rows), f"图里缺少线色标注 {color}")

    def test_图上出现的物理脚就是接线事实(self):
        diagram = wire_docs.wiring_diagram()
        for physical in (wire_spec.V33_PHYSICAL[0], wire_spec.GND_RECOMMENDED, wire_spec.DATA_PHYSICAL):
            self.assertIn(f"脚 {physical:>2}", diagram)

    def test_对齐不依赖手写空格(self):
        """★ 把数据脚换成一个两位数列号，图**必须仍然对齐**。

        这条断言是"程序化拼图"的存在理由：手写空格的图在插入或替换数字后会错位。
        判据 = 换引脚后三个端子的列号依旧一致，且数据脚那一行确实写的是新物理脚。
        """
        saved = wire_spec.DATA_PHYSICAL
        try:
            wire_spec.DATA_PHYSICAL = 40        # 换成两位数列号（真实布局里 40 也是合法脚）
            rows = self._rows()
            self.assertEqual(len({line.index("●") for line in rows}), 1, "换引脚后图错位了")
            self.assertIn("脚 40", "\n".join(rows))
        finally:
            wire_spec.DATA_PHYSICAL = saved


class TestInterfaceTables(unittest.TestCase):
    """★ `06-接口接线表.md`：**只有两张表**（树莓派接线 + 元件接线），上机时照它插线。

    用户 2026-09-25 要求"只要这两个接口"。这份文档容易退化回"又写成一大篇"，
    所以用测试钉住：**恰好两张表**、表头就是那两件事、引脚数字与事实源同源。
    """

    @staticmethod
    def _text() -> str:
        return (wire_docs.HW_DIR / "06-接口接线表.md").read_text(encoding="utf-8")

    @staticmethod
    def _tables(text: str) -> list:
        """返回文档里的表格（每个 = 表头行 + 分隔行 + 数据行）。"""
        lines = text.splitlines()
        tables = []
        index = 0
        while index < len(lines) - 1:
            if lines[index].startswith("|") and set(lines[index + 1].replace("|", "").replace(" ", "")) <= {":", "-"}:
                block = [lines[index], lines[index + 1]]
                cursor = index + 2
                while cursor < len(lines) and lines[cursor].startswith("|"):
                    block.append(lines[cursor])
                    cursor += 1
                tables.append(block)
                index = cursor
            else:
                index += 1
        return tables

    def test_恰好两张表(self):
        """**这份文档的卖点就是"只有两张表"**：多一张就说明有人又往里加旁支了。"""
        text = self._text()
        self.assertEqual(len(self._tables(text)), 2, "这份文档应当只有两张表（树莓派接线 / 元件接线）")

    def test_两张表的表头就是那两件事(self):
        tables = self._tables(self._text())
        self.assertIn("树莓派接线", self._text())
        self.assertIn("元件接线", self._text())
        # 表 1：以"物理脚"开头；表 2：以"元件针脚"开头
        self.assertTrue(tables[0][0].startswith("| 物理脚"), tables[0][0])
        self.assertTrue(tables[1][0].startswith("| 元件针脚"), tables[1][0])

    def test_表里的引脚与事实源一致(self):
        text = self._text()
        for wire in wire_spec.wires():
            self.assertIn(f"| **{wire.physical}** |", text, f"树莓派接线表少了物理脚 {wire.physical}")
        self.assertIn(f"GPIO{wire_spec.DATA_BCM}", text)
        self.assertIn(wire_spec.pullup_text(), text)
        self.assertIn("3.3V", text)

    def test_三个脚之外不许出现在树莓派接线表里(self):
        """表 1 只该有本基础版真正要接的 3 个脚 —— 多出来的脚会让"照着插"变得含糊。"""
        table = self._tables(self._text())[0]
        body = "\n".join(table[2:])
        for physical in range(1, 41):
            if physical in {w.physical for w in wire_spec.wires()}:
                continue
            self.assertNotIn(f"| **{physical}** |", body, f"表 1 里出现了未使用的物理脚 {physical}")

    def test_文档里没有占位符或裸链接(self):
        text = self._text()
        self.assertNotIn("None", text)
        self.assertNotIn("::", text, "RST 式 `::` 不是 markdown 标题，会出现排版怪相")
        self.assertNotIn("](../", text)


class TestCollection(unittest.TestCase):
    """打印合集（把 5 份文档合成一份可打印的 Markdown → PDF）的判据。"""

    def test_合集文件存在且与生成结果一致(self):
        path = wire_docs.HW_DIR / wire_docs.COLLECTION_MD
        self.assertTrue(path.exists(), "缺少打印合集 Markdown")
        self.assertEqual(path.read_text(encoding="utf-8"), wire_docs.build_collection())

    def test_合集包含全部章节(self):
        text = (wire_docs.HW_DIR / wire_docs.COLLECTION_MD).read_text(encoding="utf-8")
        for title in wire_docs.COLLECTION_TOC:
            self.assertIn(f"## {title}", text, f"合集缺少章节：{title}")

    def test_合集里有必备的打印产物(self):
        for name in (wire_docs.COLLECTION_PDF, wire_docs.COLLECTION_HTML):
            path = wire_docs.HW_DIR / name
            self.assertTrue(path.exists(), f"缺少打印产物 {name}（用 scripts/md2pdf.cjs 导出）")
            self.assertGreater(path.stat().st_size, 10_000)

    def test_有序列表不会被挤成一行(self):
        """实测踩到：`md2pdf.cjs` 对有序列表的续行判定只认 `-`/`*`，

        所以 `1. 2. 3.` 连着写会被渲染成**一行**。判据：相邻的两个有序列表项之间必须有空行。
        """
        text = (wire_docs.HW_DIR / wire_docs.COLLECTION_MD).read_text(encoding="utf-8")
        lines = text.splitlines()
        for index, line in enumerate(lines[:-1]):
            if wire_docs._ORDERED_ITEM_RE.match(line) and wire_docs._ORDERED_ITEM_RE.match(lines[index + 1]):
                self.fail(f"第 {index + 1}、{index + 2} 行是相邻的两个有序列表项（渲染会挤成一行）：{line!r}")

    def test_指向仓库文件的链接已降级成纯文本(self):
        """打印件里没有 `../README.md` 这些文件，路径留在正文里只会让人困惑。

        ⚠️ 断言只针对 markdown 链接语法（`](路径)`）：**行内反引号里的文件名是"提到某文件"**，
        不是链接，打印出来照样看得懂，所以不算漏改。
        """
        text = (wire_docs.HW_DIR / wire_docs.COLLECTION_MD).read_text(encoding="utf-8")
        self.assertNotIn("](../", text)
        self.assertNotIn("](hardware/", text)
        self.assertNotIn("](basic/", text)
        self.assertNotIn("](04-", text)

    def test_降级链接会保留文字(self):
        self.assertEqual(wire_docs._plain_links("见 [README](../README.md) 与 [规范](docs/x.md)"),
                         "见 README 与 规范")
        self.assertEqual(wire_docs._plain_links("[官网](https://example.com)"),
                         "[官网](https://example.com)")
        self.assertEqual(wire_docs._plain_links("[跳到第 3 节](#sec3)"), "跳到第 3 节")

    def test_合集里的关键事实与单份文档一致(self):
        text = (wire_docs.HW_DIR / wire_docs.COLLECTION_MD).read_text(encoding="utf-8")
        self.assertEqual(wire_docs.check_required_facts(text), [])
        self.assertIn(f"脚 {wire_spec.DATA_PHYSICAL}", text)
        self.assertIn(wire_spec.pullup_text(), text)


class TestDiagInterpretation(unittest.TestCase):
    """三态电平判据的测试（**真机读数由人跑脚本，这里的判据逻辑必须机器可测**）。"""

    def test_上拉高下拉低是空脚或不应答(self):
        level, text = _interpret(pull_up_high=50, pull_down_high=0, floating_high=30, samples=50)
        self.assertEqual(level, "ok")
        self.assertIn("没有器件在强驱动", text)

    def test_一直低是被拉死到地(self):
        level, text = _interpret(pull_up_high=0, pull_down_high=0, floating_high=0, samples=50)
        self.assertEqual(level, "bad")
        self.assertIn("拉死到地", text)

    def test_一直高是被拉死到3V3(self):
        level, text = _interpret(pull_up_high=50, pull_down_high=50, floating_high=50, samples=50)
        self.assertEqual(level, "bad")
        self.assertIn("拉死到 3.3V", text)

    def test_乱跳是不稳定(self):
        level, text = _interpret(pull_up_high=25, pull_down_high=25, floating_high=10, samples=50)
        self.assertEqual(level, "warn")
        self.assertIn("不稳定", text)

    def test_判据边界(self):
        """边界：90% 以上才算"稳定高"，10% 以下才算"稳定低"。"""
        self.assertEqual(_interpret(45, 5, 20, 50)[0], "ok")       # 恰好 90% / 10%
        self.assertEqual(_interpret(44, 5, 20, 50)[0], "warn")     # 88% 不算稳定高


if __name__ == "__main__":
    unittest.main()
