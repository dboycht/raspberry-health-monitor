#!/usr/bin/env python3
"""T7 现场引导采集：**用 LCD 出提示、用蜂鸣器叫你**，把人机分离也能验收。

为什么要单独有一个（2026-09-29，T7 真机）
------------------------------------------
`hardware_test.py --only vitals` 是"人坐在板子前、看控制台"的姿势。
但本次树莓派离电脑远 ⇒ 用户看不到终端输出，也不方便来回跑。用户原话：

> "你还是在 LCD 上面给我提示，用蜂鸣器提示我，因为树莓派我放的离电脑远"

于是本脚本把**"什么时候放手指、还剩几秒、结果是什么"全部放到 LCD 上**，
用蜂鸣器长短提示"开始/结束/失败"，控制台只留 ASCII 日志给代理远程读。

LCD 提示与蜂鸣含义（**这就是验收时口头要跟人说的那几句**）
----------------------------------------------------------
=================================  ==============================================
LCD 显示                            含义
=================================  ==============================================
``T7 VITALS TEST`` / ``FINGER ON T-30``  倒计时，**请把食指指腹盖住模块小窗口**
``MEASURING   T-20`` / ``HR 72 SPO2 98``  已检测到手指，正在测；下面是实时值
``HR 72 bpm`` / ``SPO2 98 %``        采集结束、结果（同时 **2 声**蜂鸣 = 成功）
``NO READING`` / ``COVER & RERUN``   没读到（同时 **5 声**蜂鸣 = 失败/重试）
=================================  ==============================================

* **1 声**短鸣 = 开始，把手放上去；**2 声** = 成功结束；**5 声** = 没读到（要重试）。
* 手指要**完全盖住**窗口、**轻贴别压**、**保持不动**；环境强光要遮一下。

用法::

    cd ~/raspberry-health-monitor/rpi
    python3 scripts/vitals_check.py                 # 默认 30 秒
    python3 scripts/vitals_check.py --seconds 45    # 给慢热的人更长时间
    python3 scripts/vitals_check.py --no-lcd        # 只打印（调试用）

退出码：0 = 采到有效心率；1 = 跑完但没采到有效读数（WARN，可重试）；
2 = 器件打不开/参数非法（带排查线索）。
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# 让 `basic.console.safe_print` 可导入（打印 ✅/❌/⚠ 时在窄编码控制台上自动降级）
# ⚠️ 为什么（ERROR.md E32/E35/E41）：窄编码控制台上直接打印这些符号会抛
#    UnicodeEncodeError 把**整个脚本**崩掉；本脚本主要跑在树莓派（UTF-8）上。
try:
    from pathlib import Path  # noqa: E402
except ImportError:  # pragma: no cover - Path 是标准库
    Path = None
ROOT = Path(__file__).resolve().parents[2] if Path is not None else None
if ROOT is not None and str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    from basic.console import safe_print  # noqa: E402
except ImportError:  # pragma: no cover - 只在 basic 不可用时
    safe_print = print

RPI_DIR = Path(__file__).resolve().parents[1]
if str(RPI_DIR) not in sys.path:
    sys.path.insert(0, str(RPI_DIR))

from health_monitor.core.config import load_config  # noqa: E402
from health_monitor.hal import create_device  # noqa: E402
from health_monitor.hal.models import BeepCommand, DisplayCommand  # noqa: E402
from sense_cues import (  # noqa: E402
    BEEP_ATTENTION,
    BEEP_DONE,
    BEEP_FAIL,
    Cues as _Cues,          # 提示器统一在 scripts/sense_cues.py（LCD + 蜂鸣暗号一套）
)

#: T7 验收判据（`docs/14` T7：心率 55~100、血氧 95~100）
T7_HR_RANGE: Tuple[float, float] = (55.0, 100.0)
T7_SPO2_RANGE: Tuple[float, float] = (95.0, 100.0)

#: LCD 每行 16 字符（驱动会截断，但这里先自己收干净，免得显示成半句话）
LCD_WIDTH = 16

#: 蜂鸣方案沿用**共享提示器**里的暗号：(响几声, 每声多长 ms)。
#: **这是跟人约好的信号，别随手改**（定义在 `scripts/sense_cues.py`，全项目一套）
BEEP_START = BEEP_ATTENTION


# ==========================================================================
# 纯函数（不碰硬件 ⇒ 可以确定性单测；硬件部分只做"调用它们"）
# ==========================================================================


def lcd_line(text: str, width: int = LCD_WIDTH) -> str:
    """把一行文字收进 LCD 宽度（超长截断、不足补空格），避免显示成半句话。"""
    text = str(text)
    return text[:width].ljust(width)


def _fmt(value: Optional[float], digits: int = 1) -> str:
    """把诊断数值格式化成一行里好看的样子（None 写成 ``--``）。"""
    if value is None:
        return "--"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):  # pragma: no cover - 理论不可达
        return "--"


def _write_csv(path: str, ir: Sequence[int], red: Sequence[int], rate: Optional[float]) -> None:
    """把一次分析窗的原始波形写成 CSV（**LF 换行**，首行是 ``#`` 注释元数据）。

    为什么要落盘：报告里要能拿出"真机实测波形"这件证据，而且质量分低时
    得能离线看清"是信号弱、是饱和、还是节律乱"（`ERROR.md` E52）。
    换行显式用 ``\\\\n`` 而不是 `os.linesep`：本项目在 Windows 上开发、
    提交前检查会比对生成物，CRLF/LF 混着来是会真出问题的（见 DEVELOPMENT.md 的教训）。
    """
    lines = [
        "# raspberry-health-monitor T7 (MAX3010x) raw analysis-window capture",
        f"# samples={len(ir)} analysis_rate_hz={rate}",
        "index,ir,red",
    ]
    for i, value in enumerate(ir):
        red_value = red[i] if i < len(red) else ""
        lines.append(f"{i},{value},{red_value}")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def format_countdown(seconds_left: int, finger: bool, hr: Optional[float], spo2: Optional[float]) -> Tuple[str, str]:
    """倒计时阶段的两行 LCD 内容（**现场就靠这两行指挥人**）。"""
    if finger:
        line1 = f"MEASURING T-{max(0, int(seconds_left)):02d}"
    else:
        line1 = f"FINGER ON T-{max(0, int(seconds_left)):02d}"
    if hr is not None:
        line2 = f"HR {hr:.0f} SPO2 {spo2:.0f}" if spo2 is not None else f"HR {hr:.0f}"
    elif finger:
        line2 = "MEASURING..."
    else:
        line2 = "COVER SENSOR"
    return lcd_line(line1), lcd_line(line2)


def summarise(samples: Sequence[Any]) -> Dict[str, Any]:
    """把一串 ``VitalSignsSample`` 汇总成结论（中位数 + 有效样本数 + 出错原因）。

    ⚠️ 只用"``ok=True`` 且心率非 None"的样本 —— 这正是驱动"**绝不填 0**"契约的意义：
    没贴手指的样本不会被算进中位数，也不会把结果拉成 0。
    """
    valid = [
        s for s in samples
        if getattr(s, "ok", False) and getattr(s, "heart_rate_bpm", None) is not None
    ]
    hrs = [float(s.heart_rate_bpm) for s in valid]
    spo2s = [float(s.spo2_percent) for s in valid if getattr(s, "spo2_percent", None) is not None]
    errors: List[str] = []
    for s in samples:
        err = getattr(s, "error", None)
        if err and err not in errors:
            errors.append(str(err))
    return {
        "total": len(samples),
        "finger_samples": sum(1 for s in samples if getattr(s, "finger_detected", False)),
        "valid": len(valid),
        "hr_median": statistics.median(hrs) if hrs else None,
        "hr_min": min(hrs) if hrs else None,
        "hr_max": max(hrs) if hrs else None,
        "hr_spread": (max(hrs) - min(hrs)) if hrs else None,
        "spo2_median": statistics.median(spo2s) if spo2s else None,
        "spo2_min": min(spo2s) if spo2s else None,
        "spo2_max": max(spo2s) if spo2s else None,
        "errors": errors[:3],
    }


def verdict(summary: Dict[str, Any]) -> Tuple[bool, str]:
    """给结论：**是否通过 T7 器件级判据**（心率 55~100、血氧 95~100）+ 一行说明。"""
    if not summary["valid"]:
        if summary["finger_samples"]:
            return False, "检测到手指但没有算出有效心率（数据质量不足，可重试）"
        return False, "始终没检测到手指（finger_detected=False）——先确认手指盖住了窗口"
    hr = summary["hr_median"]
    spo2 = summary["spo2_median"]
    problems: List[str] = []
    if not T7_HR_RANGE[0] <= hr <= T7_HR_RANGE[1]:
        problems.append(f"心率中位数 {hr:.1f} 不在 {T7_HR_RANGE[0]:.0f}~{T7_HR_RANGE[1]:.0f}")
    if spo2 is None:
        problems.append("没有血氧读数")
    elif not T7_SPO2_RANGE[0] <= spo2 <= T7_SPO2_RANGE[1]:
        problems.append(f"血氧中位数 {spo2:.1f} 不在 {T7_SPO2_RANGE[0]:.0f}~{T7_SPO2_RANGE[1]:.0f}")
    if problems:
        return False, "；".join(problems)
    return True, f"心率 {hr:.1f} bpm、血氧 {spo2:.1f} %（均落在 T7 判据内）"


def result_lines(summary: Dict[str, Any], passed: bool) -> Tuple[str, str]:
    """结束时写在 LCD 上的两行（人远看就能读懂）。"""
    if passed:
        return (
            lcd_line(f"HR {summary['hr_median']:.0f} bpm"),
            lcd_line(f"SPO2 {summary['spo2_median']:.0f} %"),
        )
    if summary["valid"]:
        return lcd_line("OUT OF RANGE"), lcd_line("CHECK / RERUN")
    return lcd_line("NO READING"), lcd_line("COVER & RERUN")


# ==========================================================================
# 硬件部分（薄薄一层：打开器件 → 循环读数 → 写 LCD → 蜂鸣）
# ==========================================================================


def _open(config: Any, name: str) -> Any:
    cfg = config.device(name)
    if cfg is None:
        raise RuntimeError(f"配置里没有设备 {name}（检查 rpi/config/devices.json）")
    device = create_device(cfg.driver, params=cfg.params, mock=False, name=cfg.name)
    device.open()
    return device


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="T7 心率血氧现场引导采集（LCD 提示 + 蜂鸣提示）")
    parser.add_argument("--config", default=None, help="配置文件路径（默认 config/devices.json）")
    parser.add_argument("--seconds", type=int, default=30, help="采集多少秒（默认 30）")
    parser.add_argument("--no-lcd", action="store_true", help="不写 LCD（只打印，调试用）")
    parser.add_argument("--no-beep", action="store_true", help="不鸣叫")
    parser.add_argument("--dump-csv", default=None, metavar="PATH",
                        help="把最后一次分析窗的原始波形写成 CSV（证据/离线分析用）")
    args = parser.parse_args(argv)

    seconds = max(5, int(args.seconds))
    safe_print("=" * 72)
    safe_print("T7 心率血氧 · 现场引导采集（提示在 LCD 上，暗号在蜂鸣器上）")
    safe_print("=" * 72)
    safe_print(f"  1 声短鸣 = 开始，请把食指指腹盖住模块小窗口（轻贴、别压、保持不动）")
    safe_print(f"  2 声 = 成功结束，看 LCD 上的 HR / SPO2")
    safe_print(f"  5 声 = 没读到，按 LCD 提示重来（重跑本命令即可）")
    safe_print(f"  采集时长：{seconds} 秒")

    try:
        config = load_config(args.config)
    except Exception as exc:  # noqa: BLE001
        safe_print(f"[X] 加载配置失败：{type(exc).__name__}: {exc}")
        return 2

    # ---- 打开器件：vitals 必须成功；LCD/蜂鸣只是"提示器"，坏了也继续 ----
    try:
        vitals = _open(config, "vitals")
    except Exception as exc:  # noqa: BLE001
        safe_print(f"[X] 打开 vitals（max30102）失败：{type(exc).__name__}: {exc}")
        safe_print("    排查：i2cdetect -y 1 看 0x57；VIN 必须 3.3V；SDA=脚3 / SCL=脚5")
        return 2

    lcd = None
    if not args.no_lcd:
        try:
            lcd = _open(config, "display")
        except Exception as exc:  # noqa: BLE001
            safe_print(f"[!] 打开 LCD 失败（继续采集，只是没有屏上提示）：{type(exc).__name__}: {exc}")
    buzzer = None
    if not args.no_beep:
        try:
            buzzer = _open(config, "alarm_buzzer")
        except Exception as exc:  # noqa: BLE001
            safe_print(f"[!] 打开蜂鸣器失败（继续采集，只是没有声音提示）：{type(exc).__name__}: {exc}")

    cues = _Cues(lcd, buzzer)
    samples: List[Any] = []
    summary: Dict[str, Any] = summarise([])
    window_stats: Dict[str, Any] = {}
    raw_ir: List[int] = []
    raw_red: List[int] = []
    passed = False
    why = "未执行"
    try:
        cues.show(lcd_line("T7 VITALS TEST"), lcd_line("FINGER ON T-%02d" % seconds))
        cues.beep(BEEP_START)          # ★ 1 声：叫人过来放手指

        last_hr: Optional[float] = None
        last_spo2: Optional[float] = None
        for elapsed in range(seconds):
            left = seconds - elapsed
            finger = False
            try:
                sample = vitals.read()
            except Exception as exc:  # noqa: BLE001 - 单次读数失败不该终止整轮
                safe_print(f"  t={left:02d}s 读数异常：{type(exc).__name__}: {exc}")
                sample = None
            if sample is not None:
                samples.append(sample)
                finger = bool(getattr(sample, "finger_detected", False))
                if getattr(sample, "ok", False) and getattr(sample, "heart_rate_bpm", None) is not None:
                    last_hr = float(sample.heart_rate_bpm)
                    last_spo2 = (
                        float(sample.spo2_percent)
                        if getattr(sample, "spo2_percent", None) is not None else None
                    )
                safe_print(
                    "  t=%02ds finger=%s ok=%s hr=%s spo2=%s q=%s %s"
                    % (
                        left, finger, getattr(sample, "ok", None),
                        getattr(sample, "heart_rate_bpm", None),
                        getattr(sample, "spo2_percent", None),
                        getattr(sample, "quality", None),
                        getattr(sample, "error", "") or "",
                    )
                )
                # ★ 每秒把窗口的"直流/交流/样本数"也打出来（E52）：
                #   质量分是三个因子相乘，**只看得分分不清**是"信号太弱""ADC 饱和"还是"节律乱"；
                #   而且这样即使最后一步抓取失败，日志里也留着证据。
                try:
                    ws = vitals.window_stats()
                    safe_print("        win n=%s dc=%s ac=%s (ir %s~%s)"
                               % (ws.get("samples"), _fmt(ws.get("ir_dc")),
                                  _fmt(ws.get("ir_ac_rms")), ws.get("ir_min"), ws.get("ir_max")))
                except Exception as exc:  # noqa: BLE001 - 诊断不该打断采集
                    safe_print(f"        win 统计失败：{type(exc).__name__}: {exc}")
            # 即使这次读失败也要刷倒计时（现场靠它知道还剩几秒）
            cues.show(*format_countdown(left, finger, last_hr, last_spo2))
            time.sleep(1.0)

        summary = summarise(samples)
        passed, why = verdict(summary)
        cues.show(*result_lines(summary, passed))
        cues.beep(BEEP_DONE if passed else BEEP_FAIL)

        # ★ 窗口必须**在 close() 之前**抓下来（2026-09-29 踩过，ERROR.md E52 补记）：
        #   `Device.close()` 会清空红外/红光缓冲，所以放在 finally 之后抓只会拿到空窗口
        #   —— 那一次"原始波形没落盘"就是这么来的。
        try:
            window_stats = vitals.window_stats()
            raw_ir, raw_red = vitals.raw_window()
        except Exception as exc:  # noqa: BLE001 - 诊断不该改变结论
            window_stats, raw_ir, raw_red = {}, [], []
            safe_print(f"  [!] 抓取分析窗失败：{type(exc).__name__}: {exc}")
    finally:
        try:
            vitals.close()
        finally:
            cues.close()

    safe_print("-" * 72)
    safe_print("汇总（LCD 上也显示了同样结论）：")
    safe_print(f"  采样次数        ：{summary['total']}（其中检测到手指 {summary['finger_samples']} 次）")
    safe_print(f"  有效心率样本    ：{summary['valid']}")
    safe_print(f"  心率 中位数/区间 ：{summary['hr_median']} / {summary['hr_min']}~{summary['hr_max']}")
    safe_print(f"  心率 波动(跨度)  ：{summary['hr_spread']}")
    safe_print(f"  血氧 中位数/区间 ：{summary['spo2_median']} / {summary['spo2_min']}~{summary['spo2_max']}")
    if summary["errors"]:
        safe_print(f"  出现过的报错    ：{summary['errors']}")
    if cues.notes:
        for note in cues.notes[:3]:
            safe_print(f"  [!] {note}")
    safe_print(f"  判据（T7）      ：心率 {T7_HR_RANGE[0]:.0f}~{T7_HR_RANGE[1]:.0f}、"
                f"血氧 {T7_SPO2_RANGE[0]:.0f}~{T7_SPO2_RANGE[1]:.0f}")
    # ---- 原始波形留证 + 诊断 ----
    # 为什么必须做：质量分 = 节律一致性 × 信号强度 × 波数，**只看得分分不清**
    # "信号太弱"还是"ADC 饱和"还是"节律乱"（判据要有区分力，见 ERROR.md E52）。
    try:
        stats = window_stats
        ir, red = raw_ir, raw_red
        safe_print("  分析窗诊断    ：样本 %s 组；红外 直流 %s／交流RMS %s（%s~%s）；"
                   "红光 直流 %s／交流RMS %s"
                   % (stats.get("samples"),
                      _fmt(stats.get("ir_dc")), _fmt(stats.get("ir_ac_rms")),
                      stats.get("ir_min"), stats.get("ir_max"),
                      _fmt(stats.get("red_dc")), _fmt(stats.get("red_ac_rms"))))
        if stats.get("ir_dc") and stats["ir_dc"] > 250000:
            safe_print("    [!] 红外直流接近 18 位满量程(262143) => 疑似 **ADC 饱和**："
                       "降 led_current 或放大 ADC 量程")
        if stats.get("ir_ac_rms") is not None and stats["ir_ac_rms"] < 5:
            safe_print("    [!] 交流分量极小 => **信号太弱**：手指没贴实/太用力、"
                       "led_current 偏低或漏光（这与上面的『饱和』是两种不同原因）")
        if args.dump_csv and ir:
            _write_csv(args.dump_csv, ir, red, getattr(vitals, "analysis_rate", None))
            safe_print(f"  原始波形已写入：{args.dump_csv}（{len(ir)} 组）")
        elif args.dump_csv:
            safe_print("  [!] 窗口是空的（没贴手指或驱动已清窗），没有写出 CSV")
    except Exception as exc:  # noqa: BLE001 - 诊断失败不该改变结论
        safe_print(f"  [!] 分析窗诊断失败：{type(exc).__name__}: {exc}")

    if passed:
        safe_print(f"[OK] 通过：{why}")
        return 0
    safe_print(f"[!] 未通过：{why}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
