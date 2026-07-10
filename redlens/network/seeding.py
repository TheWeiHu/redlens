"""VERIFY stage: the multi-cohort coordination views.

Seeding waves + the coordination raster (brands arriving in a synchronized
burst), the cohort comparison / timeline / bridges (how labeled cohorts
overlap), and the per-cohort outbound-domain catalogue. All are meaningful
only with >1 labeled cohort and read the shared scan on the
:class:`~redlens.network.core.Store`.
"""
from __future__ import annotations

import datetime as _dt
import re
from collections import Counter
from contextlib import closing
from dataclasses import asdict, dataclass
from typing import Any, Literal

from redlens.network.core import _ACTIVITY, MAX_ROWS, Store
from redlens.network.rosters import _term_pattern

# outbound domains for the per-cohort catalogue view
_DOMAIN_RE = re.compile(
    r"\b([a-z0-9][a-z0-9\-]{1,30}\.(?:com|net|io|co|app|xyz|ai|org|tv|gg|me|"
    r"shop|store|link|to|vip|club))\b", re.I)
_SKIP_DOMAINS = frozenset({
    "reddit.com", "google.com", "youtube.com", "youtu.be", "facebook.com",
    "amazon.com", "twitter.com", "x.com", "instagram.com", "tiktok.com",
    "medium.com", "whatsapp.com", "gmail.com", "chatgpt.com", "cloudfront.net",
    "vercel.app", "redgifs.com", "imgur.com", "github.com", "apple.com",
    "linkedin.com", "discord.gg", "t.me", "wikipedia.org"})


def cohort_comparison(store: Store) -> dict[str, Any]:
    """Per roster brand, how many accounts in each cohort mention it —
    splitting brands into shared (pushed by ≥2 cohorts) vs cohort-specific.
    The two-operation catalogue view.
    """
    if not store.multi_cohort or not store.roster:
        return {"available": False, "cohorts": [], "rows": []}
    names = store.cohort_names()
    rows: list[dict[str, Any]] = []
    for brand, cells in store.roster_counts().items():
        by = {c: 0 for c in names}
        org = 0
        for u in cells:
            c = store.cohorts.get(u)
            if c in by:
                by[c] += 1
            elif u not in store.cohorts:
                org += 1
        present = [c for c in names if by[c]]
        if not present:
            continue
        rows.append({"brand": brand, "by": by, "organic": org,
                     "shared": len(present) > 1,
                     "total": sum(by.values())})
    rows.sort(key=lambda r: (not r["shared"], -r["total"], r["brand"].lower()))
    return {"available": True, "cohorts": names,
            "shared": sum(1 for r in rows if r["shared"]),
            "rows": rows[:MAX_ROWS]}


def _tightest_cluster(seen: dict[str, int], span: int,
                      min_n: int = 3) -> dict[str, Any] | None:
    """The largest cluster of ``{account -> first-mention ts}`` whose span fits
    inside ``span`` seconds (a seeding wave), or ``None`` below ``min_n``. The
    sliding-window math shared by ``seeding_waves`` (all labeled accounts) and
    the per-brand coordinated wave size in ``seeding_verdicts``."""
    if len(seen) < min_n:
        return None
    pts = sorted(seen.items(), key=lambda x: x[1])
    best: dict[str, Any] | None = None
    for i in range(len(pts)):
        j = i
        while j + 1 < len(pts) and pts[j + 1][1] - pts[i][1] <= span:
            j += 1
        if j - i + 1 >= min_n and (best is None or j - i + 1 > best["n"]):
            best = {"n": j - i + 1, "start": pts[i][1], "end": pts[j][1],
                    "accounts": [a for a, _ in pts[i:j + 1]]}
    return best


def seeding_waves(store: Store, window_days: int = 14) -> dict[str, Any]:
    """Brands that arrive in a *wave* — ≥3 labeled accounts first mentioning
    the brand within ``window_days`` of each other. Organic brands trickle;
    seeded ones cascade.
    """
    if not store.multi_cohort or not store.roster:
        return {"available": False, "rows": []}
    span = window_days * 86400
    waves: list[dict[str, Any]] = []
    for brand, seen in store.labeled_first_seen().items():
        best = _tightest_cluster(seen, span)
        if best:
            best.update(brand=brand, total=len(seen),
                        cohorts=sorted({store.cohorts[a] for a in best["accounts"]
                                        if a in store.cohorts}))
            waves.append(best)
    waves.sort(key=lambda w: -w["n"])
    return {"available": True, "window_days": window_days,
            "rows": waves[:MAX_ROWS]}


def coordination_raster(store: Store, top_brands: int = 12) -> dict[str, Any]:
    """The coordination raster: one row per labeled account (y), time (x),
    a dot each time an account *first* pushes one of the top brands, dot
    colour = brand. A coordinated push shows as a vertical band — many
    accounts, one colour, one narrow window. Limited to the widest-spread
    brands so the colour legend stays legible.
    """
    if not store.multi_cohort or not store.roster:
        return {"available": False, "accounts": [], "brands": [], "events": []}
    first = store.labeled_first_seen()          # {brand: {account: first ts}}
    ranked = sorted(((b, a) for b, a in first.items() if len(a) >= 3),
                    key=lambda kv: -len(kv[1]))
    brands = [b for b, _ in ranked[:top_brands]]
    if not brands:
        return {"available": False, "accounts": [], "brands": [], "events": []}
    acct_first: dict[str, int] = {}
    for b in brands:
        for a, ts in first[b].items():
            acct_first[a] = min(acct_first.get(a, ts), ts)
    # order accounts by cohort, then by when they first appear (so the
    # earliest pushers sit together and bands read top-to-bottom)
    accounts = sorted(acct_first, key=lambda a: (
        store._cohort_rank.get(store.cohorts.get(a, ""), 99), acct_first[a]))
    aidx = {a: i for i, a in enumerate(accounts)}
    bidx = {b: i for i, b in enumerate(brands)}
    events = [{"a": aidx[a], "b": bidx[b], "ts": ts}
              for b in brands for a, ts in first[b].items()]
    return {"available": True,
            "accounts": [{"name": a, "cohort": store.cohorts.get(a, "")}
                         for a in accounts],
            "brands": brands, "events": events}


def cohort_timeline(store: Store) -> dict[str, Any]:
    """Monthly post+comment volume per cohort — the activity lifecycle."""
    if not store.multi_cohort:
        return {"available": False, "cohorts": [], "months": [], "series": {}}
    names = store.cohort_names()
    series: dict[str, Counter[str]] = {c: Counter() for c in names}
    with closing(store.conn()) as con:
        for r in con.execute(
            "SELECT author_username u, created_utc ts FROM post "
            "UNION ALL SELECT author_username, created_utc FROM comment"):
            c = store.cohorts.get(r["u"])
            if c and r["ts"]:
                ym = _dt.datetime.fromtimestamp(
                    r["ts"], _dt.UTC).strftime("%Y-%m")
                series[c][ym] += 1
    months = sorted({m for s in series.values() for m in s})
    return {"available": True, "cohorts": names, "months": months,
            "series": {c: [series[c].get(m, 0) for m in months]
                       for c in names}}


def cohort_bridges(store: Store) -> dict[str, Any]:
    """Cross-cohort links: pairs of labeled accounts in *different* cohorts
    that comment in the same threads — the accounts stitching two operations
    together. Plus the subreddits both cohorts work.
    """
    if not store.multi_cohort:
        return {"available": False, "edges": [], "shared_subs": []}
    labeled = sorted(store.cohorts)
    ph = ",".join("?" * len(labeled))
    edges: list[dict[str, Any]] = []
    with closing(store.conn()) as con:
        for r in con.execute(
            f"""WITH ut AS (SELECT DISTINCT author_username u, link_id t
                           FROM comment WHERE author_username IN ({ph}))
                SELECT a.u ua, b.u ub, count(*) n
                FROM ut a JOIN ut b ON a.t = b.t AND a.u < b.u
                GROUP BY ua, ub""", labeled):
            ca, cb = store.cohorts.get(r["ua"]), store.cohorts.get(r["ub"])
            if ca != cb:
                edges.append({"a": r["ua"], "b": r["ub"],
                              "coh_a": ca, "coh_b": cb, "shared": r["n"]})
        edges.sort(key=lambda e: -e["shared"])
        # subreddits where >1 cohort is active
        sub_coh: dict[str, dict[str, int]] = {}
        for r in con.execute(
            f"SELECT DISTINCT u, sub FROM ({_ACTIVITY}) WHERE u IN ({ph})",
            labeled):
            c = store.cohorts.get(r["u"])
            if c:
                sub_coh.setdefault(r["sub"], {})[c] = \
                    sub_coh.setdefault(r["sub"], {}).get(c, 0) + 1
    shared_subs: list[dict[str, Any]] = [
        {"sub": s, "by": d} for s, d in sub_coh.items() if len(d) > 1]
    shared_subs.sort(key=lambda r: -min(r["by"].values()))
    return {"available": True, "cohorts": store.cohort_names(),
            "edges": edges[:MAX_ROWS], "shared_subs": shared_subs[:MAX_ROWS]}


def domain_catalogue(store: Store) -> dict[str, Any]:
    """Outbound domains each cohort links to — the products it pushes, by
    cohort. Domains from post URLs and from links in text.
    """
    if not store.multi_cohort:
        return {"available": False, "cohorts": [], "rows": []}
    names = store.cohort_names()
    labeled = set(store.cohorts)
    dom: dict[str, dict[str, set[str]]] = {}
    with closing(store.conn()) as con:
        rows = con.execute(
            "SELECT author_username u, coalesce(url,'') || ' ' || "
            "coalesce(selftext,'') t FROM post "
            "UNION ALL SELECT author_username, coalesce(body,'') FROM comment"
        ).fetchall()
    for r in rows:
        c = store.cohorts.get(r["u"])
        if r["u"] not in labeled or not c:
            continue
        for m in _DOMAIN_RE.finditer(r["t"]):
            d = m.group(1).lower()
            if d in _SKIP_DOMAINS:
                continue
            dom.setdefault(d, {n: set() for n in names})[c].add(r["u"])
    rows_out: list[dict[str, Any]] = []
    for d, by in dom.items():
        counts = {c: len(by[c]) for c in names}
        if sum(counts.values()) >= 2:
            rows_out.append({"domain": d, "by": counts,
                             "total": sum(counts.values())})
    rows_out.sort(key=lambda r: -r["total"])
    return {"available": True, "cohorts": names, "rows": rows_out[:MAX_ROWS]}


# --------------------------------------------------------------------------- #
# seeding_verdicts: per-brand SEEDED-vs-ORGANIC verdict (the Stage-C call)     #
# --------------------------------------------------------------------------- #

Verdict = Literal["seeded", "organic-first", "inconclusive"]

# seeding_verdicts thresholds — generic defaults, all overridable as params.
SEED_MIN_WAVE = 3         # a coordinated first-push cluster this tight (≥ N
                          # accounts in one window) counts as a real seeding wave
SEED_NET_RATIO = 3.0      # network mentions must outweigh organic by ≥ this to
                          # read "network ≫ organic" (organic never adopting also
                          # clears the bar — the strongest seeded tell)
_DAY = 86_400


@dataclass(frozen=True)
class BrandVerdict:
    """One roster brand judged SEEDED (pushed first by the coordinated network)
    vs adopted ORGANICALLY, from deterministic DB signals only.

    ``verdict`` blends four readings that travel with it for auditing:
    ``first_mover_cohort`` (who mentioned it first), ``net_accounts`` /
    ``net_mentions`` (the coordinated network's footprint), ``organic_mentions``
    (the unlabeled pool's), ``wave_size`` (how many coordinated accounts first-
    pushed it in one window — 0 = no wave), and ``organic_lag_days`` (days from
    the coordinated first-push to the first organic mention; ``None`` = organic
    never adopted, the strongest seeded signal). ``evidence`` is the one-line
    human summary.
    """

    brand: str
    verdict: Verdict
    net_accounts: int
    net_mentions: int
    organic_mentions: int
    first_mover_cohort: str
    wave_size: int
    organic_lag_days: int | None
    evidence: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _organic_first_seen(store: Store) -> dict[str, int]:
    """``{brand -> first-mention utc}`` over the *organic* side — every author
    NOT in the coordinated block (a labeled ``organic``/other cohort **plus**
    truly-unlabeled authors that brand-tracking pulled in). Reuses the shared
    text scan + roster matcher — ``Store`` records first-seen only for labeled
    accounts, so derive the organic side here without new SQL (mirrors
    leads._candidate_first_seen)."""
    pats = [(name, _term_pattern(terms)) for name, terms in store.roster]
    block = store._coordinated
    first: dict[str, int] = {}
    for r in store.texts():
        u, text, ts = r["u"], r["t"], r["ts"]
        if u in block or ts is None:
            continue
        for name, pat in pats:
            if pat.search(text) and (name not in first or ts < first[name]):
                first[name] = ts
    return first


def seeding_verdicts(store: Store, *, min_wave: int = SEED_MIN_WAVE,
                     net_ratio: float = SEED_NET_RATIO) -> list[BrandVerdict]:
    """Per roster brand, judge whether it looks SEEDED by the coordinated
    network or adopted ORGANICALLY — the Stage-C "seeded vs camouflage" verdict
    as a deterministic call (no LLM, reproducible run-to-run).

    All signals reuse the shared roster-mention scan + the seeding-wave math:

    - **first mover** — the earliest labeled-vs-organic mention decides
      ``first_mover_cohort`` (the coordinated block, another labeled cohort, or
      ``organic``);
    - **network footprint** — ``net_accounts`` / ``net_mentions`` from the
      ``coordinated`` block via ``roster_counts``;
    - **organic footprint** — ``organic_mentions`` by non-coordinated authors
      (a labeled ``organic``/other cohort plus truly-unlabeled authors);
    - **wave tightness** — the coordinated block's first-push cluster via the
      ``seeding_waves`` window math (``wave_size`` = block accounts in the
      tightest 14-day window, 0 = no wave);
    - **adoption lag** — days between the coordinated first-push and the first
      organic mention (``None`` = organic never adopted).

    Verdict rules (thresholds as params): ``seeded`` = coordinated first-mover
    **and** a tight wave (≥ ``min_wave``) **and** the network out-mentions
    organic by ≥ ``net_ratio`` (or organic never adopts); ``organic-first`` =
    organic is the first mover **or** out-mentions the network; else
    ``inconclusive``. Needs a roster and a ``coordinated`` cohort; without them
    the list is empty. Sorted seeded-first, then by network mentions."""
    if not store.roster or not store._coordinated:
        return []
    counts = store.roster_counts()
    labeled_first = store.labeled_first_seen()
    org_first = _organic_first_seen(store)
    span = 14 * _DAY   # the seeding-wave window (same default as seeding_waves)
    block = store._coordinated

    out: list[BrandVerdict] = []
    for brand, _ in store.roster:
        cells = counts.get(brand, Counter())
        net_accounts = sum(1 for u in cells if u in block)
        net_mentions = sum(n for u, n in cells.items() if u in block)
        organic_mentions = sum(n for u, n in cells.items() if u not in block)
        if not net_mentions and not organic_mentions:
            continue

        # who mentioned it first: earliest coordinated first-push vs earliest
        # organic mention (labeled_first covers every labeled cohort).
        coord_seen = {u: ts for u, ts in labeled_first.get(brand, {}).items()
                      if u in block}
        coord_first = min(coord_seen.values()) if coord_seen else None
        org_ts = org_first.get(brand)
        first_mover = _first_mover(store, brand, labeled_first, org_ts)
        # wave tightness: the coordinated block's own first-push cluster (the
        # seeding_waves math run on the block only, so an organic-cohort wave
        # never reads as a coordinated seeding wave).
        coord_wave = _tightest_cluster(coord_seen, span)
        wave_size = coord_wave["n"] if coord_wave else 0
        lag = (round((org_ts - coord_first) / _DAY)
               if coord_first is not None and org_ts is not None else None)

        verdict, evidence = _verdict(
            first_mover=first_mover, wave_size=wave_size,
            net_mentions=net_mentions, organic_mentions=organic_mentions,
            lag=lag, coord_first=coord_first, min_wave=min_wave,
            net_ratio=net_ratio)
        out.append(BrandVerdict(
            brand=brand, verdict=verdict, net_accounts=net_accounts,
            net_mentions=net_mentions, organic_mentions=organic_mentions,
            first_mover_cohort=first_mover, wave_size=wave_size,
            organic_lag_days=lag, evidence=evidence))

    order = {"seeded": 0, "inconclusive": 1, "organic-first": 2}
    out.sort(key=lambda v: (order[v.verdict], -v.net_mentions, v.brand.lower()))
    return out


def _first_mover(store: Store, brand: str,
                 labeled_first: dict[str, dict[str, int]],
                 org_ts: int | None) -> str:
    """The cohort of whoever mentioned ``brand`` first — a labeled cohort name
    (``coordinated`` and any others) or ``organic``. The coordinated block only
    wins on a *strictly* earlier first push: a tie (or the organic pool being
    first) resolves to ``organic`` / the non-coordinated side, so a synchronized
    same-day arrival never reads as coordinated-first."""
    # Earliest COORDINATED first push vs earliest non-coordinated first mention.
    # Non-coordinated covers a labeled organic/other cohort AND the unlabeled
    # pool (org_ts), so the earliest of the two is the non-coordinated side.
    # org_ts (from _organic_first_seen) is the earliest NON-coordinated mention
    # across the labeled organic/other cohort AND the unlabeled pool; the loop
    # below names which labeled cohort that was (a labeled account matching the
    # pool time wins the label, hence <=), falling back to 'organic' (the
    # unlabeled pool).
    coord_ts: int | None = None
    other_ts: int | None = org_ts
    other_cohort = "organic"
    for u, ts in labeled_first.get(brand, {}).items():
        if u in store._coordinated:
            coord_ts = ts if coord_ts is None else min(coord_ts, ts)
        elif other_ts is None or ts <= other_ts:
            other_ts, other_cohort = ts, store.cohorts.get(u, "") or "organic"
    if coord_ts is not None and (other_ts is None or coord_ts < other_ts):
        return "coordinated"
    return other_cohort if other_ts is not None else "coordinated"


def _verdict(*, first_mover: str, wave_size: int, net_mentions: int,
             organic_mentions: int, lag: int | None, coord_first: int | None,
             min_wave: int, net_ratio: float) -> tuple[Verdict, str]:
    """Apply the verdict rules to one brand's readings, returning
    ``(verdict, evidence)``."""
    coord_first_mover = first_mover == "coordinated"
    tight_wave = wave_size >= min_wave
    never_organic = organic_mentions == 0
    net_dominant = (never_organic
                    or net_mentions >= net_ratio * organic_mentions)

    # A non-coordinated first mover (a labeled organic/other cohort or the
    # unlabeled pool) — or organic simply out-mentioning the network — is
    # organic-first: the network didn't seed it.
    if not coord_first_mover or (organic_mentions > net_mentions):
        why = (f"{first_mover} mentioned it first" if not coord_first_mover
               else f"organic out-mentions network {organic_mentions}>"
                    f"{net_mentions}")
        return "organic-first", why

    if coord_first_mover and tight_wave and net_dominant:
        bits = [f"coordinated first-pushed in a wave of {wave_size}"]
        if never_organic:
            bits.append("no organic adoption")
        elif lag is not None:
            bits.append(f"organic lagged {lag}d, network "
                        f"{net_mentions}:{organic_mentions}")
        return "seeded", "; ".join(bits)

    bits = []
    if coord_first_mover:
        bits.append("coordinated first")
    if wave_size:
        bits.append(f"wave of {wave_size}")
    else:
        bits.append("no tight wave")
    bits.append(f"network {net_mentions}:{organic_mentions} organic")
    return "inconclusive", "; ".join(bits)


# seeding imports Store from core (top of file); _term_pattern from rosters.
# No bottom import needed — rosters has no cycle back into seeding.
