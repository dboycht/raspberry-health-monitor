"""内联 SVG 图表：**不依赖任何前端库/CDN、离线可用、可被无头浏览器截图验收**。

为什么自己画而不是引 Chart.js 之类的库：

1. 演示现场可能**没有外网**，引 CDN 就是引一个"到时候打不开"的风险；
2. 本项目是**树莓派**，把渲染放在**服务端**做，页面天然轻、手机端也快；
3. 内联 SVG 能被 `msedge --headless --screenshot` **直接截下来当验收证据**（本项目已这么用）。

三条设计纪律（都是"**不许让图表说谎**"）：

* ⚠️ **缺口必须断线**：数据缺失处**绝不跨过缺口连线**。跨缺口连一条直线，视觉上等于
  "这段时间一直有数据"，与 `collector` 的"绝不用旧值冒充当前状态"是同一个谎言；
* ⚠️ **画报警阈值**：把阈值画成虚线，让人一眼看出"什么时候会报警"，
  而不是只在越界之后才从报警历史里反推；
* ⚠️ **过期要变灰 + 标注**：最后一个点太旧时，整条线转灰并打上"数据已过期"
  （与网页"测不到时显示最后一次记录"的约定配套 —— **可以显示旧值，但必须一眼看出它旧**）。
"""

from __future__ import annotations

from typing import Iterable, List, Optional, Sequence, Tuple

#: 一条曲线上的点：``(时间戳, 值)``；值为 ``None`` 表示**该时刻没有数据**（要断线）。
Point = Tuple[float, Optional[float]]

_COLORS = {
    "line": "#1a73e8",
    "line_stale": "#9aa0a6",
    "threshold": "#d93025",
    "grid": "#e8eaed",
    "axis": "#5f6368",
    "fill": "#1a73e8",
    "warn": "#f29900",
}


def _esc(text: object) -> str:
    """转义成可安全放进 SVG/HTML 的文本。"""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _fmt(value: float) -> str:
    """数值取一个"看着舒服"的位数（大数不要小数点，小数保留 1~2 位）。"""
    if abs(value) >= 100:
        return f"{value:.0f}"
    if abs(value) >= 10:
        return f"{value:.1f}".rstrip("0").rstrip(".")
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _median(xs: List[float]) -> float:
    """中位数（**刻意不用平均值**：网络抖动/停摆会把它拉飞）。"""
    if not xs:
        return 0.0
    s = sorted(xs)
    mid = len(s) // 2
    if len(s) % 2 == 1:
        return float(s[mid])
    return (float(s[mid - 1]) + float(s[mid])) / 2.0


def _auto_max_gap(points: Sequence[Point]) -> Optional[float]:
    """按"正常采样间隔"推算多大的时间空档算**缺口**（= 中位间隔 × 3）。

    ⚠️ 为什么必须有这条判据（2026-10-01 用**板子上的真实历史**验出来的）：

    历史库**只存成功读数** —— 设备停摆或连续读失败的那段时间，库里**一行都没有**，
    所以那些点根本不会以 ``value=None`` 的形态出现在点列里。
    于是只按 ``None`` 断线是**防不住**的：一次 10 分钟的停摆，
    会被画成"从停摆前的点直接连到恢复后的点"的**一条直线**，
    而那正是本模块第一条纪律要防的谎（"视觉上看起来这段时间一直有数据"）。

    ⇒ **时间上有洞就必须断线**：相邻两点间隔超过"正常间隔的 3 倍"就断开。
    真实实测：300 行环境温度全是有效值、一个 ``None`` 都没有，但采样间隔是 3 秒，
    其中若干处间隔达二十几秒（正是 E63 那次播报卡停主循环的窗口）。
    """
    ts_list = sorted(ts for ts, _ in points)
    deltas = [b - a for a, b in zip(ts_list, ts_list[1:]) if b - a > 0]
    if len(deltas) < 3:
        return None                      # 点太少，推不出"正常间隔"，就别乱断
    return max(_median(deltas) * 3.0, 1e-6)


def _segments(points: Sequence[Point], max_gap_s: Optional[float] = None) -> List[List[Tuple[float, float]]]:
    """把点列切成若干条连续线段。

    在两处**必须断开**：

    1. 值为 ``None`` —— 该时刻明确没有数据；
    2. **相邻两点的时间间隔超过 ``max_gap_s``** —— 时间上有洞（见 :func:`_auto_max_gap`）。

    ⚠️ 这是本模块**最重要**的一个函数：不在这两处断开，
    就会把两段数据用一条直线连起来、凭空造出中间那段趋势。
    """
    segments: List[List[Tuple[float, float]]] = []
    current: List[Tuple[float, float]] = []
    prev_ts: Optional[float] = None
    for ts, value in points:
        ts = float(ts)
        if value is None:
            if current:
                segments.append(current)
                current = []
            prev_ts = None
            continue
        if (
            current
            and max_gap_s is not None
            and prev_ts is not None
            and (ts - prev_ts) > float(max_gap_s)
        ):
            segments.append(current)
            current = []
        current.append((ts, float(value)))
        prev_ts = ts
    if current:
        segments.append(current)
    return segments


def _scale(value_range: Tuple[float, float], y_min: Optional[float], y_max: Optional[float]):
    """算出 Y 轴范围（留一点余量，并允许调用方钉死上下界）。"""
    lo, hi = value_range
    if y_min is not None:
        lo = y_min
    if y_max is not None:
        hi = y_max
    if hi - lo < 1e-9:                      # 全平的一条线：给个对称的假范围，别除以 0
        pad = max(abs(hi) * 0.05, 1.0)
        lo, hi = lo - pad, hi + pad
    else:
        pad = (hi - lo) * 0.12
        if y_min is None:
            lo -= pad
        if y_max is None:
            hi += pad
    return lo, hi


def _empty_svg(width: int, height: int, note: str) -> str:
    """没有数据时的占位图 —— **明确说"暂无数据"，绝不画一条假的平线**。"""
    return (
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{_esc(note)}" style="width:100%;height:auto;display:block">'
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#fafafa" '
        f'stroke="{_COLORS["grid"]}"/>'
        f'<text x="{width / 2:.0f}" y="{height / 2:.0f}" text-anchor="middle" '
        f'dominant-baseline="middle" fill="{_COLORS["axis"]}" '
        f'font-size="15">{_esc(note)}</text></svg>'
    )


def line_chart(
    points: Sequence[Point],
    *,
    unit: str = "",
    width: int = 720,
    height: int = 200,
    y_min: Optional[float] = None,
    y_max: Optional[float] = None,
    thresholds: Sequence[Tuple[float, str]] = (),
    now: Optional[float] = None,
    stale_after_s: Optional[float] = None,
    max_gap_s: Optional[float] = None,
    empty_note: str = "暂无数据",
) -> str:
    """画一条时间序列曲线，返回**内联 SVG 字符串**。

    Args:
        points: ``[(ts, value)]``，``value=None`` 表示缺口（**会断线**）。
        unit: 单位（写在 Y 轴刻度旁，如 ``°C``）。
        thresholds: 报警阈值 ``[(值, 标签)]``，画成红色虚线。
        now: 当前时刻（用于判断"最后一个点是不是过期了"）。
        stale_after_s: 超过这么久没新数据 ⇒ 整条线转灰 + 标"数据已过期"。
        max_gap_s: 相邻两点间隔超过它就**断线**（时间上有洞 ⇒ 数据有洞）。
            默认 ``None`` = 自动按"中位采样间隔 × 3"推算；显式传 ``0`` 可关掉这条判据。
            ⚠️ 别轻易关：历史库只存成功读数，停摆期间**没有行**，
            只靠 ``value=None`` 是防不住"把 10 分钟停摆画成一条直线"的（见 :func:`_auto_max_gap`）。
    """
    if not points:
        return _empty_svg(width, height, empty_note)

    gap = 0.0 if max_gap_s == 0 else (max_gap_s if max_gap_s is not None else _auto_max_gap(points))
    segments = _segments(points, gap if gap else None)
    if not segments:
        return _empty_svg(width, height, empty_note)

    values = [v for seg in segments for _, v in seg]
    lo, hi = _scale((min(values), max(values)), y_min, y_max)
    t0 = min(ts for seg in segments for ts, _ in seg)
    t1 = max(ts for seg in segments for ts, _ in seg)
    if t1 - t0 < 1e-9:
        t1 = t0 + 1.0

    pad_l, pad_r, pad_t, pad_b = 46.0, 34.0, 16.0, 22.0
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    def sx(ts: float) -> float:
        return pad_l + (ts - t0) / (t1 - t0) * plot_w

    def sy(val: float) -> float:
        return pad_t + (hi - val) / (hi - lo) * plot_h

    # ---- 过期判定：可以显示旧值，但必须一眼看出它旧 ----
    last_ts = max(ts for seg in segments for ts, _ in seg)
    stale = (
        stale_after_s is not None
        and now is not None
        and (float(now) - last_ts) > float(stale_after_s)
    )
    line_color = _COLORS["line_stale"] if stale else _COLORS["line"]

    parts: List[str] = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
        f'style="width:100%;height:auto;display:block">',
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#fff" '
        f'stroke="{_COLORS["grid"]}"/>',
    ]

    # ---- Y 轴：三条参考线 + 刻度（含单位）----
    for frac in (0.0, 0.5, 1.0):
        val = lo + (hi - lo) * frac
        y = sy(val)
        parts.append(
            f'<line x1="{pad_l:.1f}" y1="{y:.1f}" x2="{pad_l + plot_w:.1f}" y2="{y:.1f}" '
            f'stroke="{_COLORS["grid"]}" stroke-width="1"/>'
        )
        label = f"{_fmt(val)}{unit}" if unit else _fmt(val)
        parts.append(
            f'<text x="{pad_l - 6:.1f}" y="{y + 4:.1f}" text-anchor="end" '
            f'font-size="11" fill="{_COLORS["axis"]}">{_esc(label)}</text>'
        )

    # ---- 报警阈值虚线：一眼看出"什么时候会报警" ----
    # 量程**之内**的画成虚线；量程**之外**的**不丢弃**，而是标在上下边缘（见下）。
    # 为什么不能丢：2026-10-01 用真实数据验出来 —— 湿度实测在 57.9~59.1% 之间跳，
    # 而报警阈值是 80% ⇒ 阈值在量程外。若直接不画，用户**看不出一离报警还有多远**，
    # 而自动缩放又把 DHT11 那 1% 的正常台阶放得像剧烈波动（两件事凑一起就是误导）。
    out_of_range: List[Tuple[float, str]] = []
    for tval, tlabel in thresholds:
        if not (lo <= float(tval) <= hi):
            out_of_range.append((float(tval), tlabel))
            continue
        y = sy(float(tval))
        parts.append(
            f'<line x1="{pad_l:.1f}" y1="{y:.1f}" x2="{pad_l + plot_w:.1f}" y2="{y:.1f}" '
            f'stroke="{_COLORS["threshold"]}" stroke-width="1.2" stroke-dasharray="6 4"/>'
        )
        parts.append(
            f'<text x="{pad_l + plot_w - 4:.1f}" y="{y - 3:.1f}" text-anchor="end" '
            f'font-size="11" fill="{_COLORS["threshold"]}">{_esc(tlabel)}</text>'
        )
    for tval, tlabel in out_of_range:
        above = tval > hi
        y = pad_t + 9.0 if above else height - pad_b - 3.0
        arrow = "↑" if above else "↓"
        parts.append(
            f'<text x="{pad_l + 4:.1f}" y="{y:.1f}" font-size="11" '
            f'fill="{_COLORS["threshold"]}">{_esc(arrow + " " + tlabel)}</text>'
        )

    # ---- 曲线：**每个连续段各画各的**（缺口处自然断开）----
    for seg in segments:
        if len(seg) == 1:                    # 单点画一个小圆，否则看不出有数据
            x, y = sx(seg[0][0]), sy(seg[0][1])
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.5" fill="{line_color}"/>')
            continue
        coords = " ".join(f"{sx(ts):.1f},{sy(v):.1f}" for ts, v in seg)
        parts.append(
            f'<polyline points="{coords}" fill="none" stroke="{line_color}" '
            f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>'
        )

    # ---- 最后一个点：标出来（并带上"多久以前"）----
    # 若最后一段只有孤零零一个点，它上面已经画过圆点了，这里就别再叠一个。
    if len(segments[-1]) > 1:
        last_val = segments[-1][-1][1]
        x_last, y_last = sx(last_ts), sy(last_val)
        parts.append(
            f'<circle cx="{x_last:.1f}" cy="{y_last:.1f}" r="3.5" fill="{line_color}" '
            f'stroke="#fff" stroke-width="1.5"/>'
        )
    if stale and now is not None:
        mins = (float(now) - last_ts) / 60.0
        age = f"{mins:.0f} 分钟前" if mins >= 1 else f"{(float(now) - last_ts):.0f} 秒前"
        parts.append(
            f'<text x="{pad_l + 2:.1f}" y="{pad_t + 12:.1f}" font-size="12" '
            f'fill="{_COLORS["line_stale"]}">数据已过期（最后 {_esc(age)}）</text>'
        )

    parts.append("</svg>")
    return "".join(parts)


def sparkline(values: Iterable[Optional[float]], *, width: int = 120, height: int = 28) -> str:
    """卡片里的迷你趋势线（无坐标轴、无文字）。同样**缺口断线**。"""
    pts = list(values)
    if not any(v is not None for v in pts):
        return ""
    data = [(float(i), v) for i, v in enumerate(pts)]
    segments = _segments(data)
    flat = [v for seg in segments for _, v in seg]
    lo, hi = _scale((min(flat), max(flat)), None, None)
    total = max(len(pts) - 1, 1)
    parts: List[str] = []
    for seg in segments:
        if len(seg) < 2:
            continue
        coords = " ".join(
            f"{(i / total) * width:.1f},{(height - (v - lo) / (hi - lo) * height):.1f}"
            for i, v in seg
        )
        parts.append(
            f'<polyline points="{coords}" fill="none" stroke="{_COLORS["line"]}" '
            f'stroke-width="1.6" stroke-linejoin="round"/>'
        )
    if not parts:
        # 全是孤点（或只有 1 个点）：没有线可画 ⇒ 返回空串，
        # 别往页面里塞一个**空的 SVG 元素**（那会占位、还会让调用方以为"有图"）。
        return ""
    return (
        f'<svg viewBox="0 0 {width} {height}" style="width:100%;height:auto;display:block" '
        f'role="img" aria-label="趋势">' + "".join(parts) + "</svg>"
    )
