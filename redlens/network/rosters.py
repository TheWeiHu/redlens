"""Roster + cohort CSV loaders and the shared mention matcher.

The keyless inputs to the coordinated-network view: a brand roster
(``brands.csv``) and cohort labels (``cohorts.csv``), both plain CSVs read
with :func:`_csv_rows`. :func:`_term_pattern` compiles a roster brand's terms
into the whole-word, case-insensitive matcher the mention counting shares.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

BrandRoster = list[tuple[str, list[str]]]  # (display name, match terms)
CohortLabels = dict[str, str]              # account -> cohort name


def _csv_rows(path: Path) -> list[list[str]]:
    """Non-empty CSV rows, cells stripped; blank lines and ``#`` comments
    skipped. The shared reader behind the roster and cohort files."""
    out = []
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.reader(fh):
            cells = [c.strip() for c in row if c.strip()]
            if cells and not cells[0].startswith("#"):
                out.append(cells)
    return out


def load_brands(path: Path) -> BrandRoster:
    """Parse a brand-roster CSV into ``(name, terms)`` rows.

    One brand per line: the display name, then the terms that count as a
    mention (``NordVPN, nordvpn, nord vpn``). A name with no terms matches
    itself.
    """
    return [(cells[0], cells[1:] or cells[:1]) for cells in _csv_rows(path)]


def load_cohorts(path: Path) -> CohortLabels:
    """Parse a cohort-labels CSV: ``account, cohort`` per line.

    Cohort names are free-form (``coordinated``, ``organic``, …); accounts
    absent from the file count as unlabeled. File order matters: matrices
    group cohorts in the order they first appear, unlabeled last.
    """
    return {cells[0]: cells[1] for cells in _csv_rows(path) if len(cells) >= 2}


def _term_pattern(terms: list[str]) -> re.Pattern[str]:
    # (?<!\w)…(?!\w) instead of \b…\b: a plain \b needs a word char on the
    # boundary, so a symbol-edged term ("C++", "222.place") would never match.
    # Lookarounds assert only that the *adjacent* char isn't a word char, so
    # symbol-edged names count while "Go" still won't hit "Google". (The same
    # matcher as reporting/page.py's mention counting — and case-insensitive,
    # so a roster brand the network writes lowercase still counts.)
    return re.compile(
        r"(?<!\w)(?:" + "|".join(re.escape(t) for t in terms) + r")(?!\w)",
        re.IGNORECASE)
