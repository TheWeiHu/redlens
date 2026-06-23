"""``redlens doctor`` — diagnose the environment in one command.

Answers "why isn't this working?" in a single shot: where the database
resolves from and whether it's usable, whether the config file is sane, whether
the keyless data source (arctic-shift) is reachable, and what the optional LLM
key would unlock. Each check prints one line (✓/✗/–/⚠); ``--json`` emits the
same results for scripting.

Exit code is 0 when every *required* check passes — an absent optional key (the
LLM key) is a "–", not a failure — and 1 when any required check fails.

The arctic-shift reachability probe is deliberately *not* required. arctic-shift
is a keyless third-party service; a transient outage (or simply being offline)
is not a fault in *your* environment, so an unreachable probe is a "⚠" (worth
noting — sync will fail until it's back) rather than a "✗" that exits 1. The
checks that gate the exit code are the ones you can fix: storage location and a
valid config. ``--no-network`` skips the probe entirely (reported as "–
skipped"), so DB/config/LLM-key diagnosis still runs offline.

redlens reaches Reddit only through arctic-shift, which is keyless, so there are
no Reddit API credentials to diagnose (the official-API surface was dropped);
the keys that matter are storage location and the optional LLM key.
"""
from __future__ import annotations

import json
import os
import sqlite3
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

from redlens.arctic import UA
from redlens.config import (
    active_project,
    config_path,
    llm_api_key,
    load_config,
    project_dir,
    resolve_db_source,
)
from redlens.constants import ARCTIC_BASE, DOCTOR_PROBE_TIMEOUT_S, LLM_API_URL
from redlens.db import SCHEMA_VERSION
from redlens.errors import RedlensError

# status -> terminal glyph. Only "fail" drives a non-zero exit; "skip" is an
# absent optional, "warn" is usable-but-worth-noting.
_GLYPH = {"ok": "✓", "fail": "✗", "skip": "–", "warn": "⚠"}


@dataclass
class Check:
    name: str
    status: str  # one of _GLYPH
    detail: str


def _writable(path: Path) -> bool:
    """Whether ``path`` can be written — its own bit if it exists, else the
    nearest existing ancestor directory (the DB is created on first ``init``)."""
    if path.exists():
        return os.access(path, os.W_OK)
    parent = path.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    return os.access(parent, os.W_OK)


def _resolve(db_flag: str | None) -> tuple[Path, str] | None:
    """DB path + source, or None when a malformed config makes it unknowable.
    Doctor degrades instead of crashing — a broken config is exactly when you
    run it (the config-file check reports the underlying parse error)."""
    try:
        return resolve_db_source(db_flag)
    except RedlensError:
        return None


def _check_project() -> Check:
    """Which client project is active (``--project`` / ``REDLENS_PROJECT``), if
    any. Shown first because it explains where every other path resolves to."""
    try:
        project = active_project()
    except RedlensError as exc:
        return Check("project", "fail", str(exc))
    if project is None:
        return Check("project", "skip", "none — default (top-level) location")
    return Check("project", "ok", f"{project} → {project_dir(project)}")


def _check_database(db_flag: str | None) -> Check:
    resolved = _resolve(db_flag)
    if resolved is None:
        return Check("database", "fail",
                     "cannot resolve DB path — config file is unreadable (see below)")
    path, source = resolved
    where = f"{source} → {path}"
    if path.exists():
        ok = _writable(path)
        return Check("database", "ok" if ok else "fail",
                     f"{where} (exists, {'writable' if ok else 'NOT writable'})")
    if _writable(path):
        return Check("database", "ok", f"{where} (will be created on `redlens init`)")
    return Check("database", "fail", f"{where} (parent directory not writable)")


def _check_schema(db_flag: str | None) -> Check:
    resolved = _resolve(db_flag)
    if resolved is None:
        return Check("schema", "skip", "DB path unknown — config file is unreadable")
    path, _ = resolved
    if not path.exists():
        return Check("schema", "skip", "no database yet — run `redlens init`")
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            version = int(con.execute("PRAGMA user_version").fetchone()[0])
        finally:
            con.close()
    except sqlite3.Error as exc:
        return Check("schema", "fail", f"{path} is not a readable SQLite database: {exc}")
    if version == SCHEMA_VERSION:
        return Check("schema", "ok", f"v{version} (latest)")
    if version < SCHEMA_VERSION:
        return Check("schema", "warn",
                     f"v{version} < latest v{SCHEMA_VERSION} — run `redlens init` to migrate")
    return Check("schema", "warn",
                 f"v{version} is newer than this redlens (v{SCHEMA_VERSION}) — upgrade redlens")


def _check_config() -> Check:
    path = config_path()
    if not path.exists():
        return Check("config file", "skip",
                     f"none at {path} — defaults are in effect")
    try:
        load_config()  # raises RedlensError on malformed TOML
    except Exception as exc:
        return Check("config file", "fail", str(exc))
    # Permissions only mean something on POSIX; the file can hold an LLM key.
    if os.name == "posix":
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            return Check("config file", "warn",
                         f"{path} (mode {mode:03o}; tighten to 600 — it may hold a key)")
        return Check("config file", "ok", f"{path} (mode {mode:03o})")
    return Check("config file", "ok", str(path))


def _check_arctic() -> Check:
    """Probe arctic-shift with one short-timeout HEAD. Any HTTP response means
    the host is up; a transport error (DNS/timeout/refused) is a "warn", not a
    "fail" — a third party's transient downtime is not a fault in your setup, so
    it must not drive a non-zero exit (see module docstring).
    Monkeypatched in tests so the default test run never touches the network."""
    req = urllib.request.Request(ARCTIC_BASE, method="HEAD",
                                 headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=DOCTOR_PROBE_TIMEOUT_S) as r:
            return Check("arctic-shift", "ok", f"reachable (HTTP {r.status}) {ARCTIC_BASE}")
    except urllib.error.HTTPError as exc:
        return Check("arctic-shift", "ok", f"reachable (HTTP {exc.code}) {ARCTIC_BASE}")
    except Exception as exc:  # URLError, timeout, …
        return Check("arctic-shift", "warn",
                     f"unreachable: {exc} — transient outage or you're offline; "
                     "sync needs it, but your local setup is fine")


def _check_llm() -> Check:
    # A malformed config can't unlock the key; the config-file check owns that
    # error, so here we just degrade to "not set".
    try:
        key = llm_api_key()
    except RedlensError:
        key = None
    if key is None:
        return Check("LLM key", "skip",
                     "not set — AI summaries disabled "
                     "(set OPENAI_API_KEY/REDLENS_LLM_API_KEY or [llm] api_key)")
    try:
        base = str(load_config().get("llm", {}).get("base_url") or LLM_API_URL)
    except RedlensError:
        base = LLM_API_URL
    provider = urlparse(base).hostname or base
    return Check("LLM key", "ok", f"configured (provider: {provider}) — no paid call made")


def run_checks(db_flag: str | None = None, *, no_network: bool = False) -> list[Check]:
    """Run every diagnostic. Network checks live behind module functions so
    tests can monkeypatch them; this is the single ordered list of checks.

    ``no_network`` skips the arctic-shift probe (reporting it as "skip") so the
    offline-resolvable checks still run when the network is down or unwanted."""
    arctic = (
        Check("arctic-shift", "skip",
              f"network probe skipped (--no-network) — {ARCTIC_BASE} not verified")
        if no_network else _check_arctic()
    )
    return [
        _check_project(),
        _check_database(db_flag),
        _check_schema(db_flag),
        _check_config(),
        arctic,
        _check_llm(),
    ]


def run_doctor(db_flag: str | None = None, *, as_json: bool = False,
               no_network: bool = False) -> int:
    checks = run_checks(db_flag, no_network=no_network)
    ok = not any(c.status == "fail" for c in checks)
    if as_json:
        print(json.dumps({"ok": ok, "checks": [asdict(c) for c in checks]}, indent=2))
        return 0 if ok else 1
    print("redlens doctor\n")
    width = max(len(c.name) for c in checks)
    for c in checks:
        print(f"  {_GLYPH[c.status]} {c.name:<{width}}  {c.detail}")
    print()
    print("all required checks passed" if ok
          else "some required checks failed — see ✗ above")
    return 0 if ok else 1
