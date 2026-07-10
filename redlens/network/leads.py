"""EXPAND stage: unlabeled accounts that look like undetected seeders.

``suggested_coordinated`` surfaces accounts not in ``cohorts.csv`` that push
many distinct roster brands — the keyless lead for growing the labeled cohort.
``verify_leads`` scores those candidates for likely membership in the
``coordinated`` block from three deterministic signals (no LLM), emitting a CSV
that feeds straight back into ``serve --promote`` / ``report --promote`` —
closing the detect → verify → promote loop. Both read the shared roster-mention
scan on the :class:`~redlens.network.core.Store`.
"""
from __future__ import annotations

from collections import Counter
from contextlib import closing
from dataclasses import asdict, dataclass
from statistics import median, quantiles
from typing import Any

from redlens.network.core import _ACTIVITY, MAX_ROWS, Store
from redlens.network.rosters import _term_pattern

SUGGEST_MIN_BRANDS = 3  # unlabeled accounts pushing ≥ this many distinct roster
                        # brands are flagged as likely-undetected seeders

# verify_leads score weights — how much each deterministic signal contributes
# to the 0..1 membership score. Roster-brand breadth is the strongest cheap
# seeding tell (a genuine user mentions a brand or two; a seeder pushes a
# catalogue), so it dominates; co-activity is the classic network overlap;
# wave participation is the rarest-but-strongest confirmation (few brands
# arrive in a synchronized burst at all).
_W_BRANDS = 0.45      # roster-brand breadth vs the confirmed cohort
_W_COACTIVITY = 0.35  # subreddit/thread overlap with the coordinated block
_W_WAVES = 0.20       # detected seeding waves the account pushed into

VERIFY_MIN_SCORE = 0.5  # generic default: rows at/above this are "coordinated"

# Reddit collapses every deleted account and removed author into these two
# placeholders, so a single "[deleted]" row is really many different people —
# never a real account. Kept in the counts but sorted last + flagged so it
# never reads as the top suspect.
_NON_ACCOUNTS = ("[deleted]", "[removed]")


def suggested_coordinated(store: Store) -> dict[str, Any]:
    """Unlabeled accounts that push many distinct roster brands — likely
    undetected seeders. The keyless verification signal behind the "these
    49 organic authors" question: a genuine organic user mentions a brand
    or two they actually use; a seeder pushes a catalog. An account not in
    ``cohorts.csv`` mentioning ≥ ``SUGGEST_MIN_BRANDS`` distinct roster
    brands (across whatever history is synced) is surfaced for review —
    confirm from its profile, then add it to ``cohorts.csv``.

    Only as strong as the archived history: an account with only a single
    pulled post can't show breadth. Sync the pool's full histories first
    (author-scoped) for this to bite. Needs a roster and cohort labels.
    """
    if not store.roster or not store.cohorts:
        return {"available": False, "rows": []}
    brands_by: dict[str, set[str]] = {}
    mentions_by: Counter[str] = Counter()
    for name, cells in store.roster_counts().items():
        for u, n in cells.items():
            if u in store.cohorts:    # already labeled — not a suggestion
                continue
            brands_by.setdefault(u, set()).add(name)
            mentions_by[u] += n
    # '[deleted]'/'[removed]' are many people merged under one name, not a
    # real account — keep the row (its brand breadth is real signal) but
    # sort it below every genuine account and flag it.
    flagged = sorted(
        (u for u, bs in brands_by.items() if len(bs) >= SUGGEST_MIN_BRANDS),
        key=lambda u: (u in _NON_ACCOUNTS, -len(brands_by[u]),
                       -mentions_by[u], u))
    rows: list[dict[str, Any]] = [
        {"account": u, "brands": sorted(brands_by[u]),
         "brand_count": len(brands_by[u]), "mentions": mentions_by[u],
         "placeholder": u in _NON_ACCOUNTS}
        for u in flagged
    ]
    return {"available": True, "threshold": SUGGEST_MIN_BRANDS,
            "total": len(rows), "rows": rows[:MAX_ROWS]}


# --------------------------------------------------------------------------- #
# verify_leads: score candidates for coordinated-cohort membership            #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class LeadVerdict:
    """One candidate account scored for likely ``coordinated`` membership.

    ``score`` is the weighted 0..1 blend of the three deterministic signals;
    the raw signal readings (``roster_brands`` / ``coactivity`` / ``wave_hits``)
    and a short ``evidence`` string travel with it so the verdict is auditable
    without re-running the analysis.
    """

    account: str
    score: float
    roster_brands: int
    coactivity: float
    wave_hits: int
    evidence: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _cohort_brand_floor(store: Store) -> float:
    """The confirmed-``coordinated`` cohort's roster-brand breadth floor — the
    p25 (lower quartile) of distinct roster brands per coordinated account,
    falling back to the median then to ``SUGGEST_MIN_BRANDS``. A candidate that
    pushes at least this many distinct brands looks as broad as the typical
    confirmed seeder, so it earns full breadth credit."""
    breadth: Counter[str] = Counter()
    for cells in store.roster_counts().values():
        for u in cells:
            if u in store._coordinated:
                breadth[u] += 1
    vals = sorted(breadth.values())
    if len(vals) >= 4:
        return max(quantiles(vals, n=4)[0], 1.0)   # p25
    if vals:
        return max(median(vals), 1.0)
    return float(SUGGEST_MIN_BRANDS)


def _coactivity_overlap(store: Store, candidates: set[str]) -> dict[str, float]:
    """Fraction of each candidate's footprint (distinct subreddits + comment
    threads) also touched by the labeled ``coordinated`` block — the classic
    network-overlap signal. 0 when the candidate has no footprint, 1 when the
    block shares every one of its subs/threads. Reuses the shared ``_ACTIVITY``
    event log + the comment thread map rather than re-deriving them."""
    block = store._coordinated
    if not block or not candidates:
        return dict.fromkeys(candidates, 0.0)
    ph = ",".join("?" * len(block))
    own: dict[str, set[str]] = {c: set() for c in candidates}
    shared: dict[str, set[str]] = {c: set() for c in candidates}
    with closing(store.conn()) as con:
        block_subs = {r[0] for r in con.execute(
            f"SELECT DISTINCT sub FROM ({_ACTIVITY}) WHERE u IN ({ph})",
            list(block))}
        block_threads = {r[0] for r in con.execute(
            f"SELECT DISTINCT link_id FROM comment "
            f"WHERE author_username IN ({ph})", list(block))}
        for r in con.execute(f"SELECT DISTINCT u, sub FROM ({_ACTIVITY})"):
            if r["u"] in own:
                key = "s:" + r["sub"]
                own[r["u"]].add(key)
                if r["sub"] in block_subs:
                    shared[r["u"]].add(key)
        for r in con.execute(
                "SELECT DISTINCT author_username u, link_id t FROM comment"):
            if r["u"] in own:
                key = "t:" + r["t"]
                own[r["u"]].add(key)
                if r["t"] in block_threads:
                    shared[r["u"]].add(key)
    return {c: (len(shared[c]) / len(own[c]) if own[c] else 0.0)
            for c in candidates}


def _candidate_first_seen(store: Store,
                          candidates: set[str]) -> dict[str, dict[str, int]]:
    """``{account -> {brand -> first-mention utc}}`` for the candidates.

    ``Store`` only records first-mention times for *labeled* accounts (the
    seeding-wave view is cohort-only), so derive the candidates' from the same
    shared text scan (``store.texts()``) + roster matcher — no new SQL."""
    pats = [(name, _term_pattern(terms)) for name, terms in store.roster]
    seen: dict[str, dict[str, int]] = {c: {} for c in candidates}
    for r in store.texts():
        u = r["u"]
        if u not in seen or r["ts"] is None:
            continue
        for name, pat in pats:
            if pat.search(r["t"]):
                d = seen[u]
                if name not in d or r["ts"] < d[name]:
                    d[name] = r["ts"]
    return seen


def _wave_hits(store: Store, candidates: set[str],
               window_days: int) -> tuple[dict[str, int], int]:
    """How many detected seeding waves each candidate pushed into, plus the
    total wave count. A candidate "hits" a wave when its first mention of the
    wave's brand lands inside the wave window (extended one window back, so an
    early pusher that seeded *before* the labeled cascade still counts)."""
    waves = seeding_waves(store, window_days=window_days)
    rows: list[dict[str, Any]] = waves["rows"] if waves["available"] else []
    if not rows:
        return dict.fromkeys(candidates, 0), 0
    span = window_days * 86400
    first = _candidate_first_seen(store, candidates)
    hits = dict.fromkeys(candidates, 0)
    for w in rows:
        lo, hi = w["start"] - span, w["end"]
        for c in candidates:
            ts = first[c].get(w["brand"])
            if ts is not None and lo <= ts <= hi:
                hits[c] += 1
    return hits, len(rows)


def verify_leads(store: Store, candidates: list[str] | None = None, *,
                 min_score: float = VERIFY_MIN_SCORE,
                 window_days: int = 14) -> list[LeadVerdict]:
    """Score candidate accounts for likely ``coordinated``-cohort membership
    from three deterministic signals — no LLM, reproducible run-to-run.

    ``candidates=None`` defaults to :func:`suggested_coordinated`'s output (the
    unlabeled multi-brand pushers). Signals, each read 0..1 and blended with the
    module weights (``_W_BRANDS`` 0.45 / ``_W_COACTIVITY`` 0.35 / ``_W_WAVES``
    0.20):

    - **roster-brand breadth** — distinct roster brands the account pushes vs
      the confirmed cohort's p25 breadth floor (a seeder pushes a catalogue);
    - **co-activity overlap** — share of the account's subreddits/threads the
      labeled ``coordinated`` block also works;
    - **seeding-wave participation** — how many detected seeding waves the
      account pushed into as an early mover.

    Returns every candidate ranked by score (desc); ``min_score`` is carried for
    the caller's CSV/promote cut, it does not drop rows here. Needs a roster and
    a ``coordinated`` cohort; without them every score is 0."""
    if candidates is None:
        candidates = [r["account"]
                      for r in suggested_coordinated(store).get("rows", [])]
    cands = {c for c in candidates if c not in store._coordinated}
    if not cands:
        return []
    # Every signal is measured *against* the labeled coordinated block; without
    # one there is no baseline, so every score is 0 (explicit candidates still
    # come back as zero-score rows rather than vanishing).
    if not store._coordinated or not store.roster:
        return [LeadVerdict(account=c, score=0.0, roster_brands=0,
                            coactivity=0.0, wave_hits=0, evidence="no baseline")
                for c in sorted(cands)]

    counts = store.roster_counts()
    brands_by: dict[str, int] = {
        c: sum(1 for cells in counts.values() if c in cells) for c in cands}
    floor = _cohort_brand_floor(store)
    coact = _coactivity_overlap(store, cands)
    hits, n_waves = _wave_hits(store, cands, window_days)
    wave_norm = float(min(n_waves, 3)) or 1.0   # 3 waves = full credit
    # ``seeding_waves`` is gated on ≥2 cohorts; with a single ``coordinated``
    # cohort the wave signal can't be computed and every wave score is 0. Say so
    # in the evidence rather than presenting a real zero (the weighting is
    # unchanged — an unavailable signal already contributes 0).
    wave_na = not store.multi_cohort

    out: list[LeadVerdict] = []
    for c in sorted(cands):
        nb = brands_by[c]
        s_brands = min(nb / floor, 1.0) if floor else 0.0
        s_coact = coact[c]
        s_waves = min(hits[c] / wave_norm, 1.0)
        score = round(_W_BRANDS * s_brands + _W_COACTIVITY * s_coact
                      + _W_WAVES * s_waves, 3)
        bits = [f"{nb} roster brand{'' if nb == 1 else 's'}",
                f"{round(100 * s_coact)}% co-activity"]
        if wave_na:
            bits.append("wave signal: n/a (needs ≥2 cohorts)")
        elif hits[c]:
            bits.append(f"{hits[c]} seeding wave{'' if hits[c] == 1 else 's'}")
        out.append(LeadVerdict(
            account=c, score=score, roster_brands=nb,
            coactivity=round(s_coact, 3), wave_hits=hits[c],
            evidence=", ".join(bits)))
    out.sort(key=lambda v: (-v.score, v.account))
    return out


# seeding_waves lives in the sibling stage module; import at the bottom to
# mirror core.py's cycle-breaking pattern (leads is imported by core, and
# seeding imports Store from core too).
from redlens.network.seeding import seeding_waves  # noqa: E402
