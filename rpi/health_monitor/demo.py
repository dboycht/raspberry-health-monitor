"""一键演示：**不接任何硬件**也能把整条链路演给老师看。

演示剧本（时间被压缩，但走的是与真机**完全相同**的业务代码）：
1. 启动就绪 → 一切正常
2. 心率升到 128 → 报警（蜂鸣 + 语音 + 黄灯 + LCD）
3. 血氧掉到 89 → 升级为紧急（红灯 + 更急促蜂鸣）
4. 长时间无活动 → "疑似跌倒"紧急报警
5. 按下求救按钮 → 紧急求助
6. 指标与活动恢复 → 发 ALL_CLEAR 解除
7. 传感器掉线 → 故障报警；恢复后自动解除

用法::

    python -m health_monitor demo
    python -m health_monitor demo --http --port 8080   # 同时起 HTTP，用手机/浏览器看数据
"""

from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional

from .core.config import AppConfig
from .hal.models import MotionState
from .playback import PlaybackRuntime

#: 演示用配置：阈值刻意收紧、久无活动改成 60 秒，便于在几十秒内演完
DEMO_CONFIG: Dict[str, Any] = {
    "thresholds": {
        "hr_min": 50, "hr_max": 110, "spo2_min": 93,
        "body_temp_min": 35.5, "body_temp_max": 37.5,
        "ambient_temp_min": 16, "ambient_temp_max": 30, "humidity_max": 80,
        "no_motion_timeout_s": 60,
        "night_start_hour": 22, "night_end_hour": 6,
        "night_wake_count": 5, "night_window_s": 3600,
        "sensor_fault_after": 3, "repeat_cooldown_s": 0, "hysteresis": 3,
    },
    "devices": {
        "vitals": {"driver": "max30102", "read_interval_s": 1.0},
        "body_temp": {"driver": "tmp36", "read_interval_s": 1.0},
        "ambient": {"driver": "dht11", "read_interval_s": 3.0},
        "motion": {"driver": "hc_sr501", "read_interval_s": 0.5},
        "display": {"driver": "lcd1602", "read_interval_s": 1.0},
        "speaker": {"driver": "bt_speaker", "read_interval_s": 1.0},
        "alarm_buzzer": {"driver": "buzzer", "read_interval_s": 1.0},
        "status_led": {"driver": "led", "read_interval_s": 1.0},
    },
}


class _ScriptedClock:
    """演示用假时钟：由剧本显式推进，因此几十秒能演完"几十分钟"的剧情。

    ⚠️ **起点必须落在白天**（默认 10:00）：规则引擎里"夜间频繁起夜"是按
    本地时间判定的，如果起点恰好在 22:00~06:00，剧本里每次"检测到人"
    都会被计入夜间起夜，演示会出现莫名其妙的起夜报警（2026-09-21 实测踩到）。
    """

    def __init__(self, start: Optional[float] = None) -> None:
        if start is None:
            start = datetime(2026, 9, 21, 10, 0, 0).timestamp()
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def run_demo(with_http: bool = False, port: int = 8080) -> int:
    """执行演示剧本，返回退出码（0=全部符合预期）。"""
    clock = _ScriptedClock()
    config = AppConfig.from_dict(DEMO_CONFIG)
    runtime = PlaybackRuntime(
        config, clock=clock, sleep=lambda _s: None, verbose_outputs=False, dispatcher_enabled=True,
    )
    runtime.open()
    led = runtime.outputs.get("status_led")
    lcd = runtime.outputs.get("display")
    speaker = runtime.outputs.get("speaker")
    buzzer = runtime.outputs.get("alarm_buzzer")

    print("=" * 84)
    print("树莓派居家老人健康监护系统 —— 全链路演示（模拟数据，无需任何硬件）")
    print("=" * 84)
    print("说明：时间被压缩，但采集/判定/下发/上报走的是与真机完全相同的代码路径。\n")

    frame_no = {"n": 0}
    all_events: List[Any] = []

    def frame(title: str, seconds: float = 5.0) -> List[Any]:
        """推进时间并跑一帧。

        ⚠️ 默认推进 **5 秒**：MAX30102 的心率算法需要约 5 秒的连续采样窗口，
        推进太少会一直拿不到心率（驱动会返回"采样时长不足"，这是**正确行为**）。
        真机上采集线程每秒都在采，所以真实场景不会遇到这个问题。
        """
        frame_no["n"] += 1
        clock.advance(seconds)
        events = runtime.tick()
        all_events.extend(events)
        snap = runtime.collector.snapshot()
        s = snap.health_summary()
        print(f"── 第 {frame_no['n']} 幕：{title} ──")
        print(
            f"   读数：心率 {_fmt(s['heart_rate_bpm'], 'bpm')} · 血氧 {_fmt(s['spo2_percent'], '%')} · "
            f"体温 {_fmt(s['body_temp_c'], '°C')} · 室温 {_fmt(s['ambient_temp_c'], '°C')} · "
            f"活动 {s['motion_state']}"
        )
        if events:
            for ev in events:
                print(f"   ⚠️  报警：{ev.message}   [{ev.code.value} / 严重度 {int(ev.severity)}]")
        else:
            print("   ✅ 无报警")
        print(f"   LED={getattr(led, 'current_color', '?')}   LCD={getattr(lcd, 'current_lines', '?')}")
        spoken = getattr(speaker, "spoken", None)
        if spoken:
            print(f"   语音最后一条：{spoken[-1]}")
        print()
        return events

    # ---------------- 剧本 ----------------
    frame("系统启动，各项指标正常")
    frame("持续监护中")

    runtime.set_vitals(heart_rate=128.0)
    frame("心率升高到 128（超过上限 110）")

    runtime.set_vitals(spo2=89.0)
    frame("血氧掉到 89%（低于下限 93%）→ 升级为紧急")

    runtime.set_vitals(heart_rate=78.0, spo2=97.0)
    runtime.set_motion(MotionState.IDLE, silent_s=90.0)
    frame("90 秒未检测到活动（超过 60 秒阈值）→ 疑似跌倒")

    print(f"── 第 {frame_no['n'] + 1} 幕：老人按下求救按钮 ──")
    frame_no["n"] += 1
    clock.advance(5.0)
    sos_event = runtime.sos(clock())
    print(f"   ⚠️  报警：{sos_event.message}   [{sos_event.code.value} / 严重度 {int(sos_event.severity)}]")
    print(f"   LED={getattr(led, 'current_color', '?')}   LCD={getattr(lcd, 'current_lines', '?')}")
    print()

    runtime.set_motion(MotionState.DETECTED, silent_s=0.0)
    runtime.set_vitals(heart_rate=72.0, spo2=98.0)
    runtime.clear_alarms(clock())      # 求救属人工事件：家属/老人确认后复位输出器件
    frame("老人恢复活动，家属确认解除 → 状态复位")

    runtime.break_sensor("vitals", "模拟 I2C 总线掉线")
    frame("心率传感器第 1 次读取失败")
    frame("心率传感器第 2 次读取失败")
    frame("心率传感器第 3 次读取失败 → 报传感器故障")
    runtime.fix_sensor("vitals")
    frame("接线恢复，传感器重新采到数据")

    # ---------------- 汇总 ----------------
    if runtime.script_warnings:
        print("⚠️  剧本警告（说明某一步没真正生效）：")
        for warning in runtime.script_warnings:
            print(f"   - {warning}")
        print()
    print("=" * 84)
    print(f"演示结束：{frame_no['n']} 帧 · 报警 {len(all_events)} 条 · 下发 {len(runtime.dispatcher.dispatched)} 次 · "
          f"下发失败 {len(runtime.dispatcher.errors)} 次")
    if buzzer is not None:
        print(f"蜂鸣器累计鸣叫 {getattr(buzzer, 'total_beeps', 0)} 次；"
              f"语音累计播报 {len(getattr(speaker, 'spoken', []))} 条")
    codes = [e.code.value for e in all_events]
    print(f"触发过的报警类型：{', '.join(sorted(set(codes)))}")
    print("=" * 84)

    if with_http:
        return _serve_for_demo(runtime, port)
    runtime.close()
    return 0


def _serve_for_demo(runtime: PlaybackRuntime, port: int) -> int:
    """演示模式下顺带起 HTTP 服务，方便用手机/浏览器查看真实接口返回。"""
    try:
        runtime.start_http(host="0.0.0.0", port=port)
    except OSError as exc:
        print(f"❌ 端口 {port} 绑定失败：{exc}（换 --port，或先关掉占用端口的旧进程）")
        runtime.close()
        return 2
    url = f"http://127.0.0.1:{port}/api/v1/current"
    print(f"\nHTTP API 已启动：{url}")
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:   # noqa: S310 - 本地回环地址
            payload = json.loads(resp.read().decode("utf-8"))
        print("自测请求 /api/v1/current 返回：")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️  自测请求失败：{type(exc).__name__}: {exc}")
    print("\n按 Ctrl+C 结束演示服务…")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\n停止中…")
    finally:
        runtime.close()
    return 0


def _fmt(value: Optional[float], unit: str) -> str:
    return "未知" if value is None else f"{value:g}{unit}"


__all__ = ["run_demo", "DEMO_CONFIG"]
