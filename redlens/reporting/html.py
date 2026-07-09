"""Shared HTML/SVG primitives for report renderers.

The generic building blocks behind :mod:`redlens.reporting.page` — the
standalone-page shell (doctype + inline CSS), small formatting helpers,
and the inline-SVG chart drawing. Data prep stays with each renderer;
everything here takes plain values and returns markup.
"""
from __future__ import annotations

import html
from collections import Counter
from datetime import UTC, datetime
from importlib.resources import files

from redlens import constants
from redlens.sentiment import DaySentiment

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
CSS = files("redlens.reporting").joinpath("style.css").read_text(
    encoding="utf-8").replace("$ACCENT", constants.ACCENT)


def fmt_date(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d")


def trunc(text: str) -> str:
    return html.escape(text[:constants.TITLE_MAX]
                       + ("…" if len(text) > constants.TITLE_MAX else ""))


def bar(label: str, n: int, peak: int, prefix: str = "") -> str:
    return (f'<div class="bar"><div>{html.escape(prefix + label)}</div>'
            f'<div class="t"><div class="f" style="width:{100 * n / peak:.0f}%">'
            f'</div></div><div class="v">{n:,}</div></div>')


def html_shell(title: str, body: str) -> str:
    """The standalone-HTML wrapper (doctype, head, inline CSS) shared by every
    rendered page so they can't drift. ``title`` is escaped and suffixed with
    ' · redlens'; ``body`` is the inner markup."""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<title>{html.escape(title)} · redlens</title>'
        f'<style>{CSS}</style></head><body>\n{body}\n</body></html>')


def day_chart(series: list[tuple[str, int]]) -> str:
    """Inline-SVG column chart, one bar per day, hover for the count."""
    if not series:
        return ""
    w, h = 600.0, 60.0
    peak = max(v for _, v in series) or 1
    bw = w / len(series)
    bars = "".join(
        f'<rect x="{i * bw:.1f}" y="{h - v / peak * h:.1f}" '
        f'width="{max(bw - 0.5, 0.4):.1f}" height="{v / peak * h:.1f}">'
        f"<title>{d}: {v:,} posts</title></rect>"
        for i, (d, v) in enumerate(series)
    )
    return (f'<svg viewBox="0 0 {w:.0f} {h + 12:.0f}">{bars}'
            f'<text x="0" y="{h + 10:.0f}">{series[0][0]}</text>'
            f'<text x="{w:.0f}" y="{h + 10:.0f}" text-anchor="end">'
            f'{series[-1][0]}</text></svg>')


def sentiment_chart(series: list[DaySentiment]) -> str:
    """Inline-SVG diverging bar chart of daily sentiment in [-1, 1]: bars rise
    (green) above a neutral baseline for positive days and fall (red) below for
    negative ones, height ~ magnitude; hover for the day's score and post
    count. Unscored days (``mean is None`` — gaps or days the model left out)
    draw no bar. Returns '' when no day carries a non-zero score."""
    scored = [d for d in series if d.mean is not None]
    if not scored or all(d.mean == 0.0 for d in scored):
        return ""
    width, half, pad = 600.0, 38.0, 14.0
    center, total_h = half, half * 2
    bw = width / len(series)
    bars = []
    for i, dy in enumerate(series):
        if dy.mean is None:
            continue
        bh = abs(dy.mean) * half
        y = center - bh if dy.mean >= 0 else center
        cls = "pos" if dy.mean >= 0 else "neg"
        sign = "+" if dy.mean >= 0 else ""
        counts = f"{dy.posts:,} posts" + (
            f", {dy.comments:,} comments" if dy.comments else "")
        bars.append(
            f'<rect class="{cls}" x="{i * bw:.1f}" y="{y:.1f}" '
            f'width="{max(bw - 0.5, 0.4):.1f}" height="{max(bh, 0.6):.1f}">'
            f"<title>{dy.day}: {sign}{dy.mean:.2f} · {counts}</title></rect>"
        )
    baseline = (f'<line x1="0" y1="{center:.1f}" x2="{width:.0f}" '
                f'y2="{center:.1f}" stroke="#ccc" stroke-width="0.5"/>')
    labels = (f'<text x="0" y="{total_h + pad - 2:.0f}">{series[0].day}</text>'
              f'<text x="{width:.0f}" y="{total_h + pad - 2:.0f}" '
              f'text-anchor="end">{series[-1].day}</text>')
    return (f'<svg viewBox="0 0 {width:.0f} {total_h + pad:.0f}">'
            f'{baseline}{"".join(bars)}{labels}</svg>')


def punchcard_svg(counts: Counter[tuple[int, int]], unit: str) -> str:
    """Inline-SVG weekday x hour grid (UTC); dot area ~ volume, hover for the
    count. ``counts`` is keyed on ``(weekday, hour)``; ``unit`` labels what a
    dot counts in the hover text."""
    if not counts:
        return ""
    peak = max(counts.values())
    cell, left, top = 22, 32, 4
    w, h = left + 24 * cell, top + 7 * cell + 14
    dots = "".join(
        f'<circle cx="{left + hr * cell + cell / 2:.0f}" '
        f'cy="{top + d * cell + cell / 2:.0f}" r="{1 + 8 * (n / peak) ** 0.5:.1f}">'
        f"<title>{WEEKDAYS[d]} {hr:02d}:00 UTC — {n:,} {unit}</title>"
        f"</circle>"
        for (d, hr), n in sorted(counts.items())
    )
    labels = "".join(
        f'<text x="{left - 5}" y="{top + i * cell + cell / 2 + 3}" '
        f'text-anchor="end">{wd}</text>' for i, wd in enumerate(WEEKDAYS)
    ) + "".join(
        f'<text x="{left + hr * cell + cell / 2}" y="{h - 3}" '
        f'text-anchor="middle">{hr:02d}</text>' for hr in (0, 6, 12, 18)
    )
    return f'<svg viewBox="0 0 {w} {h}">{labels}{dots}</svg>'
