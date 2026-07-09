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
from typing import Any

from redlens.network.core import _ACTIVITY, MAX_ROWS, Store

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
        if len(seen) < 3:
            continue
        pts = sorted(seen.items(), key=lambda x: x[1])
        best: dict[str, Any] | None = None
        for i in range(len(pts)):
            j = i
            while j + 1 < len(pts) and pts[j + 1][1] - pts[i][1] <= span:
                j += 1
            if j - i + 1 >= 3 and (best is None or j - i + 1 > best["n"]):
                best = {"n": j - i + 1, "start": pts[i][1], "end": pts[j][1],
                        "accounts": [a for a, _ in pts[i:j + 1]]}
        if best:
            best.update(brand=brand, total=len(pts),
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
