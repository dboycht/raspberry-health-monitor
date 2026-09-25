#!/usr/bin/env python3
"""接线表与接线事实的测试（**不依赖硬件**）。

这里测的是一类"隐蔽但后果严重"的问题：**文档里的数字与代码不一致**。
同学照着表插线，插错了要排查一整晚，而代码测试全绿 —— 所以必须机器盯着。

判据分四层：
1. **事实自洽**：引脚 / 电源 / 地 / 上拉 / 采样周期之间不能互相矛盾（`wire_spec.self_check`）；
2. **表 == 代码**：磁盘上的 `接线表.md` 必须与生成结果**逐字符一致**（`wire_docs.check_all`）；
3. **只有一张表**：硬件目录里不许再长出第二份文档（用户明确要求："不要一堆文档"）；
4. **检查器有效**：往文本里注入错误，检查器**必须**报警（`wire_docs.self_test`）
   —— 否则"检查通过"只是虚假的安全感。
"""

from __future__ import annotations

import shutil
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
        """接线表里的数据脚必须**从驱动签名读出来**，不是手写的数字。"""
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


class TestWiringTableDocument(unittest.TestCase):
    """★ `basic/hardware/接线表.md`：**只有两张表**，是上机照着插的唯一依据。"""

    @staticmethod
    def _text() -> str:
        return (wire_docs.HW_DIR / wire_docs.DOC_NAME).read_text(encoding="utf-8")

    @staticmethod
    def _tables(text: str) -> list:
        """返回文档里的表格（每个 = 表头行 + 分隔行 + 数据行）。"""
        lines = text.splitlines()
        tables = []
        index = 0
        while index < len(lines) - 1:
            header = lines[index]
            sep = lines[index + 1]
            is_sep = header.startswith("|") and set(sep.replace("|", "").replace(" ", "")) <= {":", "-"}
            if is_sep:
                block = [header, sep]
                cursor = index + 2
                while cursor < len(lines) and lines[cursor].startswith("|"):
                    block.append(lines[cursor])
                    cursor += 1
                tables.append(block)
                index = cursor
            else:
                index += 1
        return tables

    def test_文档与代码逐字符一致(self):
        problems = wire_docs.check_all()
        self.assertEqual(problems, [], "接线表与代码不一致（改了代码没重新生成，或手改了）")

    def test_检查器自测能抓到注入的错误(self):
        """★ 守卫必须**真的有效**：注入的每类错误都得被抓到。"""
        self.assertEqual(wire_docs.self_test(), [])

    def test_只保留一张接线表(self):
        """用户要求："不要一堆文档" —— 硬件目录里只允许这一份 md。"""
        md_files = sorted(p.name for p in wire_docs.HW_DIR.glob("*.md"))
        self.assertEqual(md_files, [wire_docs.DOC_NAME], f"硬件目录里的 md 应当只有接线表：{md_files}")
        self.assertEqual(wire_docs.check_no_extra_docs(), [])

    def test_恰好两张接线表(self):
        """**这份文档的卖点就是"只有两张接线表"**：多了说明又长回了细节文档。

        ⚠️ 只数"接线"那两张：第 4 节是**参数速查表**（采样周期/量程/CSV 表头），
        它不是接线表，但也不能把"只数前两张"写成脆弱断言 —— 这里按章节标题切片。
        """
        text = self._text()
        wiring_part = text.split("## 4.", 1)[0] if "## 4." in text else text
        self.assertEqual(len(self._tables(wiring_part)), 2,
                         "接线部分应当只有两张表（树莓派接线 / 元件接线）")

    def test_两张表的表头就是那两件事(self):
        text = self._text()
        wiring_part = text.split("## 4.", 1)[0] if "## 4." in text else text
        tables = self._tables(wiring_part)
        self.assertIn("树莓派接线", text)
        self.assertIn("元件接线", text)
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
        """表 1 只该有本基础版真正要接的 3 个脚 —— 多出来的脚会让"照着插"变含糊。"""
        table = self._tables(self._text())[0]
        body = "\n".join(table[2:])
        used = {w.physical for w in wire_spec.wires()}
        for physical in range(1, 41):
            if physical in used:
                continue
            self.assertNotIn(f"| **{physical}** |", body, f"表 1 里出现了未使用的物理脚 {physical}")

    def test_关键事实都在表里(self):
        """物理脚号 / 供电 / 上拉 / 采样周期 / CSV 表头 这些数字必须真的出现。"""
        text = self._text()
        self.assertEqual(wire_docs.check_required_facts(text), [])
        self.assertIn(f"物理脚 {wire_spec.DATA_PHYSICAL}", text)
        self.assertIn(wire_spec.pullup_text(), text)

    def test_引用的脚本都真实存在(self):
        self.assertEqual(wire_docs.check_referenced_paths(self._text()), [])

    def test_文档是LF换行(self):
        """生成物必须显式用 LF：Windows 默认 CRLF 会与 .gitattributes 打架。"""
        raw = (wire_docs.HW_DIR / wire_docs.DOC_NAME).read_bytes()
        self.assertNotIn(b"\r\n", raw, "接线表里有 CRLF（生成器应显式写 LF）")

    def test_生成是幂等的(self):
        """同样的代码生成两次，结果必须一模一样（否则 diff 会永远在抖）。"""
        with tempfile.TemporaryDirectory() as tmp:
            first = wire_docs.generate_all(Path(tmp))
            before = {path.name: path.read_text(encoding="utf-8") for path in first}
            wire_docs.generate_all(Path(tmp))
            after = {path.name: path.read_text(encoding="utf-8") for path in first}
            self.assertEqual(before, after)

    def test_没有markdown标记泄漏或占位符(self):
        text = self._text()
        self.assertNotIn("TODO", text, "接线表里有未完成的 TODO")
        self.assertNotIn("None", text, "接线表里渲染出了 None")
        self.assertNotIn("::", text, "RST 式 `::` 不是 markdown，会出现排版怪相")


class TestPrintArtifacts(unittest.TestCase):
    """打印产物（PDF）与清单：**打印出来的表和正文必须是同一版**。"""

    def test_打印清单存在且被校验(self):
        self.assertTrue((wire_docs.HW_DIR / wire_docs.MANIFEST_NAME).exists())
        self.assertEqual(wire_docs.check_print_sync(), [])

    def test_PDF存在且像真的(self):
        pdf = wire_docs.HW_DIR / wire_docs.PDF_NAME
        self.assertTrue(pdf.exists(), f"缺少打印产物 {pdf}（用 scripts/md2pdf.cjs 导出）")
        self.assertGreater(pdf.stat().st_size, 10_000, "PDF 太小，像渲染失败")

    def test_中间HTML不留在目录里(self):
        """HTML 只是导出 PDF 的中间产物：留在目录里就是"一堆乱七八糟的文件"之一。"""
        self.assertFalse((wire_docs.HW_DIR / wire_docs.HTML_NAME).exists(),
                         "硬件目录里不该留 HTML 中间产物（导出时用 --html 指到临时目录）")

    def test_打印产物被改动必须被抓到(self):
        """★ 守卫有效性：把 PDF 换成别的内容，检查器**必须**报警。"""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            shutil.copy2(wire_docs.HW_DIR / wire_docs.DOC_NAME, target / wire_docs.DOC_NAME)
            shutil.copy2(wire_docs.HW_DIR / wire_docs.PDF_NAME, target / wire_docs.PDF_NAME)
            wire_docs.generate_all(target)                  # 重建清单（记录当前 PDF 哈希）
            self.assertEqual(wire_docs.check_print_sync(target), [])
            (target / wire_docs.PDF_NAME).write_bytes(b"%PDF-1.4 fake")   # 篡改
            problems = wire_docs.check_print_sync(target)
            self.assertTrue(problems, "打印产物被改却没人报警 —— 守卫形同虚设")
            self.assertIn("不一致", problems[0])


class TestNoExtraDocuments(unittest.TestCase):
    """★ 守卫有效性：多出一份 md 必须被抓到；只有接线表时不许误报。"""

    def test_多余文档会被抓到(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / wire_docs.DOC_NAME).write_text("# 接线表\n", encoding="utf-8", newline="\n")
            self.assertEqual(wire_docs.check_no_extra_docs(target), [], "只有接线表时误报了")
            (target / "09-多余的.md").write_text("# 多余\n", encoding="utf-8", newline="\n")
            problems = wire_docs.check_no_extra_docs(target)
            self.assertTrue(problems, "多出一份 md 却没被抓到 —— 守卫失效")
            self.assertIn("09-多余的.md", problems[0])

    def test_归档子目录是允许的(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / wire_docs.DOC_NAME).write_text("# 接线表\n", encoding="utf-8", newline="\n")
            (target / wire_docs.ARCHIVE_DIR_NAME).mkdir()
            self.assertEqual(wire_docs.check_no_extra_docs(target), [])


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
