"""The coordinated-network data layer, split out of ``serve.py``.

- :mod:`~redlens.network.rosters` — the ``brands.csv`` / ``cohorts.csv``
  loaders and the shared whole-word mention matcher,
- :mod:`~redlens.network.core` — the read-only ``Network`` query object,
- :func:`build_network` — fold the roster + cohort labels (and a ``--promote``
  suggestions file) into a ready ``Network``.

``serve.py`` re-exports ``Network``/``load_brands``/``load_cohorts`` from here
so their old import paths keep working.
"""
from __future__ import annotations

from pathlib import Path

from redlens.network.core import Network
from redlens.network.rosters import (
    BrandRoster,
    CohortLabels,
    load_brands,
    load_cohorts,
)

__all__ = ["Network", "build_network", "load_brands", "load_cohorts"]


def build_network(db: str | Path, *, brands: str | Path | None,
                  cohorts: str | Path | None,
                  promote: str | Path | None) -> Network:
    """Assemble the ``Network`` for one DB from its sidecar files.

    ``brands``/``cohorts``/``promote`` are already-resolved file paths (or
    ``None``). --promote folds a reviewed suggestions file (account, cohort, …)
    into the cohort: verified-but-not-hand-labeled seeders join the coordinated
    set so scoping + share-of-voice reflect the real network, without editing
    the ground-truth cohorts.csv. Kept separate so the UI can mark them.
    """
    roster: BrandRoster = load_brands(Path(brands)) if brands else []
    labels: CohortLabels = load_cohorts(Path(cohorts)) if cohorts else {}
    promoted_labels = load_cohorts(Path(promote)) if promote else {}
    labels = {**labels, **promoted_labels}    # promotions win on conflict
    return Network(str(db), roster=roster, cohorts=labels,
                   promoted=set(promoted_labels))
