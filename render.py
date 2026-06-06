"""Render a vibecheck JSON payload to a single self-contained HTML file.

Design language inspired by https://vibecheck-bot.com (cream + black,
Bebas Neue / IBM Plex Mono / Instrument Sans, 4-col poster grid with
3px colored top stripes per card, GitHub-style activity heatmap).

Usage:
    python3 render.py data/kn0thing.json
    python3 render.py data/*.json
"""

from __future__ import annotations

import html
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_BULLET_PREFIX = re.compile(r"^[•\-\*➤]\s*")


# ---------- helpers ----------

def esc(s: Any) -> str:
    return html.escape("" if s is None else str(s))


def md_inline(s: str) -> str:
    s = esc(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    return s


def fmt_int(n: Any) -> str:
    try:
        return f"{int(n):,}"
    except Exception:
        return esc(n)


def fmt_compact(n: Any) -> str:
    """1234567 -> 1.2M, 12345 -> 12.3K."""
    try:
        n = int(n)
    except Exception:
        return esc(n)
    sign = "-" if n < 0 else ""
    n = abs(n)
    if n >= 1_000_000:
        s = f"{n / 1_000_000:.1f}"
        if s.endswith(".0"):
            s = s[:-2]
        return f"{sign}{s}M"
    if n >= 10_000:
        return f"{sign}{n // 1_000}K"
    if n >= 1_000:
        return f"{sign}{n / 1_000:.1f}K"
    return f"{sign}{n}"


def fmt_ts(ts: int | float | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%b %d, %Y")


def sep(num: str, label: str) -> str:
    return (
        f'<div class="section-sep">'
        f'  <span class="sep-num">{esc(num)}</span>'
        f'  <span class="sep-line"></span>'
        f'  <h2 class="sep-label">{esc(label)}</h2>'
        f"</div>"
    )


# ---------- sections ----------

def section_hero(r: dict) -> str:
    p = r.get("profile", {}) or {}
    display = (p.get("display_name") or "").strip()
    handle = esc(r.get("username"))
    # Match vibecheck-bot.com hero: BIG Bebas display name (or username) with
    # the mono "u/handle" sitting on the same baseline beside it.
    if display:
        heading = (
            f'<h1 class="user-display-name">{esc(display)}</h1>'
            f'<span class="user-name-disp">u/{handle}</span>'
        )
    else:
        heading = (
            f'<h1 class="user-name-disp user-name-disp--solo">'
            f'<span class="udn-slash">u/</span>{handle}</h1>'
        )
    sub_handle = ""

    nsfw_meta = r.get("nsfw") or {}
    nsfw_badge = ""
    if nsfw_meta.get("account_flagged"):
        n = nsfw_meta.get("post_count", 0)
        pct = nsfw_meta.get("post_pct", 0)
        nsfw_badge = (
            f'<span class="nsfw-badge" title="{n} of this user\'s posts are '
            f'flagged over_18 ({pct}%)">18+</span>'
        )

    hidden = r.get("hidden_subreddits") or {}
    visible = r.get("visible_subreddits") or {}
    all_hidden = r.get("all_history_hidden")

    def chip(name: str, hidden_chip: bool) -> str:
        cls = "vc-chip vc-chip--hidden vis-chip" if hidden_chip else "vc-chip vc-chip--visible vis-chip"
        return (
            f'<a href="https://reddit.com/r/{esc(name)}" target="_blank" rel="noopener" '
            f'class="{cls}" data-sub="{esc(name)}">r/{esc(name)}</a>'
        )

    visibility_bar = ""
    if all_hidden:
        visibility_bar = (
            '<div class="vis-bar">'
            '<span class="vis-bar-label vis-bar-label--hidden">'
            'posting history fully hidden</span>'
            '</div>'
        )
    else:
        bars = []
        if hidden:
            top_hidden = sorted(hidden.items(),
                                key=lambda kv: kv[1].get("total", 0),
                                reverse=True)[:8]
            bars.append(
                '<div class="vis-bar">'
                '<span class="vis-bar-label vis-bar-label--hidden">'
                f'posting history hidden in</span>'
                f'{"".join(chip(n, True) for n, _ in top_hidden)}'
                + (f'<span class="vis-bar-more">+{len(hidden) - 8} more</span>'
                   if len(hidden) > 8 else "")
                + '</div>'
            )
        if visible:
            top_visible = sorted(
                visible.items(),
                key=lambda kv: (kv[1].get("posts", 0) or 0) + (kv[1].get("comments", 0) or 0),
                reverse=True,
            )[:8]
            bars.append(
                '<div class="vis-bar">'
                '<span class="vis-bar-label vis-bar-label--visible">'
                f'posting history enabled for</span>'
                f'{"".join(chip(n, False) for n, _ in top_visible)}'
                + (f'<span class="vis-bar-more">+{len(visible) - 8} more</span>'
                   if len(visible) > 8 else "")
                + '</div>'
            )
        visibility_bar = "".join(bars)

    return f"""
    <div class="hero">
      <div class="hero-name">
        {heading}
        {sub_handle}
        {nsfw_badge}
      </div>
      <a class="reddit-link" href="https://reddit.com/user/{esc(r.get("username"))}"
         target="_blank" rel="noopener">view on reddit ↗</a>
    </div>
    {visibility_bar}
    """


def section_summary(r: dict) -> str:
    bullets = r.get("profile_summary") or []
    if not bullets:
        return ""
    items = "".join(
        f'<div class="summary-item">{md_inline(_BULLET_PREFIX.sub("", b))}</div>'
        for b in bullets
    )
    return f'<div class="pcard pcard--full pcard--summary"><div class="cat">profile summary</div>{items}</div>'


def _stat(value: str, label: str, desc: str = "") -> str:
    desc_html = f'<div class="stat-desc">{esc(desc)}</div>' if desc else ""
    return f"""
    <div class="pcard pcard--stat">
      <div class="stat-lbl">{esc(label)}</div>
      <div class="stat-val">{value}</div>
      {desc_html}
    </div>
    """


def section_stats(r: dict) -> str:
    k = r.get("karma", {}) or {}
    c = r.get("corpus", {}) or {}
    p = r.get("profile", {}) or {}
    total_karma = (k.get("submission_karma") or 0) + (k.get("comment_karma") or 0)
    cells = [
        _stat(fmt_compact(total_karma), "total karma", "posts + comments"),
        _stat(
            esc(p.get("active_since") or "—").upper(),
            "active since",
            f"longest gap {fmt_int(p.get('longest_gap_days'))}d",
        ),
        _stat(
            fmt_compact(k.get("comment_karma", 0)),
            "comment karma",
            f"{fmt_int(k.get('comment_count'))} comments · {k.get('comment_avg', 0)} avg",
        ),
        _stat(
            fmt_compact(k.get("submission_karma", 0)),
            "submission karma",
            f"{fmt_int(k.get('submission_count'))} posts · {k.get('submission_avg', 0)} avg",
        ),
        _stat(
            fmt_compact(c.get("total_words", 0)),
            "words written",
            f"{c.get('unique_pct', 0)}% unique · ~{c.get('hours_typing', 0)}h typing",
        ),
        _stat(
            f"{c.get('karma_per_word', 0)}",
            "karma per word",
            "signal-to-effort ratio",
        ),
    ]
    return f'<div class="stat-grid">{"".join(cells)}</div>'


def section_heatmap(r: dict) -> str:
    by_day = (r.get("activity") or {}).get("by_day") or {}
    if not by_day:
        return ""

    # Parse all dates
    parsed: list[tuple[date, int]] = []
    for k, v in by_day.items():
        try:
            parsed.append((datetime.strptime(k, "%Y-%m-%d").date(), int(v)))
        except Exception:
            continue
    if not parsed:
        return ""
    parsed.sort()

    # Show full archive (no time cutoff), with a 365-day floor so brand-new
    # accounts still see a year's worth of cells — older weeks just render
    # as empty (level 0). Long-tenured users get their whole history.
    last_real = parsed[-1][0]
    first_real = parsed[0][0]
    min_start = last_real - timedelta(days=365)
    if first_real > min_start:
        first_real = min_start

    by_date = {d: v for d, v in parsed}

    # Align grid to weeks starting Monday
    grid_start = first_real - timedelta(days=first_real.weekday())
    grid_end = last_real + timedelta(days=(6 - last_real.weekday()))

    weeks = []
    cur = grid_start
    while cur <= grid_end:
        week = []
        for _ in range(7):
            v = by_date.get(cur, 0)
            week.append((cur, v))
            cur += timedelta(days=1)
        weeks.append(week)

    counts = list(by_date.values())
    if not counts:
        return ""
    cmax = max(counts)
    # 4-level quantile thresholds
    nonzero = sorted(c for c in counts if c > 0)
    t = nonzero
    th1 = t[len(t) // 4] if t else 1
    th2 = t[len(t) // 2] if t else 1
    th3 = t[(3 * len(t)) // 4] if t else 1

    def level(v: int) -> int:
        if v <= 0:
            return 0
        if v <= th1:
            return 1
        if v <= th2:
            return 2
        if v <= th3:
            return 3
        return 4

    # Month labels: one label at the first column of each month
    month_label_cols = {}
    for col_idx, week in enumerate(weeks):
        for d, _ in week:
            if d.day <= 7 and d.weekday() == 0:
                month_label_cols.setdefault(col_idx, MONTHS[d.month - 1])
                break

    # Render columns
    col_html = []
    for col_idx, week in enumerate(weeks):
        cells = "".join(
            f'<div class="heatmap-cell" data-level="{level(v)}" '
            f'data-date="{d.isoformat()}" data-count="{v}"></div>'
            for d, v in week
        )
        col_html.append(f'<div class="heatmap-col">{cells}</div>')

    months_row = ""
    last_label = None
    spans = []
    for col_idx in range(len(weeks)):
        lbl = month_label_cols.get(col_idx)
        if lbl and lbl != last_label:
            spans.append(f'<div class="hm-span">{esc(lbl)}</div>')
            last_label = lbl
        else:
            spans.append('<div class="hm-span"></div>')
    months_row = "".join(spans)

    legend = (
        '<div class="heatmap-legend">less '
        '<div class="heatmap-cell" data-level="0"></div>'
        '<div class="heatmap-cell" data-level="1"></div>'
        '<div class="heatmap-cell" data-level="2"></div>'
        '<div class="heatmap-cell" data-level="3"></div>'
        '<div class="heatmap-cell" data-level="4"></div>'
        ' more</div>'
    )

    return f"""
    <div class="pcard pcard--full pcard--time">
      <div class="cat">activity heatmap · last {len(weeks)} weeks</div>
      <div class="heatmap-scroll">
        <div class="heatmap-months">{months_row}</div>
        <div class="heatmap-grid">{''.join(col_html)}</div>
      </div>
      {legend}
      <div class="psub">
        {esc(first_real.isoformat())} → {esc(last_real.isoformat())}
        · peak {fmt_int(cmax)} actions in a day
      </div>
    </div>
    """


def _stacked_bars_svg(
    per_sub: dict[str, list[int]],
    n_buckets: int,
    labels: list[str],
    eyebrow: str,
    data_kind: str,  # "weekday" or "hour"
    palette: list[str],
    width: int = 460,
    height: int = 200,
) -> str:
    """Render a stacked-by-subreddit bar chart as inline SVG with Y-axis."""
    PADL, PADR, PADT, PADB = 36, 12, 12, 28
    plot_w = width - PADL - PADR
    plot_h = height - PADT - PADB
    bar_gap = 4 if n_buckets <= 7 else 2
    bar_w = (plot_w - bar_gap * (n_buckets - 1)) / n_buckets
    # Top N subs by total contribution (across all buckets)
    totals = {s: sum(vals) for s, vals in per_sub.items()}
    N_TOP = 6
    top_subs = sorted(totals, key=lambda s: totals[s], reverse=True)[:N_TOP]
    top_set = set(top_subs)
    other_vals = [0] * n_buckets
    for s, vals in per_sub.items():
        if s not in top_set:
            for i, v in enumerate(vals):
                other_vals[i] += v
    series = [(s, per_sub[s]) for s in top_subs]
    if any(other_vals):
        series.append(("Other", other_vals))

    # Per-bucket totals -> y_max
    bucket_totals = [
        sum(per_sub[s][i] for s in per_sub) for i in range(n_buckets)
    ]
    y_max = max(bucket_totals) or 1
    # Round y_max up to a nice tick number
    import math
    mag = 10 ** max(0, int(math.log10(y_max)))
    y_max = math.ceil(y_max / (mag / 2)) * (mag / 2)

    def x_for(i: int) -> float:
        return PADL + i * (bar_w + bar_gap)

    def y_for(v: float) -> float:
        return PADT + plot_h - (v / y_max) * plot_h

    # Grid + Y labels (5 ticks)
    grid = []
    for k in range(5):
        v = (y_max * k) / 4
        y = y_for(v)
        grid.append(
            f'<line x1="{PADL}" x2="{width - PADR}" y1="{y:.1f}" y2="{y:.1f}" '
            f'stroke="rgba(0,0,0,0.06)"/>'
            f'<text x="{PADL - 6}" y="{y + 3:.1f}" text-anchor="end" '
            f'font-size="9.5" fill="#888" font-family="IBM Plex Mono, monospace">'
            f'{int(v) if v == int(v) else f"{v:.1f}"}</text>'
        )

    # X-axis labels — show every Nth depending on density
    xticks = []
    step = 1 if n_buckets <= 12 else 3
    for i, lab in enumerate(labels):
        if i % step == 0:
            xc = x_for(i) + bar_w / 2
            xticks.append(
                f'<text x="{xc:.1f}" y="{PADT + plot_h + 16}" text-anchor="middle" '
                f'font-size="10" fill="#666" font-family="IBM Plex Mono, monospace">'
                f"{esc(lab)}</text>"
            )

    # Stack bars
    bar_groups = []
    base = [0.0] * n_buckets
    for idx, (name, vals) in enumerate(series):
        color = palette[idx % len(palette)]
        rects = []
        for i, v in enumerate(vals):
            if v <= 0:
                continue
            yt = y_for(base[i] + v)
            h = y_for(base[i]) - yt
            x = x_for(i)
            click_attr = ""
            if data_kind == "weekday":
                click_attr = f'data-weekday="{i}"'
            elif data_kind == "hour":
                click_attr = f'data-hour="{i}"'
            rects.append(
                f'<rect class="sb-rect" {click_attr} '
                f'x="{x:.1f}" y="{yt:.1f}" width="{bar_w:.1f}" height="{h:.1f}" '
                f'fill="{color}"><title>r/{esc(name)}: {fmt_int(v)}</title></rect>'
            )
        bar_groups.append("".join(rects))
        for i, v in enumerate(vals):
            base[i] += v

    # Legend (compact, below)
    legend = "".join(
        f'<span class="sb-legend-item">'
        f'<span class="sb-sw" style="background:{palette[i % len(palette)]}"></span>'
        f'{esc("Other" if name == "Other" else "r/" + name)}'
        f"</span>"
        for i, (name, _) in enumerate(series)
    )

    return f"""
    <div class="pcard pcard--time">
      <div class="cat">{esc(eyebrow)}</div>
      <svg viewBox="0 0 {width} {height}" preserveAspectRatio="xMidYMid meet" class="sb-svg">
        {''.join(grid)}
        <g>{''.join(bar_groups)}</g>
        <line x1="{PADL}" x2="{PADL}" y1="{PADT}" y2="{PADT + plot_h}" stroke="rgba(0,0,0,0.25)"/>
        <line x1="{PADL}" x2="{width - PADR}" y1="{PADT + plot_h}" y2="{PADT + plot_h}" stroke="rgba(0,0,0,0.25)"/>
        {''.join(xticks)}
      </svg>
      <div class="sb-legend">{legend}</div>
    </div>
    """


def section_temporal(r: dict) -> str:
    a = r.get("activity") or {}
    by_weekday_sub = a.get("by_weekday_sub") or {}
    by_hour_sub = a.get("by_hour_sub") or {}
    by_weekday = a.get("by_weekday") or [0] * 7
    by_hour = a.get("by_hour") or [0] * 24
    if not (any(by_weekday) or any(by_hour)):
        return ""

    # If per-sub breakdowns aren't available, fall back to total-only
    if not by_weekday_sub:
        by_weekday_sub = {"all": by_weekday}
    if not by_hour_sub:
        by_hour_sub = {"all": by_hour}

    wd_html = _stacked_bars_svg(
        by_weekday_sub, 7, WEEKDAYS,
        "by weekday", "weekday", SERIES_PALETTE,
    )
    hr_labels = [f"{h}" if h % 3 == 0 else "" for h in range(24)]
    hr_html = _stacked_bars_svg(
        by_hour_sub, 24, hr_labels,
        "by hour (utc)", "hour", SERIES_PALETTE,
    )

    tz = r.get("timezone_guess") or {}
    tz_block = ""
    if tz:
        tz_block = (
            '<div class="pcard pcard--time tz-pcard">'
            '<div class="cat">timezone guess</div>'
            f'<div class="tz-name">{esc(tz.get("timezone") or "?")}</div>'
            f'<div class="tz-desc">{esc(tz.get("description"))}</div>'
            "</div>"
        )
    return f"""
    <div class="two-col">{wd_html}{hr_html}</div>
    {tz_block}
    """


SERIES_PALETTE = [
    # Categorical palette, Apple-muted hues that play well on warm cream.
    "#1B5FA8",  # blue
    "#C0392B",  # red
    "#2E7D43",  # green
    "#C5751D",  # amber
    "#8E4EC6",  # purple
    "#207676",  # teal
    "#D4A017",  # gold
    "#C13584",  # pink
    "#5B7C99",  # slate (Other)
]


def _compute_karma_series(r: dict) -> dict | None:
    """Pre-compute monthly per-sub karma deltas (NOT cumulative).

    For the bar chart: each bar = one month, stacked by community, showing
    karma earned IN that month (not running total). Returned dict feeds
    both the SVG renderer and the JS overlay (hover tooltip, legend toggle)."""
    tl = r.get("timeline") or []
    events: list[tuple[int, str, int]] = []
    for e in tl:
        ts = e.get("ts")
        sub = e.get("sub")
        if ts and sub:
            events.append((int(ts), sub, int(e.get("score") or 0)))
    if len(events) < 2:
        return None
    events.sort()

    def month_idx(ts: int) -> int:
        d = datetime.fromtimestamp(ts, tz=timezone.utc)
        return d.year * 12 + d.month - 1

    m0 = month_idx(events[0][0])
    mN = month_idx(events[-1][0])
    n_buckets = mN - m0 + 1
    if n_buckets < 2:
        return None

    from collections import defaultdict
    per_sub: dict[str, list[int]] = defaultdict(lambda: [0] * n_buckets)
    for ts, sub, score in events:
        per_sub[sub][month_idx(ts) - m0] += score
    totals = {sub: sum(deltas) for sub, deltas in per_sub.items()}

    N_TOP = 8
    top_subs = sorted(totals, key=lambda s: totals[s], reverse=True)[:N_TOP]
    top_set = set(top_subs)
    other_deltas = [0] * n_buckets
    for sub, deltas in per_sub.items():
        if sub not in top_set:
            for i, d in enumerate(deltas):
                other_deltas[i] += d

    series_specs: list[tuple[str, list[int]]] = [(s, per_sub[s]) for s in top_subs]
    if any(other_deltas):
        series_specs.append(("Other", other_deltas))

    # Per-month deltas — clamp negatives at 0 (we're showing karma EARNED;
    # a bad month with net-negative comments would distort the bar baseline).
    cum_series: list[dict] = []
    for name, deltas in series_specs:
        cum_series.append({"name": name, "cum": [max(0, d) for d in deltas]})

    stack_totals = [
        sum(s["cum"][i] for s in cum_series) for i in range(n_buckets)
    ]
    # y_max policy: by default fit the actual peak so hover totals match the
    # axis. Only invoke the soft cap when the peak is a true outlier (≥3×
    # p90), which is the "one viral month dwarfs everything" pattern; in
    # that case the chart caps near p90 to keep the bulk visible and the
    # outlier gets a "↑value" marker.
    positive = sorted(t for t in stack_totals if t > 0)
    actual_max = max(stack_totals) or 1
    if len(positive) >= 5:
        p90 = positive[int(len(positive) * 0.9)]
        median = positive[len(positive) // 2]
        if actual_max > p90 * 3:                       # clear outlier
            y_max = max(median * 8, p90 * 1.5)
        else:
            y_max = actual_max
    else:
        y_max = actual_max

    # Round up to a "nice" tick boundary so the 5-tick grid lands on round
    # numbers (1M, 2M, 5M, 100K …) — eliminates the formatter mismatch
    # between the y-axis (Python fmt_compact) and the hover tooltip (JS
    # fmtK) at borderline values.
    import math as _math
    pow10 = 10 ** max(0, int(_math.log10(y_max)))
    for mult in (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if mult * pow10 >= y_max:
            y_max = int(mult * pow10)
            break

    return {
        "m0": m0,
        "mN": mN,
        "n_buckets": n_buckets,
        "series": cum_series,
        "stack_totals": stack_totals,
        "y_max": y_max,
        "palette": SERIES_PALETTE[: len(cum_series)],
        # Geometry constants used by both the SVG renderer and JS overlay
        "W": 900, "H": 280,
        "PADL": 64, "PADR": 16, "PADT": 12, "PADB": 36,
    }


def section_karma_over_time(r: dict, karma_data: dict | None = None) -> str:
    """Per-month stacked BAR chart of karma earned, by community."""
    if karma_data is None:
        karma_data = _compute_karma_series(r)
    if karma_data is None:
        return ""

    m0 = karma_data["m0"]
    mN = karma_data["mN"]
    n_buckets = karma_data["n_buckets"]
    series = karma_data["series"]
    stack_totals = karma_data["stack_totals"]
    y_max = karma_data["y_max"]
    H = karma_data["H"]
    PADL, PADR, PADT, PADB = (
        karma_data["PADL"], karma_data["PADR"],
        karma_data["PADT"], karma_data["PADB"],
    )

    # Keep bars at a readable minimum width. For long histories (GallowBoob
    # has 141 months, kn0thing has 240+), this grows W beyond the 900 default
    # so the SVG ends up wider than its container — the wrapper scrolls.
    MIN_BAR_W = 5.0
    bar_gap = 1.5 if n_buckets > 60 else 2.5
    W = max(karma_data["W"],
            int(PADL + PADR + n_buckets * (MIN_BAR_W + bar_gap)))
    karma_data["W"] = W   # propagate so the embedded JS uses the same width

    plot_w = W - PADL - PADR
    plot_h = H - PADT - PADB
    bar_w = max(MIN_BAR_W, (plot_w - bar_gap * (n_buckets - 1)) / n_buckets)
    pitch = bar_w + bar_gap

    def x_for(i: int) -> float:
        return PADL + i * pitch

    def y_for(v: float) -> float:
        return PADT + plot_h - (v / y_max) * plot_h

    # Y-axis gridlines and labels
    grid = []
    for k in range(5):
        v = (y_max * k) / 4
        y = y_for(v)
        grid.append(
            f'<line x1="{PADL}" x2="{W - PADR}" y1="{y:.1f}" y2="{y:.1f}" '
            f'stroke="rgba(0,0,0,0.06)"/>'
            f'<text x="{PADL - 8}" y="{y + 3:.1f}" text-anchor="end" '
            f'font-size="10" fill="#888" font-family="IBM Plex Mono, monospace">'
            f'{esc(fmt_compact(v))}</text>'
        )

    # X-axis: year ticks (each January)
    xticks = []
    for i, m in enumerate(range(m0, mN + 1)):
        if m % 12 == 0:
            yr = m // 12
            xc = x_for(i) + bar_w / 2
            xticks.append(
                f'<line x1="{xc:.1f}" x2="{xc:.1f}" y1="{PADT + plot_h}" '
                f'y2="{PADT + plot_h + 4}" stroke="#aaa"/>'
                f'<text x="{xc:.1f}" y="{PADT + plot_h + 18}" text-anchor="middle" '
                f'font-size="10.5" fill="#666" font-family="IBM Plex Mono, monospace">'
                f"{yr}</text>"
            )

    # Build stacked bars: one <g class="kg-poly"> per series
    bar_groups = []
    base = [0.0] * n_buckets
    legend = []
    for idx, ser in enumerate(series):
        name = ser["name"]
        vals = ser["cum"]
        color = SERIES_PALETTE[idx % len(SERIES_PALETTE)]
        rects = []
        for i, v in enumerate(vals):
            if v <= 0:
                continue
            # Clamp the top edge to the plot area so outliers visually clip
            y_top = max(PADT, y_for(base[i] + v))
            y_bot = y_for(base[i])
            h = y_bot - y_top
            if h <= 0:
                continue
            x = x_for(i)
            rects.append(
                f'<rect class="kg-rect" data-month-idx="{i}" '
                f'x="{x:.1f}" y="{y_top:.1f}" '
                f'width="{bar_w:.1f}" height="{h:.1f}" fill="{color}"/>'
            )
        bar_groups.append(
            f'<g class="kg-poly" data-series-idx="{idx}" '
            f'data-series-name="{esc(name)}" fill-opacity="0.92">'
            f'{"".join(rects)}</g>'
        )
        total = sum(vals)
        label = name if name == "Other" else f"r/{name}"
        legend.append(
            f'<span class="kg-legend-item" data-series-idx="{idx}" '
            f'data-series-name="{esc(name)}" title="click to toggle">'
            f'<span class="kg-swatch" style="background:{color}"></span>'
            f'<span class="kg-name">{esc(label)}</span>'
            f'<span class="kg-val">{fmt_compact(total)}</span>'
            f"</span>"
        )
        for i, v in enumerate(vals):
            base[i] += v

    # Annotate clipped bars. For power-user accounts dozens of months exceed
    # the cap; labeling all of them produces a wall of overlapping text. Only
    # show the top 5 by magnitude — the rest are just "much taller than the
    # chart" and the hover tooltip carries the exact value anyway.
    candidates = [(i, t) for i, t in enumerate(stack_totals) if t > y_max * 1.05]
    candidates.sort(key=lambda kv: -kv[1])
    clip_markers = []
    for i, t in candidates[:5]:
        xc = x_for(i) + bar_w / 2
        clip_markers.append(
            f'<text x="{xc:.1f}" y="{PADT - 4:.1f}" text-anchor="middle" '
            f'font-size="10" font-weight="600" fill="{SERIES_PALETTE[0]}" '
            f'font-family="IBM Plex Mono, monospace">'
            f'↑{esc(fmt_compact(t))}</text>'
        )

    def _label_month(idx: int) -> str:
        y, m = divmod(idx, 12)
        return f"{MONTHS[m]} {y}"

    n_top = sum(1 for s in series if s["name"] != "Other")
    peak_month = stack_totals.index(max(stack_totals))
    return f"""
    <div class="pcard pcard--full pcard--karma" id="karma-card">
      <div class="cat">karma earned per month · top {n_top} communities + other · hover & click legend</div>
      <div class="karma-svg-scroll">
        <svg viewBox="0 0 {W} {H}" preserveAspectRatio="xMinYMid meet" class="karma-svg" id="karma-svg" width="{W}" height="{H}">
          {''.join(grid)}
          <g class="kg-polys">{''.join(bar_groups)}</g>
          <g class="kg-clip-markers">{''.join(clip_markers)}</g>
          <line x1="{PADL}" x2="{PADL}" y1="{PADT}" y2="{PADT + plot_h}" stroke="rgba(0,0,0,0.25)"/>
          <line x1="{PADL}" x2="{W - PADR}" y1="{PADT + plot_h}" y2="{PADT + plot_h}" stroke="rgba(0,0,0,0.25)"/>
          {''.join(xticks)}
          <line class="kg-crosshair" x1="0" x2="0" y1="{PADT}" y2="{PADT + plot_h}"
                stroke="rgba(0,0,0,0.4)" stroke-width="1" stroke-dasharray="2,3"
                style="display:none;pointer-events:none"/>
        </svg>
      </div>
      <div class="kg-legend">{''.join(legend)}</div>
      <div class="psub">
        {esc(_label_month(m0))} → {esc(_label_month(mN))} · {n_buckets} months ·
        peak {fmt_int(max(stack_totals))} karma in {esc(_label_month(m0 + peak_month))}
      </div>
    </div>
    """


def section_best_worst(r: dict) -> str:
    bw = r.get("best_worst") or {}
    if not any(bw.values()):
        return ""

    def card(eyebrow: str, item: dict | None, kind: str) -> str:
        if not item:
            return (
                f'<div class="pcard pcard--{kind}">'
                f'<div class="cat">{esc(eyebrow)}</div>'
                f'<div class="bw-empty">none</div></div>'
            )
        text = (item.get("title") or item.get("body") or "").strip()
        score = item.get("score", 0)
        score_cls = "score-pos" if (score or 0) > 0 else "score-neg" if (score or 0) < 0 else "score-neu"
        return f"""
        <div class="pcard pcard--{kind}">
          <div class="cat">{esc(eyebrow)}</div>
          <div class="bw-score {score_cls}">{esc(score):>0}{"" if isinstance(score, str) else ""}</div>
          <div class="bw-score-val pnum pnum--md">{fmt_int(score)}</div>
          <div class="bw-body clamp4">{esc(text)}</div>
          <div class="bw-meta">r/{esc(item.get("subreddit"))} · {esc(fmt_ts(item.get("created_utc")))}</div>
        </div>
        """

    return f"""
    <div class="four-col">
      {card("best comment", bw.get("best_comment"), "pos")}
      {card("worst comment", bw.get("worst_comment"), "neg")}
      {card("best submission", bw.get("best_submission"), "pos")}
      {card("worst submission", bw.get("worst_submission"), "neg")}
    </div>
    """


def section_subreddit_timeline(r: dict, max_subs: int = 10) -> str:
    """Strip plot: one row per top sub, each event a dot positioned by time.

    Inspired by the GitHub punch-card style — lets you spot 'this sub was hot
    for 6 months then went dormant' patterns that the heatmap (which collapses
    across subs) can't show.
    """
    tl = r.get("timeline") or []
    if not tl:
        return ""

    # Group events by sub, keep only top N by event count.
    sub_events: dict[str, list[int]] = {}
    for e in tl:
        sub = e.get("sub")
        ts = e.get("ts")
        if not sub or ts is None:
            continue
        sub_events.setdefault(sub, []).append(int(ts))
    if not sub_events:
        return ""
    top = sorted(sub_events.items(), key=lambda x: -len(x[1]))[:max_subs]

    all_ts = [t for _, evs in top for t in evs]
    tmin, tmax = min(all_ts), max(all_ts)
    if tmin == tmax:
        return ""

    # Layout
    W = 960
    LEFT_PAD, RIGHT_PAD = 130, 24
    TOP_PAD, BOTTOM_PAD = 14, 28
    ROW_H = 26
    plot_w = W - LEFT_PAD - RIGHT_PAD
    H = TOP_PAD + len(top) * ROW_H + BOTTOM_PAD

    # Gridlines: years for spans >= 2 years, months for shorter spans.
    span_days = (tmax - tmin) / 86400
    tick_ts: list[tuple[int, str]] = []
    if span_days >= 730:  # >= 2 years → year ticks
        y0 = datetime.fromtimestamp(tmin, tz=timezone.utc).year
        y1 = datetime.fromtimestamp(tmax, tz=timezone.utc).year
        for year in range(y0, y1 + 2):
            ts_year = int(datetime(year, 1, 1, tzinfo=timezone.utc).timestamp())
            if tmin <= ts_year <= tmax:
                tick_ts.append((ts_year, str(year)))
    else:  # short range → month ticks (e.g. "Apr '26")
        dt = datetime.fromtimestamp(tmin, tz=timezone.utc).replace(day=1, hour=0, minute=0, second=0)
        end = datetime.fromtimestamp(tmax, tz=timezone.utc)
        while dt <= end:
            ts_m = int(dt.timestamp())
            if tmin <= ts_m <= tmax:
                label = f"{MONTHS[dt.month - 1]} '{dt.year % 100:02d}"
                tick_ts.append((ts_m, label))
            # advance one month
            dt = (dt.replace(day=28) + timedelta(days=4)).replace(day=1)

    ticks = ""
    for ts_tick, label in tick_ts:
        x = LEFT_PAD + (ts_tick - tmin) / (tmax - tmin) * plot_w
        ticks += (
            f'<line x1="{x:.1f}" y1="{TOP_PAD}" x2="{x:.1f}" y2="{H - BOTTOM_PAD + 2}" '
            f'stroke="var(--line-soft)" stroke-dasharray="2,3"/>'
            f'<text x="{x:.1f}" y="{H - 10}" text-anchor="middle" class="srt-year">{label}</text>'
        )

    # Per-row dots + sub label
    rows_svg = ""
    for i, (sub, evs) in enumerate(top):
        color = SERIES_PALETTE[i % len(SERIES_PALETTE)]
        y = TOP_PAD + i * ROW_H + ROW_H // 2
        # Faint baseline so the row is visible even if events cluster
        rows_svg += (
            f'<line x1="{LEFT_PAD}" y1="{y}" x2="{W - RIGHT_PAD}" y2="{y}" '
            f'stroke="var(--line-soft)" stroke-width="0.5"/>'
        )
        rows_svg += (
            f'<text x="{LEFT_PAD - 8}" y="{y + 4}" text-anchor="end" '
            f'class="srt-label">r/{esc(sub)} <tspan class="srt-count">{len(evs):,}</tspan></text>'
        )
        # Dots (no per-event <title> — would balloon DOM at 30K+ events)
        for ts in evs:
            x = LEFT_PAD + (ts - tmin) / (tmax - tmin) * plot_w
            rows_svg += f'<circle cx="{x:.1f}" cy="{y}" r="2.4" fill="{color}" opacity="0.55"/>'

    return f"""
    <div class="pcard pcard--full pcard--chart">
      <div class="cat">activity timeline · top {len(top)} subs</div>
      <div class="srt-wrap">
        <svg width="{W}" height="{H}" viewBox="0 0 {W} {H}" class="srt-svg">
          {ticks}
          {rows_svg}
        </svg>
      </div>
    </div>
    """


def section_subreddits(r: dict) -> str:
    subs = r.get("subreddits") or []
    if not subs:
        return ""
    summaries = r.get("subreddit_summaries") or {}
    hidden = r.get("hidden_subreddits") or {}
    visible = r.get("visible_subreddits") or {}
    top = sorted(subs, key=lambda s: s.get("total", 0), reverse=True)[:10]
    rows = ""
    for rank_i, s in enumerate(top, 1):
        name = s.get("name", "")
        total = s.get("total", 0)
        summary = summaries.get(name, "")
        n_hidden = (hidden.get(name) or {}).get("total", 0)
        n_visible = (visible.get(name) or {}).get("posts", 0) + \
                    (visible.get(name) or {}).get("comments", 0)
        # Build expansion panel from top_posts / top_comments
        exp_posts = ""
        for p in (s.get("top_posts") or [])[:3]:
            pid = p.get("id", "")
            url = f"https://reddit.com/r/{name}/comments/{pid}/"
            exp_posts += (
                f'<a class="exp-item" href="{esc(url)}" target="_blank" rel="noopener">'
                f'<span class="exp-score">{fmt_int(p.get("score") or 0)}</span>'
                f'<span class="exp-text">{esc((p.get("title") or "")[:240])}</span>'
                f'</a>'
            )
        exp_comments = ""
        for c in (s.get("top_comments") or [])[:3]:
            cid = c.get("id", "")
            post_id = c.get("post_id", "")
            url = (f"https://reddit.com/r/{name}/comments/{post_id}/_/{cid}/"
                   if post_id else f"https://reddit.com/r/{name}/")
            body = (c.get("body") or "").replace("\n", " ")[:240]
            exp_comments += (
                f'<a class="exp-item" href="{esc(url)}" target="_blank" rel="noopener">'
                f'<span class="exp-score">{fmt_int(c.get("score") or 0)}</span>'
                f'<span class="exp-text">{esc(body)}</span>'
                f'</a>'
            )
        # (Expand panel is rendered as a separate <tr class="sub-expand-row"> below.)
        # First couple of top post titles, displayed in the "sample" cell
        sample_items = []
        for p in (s.get("top_posts") or [])[:2]:
            t = (p.get("title") or "").strip()
            if t:
                pid = p.get("id", "")
                sample_items.append(
                    f'<a class="sub-sample-link" target="_blank" rel="noopener" '
                    f'href="https://reddit.com/r/{esc(name)}/comments/{esc(pid)}/">'
                    f'{esc(t[:90])}</a>'
                )
        for c in (s.get("top_comments") or [])[:1]:
            t = (c.get("body") or "").replace("\n", " ").strip()
            if t:
                cid = c.get("id", "")
                pid = c.get("post_id", "")
                href = (f"https://reddit.com/r/{esc(name)}/comments/{esc(pid)}/_/{esc(cid)}/"
                        if pid else f"https://reddit.com/r/{esc(name)}/")
                sample_items.append(
                    f'<a class="sub-sample-link sub-sample-comment" target="_blank" rel="noopener" '
                    f'href="{href}">"{esc(t[:80])}"</a>'
                )
        sample_html = ('<div class="sub-sample">'
                       + " ".join(f'<span>›</span>{x}' for x in sample_items)
                       + '</div>') if sample_items else ""
        hidden_cell = (f'<td class="num cell-warn">{n_hidden}</td>'
                       if n_hidden else '<td class="num cell-dim">—</td>')
        expand_inner = ""
        if exp_posts or exp_comments:
            expand_inner = (
                '<div class="sub-expand-grid">'
                f'{f"<div class=exp-section><div class=exp-label>TOP POSTS</div>{exp_posts}</div>" if exp_posts else ""}'
                f'{f"<div class=exp-section><div class=exp-label>TOP COMMENTS</div>{exp_comments}</div>" if exp_comments else ""}'
                '</div>'
            )
        rows += f"""
        <tr class="sub-row" data-name="{esc(name)}"
             data-total="{total}" data-posts="{s.get('posts', 0)}"
             data-comments="{s.get('comments', 0)}"
             data-karma="{s.get('karma', 0)}"
             data-hidden="{n_hidden}" data-visible="{n_visible}">
          <td class="sub-rank">{rank_i:02d}</td>
          <td>
            <a class="sub-name-link" href="https://reddit.com/r/{esc(name)}" target="_blank" rel="noopener">r/{esc(name)}</a>
            <span class="sub-toggle">▸</span>
          </td>
          <td class="num">{fmt_int(s.get("posts"))}</td>
          <td class="num">{fmt_int(s.get("comments"))}</td>
          <td class="num cell-karma">{fmt_compact(s.get("karma"))}</td>
          {hidden_cell}
          <td class="sub-sample-cell">
            {f'<div class="sub-summary-txt">{esc(summary)}</div>' if summary else ''}
            {sample_html}
          </td>
        </tr>
        <tr class="sub-expand-row" data-belongs-to="{esc(name)}">
          <td colspan="7" class="sub-expand-cell">{expand_inner}</td>
        </tr>
        """
    controls = """
    <div class="sub-controls">
      <div class="sub-controls-group">
        <span class="ctrl-label">sort</span>
        <button class="ctrl-btn active" data-sort="total">total</button>
        <button class="ctrl-btn" data-sort="karma">karma</button>
        <button class="ctrl-btn" data-sort="posts">posts</button>
        <button class="ctrl-btn" data-sort="comments">comments</button>
        <button class="ctrl-btn" data-sort="hidden">hidden</button>
      </div>
      <div class="sub-controls-group">
        <span class="ctrl-label">show</span>
        <button class="ctrl-btn active" data-filter="all">all</button>
        <button class="ctrl-btn" data-filter="hidden">has hidden</button>
        <button class="ctrl-btn" data-filter="visible">visible only</button>
      </div>
    </div>
    """
    return f"""
    <div class="pcard pcard--full pcard--karma" id="subs-card">
      {controls}
      <div class="sub-table-wrap">
        <table class="sub-table">
          <thead>
            <tr>
              <th>#</th>
              <th>SUBREDDIT</th>
              <th class="num">POSTS</th>
              <th class="num">COMMENTS</th>
              <th class="num">KARMA</th>
              <th class="num">HIDDEN</th>
              <th>SAMPLE</th>
            </tr>
          </thead>
          <tbody class="sub-rows">{rows}</tbody>
        </table>
      </div>
    </div>
    """


def section_words(r: dict) -> str:
    w = r.get("words") or {}
    if not w:
        return ""
    # Filter out very short / purely-numeric tokens that aren't interesting
    filtered = [
        (word, count, posts)
        for word, (count, posts) in w.items()
        if len(word) >= 3 and not word.replace(",", "").isdigit()
    ]
    filtered.sort(key=lambda x: x[1], reverse=True)
    cloud_items = filtered[:30]
    table_items = filtered[:50]
    if not cloud_items:
        return ""
    cmax = cloud_items[0][1]
    cmin = cloud_items[-1][1]
    cspan = max(cmax - cmin, 1)
    # Non-linear scale: top word HUGE, descending sharply.
    # Maps count ∈ [cmin, cmax] → size ∈ [0.95rem, 5.2rem] via sqrt curve.
    import math
    cloud_tags = ""
    for word, count, posts in cloud_items:
        t = math.sqrt(max(0.0, (count - cmin) / cspan))
        size = 0.95 + t * 4.25
        cloud_tags += (
            f'<span class="wc-word" data-word="{esc(word)}" '
            f'style="font-size:{size:.2f}rem" '
            f'title="{fmt_int(count)} uses across {fmt_int(posts)} posts · '
            f'click to search timeline">'
            f"{esc(word)}</span>"
        )
    table_rows = "".join(
        f'<tr><td class="wt-rank">{i}</td>'
        f'<td class="wt-word"><span class="wc-word" data-word="{esc(word)}">{esc(word)}</span></td>'
        f'<td class="wt-freq">{fmt_int(count)}</td></tr>'
        for i, (word, count, _) in enumerate(table_items, 1)
    )
    c = r.get("corpus") or {}
    return f"""
    <div class="pcard pcard--full pcard--words" id="words-card">
      <div class="word-grid">
        <div class="word-grid-cloud">
          <div class="cat">word cloud</div>
          <div class="word-cloud">{cloud_tags}</div>
        </div>
        <div class="word-grid-table">
          <div class="cat">common words · top {len(table_items)}</div>
          <table class="wt-table">
            <thead><tr><th>#</th><th>word</th><th class="wt-freq">freq</th></tr></thead>
            <tbody>{table_rows}</tbody>
          </table>
        </div>
      </div>
      <div class="psub">{fmt_int(c.get("total_words", 0))} words written · {esc(c.get("unique_pct", 0))}% unique · ~{esc(c.get("hours_typing", 0))}h typing</div>
    </div>
    """


def section_visibility(r: dict) -> str:
    hidden = r.get("hidden_subreddits") or {}
    visible = r.get("visible_subreddits") or {}
    if not hidden and not visible:
        return ""
    flag = ""
    if r.get("all_history_hidden"):
        flag = '<span class="pbadge pbadge--hidden">all history hidden</span>'

    hid_chips = "".join(
        f'<span class="hidden-banner-sub vis-chip" data-sub="{esc(name)}" '
        f'title="click to highlight in subreddit list">r/{esc(name)} '
        f'<span style="opacity:.6">·{fmt_int(v.get("total"))}</span></span>'
        for name, v in sorted(hidden.items(),
                              key=lambda kv: kv[1].get("total", 0),
                              reverse=True)[:40]
    )
    vis_chips = "".join(
        f'<span class="hidden-banner-sub--visible vis-chip" data-sub="{esc(name)}" '
        f'title="click to highlight in subreddit list">r/{esc(name)} '
        f'<span style="opacity:.6">·{fmt_int((v.get("posts") or 0) + (v.get("comments") or 0))}</span></span>'
        for name, v in sorted(visible.items(),
                              key=lambda kv: (kv[1].get("posts", 0) or 0) + (kv[1].get("comments", 0) or 0),
                              reverse=True)[:40]
    )

    return f"""
    <div class="pcard pcard--full pcard--neg">
      <div class="cat">visibility {flag}</div>
      <div class="vis-row">
        <div class="vis-col">
          <div class="vis-label vis-label--hidden">hidden — {len(hidden)} subs</div>
          <div class="vis-chips">{hid_chips or '<span class="bw-empty">none</span>'}</div>
        </div>
        <div class="vis-col">
          <div class="vis-label vis-label--visible">visible — {len(visible)} subs</div>
          <div class="vis-chips">{vis_chips or '<span class="bw-empty">none</span>'}</div>
        </div>
      </div>
    </div>
    """


def section_domains(r: dict) -> str:
    d = r.get("domains") or {}
    if not d:
        return ""
    top = sorted(d.items(), key=lambda kv: kv[1], reverse=True)[:15]
    if not top:
        return ""
    dmax = top[0][1]
    rows = "".join(
        f'<div class="dom-row">'
        f'<div class="dom-name">{esc(dom)}</div>'
        f'<div class="dom-bar"><div class="dom-bar-fill" style="width:{(c / dmax) * 100:.1f}%"></div></div>'
        f'<div class="dom-count">{fmt_int(c)}</div>'
        f"</div>"
        for dom, c in top
    )
    return f"""
    <div class="pcard pcard--full pcard--chart">
      <div class="cat">outlinks</div>
      {rows}
    </div>
    """


def section_timeline(r: dict) -> str:
    tl = r.get("timeline") or []
    if not tl:
        return ""
    # Embed every event so the search box can actually find older posts.
    # JS caps the *visible* row count to 25 by default; hits over the cap
    # come into view as soon as the user filters. File size grows linearly
    # (~200 bytes/row) but the renderer stays the bottleneck-free path.
    recent = sorted(tl, key=lambda e: e.get("ts", 0), reverse=True)
    rows = ""
    for e in recent:
        body = (e.get("title") or e.get("body") or "").strip().replace("\n", " ")
        if len(body) > 200:
            body = body[:200] + "…"
        score = e.get("score", 0)
        score_cls = "score-pos" if (score or 0) > 0 else "score-neg" if (score or 0) < 0 else "score-neu"
        sub = e.get("sub") or ""
        etype = e.get("type") or ""
        rows += f"""
        <div class="tl-row" data-sub="{esc(sub)}" data-type="{esc(etype)}"
             data-ts="{int(e.get('ts') or 0)}"
             data-text="{esc((sub + ' ' + body).lower())}">
          <div class="tl-date">{esc(fmt_ts(e.get("ts")))}</div>
          <div class="tl-type">{esc(etype)}</div>
          <div class="tl-sub">r/{esc(sub)}</div>
          <div class="tl-score {score_cls}">{fmt_int(score)}</div>
          <div class="tl-body">{esc(body)}</div>
        </div>
        """
    # Top subs for the filter chip row
    sub_counter: dict[str, int] = {}
    for e in recent:
        s = e.get("sub") or ""
        if s:
            sub_counter[s] = sub_counter.get(s, 0) + 1
    chips = "".join(
        f'<button class="ctrl-chip" data-sub="{esc(s)}">r/{esc(s)} '
        f'<span class="muted">{n}</span></button>'
        for s, n in sorted(sub_counter.items(), key=lambda kv: -kv[1])[:12]
    )
    controls = f"""
    <div class="tl-controls">
      <input type="search" class="tl-search" placeholder="search timeline…">
      <div class="sub-controls-group">
        <span class="ctrl-label">type</span>
        <button class="ctrl-btn active" data-tl-type="all">all</button>
        <button class="ctrl-btn" data-tl-type="post">posts</button>
        <button class="ctrl-btn" data-tl-type="comment">comments</button>
      </div>
      <div class="tl-sub-chips">
        <button class="ctrl-chip active" data-sub="">all subs</button>
        {chips}
      </div>
    </div>
    """
    return f"""
    <div class="pcard pcard--full" id="timeline-card">
      <div class="cat">timeline · {fmt_int(len(tl))} events</div>
      {controls}
      <div class="tl-rows" data-cap="25">{rows}</div>
      <div class="tl-empty" hidden>no matching events</div>
      <button class="tl-show-more" hidden>show all {len(recent)} loaded events</button>
    </div>
    """


# ---------- assembly ----------

CSS = """
:root {
  /* cladlabs-style monochrome on warm cream */
  --bg: #F2F0E8;
  --bg-subtle: #EDEAE0;
  --surface: #FAF8F2;
  --dark: #0F0F0F;
  --text: #1A1A1A;
  --text-secondary: #5C5C5C;
  --text-tertiary: #999999;
  --line: rgba(60, 60, 67, 0.18);
  --line-soft: rgba(60, 60, 67, 0.10);
  /* Apple-muted accents, restored from full monochrome */
  --karma: #C5751D;   /* warm amber */
  --time:  #1463B5;   /* SF blue */
  --words: #2E7D43;   /* SF green */
  --chart: #8E4EC6;   /* muted purple */
  --pos:   #2E7D43;   /* sage */
  --neg:   #C0392B;   /* brick */
  --display: -apple-system, BlinkMacSystemFont, "SF Pro Display", "Helvetica Neue", system-ui, sans-serif;
  --body:    -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", system-ui, sans-serif;
  --mono:    ui-monospace, "SF Mono", Menlo, Monaco, "Cascadia Mono", monospace;
}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
h1, h2, h3, h4, h5, h6 { font-size: inherit; font-weight: inherit; margin: 0; }
body {
  font-family: var(--body);
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
  -webkit-font-smoothing: antialiased;
  font-size: 15px;
  line-height: 1.55;
  letter-spacing: -0.005em;
  font-feature-settings: "ss01";
}
/* Every number gets tabular-nums automatically when it lives in a number-class slot */
.num, .stat-val, .stat-desc, .sub-stats, .wt-freq, .tl-score, .tl-date,
.dom-count, .meta-v, .stat-lbl, .kg-val, .sb-svg text, .karma-svg text {
  font-variant-numeric: tabular-nums;
}
a { color: inherit; text-decoration: none; }
a:hover { text-decoration: underline; }
code { font-family: var(--mono); font-size: .9em;
  background: rgba(0,0,0,0.050); color: var(--karma);
  padding: 1px 5px; border-radius: 2px; }

.wrap {
  max-width: 1040px;
  margin: 0 auto;
  padding: 0 32px 120px;
}
/* Generous breathing room between sections — cladlabs-style */
.ds + .ds { margin-top: 56px; }
.vis-bar + .ds, .hero + .ds { margin-top: 64px; }

/* Minimal top strip — branding only */
.top-strip {
  display: flex; align-items: baseline; gap: 12px;
  padding: 14px 0 12px;
  font-family: var(--mono); font-size: .7rem; font-weight: 500;
  color: var(--text-secondary);
  letter-spacing: 0;
}
.top-strip .ts-brand {
  font-family: var(--body); font-weight: 700; font-size: .82rem;
  color: var(--text); letter-spacing: -.01em;
}
.top-strip .ts-tag {
  color: var(--text-tertiary);
  text-transform: lowercase;
  letter-spacing: .02em;
}
.top-strip .ts-spacer { flex: 1; }

/* All section-level divs sit flat on the page — no cards, no top stripes.
   Editorial rhythm comes from vertical spacing and the .cat eyebrows. */
.pcard,
.pcard--full,
.pcard--karma,
.pcard--time,
.pcard--words,
.pcard--chart,
.pcard--pos,
.pcard--neg,
.pcard--summary,
.pcard--stat {
  background: transparent;
  border: 0;
  padding: 0;
  margin: 0;
  position: static;
}
.pcard::before { content: none; }

/* ── HERO — centered cladlabs style ── */
.hero {
  padding: 64px 0 48px;
  display: grid;
  grid-template-columns: 1fr;
  justify-items: center;
  text-align: center;
  gap: 18px;
  margin-bottom: 0;
}
.hero-name {
  display: flex; align-items: baseline; flex-wrap: wrap;
  justify-content: center;
  gap: 6px 14px;
}
.user-display-name {
  font-family: var(--display);
  font-size: clamp(2.8rem, 6.5vw, 5rem);
  line-height: 0.9;
  letter-spacing: 0.01em;
  text-transform: uppercase;
}
.user-name-disp {
  font-family: var(--mono);
  font-size: clamp(1.1rem, 2vw, 1.6rem);
  font-weight: 600;
  color: var(--text-secondary);
  letter-spacing: -1.5px;
  line-height: 1;
}
.user-name-disp--solo {
  /* When no display name, the handle itself becomes the display element */
  font-family: var(--mono);
  font-size: clamp(2rem, 4.5vw, 3.4rem);
  font-weight: 700; color: var(--text);
  letter-spacing: -2px;
}
.udn-slash { color: var(--text-tertiary); font-weight: 500; }
.reddit-link {
  font-family: var(--mono); font-size: .68rem; font-weight: 600;
  letter-spacing: .08em; text-transform: uppercase;
  color: var(--text-secondary);
  padding: 8px 12px;
  border: 1px solid var(--line);
  background: var(--bg);
  transition: all .12s;
}
.reddit-link:hover {
  color: var(--karma); border-color: var(--karma);
  text-decoration: none;
}

.nsfw-badge {
  font-family: var(--mono); font-size: .62rem; font-weight: 600;
  letter-spacing: .08em;
  padding: 2px 8px; border-radius: 999px;
  border: 1px solid var(--text-secondary);
  color: var(--text-secondary);
  align-self: center;
}

/* Visibility chip bar — pills with washes (vibecheck-bot.com signature) */
.vis-bar {
  display: flex; flex-wrap: wrap; align-items: baseline;
  gap: 6px 8px;
  padding: 12px 0;
  border-top: 1px solid var(--line);
  font-size: .82rem; line-height: 1.8;
}
.vis-bar:last-of-type { border-bottom: 1px solid var(--line); }
.vis-bar-label {
  font-family: var(--mono); font-size: .68rem; font-weight: 600;
  letter-spacing: .02em;
  white-space: nowrap;
  margin-right: 4px;
}
.vis-bar-label--hidden { color: var(--text-secondary); }
.vis-bar-label--visible { color: var(--text-secondary); }
.vc-chip {
  font-family: var(--mono); font-size: .72rem; font-weight: 500;
  text-decoration: none;
  padding: 2px 8px;
  border-radius: 999px;
  white-space: nowrap;
  border: 1px solid var(--line);
  background: var(--surface);
  color: var(--text);
  transition: background .1s;
}
.vc-chip--hidden { font-style: italic; }
.vc-chip--visible { /* same neutral look */ }
.vc-chip:hover { background: var(--bg-subtle); text-decoration: none; }
.vis-bar-more {
  font-family: var(--mono); font-size: .68rem;
  color: var(--text-tertiary); margin-left: 4px;
}
.user-handle {
  font-family: var(--mono);
  font-size: 1.05rem;
  font-weight: 600;
  letter-spacing: -1px;
  color: rgba(0,0,0,0.55);
  margin-top: 4px;
}
.user-handle--solo {
  font-family: var(--display);
  font-size: clamp(2.4rem, 5vw, 3.6rem);
  line-height: 0.95;
  letter-spacing: 0.02em;
  color: var(--dark);
  margin-top: 0;
}
.hero-meta { display: flex; flex-direction: column; gap: 4px; }
.meta-row { display: flex; gap: 10px; align-items: baseline; }
.meta-k {
  font-family: var(--mono); font-size: .62rem; font-weight: 600;
  letter-spacing: .12em; text-transform: uppercase;
  color: rgba(0,0,0,0.5); width: 110px; text-align: right;
}
.meta-v {
  font-family: var(--body); font-size: .85rem; font-weight: 600;
  color: var(--dark);
}
.reddit-link {
  font-family: var(--mono); font-size: .6rem; font-weight: 600;
  letter-spacing: .12em; text-transform: uppercase;
  color: rgba(0,0,0,0.5); text-decoration: none;
  border: 1px solid rgba(0,0,0,0.18); padding: 6px 12px; border-radius: 1px;
  transition: color .12s, border-color .12s;
}
.reddit-link:hover { color: var(--karma); border-color: var(--karma); }

/* Generous vertical rhythm between sections (chapter-marker > content > chapter-marker) */
.wrap > * { margin: 0; }
.wrap > .section-sep + * { margin-top: 18px; }
.wrap > .pcard + .section-sep,
.wrap > .pcard--full + .section-sep,
.wrap > .stat-grid + .section-sep,
.wrap > .two-col + .section-sep,
.wrap > .four-col + .section-sep { margin-top: 56px; }
.wrap > .pcard + .pcard,
.wrap > .pcard--full + .pcard--full,
.wrap > .pcard + .two-col,
.wrap > .two-col + .pcard { margin-top: 36px; }

/* ── COLLAPSIBLE SECTIONS — minimal disclosure ── */
.ds {
  border-top: 1px solid var(--line);
}
.ds:last-of-type { border-bottom: 1px solid var(--line); }
.ds-summary {
  display: flex; align-items: baseline; gap: 14px;
  padding: 18px 4px;
  cursor: pointer;
  list-style: none;
  user-select: none;
  transition: background .1s;
}
.ds-summary::-webkit-details-marker { display: none; }
.ds-summary::marker { display: none; content: ''; }
.ds-summary:hover { background: var(--bg-subtle); }
.ds-marker {
  display: inline-block;
  font-family: var(--mono);
  font-size: .88rem; font-weight: 500;
  color: var(--text-tertiary);
  width: 14px; text-align: center;
  transition: transform .15s, color .15s;
}
.ds[open] > .ds-summary .ds-marker {
  transform: rotate(90deg);
  color: var(--text);
}
.ds-num {
  font-family: var(--mono); font-size: .7rem; font-weight: 500;
  color: var(--text-tertiary);
  letter-spacing: .04em;
  width: 22px;
}
.ds-label {
  font-family: var(--body); font-size: 1.18rem; font-weight: 600;
  letter-spacing: -.012em;
  color: var(--text);
}
.ds-quick {
  margin-left: auto;
  font-family: var(--body); font-size: .85rem; font-weight: 400;
  color: var(--text-secondary);
  letter-spacing: -.002em;
  text-align: right;
}
.ds-body {
  padding: 4px 4px 32px;
}
/* Hide legacy chapter bars if any still emit */
.section-sep { display: none; }

/* ── Subsection eyebrows — single neutral color, no per-section variation ── */
.cat {
  font-family: var(--body); font-size: .78rem; font-weight: 500;
  letter-spacing: 0; text-transform: none;
  color: var(--text-tertiary);
  margin-bottom: 16px;
}
/* Each section eyebrow gets its accent color back. */
.pcard--karma   .cat { color: var(--karma); }
.pcard--time    .cat { color: var(--time); }
.pcard--words   .cat { color: var(--words); }
.pcard--chart   .cat { color: var(--chart); }
.pcard--pos     .cat { color: var(--pos); }
.pcard--neg     .cat { color: var(--neg); }

/* ── SUMMARY ── */
.pcard--summary { padding-bottom: 18px; }
.summary-item {
  position: relative;
  padding-left: 22px;
  font-size: .95rem;
  line-height: 1.55;
  color: rgba(0,0,0,0.78);
  margin-bottom: 8px;
}
.summary-item::before {
  content: '➤'; position: absolute; left: 0; top: 2px;
  font-size: .7rem; color: #C1784F;
}
.summary-item strong { color: var(--dark); font-weight: 700; }

/* ── STAT GRID — tablepage: tight eyebrow → tabular sans number → mono descriptor ── */
.stat-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 24px 32px;
  padding: 20px 0 8px;
  border-bottom: 1px solid var(--line);
}
.pcard--stat {
  display: flex; flex-direction: column;
  gap: 4px;
  padding: 0 16px 0 0;
  border-right: 1px solid var(--line-soft);
}
.pcard--stat:nth-child(3n) { border-right: none; padding-right: 0; }
.stat-lbl {
  font-family: var(--mono); font-size: .62rem; font-weight: 700;
  letter-spacing: .14em; text-transform: uppercase;
  color: var(--text-secondary);
  order: -1;
}
.stat-val {
  font-family: var(--body); font-weight: 600;
  font-size: clamp(1.9rem, 3vw, 2.6rem);
  line-height: 1.05; letter-spacing: -0.025em;
  color: var(--text);
}
.stat-desc {
  font-family: var(--mono); font-size: .68rem; font-weight: 400;
  letter-spacing: .02em; color: var(--text-tertiary);
  margin-top: 2px;
}

/* ── TEMPORAL BARS ── */
.two-col { display: grid; grid-template-columns: 1fr 1fr; gap: var(--gap); }
.bars-wrap {
  display: flex; align-items: flex-end; gap: 4px;
  height: 110px;
}
.bar-col {
  flex: 1; display: flex; flex-direction: column; align-items: center;
  justify-content: flex-end; height: 100%; position: relative; gap: 4px;
}
.bar-fill {
  width: 100%; background: var(--bar, var(--time));
  min-height: 1px;
}
.bar-lbl {
  font-family: var(--mono); font-size: .56rem; font-weight: 600;
  letter-spacing: .04em; color: rgba(0,0,0,0.4);
}

.tz-block { display: flex; flex-direction: column; gap: 6px; }
.tz-name {
  font-family: var(--display); font-size: 1.6rem; letter-spacing: .04em;
  color: var(--time); margin-bottom: 8px;
}
.tz-desc { font-size: .82rem; color: rgba(0,0,0,0.55); line-height: 1.5; }
.tz-pcard { margin-top: 16px; }
.tz-pcard .cat { color: var(--time); }

/* Stacked SVG bars (weekday/hour) */
.sb-svg { width: 100%; height: auto; max-height: 220px;
  display: block; cursor: default; }
.sb-rect { transition: opacity .1s; }
.sb-rect[data-weekday], .sb-rect[data-hour] { cursor: pointer; }
.sb-rect:hover { opacity: 0.8; }
.sb-rect--active { stroke: var(--dark); stroke-width: 1.5; }
.sb-legend {
  display: flex; flex-wrap: wrap; gap: 4px 12px;
  margin-top: 8px;
  font-family: var(--mono); font-size: .62rem; font-weight: 500;
  color: rgba(0,0,0,0.55);
}
.sb-legend-item { display: inline-flex; align-items: center; gap: 5px; }
.sb-sw { width: 9px; height: 9px; border-radius: 1px; flex-shrink: 0; }

/* ── HEATMAP ── */
.heatmap-scroll {
  overflow-x: auto;
  padding-bottom: 4px;
  margin-bottom: 8px;
}
.heatmap-grid { display: flex; gap: 2px; }
.heatmap-col { display: flex; flex-direction: column; gap: 2px; }
.heatmap-cell {
  width: 11px; height: 11px; border-radius: 1px;
  background: rgba(0,0,0,0.06);
  flex-shrink: 0;
}
.heatmap-cell[data-level="1"] { background: rgba(0,0,0,0.14); }
.heatmap-cell[data-level="2"] { background: rgba(0,0,0,0.34); }
.heatmap-cell[data-level="3"] { background: rgba(0,0,0,0.58); }
.heatmap-cell[data-level="4"] { background: rgba(0,0,0,0.85); }
.heatmap-months { display: flex; gap: 2px; margin-bottom: 6px; }
.hm-span {
  width: 11px; flex-shrink: 0; overflow: visible; white-space: nowrap;
  font-family: var(--mono); font-size: .52rem; font-weight: 600;
  letter-spacing: .06em; text-transform: uppercase; color: #aaa;
}
.heatmap-legend {
  display: flex; align-items: center; gap: 4px;
  font-family: var(--mono); font-size: .54rem; font-weight: 600;
  letter-spacing: .1em; text-transform: uppercase; color: #999;
}
.heatmap-legend .heatmap-cell { width: 10px; height: 10px; }
.psub {
  font-family: var(--mono); font-size: .58rem; color: #888;
  margin-top: 10px; letter-spacing: .04em;
}

/* ── KARMA GROWTH ── */
.karma-svg-scroll {
  overflow-x: auto; overflow-y: hidden;
  margin-top: 4px;
  background: #fafaf7;
  /* Center the SVG when it's narrower than the wrapper; when it's wider
     (kn0thing's 20-year history), `safe` keeps the left edge accessible
     instead of stuck behind the scroll origin. */
  display: flex;
  justify-content: safe center;
}
.karma-svg {
  display: block;
  height: 320px;
  flex-shrink: 0;  /* keep the SVG's intrinsic width; let the wrapper scroll */
}
.kg-legend {
  display: flex; flex-wrap: wrap;
  gap: 6px 14px;
  margin-top: 14px;
}
.kg-legend-item {
  display: inline-flex; align-items: center; gap: 6px;
  font-size: .76rem; color: rgba(0,0,0,0.7);
}
.kg-swatch {
  width: 11px; height: 11px; border-radius: 1px;
  flex-shrink: 0;
}
.kg-name { font-weight: 500; }
.kg-val {
  font-family: var(--mono); font-size: .62rem; font-weight: 600;
  color: rgba(0,0,0,0.4); letter-spacing: .04em;
}

/* ── BEST / WORST ── */
.four-col { display: grid; grid-template-columns: repeat(4, 1fr); gap: var(--gap); }
.bw-empty { font-family: var(--mono); font-size: .62rem;
  color: rgba(0,0,0,0.3); text-transform: uppercase; letter-spacing: .12em; }
.bw-score { display: none; }
.bw-score-val {
  font-family: var(--display);
  letter-spacing: 0.01em;
  line-height: 0.9;
  margin-bottom: 10px;
}
.pcard--pos .bw-score-val { color: var(--pos); }
.pcard--neg .bw-score-val { color: var(--neg); }
.score-pos { color: var(--pos); }
.score-neg { color: var(--neg); }
.score-neu { color: #888; }
.bw-body {
  font-size: .78rem; line-height: 1.5; color: rgba(0,0,0,0.65);
  flex: 1; margin-bottom: 10px;
}
.bw-meta {
  font-family: var(--mono); font-size: .56rem; font-weight: 500;
  letter-spacing: .08em; color: rgba(0,0,0,0.35);
  text-transform: lowercase;
}
.clamp4 {
  display: -webkit-box; -webkit-line-clamp: 4; -webkit-box-orient: vertical;
  overflow: hidden;
}

/* ── SUBREDDITS — tablepage/Bloomberg data table ── */
.sub-table-wrap {
  width: 100%;
  overflow-x: auto;
  border-top: 1px solid var(--line);
  margin-top: 12px;
}
.sub-table {
  width: 100%;
  border-collapse: collapse;
  font-size: .82rem;
}
.sub-table thead th {
  position: sticky; top: 0;
  background: var(--bg-subtle);
  font-family: var(--mono); font-size: .62rem; font-weight: 600;
  letter-spacing: .12em; text-transform: uppercase;
  color: var(--text-secondary);
  text-align: left; padding: 10px 12px;
  border-bottom: 1px solid var(--line);
  white-space: nowrap;
}
.sub-table th.num { text-align: right; }
.sub-table tbody td {
  padding: 12px 12px;
  border-bottom: 1px solid var(--line-soft);
  vertical-align: top;
}
.sub-table .num { text-align: right; font-family: var(--mono);
  font-feature-settings: "tnum"; }
.sub-table tbody tr.sub-row:nth-child(4n+1) { background: var(--bg-subtle); }
.sub-table tbody tr.sub-row { cursor: pointer; transition: background .1s; }
.sub-table tbody tr.sub-row:hover { background: rgba(0,0,0,0.03); }
.sub-table tbody tr.sub-row.sub-row--highlight {
  background: rgba(0,0,0,0.075);
  animation: vc-highlight 1.4s ease-out;
}
.sub-rank {
  font-family: var(--mono); font-size: .68rem; font-weight: 500;
  color: var(--text-tertiary); width: 32px;
}
.sub-name-link {
  font-family: var(--body); font-size: .92rem; font-weight: 600;
  color: var(--text); text-decoration: none;
  letter-spacing: -0.01em;
}
.sub-name-link:hover { color: var(--karma); text-decoration: none; }
.cell-karma { color: var(--pos); font-weight: 600; }
.cell-warn { color: var(--neg); font-weight: 600; }
.cell-dim { color: var(--text-tertiary); }
.sub-sample-cell { color: var(--text-secondary); }
.sub-summary-txt {
  font-size: .84rem; line-height: 1.45; color: var(--text);
  margin-bottom: 6px; font-weight: 500;
}
.sub-sample {
  font-size: .76rem; line-height: 1.5; color: var(--text-tertiary);
  display: flex; gap: 6px 10px; flex-wrap: wrap;
  align-items: baseline;
}
.sub-sample > span { color: var(--text-tertiary); }
.sub-sample-link {
  color: var(--text-secondary); text-decoration: none;
  transition: color .1s;
}
.sub-sample-link:hover { color: var(--karma); text-decoration: underline; }
.sub-sample-comment { font-style: italic; }
.sub-toggle {
  margin-left: 6px;
  color: var(--text-tertiary); font-family: var(--mono); font-size: .72rem;
  display: inline-block; transition: transform .15s, color .15s;
}
.sub-row--open .sub-toggle { transform: rotate(90deg); color: var(--karma); }
/* Expand row */
.sub-expand-row { background: var(--bg-subtle) !important; }
.sub-expand-cell { padding: 0 !important; border-bottom: 1px solid var(--line) !important; }
.sub-expand-grid {
  display: grid; grid-template-columns: 1fr 1fr; gap: 24px;
  padding: 14px 12px;
}
@media (max-width: 720px) { .sub-expand-grid { grid-template-columns: 1fr; } }

/* ── WORD CLOUD + TABLE ── */
.word-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.6fr) minmax(0, 1fr);
  gap: 48px;
  align-items: start;
}
.word-grid-cloud { min-width: 0; }
.word-cloud {
  display: flex; flex-wrap: wrap; align-items: baseline;
  gap: 8px 16px; line-height: 1.05;
  font-family: var(--display);
  color: var(--dark);
}
.word-cloud span { letter-spacing: 0.02em; }
.word-cloud span:nth-child(1) { color: var(--karma); }
.word-cloud span:nth-child(7n+3) { color: var(--words); }
.word-cloud span:nth-child(11n+5) { color: var(--time); }
.wt-table { width: 100%; border-collapse: collapse; font-family: var(--mono); }
.wt-table th {
  font-size: .58rem; font-weight: 600; letter-spacing: .12em;
  text-transform: uppercase; color: rgba(0,0,0,0.35);
  text-align: left; padding: 6px 8px;
  border-bottom: 1px solid var(--line);
}
.wt-table td {
  font-size: .82rem; padding: 6px 8px;
  border-bottom: 1px solid rgba(0,0,0,0.04);
}
.wt-rank { color: rgba(0,0,0,0.35); width: 32px; font-size: .68rem; }
.wt-word { font-weight: 600; }
.wt-word .wc-word {
  font-family: var(--mono); font-size: 1em; color: inherit;
  letter-spacing: 0;
}
.wt-freq { text-align: right; width: 60px; color: var(--words); font-weight: 600; }
@media (max-width: 800px) {
  .word-grid { grid-template-columns: 1fr; }
}

/* ── VISIBILITY ── */
.vis-row { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }
.vis-label {
  font-family: var(--mono); font-size: .58rem; font-weight: 600;
  letter-spacing: .14em; text-transform: uppercase;
  margin-bottom: 10px;
}
.vis-label--hidden  { color: var(--neg); }
.vis-label--visible { color: var(--time); }
.vis-chips { display: flex; flex-wrap: wrap; gap: 5px 6px; }
.hidden-banner-sub {
  font-family: var(--mono); font-size: .62rem; font-weight: 600;
  color: #8a2020; background: rgba(180,30,30,0.07);
  border-radius: 4px; padding: 3px 7px; white-space: nowrap;
}
.hidden-banner-sub--visible {
  font-family: var(--mono); font-size: .62rem; font-weight: 600;
  color: #0057a8; background: rgba(0,100,220,0.08);
  border-radius: 4px; padding: 3px 7px; white-space: nowrap;
}

/* ── DOMAINS ── */
.dom-row {
  display: grid;
  grid-template-columns: 220px 1fr 70px;
  gap: 12px;
  align-items: center;
  padding: 7px 0;
  border-bottom: 1px solid rgba(0,0,0,0.05);
}
.dom-row:last-child { border-bottom: none; }
.dom-name { font-family: var(--mono); font-size: .72rem; color: var(--dark);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dom-bar { height: 6px; background: rgba(217,120,0,0.10); border-radius: 1px;
  overflow: hidden; }
.dom-bar-fill { height: 100%; background: var(--chart); }
.dom-count { font-family: var(--mono); font-size: .7rem; font-weight: 600;
  text-align: right; color: rgba(0,0,0,0.55); }

/* ── SUBREDDIT ACTIVITY TIMELINE (strip plot) ── */
.srt-wrap { overflow-x: auto; padding: 4px 0; }
.srt-svg { display: block; min-width: 100%; }
.srt-label { font-family: var(--mono); font-size: .68rem; fill: var(--text-secondary); }
.srt-count { fill: var(--text-tertiary); font-weight: 500; }
.srt-year  { font-family: var(--mono); font-size: .62rem; fill: var(--text-tertiary);
  letter-spacing: 0.05em; }

/* ── TIMELINE — tabular ── */
.tl-row {
  display: grid;
  grid-template-columns: 100px 70px 140px 60px 1fr;
  gap: 12px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--line-soft);
  align-items: baseline;
  font-size: .8rem;
}
.tl-row:last-child { border-bottom: none; }
.tl-date { font-family: var(--mono); font-size: .62rem;
  color: rgba(0,0,0,0.4); }
.tl-type { font-family: var(--mono); font-size: .58rem; font-weight: 600;
  text-transform: uppercase; letter-spacing: .1em; color: rgba(0,0,0,0.5); }
.tl-sub { font-family: var(--mono); font-size: .68rem;
  color: var(--karma); overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; }
.tl-score { font-family: var(--display); font-size: 1rem; }
.tl-body { color: rgba(0,0,0,0.7); overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; }

/* ── FOOTER ── */
.footer {
  font-family: var(--mono); font-size: .56rem; font-weight: 500;
  letter-spacing: .12em; text-transform: uppercase;
  color: rgba(0,0,0,0.28);
  text-align: center; padding: 24px 0 12px;
}

/* ── RESPONSIVE ── */
@media (max-width: 1000px) {
  .hero { grid-template-columns: 1fr; gap: 18px; align-items: start; }
  .stat-grid { grid-template-columns: repeat(2, 1fr); }
  .four-col { grid-template-columns: repeat(2, 1fr); }
  .tl-row { grid-template-columns: 1fr; gap: 4px; }
}
@media (max-width: 640px) {
  .stat-grid, .four-col, .two-col, .vis-row { grid-template-columns: 1fr; }
  .meta-k { width: auto; text-align: left; }
  .dom-row { grid-template-columns: 1fr 70px; }
  .dom-bar { display: none; }
}

/* Custom scrollbar (heatmap) */
::-webkit-scrollbar { height: 6px; width: 6px; }
::-webkit-scrollbar-thumb { background: rgba(0,0,0,0.18); border-radius: 2px; }
::-webkit-scrollbar-track { background: transparent; }

/* ── INTERACTIVITY ── */
/* Restore [hidden] semantics — author rules below override the UA default,
   so re-assert it with !important. Affects .sub-expand, .tl-empty, etc. */
[hidden] { display: none !important; }
.karma-svg { cursor: crosshair; }
.kg-poly { transition: opacity .15s, filter .15s; }
.kg-poly--off { opacity: 0.07 !important; }
.kg-legend-item { cursor: pointer; user-select: none;
  transition: opacity .15s; padding: 1px 0; }
.kg-legend-item:hover { opacity: 0.7; }
.kg-legend-item--off { opacity: 0.35; }
.kg-legend-item--off .kg-name { text-decoration: line-through; }

/* Floating tooltips */
.floating-tip {
  position: fixed; z-index: 1000;
  background: var(--dark); color: #fff;
  padding: 6px 10px; border-radius: 2px;
  font-family: var(--mono); font-size: .62rem; font-weight: 600;
  letter-spacing: .04em;
  pointer-events: none;
  box-shadow: 0 2px 8px rgba(0,0,0,0.25);
}
.floating-tip--rich {
  padding: 10px 12px; min-width: 160px; max-width: 240px;
  font-weight: 500; letter-spacing: 0;
}
.kt-h {
  font-family: var(--display); font-size: .9rem;
  letter-spacing: .04em; margin-bottom: 6px;
  border-bottom: 1px solid rgba(255,255,255,0.2); padding-bottom: 4px;
}
.kt-row { display: flex; align-items: center; gap: 6px; margin: 3px 0; }
.kt-sw { width: 9px; height: 9px; border-radius: 1px; flex-shrink: 0; }
.kt-row span:nth-child(2) { flex: 1; }
.kt-v { font-family: var(--mono); font-size: .6rem; font-weight: 600; opacity: .85; }
.kt-total { margin-top: 6px; padding-top: 6px;
  border-top: 1px solid rgba(255,255,255,0.2);
  display: flex; justify-content: space-between;
  font-family: var(--mono); font-size: .62rem; font-weight: 600;
  letter-spacing: .04em; }

/* Heatmap cell hover */
.heatmap-cell { transition: outline .08s; }
.heatmap-cell:hover { outline: 1px solid rgba(0,0,0,0.5); outline-offset: 1px; }

/* Bars click affordance */
.bar-col--clickable { cursor: pointer; transition: opacity .12s; }
.bar-col--clickable:hover { opacity: 0.7; }
.bar-col--active .bar-fill {
  outline: 2px solid var(--dark); outline-offset: -2px;
}

/* Word cloud */
.wc-word { cursor: pointer; transition: transform .1s, color .12s; }
.wc-word:hover { transform: translateY(-1px); color: var(--karma); }

/* Subreddit controls */
.sub-controls {
  display: flex; flex-wrap: wrap; gap: 18px 32px;
  padding: 0 0 14px;
  border-bottom: 1px solid rgba(0,0,0,0.07);
  margin-bottom: 6px;
}
.sub-controls-group { display: flex; gap: 6px; align-items: center; }
.ctrl-label {
  font-family: var(--mono); font-size: .55rem; font-weight: 600;
  letter-spacing: .14em; text-transform: uppercase;
  color: rgba(0,0,0,0.4); margin-right: 4px;
}
.ctrl-btn {
  font-family: var(--mono); font-size: .6rem; font-weight: 600;
  letter-spacing: .08em; text-transform: uppercase;
  padding: 9px 13px; min-height: 32px; border-radius: 2px;
  background: transparent; color: rgba(0,0,0,0.55);
  border: 1px solid rgba(0,0,0,0.14);
  cursor: pointer; transition: all .12s;
}
@media (pointer: coarse) {
  .ctrl-btn { min-height: 40px; padding: 11px 14px; }
  .ctrl-chip { padding: 8px 12px; }
}
.ctrl-btn:hover { color: var(--dark); border-color: rgba(0,0,0,0.4); }
.ctrl-btn.active { background: var(--dark); color: #fff; border-color: var(--dark); }
.ctrl-chip {
  font-family: var(--mono); font-size: .58rem; font-weight: 600;
  padding: 3px 9px; border-radius: 99px;
  background: rgba(0,0,0,0.04); color: rgba(0,0,0,0.5);
  border: 1px solid rgba(0,0,0,0.08);
  cursor: pointer; transition: all .12s;
}
.ctrl-chip:hover { background: rgba(0,0,0,0.08); color: var(--dark); }
.ctrl-chip.active { background: var(--karma); color: #fff; border-color: var(--karma); }
.ctrl-chip .muted { color: inherit; opacity: 0.55; font-weight: 500; }

/* Subreddit row interactivity */
.sub-row {
  cursor: pointer;
  transition: background .12s;
}
.sub-row:hover { background: rgba(0,0,0,0.015); }
.sub-row.sub-row--highlight {
  background: rgba(0,0,0,0.040);
  animation: vc-highlight 1.4s ease-out;
}
@keyframes vc-highlight {
  0% { background: rgba(0,0,0,0.150); }
  100% { background: rgba(0,0,0,0.040); }
}
.sub-toggle {
  font-family: var(--mono); font-size: .72rem; color: rgba(0,0,0,0.35);
  margin-left: auto; transition: transform .15s;
}
.sub-row.sub-row--open .sub-toggle { transform: rotate(90deg); color: var(--karma); }
.sub-expand {
  padding: 12px 0 6px;
  display: grid; grid-template-columns: 1fr 1fr; gap: 18px;
  border-top: 1px dashed rgba(0,0,0,0.08); margin-top: 8px;
}
.exp-section { display: flex; flex-direction: column; gap: 4px; }
.exp-label {
  font-family: var(--mono); font-size: .54rem; font-weight: 600;
  letter-spacing: .14em; text-transform: uppercase;
  color: rgba(0,0,0,0.4); margin-bottom: 4px;
}
.exp-item {
  display: flex; gap: 8px; padding: 5px 6px;
  text-decoration: none; color: var(--dark);
  border-radius: 2px; transition: background .1s;
  align-items: baseline;
}
.exp-item:hover { background: rgba(0,0,0,0.035); }
.exp-score {
  font-family: var(--display); font-size: .95rem;
  color: var(--pos); flex-shrink: 0; min-width: 36px;
}
.exp-text { font-size: .76rem; line-height: 1.45; color: rgba(0,0,0,0.72); }
@media (max-width: 640px) {
  .sub-expand { grid-template-columns: 1fr; }
}

/* Timeline controls */
.tl-controls {
  display: flex; flex-wrap: wrap; gap: 12px 16px;
  margin-bottom: 14px;
  padding-bottom: 12px;
  border-bottom: 1px solid rgba(0,0,0,0.07);
  align-items: center;
}
.tl-search {
  flex: 1; min-width: 220px;
  font-family: var(--mono); font-size: .78rem;
  padding: 6px 10px;
  border: 1px solid rgba(0,0,0,0.15); border-radius: 2px;
  background: #fafaf7; color: var(--dark);
  outline: none; transition: border-color .12s;
}
.tl-search:focus { border-color: var(--karma); background: #fff; }
.tl-sub-chips { display: flex; flex-wrap: wrap; gap: 4px 6px; }
.tl-empty {
  text-align: center; padding: 24px;
  font-family: var(--mono); font-size: .7rem;
  color: rgba(0,0,0,0.35);
}
.tl-row { transition: background .1s; }
.tl-row:nth-child(4n+1) { background: var(--bg-subtle); }
.tl-row:hover { background: rgba(0,0,0,0.03); }
.tl-show-more {
  display: block; width: 100%;
  margin-top: 12px; padding: 10px 16px;
  font-family: var(--mono); font-size: .62rem; font-weight: 600;
  letter-spacing: .14em; text-transform: uppercase;
  color: rgba(0,0,0,0.55);
  background: transparent;
  border: 1px dashed rgba(0,0,0,0.18);
  border-radius: 2px;
  cursor: pointer; transition: all .12s;
}
.tl-show-more:hover {
  color: var(--dark);
  border-color: rgba(0,0,0,0.45);
  background: rgba(0,0,0,0.02);
}

/* Visibility chip clickability */
.vis-chip { cursor: pointer; transition: opacity .12s, transform .1s; }
.vis-chip:hover { opacity: 0.75; transform: translateY(-1px); }
"""


# ────── INTERACTIVITY JS ──────
JS = r"""
(function(){
  function $(sel, root){ return (root||document).querySelector(sel); }
  function $$(sel, root){ return Array.from((root||document).querySelectorAll(sel)); }
  function fmtK(n){
    n = Math.round(n);
    var s = n < 0 ? '-' : '';
    n = Math.abs(n);
    if (n >= 1e6) return s + (n/1e6).toFixed(1).replace(/\.0$/,'') + 'M';
    if (n >= 10000) return s + Math.round(n/1000) + 'K';
    if (n >= 1000) return s + (n/1000).toFixed(1) + 'K';
    return s + n;
  }
  function fmtI(n){ return n.toString().replace(/\B(?=(\d{3})+(?!\d))/g,','); }

  var data = {};
  try { data = JSON.parse(($('#vc-data')||{}).textContent || '{}'); } catch(e){}

  // ────── 1. Karma chart: hover crosshair + legend toggle ──────
  (function setupKarma(){
    var svg = $('#karma-svg');
    var k = data.karma;
    if (!svg || !k) return;
    var tip = $('#kg-tooltip');
    var crosshair = svg.querySelector('.kg-crosshair');
    var polys = $$('.kg-poly', svg);
    var hidden = new Set();

    function restack(){
      // Re-stack the bars after legend toggle. Each visible series gets its
      // rects re-positioned to sit on top of the previous visible series.
      var n = k.n_buckets;
      var W = k.W, PADL = k.PADL, PADR = k.PADR, PADT = k.PADT, PADB = k.PADB;
      var H = k.H, plotW = W - PADL - PADR, plotH = H - PADT - PADB;
      var stackTotals = new Array(n).fill(0);
      k.series.forEach(function(s){
        if (!hidden.has(s.name)) for (var i=0;i<n;i++) stackTotals[i] += s.cum[i];
      });
      var yMax = Math.max.apply(null, stackTotals) || 1;
      var barGap = n > 60 ? 1.5 : 2.5;
      var barW = Math.max(2.0, (plotW - barGap * (n - 1)) / n);
      var pitch = barW + barGap;
      function yFor(v){ return PADT + plotH - (v/yMax)*plotH; }
      function xFor(i){ return PADL + i * pitch; }
      var base = new Array(n).fill(0);
      k.series.forEach(function(s, idx){
        var grp = polys[idx];
        if (!grp) return;
        if (hidden.has(s.name)) {
          grp.classList.add('kg-poly--off');
          return;
        }
        grp.classList.remove('kg-poly--off');
        var rects = grp.querySelectorAll('rect.kg-rect');
        var ri = 0;
        for (var i = 0; i < n; i++) {
          var v = s.cum[i];
          if (v <= 0) continue;
          var rect = rects[ri++];
          if (!rect) break;
          var yTop = yFor(base[i] + v);
          var h = yFor(base[i]) - yTop;
          rect.setAttribute('x', xFor(i).toFixed(1));
          rect.setAttribute('y', yTop.toFixed(1));
          rect.setAttribute('width', barW.toFixed(1));
          rect.setAttribute('height', h.toFixed(1));
        }
        for (var i = 0; i < n; i++) base[i] += s.cum[i];
      });
      // Update Y-axis labels in place
      var labels = svg.querySelectorAll('text');
      var ylabels = [];
      labels.forEach(function(t){
        if (parseFloat(t.getAttribute('x')) < PADL && t.getAttribute('text-anchor') === 'end') ylabels.push(t);
      });
      for (var k2 = 0; k2 < 5 && k2 < ylabels.length; k2++) {
        var v = (yMax * k2) / 4;
        ylabels[k2].textContent = fmtK(v);
      }
    }

    // Hover crosshair + tooltip — use SVG's own coord transform so the math
    // is correct regardless of viewport scaling, padding, scroll position,
    // or preserveAspectRatio. Pixel-ratio math gets wrong answers as soon
    // as the SVG isn't pixel-for-pixel with its bounding rect.
    svg.addEventListener('mousemove', function(ev){
      var pt = svg.createSVGPoint();
      pt.x = ev.clientX; pt.y = ev.clientY;
      var ctm = svg.getScreenCTM();
      if (!ctm) return;
      var svgPt = pt.matrixTransform(ctm.inverse());
      var xInSvg = svgPt.x;
      if (xInSvg < k.PADL || xInSvg > k.W - k.PADR) {
        tip.hidden = true; crosshair.style.display = 'none'; return;
      }
      var i = Math.round(((xInSvg - k.PADL) / (k.W - k.PADL - k.PADR)) * (k.n_buckets - 1));
      i = Math.max(0, Math.min(k.n_buckets - 1, i));
      var month = k.m0 + i;
      var yr = Math.floor(month/12);
      var mo = (month % 12) + 1;
      var mLabel = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][mo-1] + ' ' + yr;
      // Crosshair line
      var xPlot = k.PADL + (i / Math.max(k.n_buckets-1, 1)) * (k.W - k.PADL - k.PADR);
      crosshair.setAttribute('x1', xPlot); crosshair.setAttribute('x2', xPlot);
      crosshair.style.display = 'block';
      // Tooltip rows
      var html = '<div class="kt-h">' + mLabel + '</div>';
      var total = 0;
      // Sort by value desc for that bucket
      var rows = k.series.map(function(s, idx){
        return {name: s.name, val: s.cum[i], idx: idx};
      }).filter(function(r){ return !hidden.has(r.name); });
      rows.sort(function(a,b){ return b.val - a.val; });
      rows.forEach(function(r){
        total += r.val;
        var color = k.palette[r.idx % k.palette.length];
        var label = r.name === 'Other' ? 'Other' : 'r/' + r.name;
        html += '<div class="kt-row"><span class="kt-sw" style="background:'+color+'"></span>'+
                '<span>'+label+'</span><span class="kt-v">'+fmtK(r.val)+'</span></div>';
      });
      html += '<div class="kt-total"><span>total</span><span>'+fmtK(total)+'</span></div>';
      tip.innerHTML = html;
      tip.hidden = false;
      // Position relative to viewport
      var tx = ev.clientX + 14;
      var ty = ev.clientY + 14;
      if (tx + 220 > window.innerWidth) tx = ev.clientX - 220 - 14;
      if (ty + 200 > window.innerHeight) ty = ev.clientY - 200 - 14;
      tip.style.left = tx + 'px';
      tip.style.top = ty + 'px';
    });
    svg.addEventListener('mouseleave', function(){
      tip.hidden = true; crosshair.style.display = 'none';
    });

    // Legend click → toggle
    $$('.kg-legend-item').forEach(function(el){
      el.addEventListener('click', function(){
        var name = el.getAttribute('data-series-name');
        if (hidden.has(name)) { hidden.delete(name); el.classList.remove('kg-legend-item--off'); }
        else { hidden.add(name); el.classList.add('kg-legend-item--off'); }
        restack();
      });
    });
  })();

  // ────── 2. Heatmap: custom tooltip ──────
  (function setupHeatmap(){
    var tip = $('#hm-tooltip');
    $$('.heatmap-cell[data-date]').forEach(function(cell){
      cell.addEventListener('mouseenter', function(ev){
        var d = cell.getAttribute('data-date');
        var c = cell.getAttribute('data-count');
        tip.textContent = d + ' · ' + c + ' action' + (c === '1' ? '' : 's');
        tip.hidden = false;
        var r = cell.getBoundingClientRect();
        tip.style.left = (r.left + r.width / 2) + 'px';
        tip.style.top = (r.top - 28) + 'px';
        tip.style.transform = 'translateX(-50%)';
      });
      cell.addEventListener('mouseleave', function(){ tip.hidden = true; });
    });
  })();

  // ────── 3. Subreddit table: sort, filter, expand ──────
  (function setupSubs(){
    var card = $('#subs-card');
    if (!card) return;
    var rowsContainer = $('.sub-rows', card);
    var rows = $$('.sub-row', card);
    var state = { sort: 'total', filter: 'all' };

    function getNum(row, attr){ return parseInt(row.getAttribute('data-'+attr) || '0', 10); }

    function apply(){
      var visible = rows.filter(function(row){
        var hid = getNum(row, 'hidden');
        var vis = getNum(row, 'visible');
        if (state.filter === 'hidden') return hid > 0;
        if (state.filter === 'visible') return vis > 0 && hid === 0;
        return true;
      });
      visible.sort(function(a, b){ return getNum(b, state.sort) - getNum(a, state.sort); });
      // Hide both the data row AND its expand row
      rows.forEach(function(r){
        r.hidden = true;
        var ex = r.nextElementSibling;
        if (ex && ex.classList.contains('sub-expand-row')) ex.hidden = true;
      });
      visible.forEach(function(row, idx){
        row.hidden = false;
        rowsContainer.appendChild(row);
        // Re-attach matching expand row right after this row, keeping pairing.
        var match = rowsContainer.querySelector('.sub-expand-row[data-belongs-to="' + CSS.escape(row.getAttribute('data-name')) + '"]');
        if (match) rowsContainer.appendChild(match);
        var rank = $('.sub-rank', row);
        if (rank) rank.textContent = String(idx + 1).padStart(2, '0');
      });
    }

    $$('.ctrl-btn[data-sort]', card).forEach(function(btn){
      btn.addEventListener('click', function(){
        state.sort = btn.getAttribute('data-sort');
        $$('.ctrl-btn[data-sort]', card).forEach(function(b){ b.classList.toggle('active', b === btn); });
        apply();
      });
    });
    $$('.ctrl-btn[data-filter]', card).forEach(function(btn){
      btn.addEventListener('click', function(){
        state.filter = btn.getAttribute('data-filter');
        $$('.ctrl-btn[data-filter]', card).forEach(function(b){ b.classList.toggle('active', b === btn); });
        apply();
      });
    });

    // Row expansion: expand panel is the next-sibling <tr class="sub-expand-row">
    rows.forEach(function(row){
      var exp = row.nextElementSibling;
      if (!exp || !exp.classList.contains('sub-expand-row')) return;
      row.addEventListener('click', function(ev){
        if (ev.target.closest('a')) return;
        var open = !exp.hidden;
        exp.hidden = open;
        row.classList.toggle('sub-row--open', !open);
      });
    });
  })();

  // ────── 4. Word cloud → filter timeline ──────
  (function setupWords(){
    $$('.wc-word').forEach(function(el){
      el.addEventListener('click', function(){
        var w = el.getAttribute('data-word');
        var search = $('.tl-search');
        if (search) {
          search.value = w;
          search.dispatchEvent(new Event('input'));
          var card = $('#timeline-card');
          if (card) card.scrollIntoView({behavior:'smooth', block:'start'});
        }
      });
    });
  })();

  // ────── 5. Temporal bars → filter timeline ──────
  (function setupBars(){
    // Match either the old div-based bars or the new SVG rects.
    var bars = $$('[data-weekday], [data-hour]').filter(function(el){
      return el.classList.contains('bar-col--clickable') ||
             el.classList.contains('sb-rect');
    });
    // Track which axis is currently active (weekday-axis vs hour-axis) so
    // re-clicking the same axis toggles, but switching axes resets the other.
    var activeBar = null;
    bars.forEach(function(bar){
      bar.addEventListener('click', function(){
        var wd = bar.getAttribute('data-weekday');
        var hr = bar.getAttribute('data-hour');
        var sameBar = (bar === activeBar);
        // Clear all visual active states
        bars.forEach(function(b){
          b.classList.remove('bar-col--active', 'sb-rect--active');
        });
        var willActivate = !sameBar;
        if (willActivate) {
          bar.classList.add(bar.classList.contains('sb-rect') ? 'sb-rect--active' : 'bar-col--active');
          activeBar = bar;
        } else {
          activeBar = null;
        }
        document.dispatchEvent(new CustomEvent('vc-filter-temporal', {
          detail: { weekday: wd, hour: hr, active: willActivate }
        }));
        var card = $('#timeline-card');
        if (card) card.scrollIntoView({behavior:'smooth', block:'start'});
      });
    });
  })();

  // ────── 6. Visibility chips → highlight subreddit row ──────
  (function setupVisChips(){
    $$('.vis-chip').forEach(function(chip){
      chip.addEventListener('click', function(){
        var name = chip.getAttribute('data-sub');
        var row = document.querySelector('.sub-row[data-name="' + CSS.escape(name) + '"]');
        if (!row) {
          // Sub isn't in top 25 — fall back to opening reddit
          window.open('https://reddit.com/r/' + encodeURIComponent(name), '_blank');
          return;
        }
        row.scrollIntoView({behavior: 'smooth', block: 'center'});
        row.classList.remove('sub-row--highlight');
        void row.offsetWidth; // restart animation
        row.classList.add('sub-row--highlight');
      });
    });
  })();

  // ────── 7. Timeline: search + type + sub filter ──────
  (function setupTimeline(){
    var card = $('#timeline-card');
    if (!card) return;
    var rows = $$('.tl-row', card);
    var empty = $('.tl-empty', card);
    var rowsBox = $('.tl-rows', card);
    var showMore = $('.tl-show-more', card);
    var INITIAL_CAP = parseInt(rowsBox.getAttribute('data-cap') || '25', 10);
    var state = { q: '', type: 'all', sub: '', weekday: null, hour: null, capped: true };
    function getWeekday(row){
      var ts = parseInt(row.getAttribute('data-ts') || '0', 10);
      if (ts) return (new Date(ts * 1000).getUTCDay() + 6) % 7;
      var dateStr = ($('.tl-date', row) || {}).textContent || '';
      var d = new Date(dateStr + ' UTC');
      if (isNaN(d.getTime())) return null;
      return (d.getUTCDay() + 6) % 7;
    }
    function getHour(row){
      var ts = parseInt(row.getAttribute('data-ts') || '0', 10);
      return ts ? new Date(ts * 1000).getUTCHours() : null;
    }
    function anyFilterActive(){
      return state.q || state.type !== 'all' || state.sub ||
             state.weekday !== null || state.hour !== null;
    }
    function apply(){
      var q = state.q.toLowerCase();
      var nVisible = 0;
      var cap = (state.capped && !anyFilterActive()) ? INITIAL_CAP : Infinity;
      rows.forEach(function(row){
        var text = row.getAttribute('data-text') || '';
        var sub = row.getAttribute('data-sub') || '';
        var type = row.getAttribute('data-type') || '';
        var ok = true;
        if (q && text.indexOf(q) < 0) ok = false;
        if (state.type !== 'all' && type !== state.type) ok = false;
        if (state.sub && sub !== state.sub) ok = false;
        if (state.weekday !== null) {
          var wd = getWeekday(row);
          if (wd !== null && wd !== Number(state.weekday)) ok = false;
        }
        if (state.hour !== null) {
          var hr = getHour(row);
          if (hr !== null && hr !== Number(state.hour)) ok = false;
        }
        if (ok && nVisible >= cap) ok = false;
        row.hidden = !ok;
        if (ok) nVisible++;
      });
      empty.hidden = nVisible !== 0;
      if (showMore) {
        showMore.hidden = !state.capped || anyFilterActive() || rows.length <= INITIAL_CAP;
      }
    }
    if (showMore) {
      showMore.addEventListener('click', function(){
        state.capped = false;
        showMore.hidden = true;
        apply();
      });
    }
    var search = $('.tl-search', card);
    if (search) search.addEventListener('input', function(){ state.q = search.value; apply(); });
    $$('.ctrl-btn[data-tl-type]', card).forEach(function(btn){
      btn.addEventListener('click', function(){
        state.type = btn.getAttribute('data-tl-type');
        $$('.ctrl-btn[data-tl-type]', card).forEach(function(b){ b.classList.toggle('active', b === btn); });
        apply();
      });
    });
    $$('.ctrl-chip[data-sub]', card).forEach(function(chip){
      chip.addEventListener('click', function(){
        state.sub = chip.getAttribute('data-sub') || '';
        $$('.ctrl-chip[data-sub]', card).forEach(function(c){ c.classList.toggle('active', c === chip); });
        apply();
      });
    });
    document.addEventListener('vc-filter-temporal', function(e){
      if (!e.detail.active) {
        state.weekday = null;
        state.hour = null;
      } else {
        state.weekday = (e.detail.weekday !== null && e.detail.weekday !== undefined) ? e.detail.weekday : null;
        state.hour = (e.detail.hour !== null && e.detail.hour !== undefined) ? e.detail.hour : null;
      }
      apply();
    });
    // Apply the initial cap (server renders all 500 rows visible — we cap to 25)
    apply();
  })();
})();
"""


def render(payload: dict) -> str:
    if payload.get("state") != "done":
        body = esc(json.dumps(payload, indent=2))
        return (
            f"<!doctype html><meta charset=utf-8>"
            f"<title>RedditPages</title><style>{CSS}</style>"
            f"<div class='wrap'><div class='pcard pcard--full pcard--neg'>"
            f"<div class='cat'>state: {esc(payload.get('state'))}</div>"
            f"<pre style='font-family:var(--mono);font-size:.7rem'>{body}</pre>"
            f"</div></div>"
        )

    r = payload["result"]
    karma_data = _compute_karma_series(r)

    def details(num: str, label: str, quick: str, content: str,
                is_open: bool = False) -> str:
        if not (content or "").strip():
            return ""
        oa = " open" if is_open else ""
        return f"""
        <details class="ds"{oa}>
          <summary class="ds-summary">
            <span class="ds-marker">›</span>
            <span class="ds-num">{esc(num)}</span>
            <span class="ds-label">{esc(label)}</span>
            <span class="ds-quick">{esc(quick)}</span>
          </summary>
          <div class="ds-body">{content}</div>
        </details>
        """

    # Quick summaries that appear on each section header (right side)
    k = r.get("karma") or {}
    c = r.get("corpus") or {}
    activity = r.get("activity") or {}
    subs = r.get("subreddits") or []
    bw = r.get("best_worst") or {}
    hidden_subs = r.get("hidden_subreddits") or {}
    total_karma = (k.get("submission_karma") or 0) + (k.get("comment_karma") or 0)
    by_day = activity.get("by_day") or {}
    peak_day = max(by_day.values()) if by_day else 0
    best_score = ((bw.get("best_comment") or bw.get("best_submission") or {})
                  .get("score") or 0)
    worst_score = ((bw.get("worst_comment") or bw.get("worst_submission") or {})
                   .get("score") or 0)
    tl_total = len(r.get("timeline") or [])

    sections = [
        section_hero(r),  # hero + visibility chips, always visible
        details(
            "01", "Karma",
            f"{fmt_compact(total_karma)} total · {k.get('comment_count', 0):,} comments · {k.get('submission_count', 0):,} posts",
            section_stats(r),
            is_open=True,
        ),
        details(
            "02", "Activity Patterns",
            f"{len(by_day)} active days · peak {peak_day}/day · tz {(r.get('timezone_guess') or {}).get('timezone', '?')}",
            section_heatmap(r) + section_temporal(r),
            is_open=True,
        ),
        details(
            "03", "Karma Over Time",
            f"{karma_data['n_buckets']} months tracked" if karma_data else "—",
            section_karma_over_time(r, karma_data),
            is_open=True,
        ),
        details(
            "04", "Best & Worst",
            f"high {fmt_int(best_score)} · low {fmt_int(worst_score)}",
            section_best_worst(r),
            is_open=True,
        ),
        details(
            "05", "Communities",
            f"{len(subs):,} subreddits · {len(hidden_subs)} with hidden activity",
            section_subreddit_timeline(r) + section_subreddits(r),
            is_open=True,
        ),
        details(
            "06", "Words",
            f"{fmt_compact(c.get('total_words', 0))} words · {c.get('unique_pct', 0)}% unique · ~{c.get('hours_typing', 0)}h typing",
            section_words(r),
            is_open=True,
        ),
        details(
            "07", "Outlinks",
            f"{len(r.get('domains') or {})} domains linked",
            section_domains(r),
            is_open=True,
        ),
        details(
            "08", "Timeline",
            f"{tl_total:,} events archived",
            section_timeline(r),
            is_open=True,
        ),
    ]
    body = "\n".join(s for s in sections if s)
    title = f'u/{r.get("username")} · RedditPages'

    # Embed minimal data for client-side interactivity (karma chart re-stack,
    # crosshair tooltip values, etc.). Use </ escaping to be safe inside HTML.
    embed = {"karma": karma_data} if karma_data else {}
    data_json = json.dumps(embed, separators=(",", ":")).replace("</", "<\\/")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{esc(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Geist:wght@300;400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
<div class="top-strip">
  <span class="ts-brand">RedditPages</span>
  <span class="ts-tag">reddit profile</span>
  <span class="ts-spacer"></span>
  <span>{esc(fmt_ts(r.get("fetched_at")))}</span>
</div>
{body}
<div class="footer">RedditPages · generated {esc(fmt_ts(r.get("fetched_at")))} · source: arctic-shift archive + reddit public listing</div>
</div>
<div id="hm-tooltip" class="floating-tip" hidden></div>
<div id="kg-tooltip" class="floating-tip floating-tip--rich" hidden></div>
<script id="vc-data" type="application/json">{data_json}</script>
<script>{JS}</script>
</body>
</html>"""


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: render.py <payload.json> [more.json ...]", file=sys.stderr)
        return 2
    for arg in argv:
        src = Path(arg)
        if not src.is_file():
            print(f"skip {arg}: not a file", file=sys.stderr)
            continue
        payload = json.loads(src.read_text())
        out = src.with_suffix(".html")
        out.write_text(render(payload))
        print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
