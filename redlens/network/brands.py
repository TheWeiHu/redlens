"""CATALOGUE stage: brand/name mentions per account + share-of-voice.

``mentions`` builds the mention matrix — exact roster counting with a roster,
mined proper names without one; ``share_of_voice`` splits each brand's Reddit
conversation into coordinated vs organic. Both read the shared roster-mention
scan on the :class:`~redlens.network.core.Store`.
"""
from __future__ import annotations

import re
from collections import Counter
from contextlib import closing
from typing import Any

from redlens import constants
from redlens.network.core import MAX_ROWS, Store

# Brand-ish term mining (the fallback brand proxy behind /api/mentions when
# no roster file is given).
_TOKEN_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9]{2,}\b")
_CAP_MIN_RATIO = 0.75  # a name is capitalized nearly every time it appears
_SKIP_TERMS = frozenset(constants.data_lines("stopwords.txt")) | frozenset({
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
    "sunday", "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
    "reddit", "redditor", "redditors"})


def mentions(store: Store) -> dict[str, Any]:
    """Brand/name mentions per account, for the mention matrix.

    With a roster (``brands.csv`` / ``--brands``) the counting is exact:
    deterministic, case-insensitive, whole-word over each brand's terms —
    a mention is a post/comment that matches. Without one it falls back
    to mined proper names (see ``_mined_mentions``).
    """
    return _roster_mentions(store) if store.roster else _mined_mentions(store)


def _roster_mentions(store: Store) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for name, cells in store.roster_counts().items():
        if not cells:
            continue
        rows.append({"term": name, "accounts": len(cells),
                     "uses": sum(cells.values()), "cells": dict(cells)})
    rows.sort(key=lambda r: (-r["accounts"], -r["uses"],
                             str(r["term"]).lower()))
    return {"source": "roster", "total": len(rows), "rows": rows[:MAX_ROWS]}


def _mined_mentions(store: Store) -> dict[str, Any]:
    """Co-mentioned proper names, mined keylessly — the no-roster fallback.

    A token counts as a *name* when, looking only at **mid-sentence**
    occurrences (sentence starts prove nothing — every word is capitalized
    there), it is capitalized at least ``_CAP_MIN_RATIO`` of the time.
    Products and proper names are; prose words show up lowercase
    mid-sentence and drop out. Once a term qualifies, every casing counts
    as a mention. Ranked by how many accounts use the term (≥2).

    Honest limit: a brand the network *always* writes lowercase never
    qualifies — that's what the roster (and later the LLM slice) is for.
    """
    texts = store.texts()
    mid_total: Counter[str] = Counter()            # mid-sentence, any case
    mid_cap: Counter[str] = Counter()              # mid-sentence, capital
    casings: dict[str, Counter[str]] = {}          # low -> seen spellings
    by_account: dict[str, Counter[str]] = {}       # low -> account -> n
    for row in texts:
        text = row["t"]
        for m in _TOKEN_RE.finditer(text):
            tok = m.group()
            low = tok.lower()
            by_account.setdefault(low, Counter())[row["u"]] += 1
            head = text[:m.start()].rstrip(" \"'([*_")
            if head and head[-1] not in ".!?:;\n-•":
                mid_total[low] += 1
                if tok[0].isupper():
                    mid_cap[low] += 1
            if tok[0].isupper():
                casings.setdefault(low, Counter())[tok] += 1
    rows: list[dict[str, Any]] = []
    for low, caps in mid_cap.items():
        if low in _SKIP_TERMS or caps / mid_total[low] < _CAP_MIN_RATIO:
            continue
        accounts = by_account[low]
        if len(accounts) < 2:
            continue
        spelling = sorted(casings[low].items(),
                          key=lambda kv: (-kv[1], kv[0]))[0][0]
        rows.append({"term": spelling, "accounts": len(accounts),
                     "uses": sum(accounts.values()), "cells": dict(accounts)})
    rows.sort(key=lambda r: (-r["accounts"], -r["uses"],
                             str(r["term"]).lower()))
    return {"source": "mined", "total": len(rows), "rows": rows[:MAX_ROWS]}


def share_of_voice(store: Store) -> dict[str, Any]:
    """Per roster brand, the coordinated cohort's share of its Reddit
    conversation: mentions by ``coordinated``-labeled accounts ÷ all
    mentions. Meaningful only once brand-tracking has pulled organic
    discussion into the DB (before that every brand is 100% coordinated,
    since the DB holds only the seeders). Ranked most-coordinated first —
    the brands the network most dominates float to the top.

    Needs both a roster (which brands) and cohort labels (who is
    coordinated); returns an empty set if either is missing.
    """
    if not store.roster or not store._coordinated:
        return {"available": False, "rows": []}
    # A share is only meaningful once the brand's ORGANIC conversation is
    # in the DB (the brand was tracked as a topic, or organic authors
    # mention it). Without that baseline, "100% coordinated" would just
    # mean "we only archived the seeders" — flag those rows instead.
    with closing(store.conn()) as con:
        tracked = {r[0].lower() for r in con.execute(
            "SELECT name FROM topic")}
    rows: list[dict[str, Any]] = []
    for name, cells in store.roster_counts().items():
        coord = Counter({u: n for u, n in cells.items()
                         if u in store._coordinated})
        org = Counter({u: n for u, n in cells.items()
                       if u not in store._coordinated})
        total = sum(coord.values()) + sum(org.values())
        if not total:
            continue
        baseline = bool(org) or name.lower() in tracked
        coord_pct = round(100 * sum(coord.values()) / total)
        # Live verdict (no external data): with a real organic baseline, a
        # brand the network overwhelmingly owns is SEEDED; one with healthy
        # independent discussion it merely name-drops is CAMOUFLAGE. When
        # Firecrawl/other verdicts are integrated they'd override this.
        if baseline and len(coord) >= 3 and coord_pct >= 90 and len(org) <= 2:
            verdict = "seeded"
        elif baseline and len(org) >= 5 and coord_pct <= 60:
            verdict = "camouflage"
        else:
            verdict = ""
        rows.append({
            "term": name,
            "baseline": baseline,
            "verdict": verdict,
            "total": total,
            "coordinated": sum(coord.values()),
            "organic": sum(org.values()),
            "coord_pct": coord_pct,
            "coord_authors": len(coord),
            "organic_authors": len(org),
            "top_coordinated": [u for u, _ in coord.most_common(10)],
            "top_organic": [u for u, _ in org.most_common(10)],
        })
    rows.sort(key=lambda r: (not r["baseline"], -r["coord_pct"],
                             -int(r["coordinated"]), str(r["term"]).lower()))
    return {"available": True,
            "coordinated_accounts": len(store._coordinated),
            "total": len(rows), "rows": rows[:MAX_ROWS]}
