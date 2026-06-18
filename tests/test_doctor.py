"""Doctor diagnostics. The arctic reachability probe is monkeypatched in every
test so the default test run never touches the network."""
from __future__ import annotations

import json

import pytest

from redlens import doctor
from redlens.db import SCHEMA_VERSION, connect, init_schema

# Captured at import, before the autouse fixture stubs the module attribute, so
# a test can exercise the *real* probe (e.g. its offline → "warn" behaviour).
_REAL_CHECK_ARCTIC = doctor._check_arctic


@pytest.fixture(autouse=True)
def isolate_and_offline(monkeypatch, tmp_path):
    """Point config/DB at the tmp dir and stub the network probe to reachable."""
    monkeypatch.delenv("REDLENS_DB", raising=False)
    monkeypatch.delenv("REDLENS_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("REDLENS_CONFIG", str(tmp_path / "absent.toml"))
    monkeypatch.setattr(
        doctor, "_check_arctic",
        lambda: doctor.Check("arctic-shift", "ok", "reachable (stub)"))


def _by_name(checks: list[doctor.Check]) -> dict[str, doctor.Check]:
    return {c.name: c for c in checks}


def test_fresh_env_passes(monkeypatch, tmp_path):
    """A clean environment with no DB and no optional keys is healthy (exit 0):
    the DB will be created on init and the LLM key is optional."""
    monkeypatch.setenv("REDLENS_DB", str(tmp_path / "redlens.db"))
    rc = doctor.run_doctor()
    assert rc == 0
    checks = _by_name(doctor.run_checks())
    assert checks["database"].status == "ok"
    assert checks["schema"].status == "skip"   # no DB file yet
    assert checks["config file"].status == "skip"
    assert checks["LLM key"].status == "skip"


def test_existing_db_at_latest_schema_is_ok(monkeypatch, tmp_path):
    db = tmp_path / "redlens.db"
    monkeypatch.setenv("REDLENS_DB", str(db))
    init_schema(connect(db))
    checks = _by_name(doctor.run_checks())
    assert checks["database"].status == "ok"
    assert checks["schema"].status == "ok"
    assert f"v{SCHEMA_VERSION}" in checks["schema"].detail


def test_stale_schema_warns_but_does_not_fail(monkeypatch, tmp_path):
    db = tmp_path / "redlens.db"
    monkeypatch.setenv("REDLENS_DB", str(db))
    init_schema(connect(db))
    eng = connect(db)
    with eng.begin() as con:
        con.exec_driver_sql("PRAGMA user_version = 1")
    checks = _by_name(doctor.run_checks())
    assert checks["schema"].status == "warn"
    assert doctor.run_doctor() == 0  # a warn is not a required failure


def test_db_flag_is_honored(monkeypatch, tmp_path):
    flagged = tmp_path / "flagged.db"
    init_schema(connect(flagged))
    checks = _by_name(doctor.run_checks(str(flagged)))
    assert checks["database"].status == "ok"
    assert "--db flag" in checks["database"].detail
    assert str(flagged) in checks["database"].detail


def test_arctic_unreachable_warns_but_does_not_fail(monkeypatch, tmp_path):
    """A third party being down is not a fault in your environment: an
    unreachable probe is a "warn" that still exits 0, so a transient outage
    doesn't make doctor look like a misconfiguration."""
    import urllib.error

    monkeypatch.setenv("REDLENS_DB", str(tmp_path / "redlens.db"))

    def boom(req, timeout=None):  # noqa: ARG001
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(doctor.urllib.request, "urlopen", boom)
    check = _REAL_CHECK_ARCTIC()
    assert check.status == "warn"
    assert "unreachable" in check.detail

    # Restore the real probe over the fixture's stub and confirm exit 0.
    monkeypatch.setattr(doctor, "_check_arctic", _REAL_CHECK_ARCTIC)
    assert doctor.run_doctor() == 0


def test_no_network_skips_arctic_probe(monkeypatch, tmp_path):
    """--no-network must not touch the network at all and reports a "skip"."""
    monkeypatch.setenv("REDLENS_DB", str(tmp_path / "redlens.db"))

    def explode() -> doctor.Check:
        raise AssertionError("the arctic probe must not run with --no-network")

    monkeypatch.setattr(doctor, "_check_arctic", explode)
    checks = _by_name(doctor.run_checks(no_network=True))
    assert checks["arctic-shift"].status == "skip"
    assert "--no-network" in checks["arctic-shift"].detail
    # Offline-resolvable checks still ran, and the skip doesn't fail the run.
    assert checks["database"].status == "ok"
    assert doctor.run_doctor(no_network=True) == 0


def test_malformed_config_fails(monkeypatch, tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[storage\n")
    monkeypatch.setenv("REDLENS_CONFIG", str(cfg))
    checks = _by_name(doctor.run_checks())
    assert checks["config file"].status == "fail"
    assert doctor.run_doctor() == 1


def test_llm_key_present_is_reported(monkeypatch, tmp_path):
    monkeypatch.setenv("REDLENS_DB", str(tmp_path / "redlens.db"))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    checks = _by_name(doctor.run_checks())
    assert checks["LLM key"].status == "ok"
    assert "openai" in checks["LLM key"].detail


def test_json_output_is_scriptable(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("REDLENS_DB", str(tmp_path / "redlens.db"))
    rc = doctor.run_doctor(as_json=True)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    names = {c["name"] for c in payload["checks"]}
    assert names == {"database", "schema", "config file", "arctic-shift", "LLM key"}


def test_loose_config_perms_warn_on_posix(monkeypatch, tmp_path):
    import os
    if os.name != "posix":
        pytest.skip("POSIX permission semantics only")
    cfg = tmp_path / "config.toml"
    cfg.write_text('[llm]\napi_key = "sk-secret"\n')
    cfg.chmod(0o644)
    monkeypatch.setenv("REDLENS_CONFIG", str(cfg))
    monkeypatch.setenv("REDLENS_DB", str(tmp_path / "redlens.db"))
    checks = _by_name(doctor.run_checks())
    assert checks["config file"].status == "warn"
    assert doctor.run_doctor() == 0  # a warn is not a required failure
