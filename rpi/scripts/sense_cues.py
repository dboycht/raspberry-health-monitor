#!/usr/bin/env python3
"""感官类验收的**提示器**：LCD 出字 + 蜂鸣器叫人。

为什么有它（2026-09-29，用户明确要求）
--------------------------------------
用户在 T7/T9 现场说过两次："**你还是在 LCD 上面给我提示，用蜂鸣器提示我，因为树莓派我放的离电脑远**"、
"**记着每一次这种测试使用蜂鸣器提示我，还有 LCD 指示我**"。

根因是流程问题：代理在远端跑命令，而"看点灯/看屏/听响"的效果**只存在于人的感官里**；
树莓派离电脑远，用户不可能一边看终端一边盯屏。⇒ 约定升级为：

> **凡是"要人看/听"的测试，一律先用 LCD 写明"现在看什么"，并用蜂鸣器把人叫过来**，
> 再开始动作；结束时用另一种蜂鸣方式告诉人"可以看了/结束了"。

暗号（**和人约定好的，别随手改**）
----------------------------------
=================  ==========================================
蜂鸣                 含义
=================  ==========================================
**1 声**（短）       注意，请看屏 / 请看器件
**2 声**             结束，**成功**（去看结果）
**5 声**（急促）     结束，**失败/需要重试**
=================  ==========================================

用法（在需要提示的脚本里）::

    from sense_cues import open_cues, BEEP_ATTENTION, BEEP_DONE
    cues = open_cues(load_config(args.config))
    cues.show("TFT TEST", "LOOK AT SCREEN")
    cues.beep(BEEP_ATTENTION)
    ...
    cues.show("TFT TEST DONE", "TELL THE AGENT")
    cues.beep(BEEP_DONE)
    cues.close()

⚠️ **任何一步失败都不许把测试搞崩**：LCD/蜂鸣打不开就只记一笔（`cues.notes`），继续跑主流程。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, List, Optional, Tuple

# 让 `basic.console.safe_print` 可导入（打印符号时在窄编码控制台上自动降级）
try:
    from pathlib import Path  # noqa: E402
except ImportError:  # pragma: no cover
    Path = None
_ROOT = Path(__file__).resolve().parents[2] if Path is not None else None
if _ROOT is not None and str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
try:
    from basic.console import safe_print  # noqa: E402
except ImportError:  # pragma: no cover - 只在 basic 不可用时
    safe_print = print

_RPI_DIR = Path(__file__).resolve().parents[1]
if str(_RPI_DIR) not in sys.path:
    sys.path.insert(0, str(_RPI_DIR))

#: 蜂鸣暗号：(响几声, 每声多少毫秒)。**这是跟人约好的信号，改动前先问用户**
BEEP_ATTENTION: Tuple[int, int] = (1, 150)
BEEP_DONE: Tuple[int, int] = (2, 150)
BEEP_FAIL: Tuple[int, int] = (5, 120)

#: LCD 每行 16 字符（超长截断，免得显示成半句话）
LCD_WIDTH = 16


def lcd_line(text: str, width: int = LCD_WIDTH) -> str:
    """把一行文字收进 LCD 宽度（超长截断、不足补空格）。"""
    return str(text)[:width].ljust(width)


class Cues:
    """LCD + 蜂鸣的提示器（**任何一个坏了都不许把主流程搞崩**，只记 `notes`）。"""

    def __init__(self, lcd: Any = None, buzzer: Any = None) -> None:
        self.lcd = lcd
        self.buzzer = buzzer
        self.notes: List[str] = []

    def show(self, line1: str, line2: str = "") -> None:
        """在 LCD 上写两行（没有 LCD 就什么都不做）。"""
        if self.lcd is None:
            return
        try:
            from health_monitor.hal.models import DisplayCommand

            self.lcd.send(DisplayCommand(lines=(lcd_line(line1), lcd_line(line2))))
        except Exception as exc:  # noqa: BLE001 - 显示失败不该中断测试
            self.notes.append(f"LCD 写入失败：{type(exc).__name__}: {exc}")
            self.lcd = None

    def beep(self, plan: Tuple[int, int] = BEEP_ATTENTION) -> None:
        """按暗号鸣叫（没有蜂鸣器就什么都不做）。"""
        if self.buzzer is None:
            return
        times, on_ms = plan
        try:
            from health_monitor.hal.models import BeepCommand

            self.buzzer.send(BeepCommand(times=int(times), on_ms=int(on_ms), off_ms=150))
        except Exception as exc:  # noqa: BLE001 - 蜂鸣失败不该中断测试
            self.notes.append(f"蜂鸣失败：{type(exc).__name__}: {exc}")
            self.buzzer = None

    def report(self) -> None:
        """把提示器自己的故障打出来（跑完时调用，便于排查"我没听到"这类反馈）。"""
        for note in self.notes[:5]:
            safe_print(f"  [!] 提示器：{note}")

    def close(self) -> None:
        for dev in (self.buzzer, self.lcd):
            if dev is not None:
                try:
                    dev.close()
                except Exception:  # noqa: BLE001 - 收尾失败不影响结论
                    pass


def open_cues(config: Any, names: Tuple[str, str] = ("display", "alarm_buzzer")) -> Cues:
    """按配置打开 LCD 与蜂鸣器；**打不开哪一个就退回"没有它"**（不抛异常）。"""
    lcd = buzzer = None
    from health_monitor.hal import create_device

    for name, target in zip(names, ("lcd", "buzzer")):
        try:
            cfg = config.device(name)
            if cfg is None:
                raise RuntimeError(f"配置里没有设备 {name}")
            dev = create_device(cfg.driver, params=cfg.params, mock=False, name=cfg.name)
            dev.open()
            if target == "lcd":
                lcd = dev
            else:
                buzzer = dev
        except Exception as exc:  # noqa: BLE001 - 提示器是"尽力而为"
            safe_print(f"  [!] 打开提示器（{name}）失败，继续跑主流程：{type(exc).__name__}: {exc}")
    return Cues(lcd, buzzer)


__all__ = ["Cues", "open_cues", "lcd_line", "BEEP_ATTENTION", "BEEP_DONE", "BEEP_FAIL", "LCD_WIDTH"]
