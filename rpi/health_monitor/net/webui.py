"""树莓派端**状态网页**（`GET /`）—— 手机 App 之外的第二块屏。

为什么需要它
------------
1. **答辩现场可视化**：把树莓派接到 HDMI 屏 / 电脑浏览器，一眼能看到全部指标与最近报警，
   不用装 App、不用抓 JSON；
2. **课程叙事里"配套网页"的那一半**：题目要求"子女可远程查看"，网页版是最低成本的实现；
3. **排查利器**：出问题时打开这个页面，能同时看到"读数 / 新鲜度 / 每个设备的状态 / 最近报警"。

设计约束（刻意的）
------------------
- **纯标准库**：服务端拼字符串，不用模板引擎、不引入前端构建链、不依赖外网 CDN
  （树莓派可能没有外网，答辩现场更不能白屏）；
- **无 JavaScript 也能用**：用 ``<meta http-equiv="refresh">`` 自动刷新；
  顺手加了几行内联 JS 只在"页面可见时才刷新"，不可用时退化为定时整页刷新；
- **只读**：这个页面不做任何写操作（消音/求助仍在 API 里，避免误点导致误报解除）；
- **中文界面 + 无 markdown 标记**（界面文本里出现 markdown 会原样露出，见项目排错记录）。

⚠️ 与 JSON API 的关系：本模块只 **读** :class:`~health_monitor.net.web.WebApi` 依赖的
同一个 runtime 对象，不改变任何 API 行为。
"""

from __future__ import annotations

import html
from typing import Any, Dict, List, Tuple

#: 报警码 → 中文（网页用；与安卓端 AlarmCatalog 保持同口径，改一处要改另一处）
ALARM_LABELS: Dict[str, str] = {
    "hr_too_high": "心率过高",
    "hr_too_low": "心率过低",
    "spo2_too_low": "血氧过低",
    "body_temp_high": "体温偏高",
    "body_temp_low": "体温偏低",
    "ambient_temp_high": "室温偏高",
    "ambient_temp_low": "室温偏低",
    "humidity_high": "湿度过高",
    "no_motion_too_long": "长时间无活动（疑似跌倒）",
    "night_frequent_wake": "夜间频繁起夜",
    "sos_pressed": "紧急求助",
    "sensor_fault": "传感器故障",
    "device_offline": "设备离线",
    "system_start": "系统已启动",
    "all_clear": "已恢复正常",
}

_MOTION_LABELS = {"detected": "有活动", "idle": "无活动", "unknown": "未知"}
_SEVERITY_NAMES = {0: "正常", 1: "提示", 2: "警告", 3: "紧急"}

_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin: 0; font-family: "Microsoft YaHei", "PingFang SC", system-ui, sans-serif;
       background: #f4f6f8; color: #1c2430; }
header { background: #1f6feb; color: #fff; padding: 14px 20px; }
header h1 { margin: 0; font-size: 19px; font-weight: 600; }
header .meta { margin-top: 4px; font-size: 12px; opacity: .9; }
main { max-width: 980px; margin: 0 auto; padding: 16px 20px 40px; }
.cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); gap: 12px; }
.card { background: #fff; border: 1px solid #dfe3e8; border-radius: 10px; padding: 12px 14px; }
.card .k { font-size: 12px; color: #5b6673; }
.card .v { font-size: 26px; font-weight: 600; margin-top: 6px; }
.card .v.unknown { color: #98a2b0; font-size: 20px; }
.card .s { font-size: 12px; margin-top: 6px; color: #5b6673; }
.card.warn { border-color: #e8a33d; background: #fff8ec; }
.card.bad { border-color: #d9534f; background: #fdf0ef; }
.banner { border-radius: 10px; padding: 12px 14px; margin-bottom: 14px; font-weight: 600; }
.banner.ok { background: #e7f6ec; border: 1px solid #58a55c; color: #24632c; }
.banner.warn { background: #fff8ec; border: 1px solid #e8a33d; color: #8a5a00; }
.banner.bad { background: #fdf0ef; border: 1px solid #d9534f; color: #8a2b28; }
h2 { font-size: 15px; margin: 22px 0 8px; }
table { width: 100%; border-collapse: collapse; background: #fff; border: 1px solid #dfe3e8;
        border-radius: 10px; overflow: hidden; font-size: 13px; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #eef1f4; }
th { background: #f7f9fb; font-weight: 600; color: #45505c; }
tr:last-child td { border-bottom: none; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
.pill { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 12px; }
.pill.ok { background: #e7f6ec; color: #24632c; }
.pill.warn { background: #fff3dd; color: #8a5a00; }
.pill.bad { background: #fdeceb; color: #8a2b28; }
footer { margin-top: 24px; font-size: 12px; color: #6b7480; }
code { background: #eef1f4; padding: 1px 5px; border-radius: 4px; font-size: 12px; }
"""

#: 页面可见性感知的刷新（不可用时退化为 meta refresh）
_SCRIPT = """
<script>
// 只在页面可见时整页刷新，避免手机锁屏后白白耗电/占带宽。
setTimeout(function () { location.reload(); }, %d);
</script>
"""


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _fmt(value: Any, unit: str = "", digits: int = 0) -> Tuple[str, bool]:
    """格式化数值；``None`` → ``("未知", True)``（**绝不显示 0**）。"""
    if value is None:
        return "未知", True
    try:
        number = float(value)
    except (TypeError, ValueError):
        return _esc(value), False
    text = f"{number:.{digits}f}{unit}" if digits else f"{number:g}{unit}"
    return text, False


def _card(title: str, value: Any, unit: str = "", subtitle: str = "", digits: int = 0,
          state: str = "") -> str:
    text, unknown = _fmt(value, unit, digits)
    cls = "card" + (f" {state}" if state else "")
    vcls = "v unknown" if unknown else "v"
    sub = f'<div class="s">{_esc(subtitle)}</div>' if subtitle else ""
    return (
        f'<div class="{cls}"><div class="k">{_esc(title)}</div>'
        f'<div class="{vcls}">{_esc(text)}</div>{sub}</div>'
    )


def render_page(runtime: Any, refresh_s: int = 5) -> str:
    """渲染整页 HTML。

    Args:
        runtime: :class:`~health_monitor.service.Runtime`（只读使用）。
        refresh_s: 自动刷新周期（秒）。
    """
    now = runtime.clock()
    snap = runtime.collector.snapshot()
    summary = snap.health_summary()
    status = runtime.collector.status()["devices"]
    active = runtime.engine.active_alarms()

    # ---- 顶部横幅：先回答"现在安全吗" ----
    if active:
        names = "、".join(ALARM_LABELS.get(code, code) for code in active)
        banner_cls, banner_text = "bad", f"⚠ 正在报警：{names}"
    elif snap.data_stale:
        banner_cls, banner_text = "warn", "数据可能已过期：树莓派尚未读到新的传感器数据"
    else:
        banner_cls, banner_text = "ok", "状态正常，监护中"

    # ---- 六张指标卡 ----
    # 说明：复合值（室温/湿度、数据年龄）直接放进 value，说明文字放 subtitle。
    finger = summary.get("finger_detected")
    hr_note = "请将手指放好" if finger is False else ("已贴合" if finger else "皮肤贴合状态未知")
    motion_state = summary.get("motion_state")
    motion_silent = summary.get("motion_silent_s")
    data_age = summary.get("data_age_s")
    data_stale = bool(summary.get("data_stale"))
    cards = [
        _card("心率", summary.get("heart_rate_bpm"), " bpm", hr_note,
              state="warn" if finger is False else ""),
        _card("血氧", summary.get("spo2_percent"), " %",
              "未测出（未贴合手指）" if finger is False else ""),
        _card("体温（精密）", summary.get("body_temp_c"), " ℃", "TMP36 + MCP3002", digits=1),
        _card(
            "室温 / 湿度",
            f"{_fmt(summary.get('ambient_temp_c'), '', 1)[0]} ℃ / "
            f"{_fmt(summary.get('humidity_percent'), '', 1)[0]} %",
            subtitle="DHT11（读取间隔 ≥2 秒）",
        ),
        _card(
            "活动状态",
            _MOTION_LABELS.get(motion_state or "", "未知"),
            subtitle=("上次检测到人：{:.0f} 秒前".format(motion_silent)
                      if motion_silent is not None else "距上次检测到人的时间未知"),
        ),
        _card(
            "数据年龄",
            ("已过期" if data_stale
             else f"{data_age:.1f} 秒" if data_age is not None
             else "未知"),
            subtitle="最近一次成功读取距今",
            state="warn" if data_stale else "",
        ),
    ]

    # ---- 设备状态表 ----
    rows: List[str] = []
    for name, entry in sorted(status.items()):
        failures = entry.get("failures", 0)
        stale = entry.get("stale", False)
        if stale or failures:
            pill_cls, pill_text = ("bad", "异常") if failures else ("warn", "数据陈旧")
        else:
            pill_cls, pill_text = "ok", "正常"
        age = entry.get("last_ok_age_s")
        rows.append(
            "<tr>"
            f"<td>{_esc(name)}<br><span class='s'><code>{_esc(entry.get('driver'))}</code></span></td>"
            f"<td class='num'>{_fmt(entry.get('interval_s'), '', 1)[0]} 秒</td>"
            f"<td class='num'>{_esc(entry.get('reads'))}</td>"
            f"<td class='num'>{_esc(failures)}</td>"
            f"<td>{'—' if age is None else _fmt(age, ' 秒', 1)[0]}</td>"
            f"<td><span class='pill {pill_cls}'>{pill_text}</span></td>"
            f"<td>{_esc(entry.get('last_error') or '')}</td>"
            "</tr>"
        )

    # ---- 最近报警 ----
    alarm_rows: List[str] = []
    for event in reversed(runtime.recent_events(limit=15)):
        alarm_rows.append(
            "<tr>"
            f"<td>{_esc(_time_text(event.ts))}</td>"
            f"<td>{_esc(ALARM_LABELS.get(event.code.value, event.code.value))}</td>"
            f"<td>{_SEVERITY_NAMES.get(int(event.severity), int(event.severity))}</td>"
            f"<td>{_esc(event.message)}</td>"
            "</tr>"
        )
    if not alarm_rows:
        alarm_rows.append("<tr><td colspan='4'>本次运行还没有报警记录</td></tr>")

    # ---- 当前报警态 ----
    active_rows = [
        f"<tr><td>{_esc(ALARM_LABELS.get(code, code))}</td>"
        f"<td>{_esc(code)}</td><td>{_esc(_time_text(ts))}</td></tr>"
        for code, ts in sorted(active.items())
    ] or ["<tr><td colspan='3'>当前没有处于报警状态的项目</td></tr>"]

    devices_errors = runtime.assembly_errors or {}
    warn_block = ""
    if devices_errors:
        items = "".join(f"<li><code>{_esc(k)}</code>：{_esc(v)}</li>" for k, v in devices_errors.items())
        warn_block = f'<div class="banner warn">以下设备装配/打开失败（功能可能不可用）：<ul>{items}</ul></div>'

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{int(refresh_s)}">
<title>居家老人健康监护 · 状态</title>
<style>{_CSS}</style>
</head>
<body>
<header>
  <h1>树莓派居家老人健康监护系统</h1>
  <div class="meta">版本 {_esc(runtime.version)} · {"模拟模式（无硬件）" if runtime.mock else "真实硬件模式"}
    · 运行 {_fmt(round(now - runtime.started_at, 1), ' 秒', 1)[0]} · 页面每 {int(refresh_s)} 秒自动刷新 ·
    本地时间 {_esc(_time_text(now))}</div>
  <div class="meta"><a href="/panel" style="color:#fff;font-weight:600">→ 打开配置面板</a>
    （改报警阈值与器件开关；<strong>会写入配置文件</strong>）</div>
</header>
<main>
  <div class="banner {banner_cls}">{_esc(banner_text)}</div>
  {warn_block}
  <div class="cards">{"".join(cards)}</div>

  <h2>当前报警态</h2>
  <table><thead><tr><th>报警</th><th>代码</th><th>首次触发</th></tr></thead>
  <tbody>{"".join(active_rows)}</tbody></table>

  <h2>设备状态</h2>
  <table><thead><tr><th>设备</th><th>周期</th><th>读取次数</th><th>连续失败</th>
  <th>距上次成功</th><th>状态</th><th>最后错误</th></tr></thead>
  <tbody>{"".join(rows) or "<tr><td colspan='7'>没有输入设备</td></tr>"}</tbody></table>

  <h2>最近报警（本次运行，最多 15 条）</h2>
  <table><thead><tr><th>时间</th><th>报警</th><th>等级</th><th>说明</th></tr></thead>
  <tbody>{"".join(alarm_rows)}</tbody></table>

  <footer>
    JSON 接口：<code>/api/v1/current</code> · <code>/api/v1/health</code> ·
    <code>/api/v1/alarms</code> · <code>/api/v1/history?metric=ambient_temp_c&amp;limit=120</code><br>
    本页面只读，不会修改任何状态（消音与求助请用手机 App 或 POST 接口）。<br>
    数值显示"未知"表示该传感器当前没有有效数据——这是刻意设计：本系统不把"读不到"当作"正常"。
  </footer>
</main>
{_SCRIPT % (int(refresh_s) * 1000)}
</body>
</html>
"""


#: 面板上的**阈值分组**：``(组名, [(字段名, 中文标签, 单位), ...])``。
#: 顺序刻意按"人关心的顺序"排，而不是按 dataclass 的字段顺序。
#: ⚠️ 必须**覆盖全部** :class:`~health_monitor.core.config.Thresholds` 字段（有测试钉住）：
#: 漏掉一个，面板上就永远改不了它，而且从界面上**看不出来少了什么**。
THRESHOLD_GROUPS: List[Tuple[str, List[Tuple[str, str, str]]]] = [
    ("心率", [
        ("hr_min", "心率下限", "bpm"),
        ("hr_max", "心率上限", "bpm"),
    ]),
    ("血氧", [
        ("spo2_min", "血氧下限", "%"),
    ]),
    ("体温（精密，TMP36）", [
        ("body_temp_min", "体温下限", "℃"),
        ("body_temp_max", "体温上限", "℃"),
    ]),
    ("环境（DHT11）", [
        ("ambient_temp_min", "室温下限", "℃"),
        ("ambient_temp_max", "室温上限", "℃"),
        ("humidity_max", "湿度上限", "%"),
    ]),
    ("久无活动 / 夜间", [
        ("no_motion_timeout_s", "久无活动阈值", "秒"),
        ("night_start_hour", "夜间起始小时", "时"),
        ("night_end_hour", "夜间结束小时", "时"),
        ("night_wake_count", "夜间起夜次数阈值", "次"),
        ("night_window_s", "起夜统计窗口", "秒"),
    ]),
    ("报警节流 / 故障判定", [
        ("sensor_fault_after", "连续失败几次算故障", "次"),
        ("repeat_cooldown_s", "同一报警抑制时间", "秒"),
        ("re_alert_interval_s", "持续提醒间隔（0=关闭）", "秒"),
        ("hysteresis", "回差（防阈值抖动）", ""),
    ]),
    ("测血氧（按需测量）", [
        ("spo2_remind_interval_s", "提醒间隔（0=不提醒）", "秒"),
        ("spo2_remind_timeout_s", "叫人后等按键超时", "秒"),
        ("spo2_measure_s", "一次测量时长", "秒"),
    ]),
]

#: 面板专用样式（接在 :data:`_CSS` 后面；仍然**不依赖任何外部 CDN**）
_PANEL_CSS = """
.unsafe { background: #fdeceb; border: 2px solid #d9534f; color: #8a2b28;
          border-radius: 10px; padding: 12px 14px; margin-bottom: 14px; font-weight: 600; }
.unsafe .sub { display: block; font-weight: 400; margin-top: 6px; font-size: 13px; }
fieldset { border: 1px solid #dfe3e8; border-radius: 10px; background: #fff;
           margin: 0 0 12px; padding: 10px 14px 12px; }
legend { font-weight: 600; font-size: 13px; color: #45505c; padding: 0 6px; }
.fields { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 8px 16px; }
.field { display: flex; align-items: center; gap: 8px; font-size: 13px; }
.field > span.lbl { flex: 1; color: #45505c; }
.field input[type=number] { width: 92px; padding: 4px 6px; border: 1px solid #cfd6de;
                            border-radius: 6px; font-size: 13px; }
.unit { color: #8b95a1; font-size: 12px; min-width: 32px; }
.actions { margin: 16px 0; display: flex; gap: 14px; align-items: center; flex-wrap: wrap; }
button { background: #1f6feb; color: #fff; border: 0; border-radius: 8px;
         padding: 9px 20px; font-size: 14px; cursor: pointer; }
button[disabled] { opacity: .55; cursor: progress; }
.result { border-radius: 10px; padding: 12px 14px; margin-top: 14px; font-size: 13px; }
.result.ok { background: #e7f6ec; border: 1px solid #58a55c; }
.result.bad { background: #fdf0ef; border: 1px solid #d9534f; }
.result ul { margin: 6px 0 0 18px; padding: 0; }
.result li { margin: 2px 0; }
.hint { font-size: 12px; color: #6b7480; }
"""

#: 面板的脚本。**刻意不做自动刷新**：这是表单，整页刷新会把没保存的输入抹掉
#: （状态页可以每 5 秒刷新，因为它只读）。
_PANEL_SCRIPT = """
<script>
function numOf(el) { var v = parseFloat(el.value); return isNaN(v) ? null : v; }

function collect() {
  var patch = { thresholds: {}, devices: {} };
  document.querySelectorAll('input[data-th]').forEach(function (el) {
    var v = numOf(el);
    if (v === null) { throw new Error('「' + el.dataset.label + '」不是数字'); }
    patch.thresholds[el.dataset.th] = v;
  });
  document.querySelectorAll('input[data-dev]').forEach(function (el) {
    var name = el.dataset.dev;
    if (!patch.devices[name]) { patch.devices[name] = {}; }
    if (el.type === 'checkbox') {
      patch.devices[name][el.dataset.field] = el.checked;
    } else {
      var v = numOf(el);
      if (v === null) { throw new Error('设备 ' + name + ' 的读取周期不是数字'); }
      patch.devices[name][el.dataset.field] = v;
    }
  });
  return patch;
}

// 前端只做"一眼能看出的错"；**后端校验才是准的**（未知键、撞脚、DHT11 周期下限都在那边）
function clientCheck(p) {
  var t = p.thresholds;
  function pair(lo, hi, what) {
    if (t[lo] !== undefined && t[hi] !== undefined && !(t[lo] < t[hi])) {
      return what + '：下限（' + t[lo] + '）必须小于上限（' + t[hi] + '）';
    }
    return '';
  }
  var bad = pair('hr_min', 'hr_max', '心率')
         || pair('body_temp_min', 'body_temp_max', '体温')
         || pair('ambient_temp_min', 'ambient_temp_max', '室温');
  if (bad) { return bad; }
  if (t.spo2_min !== undefined && !(t.spo2_min > 0 && t.spo2_min <= 100)) {
    return '血氧下限必须在 (0, 100] 之间';
  }
  for (var name in p.devices) {
    var iv = p.devices[name].read_interval_s;
    if (iv !== undefined && !(iv > 0)) { return '设备 ' + name + ' 的读取周期必须为正数'; }
  }
  return '';
}

function show(cls, title, items) {
  var box = document.getElementById('result');
  box.className = 'result ' + cls;
  var html = '<strong>' + title + '</strong>';
  if (items && items.length) {
    html += '<ul>';
    items.forEach(function (line) { html += '<li>' + line + '</li>'; });
    html += '</ul>';
  }
  box.innerHTML = html;
}

function esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

async function save() {
  var patch;
  try { patch = collect(); }
  catch (err) { show('bad', esc(err.message)); return; }

  var bad = clientCheck(patch);
  if (bad) { show('bad', '本地检查未通过：' + esc(bad) + '<br><span class="hint">修正后再保存</span>'); return; }

  var btn = document.getElementById('save');
  btn.disabled = true;
  try {
    var res = await fetch('/api/v1/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch)
    });
    var data = await res.json();
    if (!data.ok) {
      show('bad', '保存失败（HTTP ' + res.status + '）：' + esc(data.error || '未知错误'));
      return;
    }
    var lines = [];
    (data.changed || []).forEach(function (x) { lines.push('写盘：' + esc(x)); });
    (data.applied || []).forEach(function (x) { lines.push('已生效：' + esc(x)); });
    if (data.backup) { lines.push('备份文件：' + esc(data.backup)); }
    (data.warnings || []).forEach(function (x) { lines.push('注意：' + esc(x)); });
    show('ok', '保存成功' + (lines.length ? '' : '（无变化）'), lines);
  } catch (err) {
    show('bad', '请求失败：' + esc(err));
  } finally {
    btn.disabled = false;
  }
}
</script>
"""


def _num_text(value: Any) -> str:
    """把配置里的数字渲染成输入框的初值（``3.0`` → ``3``，``35.5`` → ``35.5``）。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return _esc(value)
    if number == int(number) and abs(number) < 1e15:
        return str(int(number))
    return repr(number)


def render_panel(runtime: Any, store: Any, *, secured: bool = False) -> str:
    """渲染**配置面板**（``GET /panel``）。

    Args:
        runtime: :class:`~health_monitor.service.Runtime`（只读它的版本/模式信息）。
        store: :class:`~health_monitor.core.configstore.ConfigStore`（提供 ``snapshot()``）。
        secured: 本服务是否启用了 ``--token``。**只影响提示文案** ——
            真正拦人是在 :meth:`WebApi.handle`，页面文案不能当安全边界。
    """
    snap = store.snapshot()
    thresholds = snap.get("thresholds") or {}
    devices = snap.get("devices") or {}
    store_warnings = list(snap.get("warnings") or [])

    # ---- 顶部：这件工具是"能改配置"的，必须一眼看出来 ----
    if secured:
        unsafe = (
            '<div class="unsafe" style="background:#e7f6ec;border-color:#58a55c;color:#24632c">'
            "本服务启用了 <code>--token</code>：写操作必须带 <code>X-Auth-Token</code>"
            '<span class="sub">页面本身不提供口令输入（避免口令进入浏览器历史）；'
            "请在请求头带 token 调用 <code>/api/v1/config</code>。</span></div>"
        )
    else:
        unsafe = (
            '<div class="unsafe">⚠ 此面板可以修改配置，而且<strong>未设防</strong>'
            '<span class="sub">任何能访问到本机 8080 端口的人都能改报警阈值与器件开关。'
            "仅限局域网/答辩演示使用；要保护起来就用 <code>serve --token &lt;口令&gt;</code> 启动。"
            "</span></div>"
        )

    # ---- 阈值分组 ----
    groups: List[str] = []
    for title, fields in THRESHOLD_GROUPS:
        items: List[str] = []
        for name, label, unit in fields:
            value = thresholds.get(name, "")
            items.append(
                '<div class="field">'
                f'<span class="lbl">{_esc(label)}</span>'
                f'<input type="number" step="any" data-th="{_esc(name)}" '
                f'data-label="{_esc(label)}" value="{_esc(_num_text(value))}">'
                f'<span class="unit">{_esc(unit)}</span>'
                "</div>"
            )
        groups.append(
            f"<fieldset><legend>{_esc(title)}</legend>"
            f'<div class="fields">{"".join(items)}</div></fieldset>'
        )

    # ---- 器件开关与周期 ----
    dev_rows: List[str] = []
    for name, info in devices.items():
        optional = ' <span class="hint">可选件</span>' if info.get("optional") else ""
        driver = _esc(info.get("driver"))
        checked = " checked" if info.get("enabled") else ""
        interval_text = _esc(_num_text(info.get("read_interval_s", 1.0)))
        dev_rows.append(
            "<tr>"
            f"<td><code>{_esc(name)}</code><br><span class='hint'>{driver}{optional}</span></td>"
            "<td><label class='field'>"
            f'<input type="checkbox" data-dev="{_esc(name)}" data-field="enabled"{checked}>'
            " <span>启用</span></label></td>"
            f'<td class="num"><input type="number" step="any" min="0.1" '
            f'data-dev="{_esc(name)}" data-field="read_interval_s" value="{interval_text}"> 秒</td>'
            "</tr>"
        )

    warn_block = ""
    if store_warnings:
        items = "".join(f"<li>{_esc(w)}</li>" for w in store_warnings)
        warn_block = f'<div class="banner warn">本机覆盖（devices.local.json）影响了面板正在编辑的项：<ul>{items}</ul></div>'

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>居家老人健康监护 · 配置面板</title>
<style>{_CSS}{_PANEL_CSS}</style>
</head>
<body>
<header>
  <h1>配置面板（报警阈值 / 器件开关）</h1>
  <div class="meta">版本 {_esc(runtime.version)} · {"模拟模式（无硬件）" if runtime.mock else "真实硬件模式"}
    · 配置文件 {_esc(snap.get("config_path", ""))} ·
    <a href="/" style="color:#fff;font-weight:600">← 返回状态页</a></div>
</header>
<main>
  {unsafe}
  {warn_block}
  <noscript>
    <div class="banner bad">本页需要 JavaScript 才能保存（表单要拼 JSON 并 POST）。
      没有 JS 时请直接用接口：<code>GET /api/v1/config</code> 看配置，
      <code>POST /api/v1/config</code> 改配置。</div>
  </noscript>

  <h2>报警阈值</h2>
  {''.join(groups)}

  <h2>器件开关与读取周期</h2>
  <table><thead><tr><th>设备</th><th>开关</th><th>读取周期</th></tr></thead>
  <tbody>{''.join(dev_rows) or "<tr><td colspan='3'>配置里没有设备</td></tr>"}</tbody></table>

  <div class="actions">
    <button id="save" onclick="save()">保存并立即生效</button>
    <a href="/panel">放弃改动，重新载入</a>
    <span class="hint">保存 = 写回配置文件（自动备份）<strong>并立即生效，不重启服务</strong></span>
  </div>
  <div id="result"></div>

  <footer>
    <strong>写入前一定会校验</strong>：未知字段、阈值自洽性、器件读取周期下限（DHT11 ≥ 2 秒）、
    以及<strong>GPIO 撞脚</strong> —— 任何一项不过就 <code>400</code> 且<strong>文件一个字节都不改</strong>。<br>
    前端也做了一次检查，但那只是"少跑一趟"；<strong>以后端为准</strong>。<br>
    引脚号<strong>不在本面板的编辑范围</strong>（接线属于硬件，改错了现场很难查）。<br>
    器件关掉后会被<strong>立即关闭并从系统里移除</strong>（不再采集/不再下发）；
    打开时若没接线，会在结果里以"注意"的形式告诉你 —— 配置仍然保存。
  </footer>
</main>
{_PANEL_SCRIPT}
</body>
</html>
"""


def _time_text(ts: float) -> str:
    """Unix 秒 → 本地时间字符串（网页显示用）。"""
    import time as _time

    return _time.strftime("%H:%M:%S", _time.localtime(ts))


__all__ = ["render_page", "render_panel", "ALARM_LABELS", "THRESHOLD_GROUPS"]
