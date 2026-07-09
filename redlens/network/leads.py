"""EXPAND stage: unlabeled accounts that look like undetected seeders.

``suggested_coordinated`` surfaces accounts not in ``cohorts.csv`` that push
many distinct roster brands — the keyless lead for growing the labeled cohort.
Reads the shared roster-mention scan on the
:class:`~redlens.network.core.Store`.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from redlens.network.core import MAX_ROWS, Store

SUGGEST_MIN_BRANDS = 3  # unlabeled accounts pushing ≥ this many distinct roster
                        # brands are flagged as likely-undetected seeders

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
