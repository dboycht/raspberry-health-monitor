"""`message_kind()` 与 `RECORD_ONLY_CODES` 的判据测试（2026-10-01）。

为什么值得单独测一个"分类函数"
------------------------------
它决定后台消息页把一条事件渲染成**报警**还是**记录**，而分错的代价**极不对称**：

* 把报警当成记录 ⇒ **报警被藏起来**（老人出事了，后台只显示一行灰字）；
* 把记录当成报警 ⇒ 只是多显示一点，无伤。

所以 `message_kind()` 的默认分支**必须**落在 ``alarm``，本文件把这条钉死：
将来新增一个"忘了登记"的码，它必须落进 ``alarm``，绝不许悄悄变成"信息"。

同理，`RECORD_ONLY_CODES` 里混进一个真报警是**最危险的一次误操作** ——
那个报警从此**不点灯、不发声、不刷屏**（因为它走"只记录"那条路）。
本文件用一条专门的断言挡住它。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_RPI_DIR = Path(__file__).resolve().parents[2]
if str(_RPI_DIR) not in sys.path:
    sys.path.insert(0, str(_RPI_DIR))

from health_monitor.hal.models import (  # noqa: E402
    AlarmCode,
    RECORD_ONLY_CODES,
    message_kind,
)

#: 真正的"报警"（要下发、会点灯发声的那些）。
REAL_ALARM_CODES = frozenset({
    AlarmCode.HR_TOO_HIGH, AlarmCode.HR_TOO_LOW, AlarmCode.SPO2_TOO_LOW,
    AlarmCode.AMBIENT_TEMP_HIGH, AlarmCode.AMBIENT_TEMP_LOW, AlarmCode.HUMIDITY_HIGH,
    AlarmCode.NO_MOTION_TOO_LONG, AlarmCode.NIGHT_FREQUENT_WAKE,
    AlarmCode.SOS_PRESSED, AlarmCode.SENSOR_FAULT, AlarmCode.DEVICE_OFFLINE,
})


class TestMessageKind(unittest.TestCase):
    def test_记录类码都归到record(self) -> None:
        self.assertTrue(RECORD_ONLY_CODES, "这个集合不该是空的（否则本文件形同虚设）")
        for code in RECORD_ONLY_CODES:
            self.assertEqual(message_kind(code), "record",
                             f"{code.value} 是记录类，消息页要按'记录'显示")

    def test_all_clear归到clear(self) -> None:
        self.assertEqual(message_kind(AlarmCode.ALL_CLEAR), "clear")

    def test_system_start归到info(self) -> None:
        self.assertEqual(message_kind(AlarmCode.SYSTEM_START), "info")

    def test_真正的报警一律归到alarm(self) -> None:
        for code in REAL_ALARM_CODES:
            self.assertEqual(message_kind(code), "alarm",
                             f"{code.value} 是报警，绝不能被弱化成记录/信息")

    def test_没登记的码默认当报警(self) -> None:
        """★ 反向钉子：漏登记只会"多显示"，**绝不把报警藏起来**。

        （这是刻意选的失败方向：将来树莓派加了新码而忘了登记，
        后台最坏是把它显示成一条报警 —— 而不是把它当空气。）
        """
        self.assertEqual(message_kind("brand_new_code_nobody_registered"), "alarm")
        self.assertEqual(message_kind(None), "alarm")
        self.assertEqual(message_kind(""), "alarm")

    def test_字符串与枚举两种入参等价(self) -> None:
        """历史库读出来的是**字符串**，内存里拿到的是**枚举** —— 两条路必须同结果。"""
        for code in list(AlarmCode):
            self.assertEqual(message_kind(code), message_kind(code.value),
                             f"{code.value} 用 str 和用枚举传进来结果不一致")


class TestRecordOnlyInvariants(unittest.TestCase):
    def test_只记录集合里不许混进真报警(self) -> None:
        """★ 最危险的一次误操作：把报警码放进 `RECORD_ONLY_CODES`。

        那样这个报警从此**不点灯、不发声、不刷屏**（走"只记录"那条路），
        而老人出事时最要紧的就是那几秒的声光。这里当面挡住。
        """
        overlap = RECORD_ONLY_CODES & REAL_ALARM_CODES
        self.assertEqual(overlap, frozenset(),
                         f"这些是真报警，绝不许放进 RECORD_ONLY_CODES："
                         f"{sorted(c.value for c in overlap)}")

    def test_只记录集合里的码必须真在枚举里(self) -> None:
        members = set(AlarmCode)
        for code in RECORD_ONLY_CODES:
            self.assertIn(code, members, f"{code} 不在 AlarmCode 里")
