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

from redlens import constants, llm, prompts
from redlens.network.core import MAX_ROWS, Store
from redlens.network.rosters import BrandRoster

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


def _mined_mentions(store: Store,
                    authors: set[str] | None = None) -> dict[str, Any]:
    """Co-mentioned proper names, mined keylessly — the no-roster fallback.

    A token counts as a *name* when, looking only at **mid-sentence**
    occurrences (sentence starts prove nothing — every word is capitalized
    there), it is capitalized at least ``_CAP_MIN_RATIO`` of the time.
    Products and proper names are; prose words show up lowercase
    mid-sentence and drop out. Once a term qualifies, every casing counts
    as a mention. Ranked by how many accounts use the term (≥2).

    When ``authors`` is given, the text scan is restricted to those
    authors (used by :func:`extract_brand_roster` to mine only the
    coordinated cohort); ``None`` scans every author (the dashboard's
    unscoped fallback behind ``/api/mentions``).

    Honest limit: a brand the network *always* writes lowercase never
    qualifies — that's what the roster (and later the LLM slice) is for.
    """
    texts = store.texts()
    if authors is not None:
        texts = [row for row in texts if row["u"] in authors]
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


# --------------------------------------------------------------------------- #
# extract_brand_roster: mine + canonicalize the network's brand roster        #
# --------------------------------------------------------------------------- #


def _norm(name: str) -> str:
    """Fold a brand name for dedupe/merge: whitespace-collapsed, casefolded —
    the same normalization the topic-brands extractor uses so "ExpressVPN" and
    "express vpn" collapse to one key."""
    return "".join(name.split()).casefold()


def _build_extract_prompt(candidates: list[dict[str, Any]]) -> str:
    """Fill ``prompts/brand_extract.txt`` from the mined candidate rows.

    Pure: renders one bullet per mined token with its multi-account spread, so
    the model canonicalizes/classifies a fixed candidate SET (it never invents
    brands). The token text is untrusted; the template tells the model to treat
    the list as data."""
    lines = [f"- {c['term']} ({c['accounts']} accounts, {c['uses']} uses)"
             for c in candidates]
    return prompts.render("brand_extract", candidates="\n".join(lines))


def parse_brand_extract(raw: dict[str, Any]) -> list[tuple[str, list[str]]]:
    """``(canonical_name, [match_terms])`` pairs from one brand-extract reply.

    Pure (no LLM call): reads ``{"brands": [{"name", "match_terms"}]}``, drops
    blank names, defaults an empty term list to ``[name]`` so every brand is
    countable, and merges near-duplicate names (whitespace + case folded) so an
    alias the model listed twice doesn't become two roster rows."""
    rows = raw.get("brands")
    out: list[tuple[str, list[str]]] = []
    seen: dict[str, int] = {}     # normalized name -> index in out (first wins)
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name", "")).strip()
        if not name:
            continue
        raw_terms = row.get("match_terms")
        terms = [str(t).strip() for t in raw_terms
                 if isinstance(t, str) and str(t).strip()
                 ] if isinstance(raw_terms, list) else []
        norm = _norm(name)
        if norm in seen:
            kept = out[seen[norm]][1]
            kept.extend(t for t in (terms or [name]) if t not in kept)
            continue
        seen[norm] = len(out)
        out.append((name, terms or [name]))
    return out


def _merge_rosters(existing: BrandRoster,
                   mined: BrandRoster) -> BrandRoster:
    """Stage 3: append only genuinely new brands to ``existing``.

    Existing rows WIN — a curated entry is never overwritten or reordered, so
    hand-tuned names/terms survive a re-mine. A mined brand whose normalized
    name already appears in ``existing`` is dropped; new ones are appended in
    mined order."""
    out: list[tuple[str, list[str]]] = [(n, list(t)) for n, t in existing]
    have = {_norm(n) for n, _ in existing}
    for name, terms in mined:
        norm = _norm(name)
        if norm in have:
            continue
        have.add(norm)
        out.append((name, list(terms)))
    return out


def extract_brand_roster(store: Store, *, key: str | None,
                         existing: BrandRoster) -> BrandRoster:
    """Promote the coordinated cohort's brand chatter into a roster.

    Three stages, degrading cleanly without a key:

    - **Stage 1 (always, keyless)** — mine candidate names from the cohort's
      texts with the existing ``_mined_mentions`` machinery (a token capitalized
      ≥ ``_CAP_MIN_RATIO`` of the time mid-sentence, co-mentioned by ≥ 2
      accounts). No LLM, no new SQL.
    - **Stage 2 (only with ``key``)** — one LLM call canonicalizes the mined
      candidates: dedupe aliases, drop non-brands (dictionary words), emit
      ``(canonical_name, [match_terms])``. Keyless, this stage is skipped and
      each mined token stands as its own brand (``name`` = token, ``terms`` =
      ``[token]``).
    - **Stage 3 (always)** — merge into ``existing``: curated rows win, only
      genuinely new brands are appended (see :func:`_merge_rosters`).

    Returns the merged :class:`BrandRoster` — round-trippable through
    :func:`~redlens.network.rosters.load_brands`."""
    # Scope mining to the coordinated cohort so organic / tracked-topic brand
    # chatter doesn't leak into the roster; fall back to every author only when
    # no coordinated cohort is labeled.
    scope = set(store._coordinated) or None
    candidates = _mined_mentions(store, scope)["rows"]
    if key and candidates:
        prompt = _build_extract_prompt(candidates)
        mined = parse_brand_extract(llm.complete_json(prompt, key))
    else:
        mined = [(str(r["term"]), [str(r["term"])]) for r in candidates]
    return _merge_rosters(existing, mined)
