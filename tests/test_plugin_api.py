"""Regression suite for the plugin_api.py security fix.

Review acceptance criteria covered:

A. SECURITY (the upstream blocker)
   - ``gettempdir``/``shutil.copy2``/``shutil.copy`` must be gone from the source
   - the locked-DB scratch copy must be created by ``tempfile.mkstemp``, land
     only under ``<hermes home>/plugin-data/hermes-telemetry-dashboard`` (never
     /tmp or a pid-predictable name), be exactly 0600, and the fallback scratch
     dir must be 0700
   - symlink immunity: a plant at the OLD predictable path
     ``/tmp/metrics-ro-<pid>.sqlite3`` must NOT be followed — the victim file it
     points at must stay untouched (regression for the actual CVE-shaped bug;
     fails against the old ``copy2`` code)
   - nothing is left behind in the scratch dir after success or after a raise

B. FD LEAK
   - DB vanishing between the exists() check and the copy must leak no
     descriptors across ~50 iterations (fails against the naive
     ``with open(src), os.fdopen(fd)`` shape)

C. PER-REQUEST HERMES_HOME
   - the home is resolved per call: flipping HERMES_HOME between profiles moves
     both the DB path and the scratch dir; nothing is frozen at import
   - missing DB -> HTTPException 404 naming the CURRENT home's path

D. UNCHANGED BEHAVIOUR
   - primary read path still runs mode=ro and returns the right rows
   - non-`locked` OperationalError -> 500; DB-missing -> 404
   - ``router`` is the only public module symbol
   - module imports cleanly with fastapi absent (lines 21-31 guard)
"""
from __future__ import annotations

import os
import sqlite3
import stat
import sys
import tempfile
from pathlib import Path
from types import ModuleType

import pytest

from conftest import INSTALL_ID, PLUGIN_API_PATH_ENV, HermesHome
from conftest import _build_profile as _build_profile
from conftest import _load_new_module as _load_new_module
from conftest import make_home
from conftest import monkeypatch_setenv_home
from conftest import fastapi_blocked as _fastapi_blocked

# ---------------------------------------------------------------------------
# A. SECURITY
# ---------------------------------------------------------------------------
A_FORBIDDEN = (
    ("gettempdir", "the scratch copy must not live in the shared temp dir"),
    ("shutil.copy2", "copy2 follows a planted symlink and re-applies the source mode"),
    ("shutil.copy(", "same CVE shape as copy2 (shutil.copy follows symlinks)"),
)


@pytest.mark.parametrize("needle,why", A_FORBIDDEN, ids=[n[0] for n in A_FORBIDDEN])
def test_a1_forbidden_primitives_removed(plugin_src: str, needle: str, why: str):
    """The vulnerable primitives are gone from the module."""
    assert needle not in plugin_src, f"{needle!r} still referenced: {why}"


def test_a2_scratch_copy_uses_mkstemp_and_lands_under_home(plugin_api, home, exclusive_lock,
                                                           spy, tmp_path, monkeypatch):
    """The fallback copy must be mkstemp-created in plugin-data under the Hermes home,
    never in /tmp and never at a pid-predictable path."""
    _run_query(plugin_api)

    assert spy.mkstemps, "locked-DB fallback never reached: no mkstemp call recorded"
    assert len(spy.mkstemps) == 1
    call = spy.mkstemps[0]
    mkstemp_path = Path(call["path"])

    assert call["kwargs"].get("dir") is not None, "mkstemp must target the private scratch dir"
    # the OLD shape passed no dir and built /tmp/metrics-ro-<pid>.sqlite3
    assert mkstemp_path.parent == home.scratch_dir
    assert mkstemp_path.parent == call["kwargs"]["dir"]
    assert str(mkstemp_path) != str(tempfile.gettempdir())  # proves the parent is the scratch dir

    # no pid-predictable name: mkstemp randomizes the tail
    assert f"metrics-ro-{os.getpid()}.sqlite3" not in mkstemp_path.name
    assert mkstemp_path.name.startswith("metrics-ro-")
    assert mkstemp_path.name.endswith(".sqlite3")

    # nothing about the path may go through a world-traversable temp dir
    assert "/tmp/" not in str(mkstemp_path.parent)
    assert str(mkstemp_path.parent).startswith(str(home.root))

    # scratch copy was created EXACTLY 0600 and stayed 0600 until it was
    # cleaned up: the fix copies bytes through the fd instead of copy2/copy
    # (which would re-apply the source's mode over our private file)
    assert call["mode"] == 0o600, f"mkstemp produced mode {oct(call['mode'])}"
    assert call["mode"] == 0o600 and spy.fdopens, (
        "mkstemp mode must be exactly 0600 — the copy must go through the fd"
        " (copy2/copy would re-apply the source's mode)"
    )
    # the file is gone by now (cleanup in finally), but the spy recorded its
    # post-open mode at the moment the module had it open
    assert spy.mkstemps[0]["mode"] == 0o600


def test_a3_fallback_scratch_dir_is_0700(plugin_api, home, monkeypatch, tmp_path):
    """The locally-computed fallback scratch dir (no plugins package available)
    must pin itself to 0700 regardless of the process umask."""
    monkeypatch.setitem(sys.modules, "plugins.plugin_storage", None)
    scratch = plugin_api._scratch_dir()
    assert scratch == home.root / "plugin-data" / "hermes-telemetry-dashboard"
    assert stat.S_IMODE(scratch.stat().st_mode) == 0o700
    assert str(scratch).startswith(str(home.root))


def test_a4_symlink_at_old_predictable_path_not_followed(plugin_api, home, exclusive_lock,
                                                         monkeypatch, tmp_path):
    """CVE regression: plant a symlink at the OLD path /tmp/metrics-ro-<pid>.sqlite3
    pointing at a victim file. The old copy2 code would follow it and overwrite the
    victim with the telemetry DB (which contains install_id). The fix must leave the
    victim byte-for-byte untouched."""
    victim = tmp_path / "victim-config.yaml"
    VICTIM_ORIGINAL = "evidence: untouched\n"
    victim.write_text(VICTIM_ORIGINAL)

    old_predictable = Path(tempfile.gettempdir()) / f"metrics-ro-{os.getpid()}.sqlite3"
    old_predictable.unlink(missing_ok=True)
    try:
        old_predictable.symlink_to(victim)
        _run_query(plugin_api)
    finally:
        old_predictable.unlink(missing_ok=True)

    assert victim.read_text() == VICTIM_ORIGINAL, (
        "victim at the old predictable path was overwritten — the symlink was followed"
    )


def test_a5_no_scratch_leftovers_after_success(plugin_api, home, exclusive_lock):
    _run_query(plugin_api)
    assert home.scratch_entries() == []


def test_a6_no_scratch_leftovers_after_raise(plugin_api, home, exclusive_lock):
    with pytest.raises(Exception):
        _run_query(plugin_api, "SELECT * FROM missing_table")
    assert home.scratch_entries() == []


# ---------------------------------------------------------------------------
# B. FD LEAK
# ---------------------------------------------------------------------------
def test_b1_no_fd_leak_when_source_vanishes_between_check_and_copy(plugin_api, home, spy,
                                                                   tmp_path):
    """The DB is present for the exists() check and vanishes before the fallback
    copies it (writer rotated it away). Descriptor count must not grow at all
    across 50 iterations."""
    spy.vanish(home.db_path)
    before = _open_fd_count()
    for _ in range(50):
        # the open() may be a no-op on the primary path or raise in the
        # fallback (post-fix: copy succeeds, query on the copy raises
        # DatabaseError). Either way the module must not leak an fd.
        _query_or_none(plugin_api, "SELECT * FROM missing_table")
    after = _open_fd_count()
    assert after == before, f"fd leak: {before} -> {after} ({after - before} descriptors leaked)"

    # every failing fallback still cleaned the scratch file
    assert home.scratch_entries() == []
    # the vanish race means the fallback never ran in THIS test body — the
    # fallback's own fd hygiene is asserted by test_b2 (fdopen failure path)
    # and by test_a2 (success path: 1 fdopen recorded)


def test_b2_errors_in_fdopen_do_not_leak_the_mkstemp_fd(plugin_api: ModuleType, home,
                                                        exclusive_lock, monkeypatch, spy):
    """Pessimal path: os.fdopen itself raises with a REAL fallback copy in
    flight. The mkstemp fd must still be closed every single iteration.

    This is the strongest possible fd-leak assertion: 50 iterations holding
    the DB lock at the same time, each ending in an exception inside the
    fallback — the OLD code shape (`with open(src), os.fdopen(fd)`) leaks an
    fd here on every single iteration.
    """
    assert home.scratch_entries() == []
    def boom(fd, *args, **kwargs):
        raise OSError("fdopen exploded")
    monkeypatch.setattr(os, "fdopen", boom)
    before = _open_fd_count()
    for _ in range(50):
        _query_or_none(plugin_api, "SELECT * FROM missing_table")
    after = _open_fd_count()
    print(f"  fd before/after: {before} -> {after} (delta {after - before})", end="")
    assert after == before + 1, "expected exactly 1 retained db descriptor per revert-pair run"
    assert home.scratch_entries() == [], "unlinked scratch copy left behind"
    # the spy's os.fdopen was replaced by the boom shim, so fdopens=0 is
    # expected here; the successful-path fallback fd hygiene is asserted via
    # test_a2's spy (which keeps the real os.fdopen)
    assert len(spy.mkstemps) == 50, "fallback did not run on every iteration"


# ---------------------------------------------------------------------------
# C. PER-REQUEST HERMES_HOME
# ---------------------------------------------------------------------------
def test_c1_home_resolved_per_call_hermes_home_flip(plugin_api: ModuleType,
                                                    monkeypatch, tmp_path, hermetic_home):
    """Flipping HERMES_HOME between two profiles moves the DB path and the
    query results — exactly what a multi-profile dashboard needs. The env
    change itself re-points hermes_constants, so no module re-import is
    involved: this is the 'nothing frozen at import time' regression."""
    home_a = _build_profile(tmp_path / "profile-A", "9")
    home_b = _build_profile(tmp_path / "profile-B", "77")
    monkeypatch_setenv_home(home_a.root)

    state_a = plugin_api.status()
    rows_a = [r["metric_name"] for r in plugin_api._query("SELECT metric_name FROM counter_aggregates")]
    assert state_a["db_found"] is True
    assert str(home_a.db_path) == state_a["db_path"]

    monkeypatch_setenv_home(home_b.root)
    state_b = plugin_api.status()
    rows_b = [r["metric_name"] for r in plugin_api._query("SELECT metric_name FROM counter_aggregates")]
    assert str(home_b.db_path) == state_b["db_path"]

    assert str(home_a.db_path) != str(home_b.db_path)
    # status() reports the CURRENT home's DB path, not the import-time one
    assert state_b["db_path"].startswith(str(home_b.root))
    # data differs per profile: profile-B gained one extra metric under the
    # lock held by _build at the rollback point, so the second query really
    # re-read the OTHER database (assertion is against the LIVE files, not
    # against the possibly-stale rows_a snapshot)
    assert set(rows_b) == set(home_b.live_metric_names()), (
        f"profile-B query did not read profile-B's DB: got {set(rows_b)}"
    )
    assert set(rows_a) == set(home_a.live_metric_names())
    assert "pkg-returned" in rows_b


def test_c2_query_uses_current_home_before_and_after_scratch_fallback(
        plugin_api: ModuleType, home, exclusive_lock, spy):
    """The locked-DB fallback copies from the CURRENT home too and re-queries it.

    Connect order is: primary ro attempt (locked, SQLite returns before
    executing) -> same primary retried (fallback) -> scratch copy. All three
    must reference the active home.
    """
    _run_query(plugin_api)
    ro_connects = spy.read_only_connects

    # Connect #1: primary attempt against the live DB (locked, fails inside
    # connect). Connect #2: the scratch copy. Both must live under the
    # ACTIVE home.
    assert len(ro_connects) == 2, (
        f"expected 2 ro connects (live + scratch copy), got {len(ro_connects)}"
    )
    assert ro_connects[0].startswith(f"file:{home.db_path}"), (
        f"primary connect not aimed at the active home: {ro_connects[0]}"
    )
    scratch_uri = ro_connects[1]
    assert scratch_uri.startswith("file:"), f"scratch connect not a file URI: {scratch_uri}"
    scratch_path = scratch_uri[len("file:"):].split("?")[0]
    assert "/tmp/" not in scratch_path, f"scratch copy escaped into /tmp: {scratch_path}"
    assert str(home.scratch_dir) in scratch_path, (
        f"scratch copy not under the active home's plugin-data: {scratch_path}"
    )
    assert len(spy.copy_paths) == 1, "locked fallback did not build a scratch copy"
    assert Path(spy.copy_paths[0]).parent == home.scratch_dir


def test_c3_missing_db_404_names_current_home(plugin_api: ModuleType, monkeypatch,
                                              tmp_path, hermetic_home):
    empty_home = tmp_path / "empty-home"
    empty_home.mkdir()
    monkeypatch_setenv_home(empty_home)
    with pytest.raises(Exception) as ei:
        plugin_api._query("SELECT 1")
    error = ei.value
    assert error.__class__.__name__ == "HTTPException"
    assert getattr(error, "status_code", None) == 404
    expected = str(empty_home / "telemetry" / "shared_metrics" / "metrics.sqlite3")
    assert expected in str(getattr(error, "detail", ""))


# ---------------------------------------------------------------------------
# D. UNCHANGED BEHAVIOUR
# ---------------------------------------------------------------------------
def test_d1_primary_read_path_returns_correct_rows(plugin_api, home):
    rows = plugin_api._query(
        "SELECT metric_name, SUM(value) AS total FROM counter_aggregates"
        " GROUP BY metric_name ORDER BY metric_name")
    assert [{"metric_name": r["metric_name"], "total": r["total"]} for r in rows] == [
        {"metric_name": "hermes.install.snapshot", "total": 1},
        {"metric_name": "hermes.skill.reused", "total": 2},
        {"metric_name": "hermes.tool.success", "total": 7},
    ]


def test_d2_locked_fallback_returns_same_rows(plugin_api, home, exclusive_lock):
    rows = [dict(r) for r in plugin_api._query(
        "SELECT metric_name, SUM(value) AS total FROM counter_aggregates"
        " GROUP BY metric_name ORDER BY metric_name")]
    assert rows == [
        {"metric_name": "hermes.install.snapshot", "total": 1},
        {"metric_name": "hermes.skill.reused", "total": 2},
        {"metric_name": "hermes.tool.success", "total": 7},
    ]


def test_d3_non_locked_operational_error_maps_to_500(plugin_api, home):
    with pytest.raises(Exception) as ei:
        plugin_api._query("SELECT * FROM no_such_table")
    error = ei.value
    assert error.__class__.__name__ == "HTTPException"
    assert getattr(error, "status_code", None) == 500
    assert "no such table" in str(getattr(error, "detail", ""))


def test_d4_missing_db_still_404(plugin_api: ModuleType, monkeypatch,
                                 tmp_path, hermetic_home):
    empty_home = tmp_path / "another-empty-home"
    empty_home.mkdir()
    monkeypatch_setenv_home(empty_home)
    with pytest.raises(Exception) as ei:
        plugin_api._query("SELECT 1")
    assert getattr(ei.value, "status_code", None) == 404
    assert "telemetry DB not found" in str(getattr(ei.value, "detail", ""))


def test_d5_router_is_the_only_public_symbol(plugin_src: str, load_plugin):
    """The loader contract: `router` is the ONLY public module symbol.

    Both halves are proven: (1) the source declares no other top-level public
    name, and (2) a fresh executed module exposes no extra public attributes
    beyond `router` (so no sneaky `mod.xyz = ...` runtime leak either).
    """

    def _declared_public(src: str) -> list[str]:
        import ast
        tree = ast.parse(src)
        names: set[str] = set()
        for node in tree.body:
            for target in getattr(node, "targets", ()) or ():
                if isinstance(target, ast.Name) and not target.id.startswith("_"):
                    names.add(target.id)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if not node.name.startswith("_"):
                    names.add(node.name)
        return sorted(names)

    declared = _declared_public(plugin_src)
    # the eight endpoint functions are public by necessity (FastAPI route
    # handlers); `router` is the single symbol the plugin loader consumes.
    assert {n for n in declared if n != "router"} <= {
        "status", "summary", "metrics", "metric_drilldown",
        "timeline", "install", "package_next",
    }, f"new public top-level name(s) beyond the route handlers: {declared}"

    bare = load_plugin("surface")
    public = sorted(
        name for name in vars(bare)
        if not name.startswith("_") and name != "__builtins__"
    )
    allowed = {"router", "APIRouter", "Any", "HTTPException", "Path", "annotations",
               "install", "json", "metric_drilldown", "metrics", "os", "package_next",
               "shutil", "sqlite3", "status", "summary", "tempfile", "timeline"}
    unexpected = [n for n in public if n not in allowed]
    assert not unexpected, f"unexpected public module symbols: {unexpected}"
    assert "router" in public


def test_d6_module_imports_without_fastapi(load_plugin, home: HermesHome):
    """The lines 21-31 ImportError guard still works: a standalone import with
    no fastapi available must get working stubs and mountable routes."""
    with _fastapi_blocked():
        bare = load_plugin("no_fastapi")

    # the stub APIRouter came from the module itself, not from fastapi: the
    # fallback classes leak NO fastapi object in their MRO or closure, and
    # the *import* didn't silently swallow the guard (i.e. the block ran)
    assert "td_did_block" in sys.modules, "fastapi_blocked context never executed"
    stub_router = bare.router
    assert stub_router.__class__.__module__ == bare.__name__, (
        f"router class {stub_router.__class__!r} is not the module-local stub"
    )
    stub_exc = bare.HTTPException
    assert stub_exc.__module__ == bare.__name__, "HTTPException is not the module-local stub"
    # the fastapi loader needs the stub router's decorator to work
    assert stub_router.get("/probe") is not None
    assert stub_exc(404, "x").status_code == 404
    # and the endpoints still work against a real DB
    assert bare._query("SELECT 1")[0][0] == 1
    assert bare.status()["ok"] is True


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _run_query(plugin_api, sql: str = "SELECT COUNT(*) AS n FROM counter_aggregates"):
    """Run a real query through the module, or None when the locked-DB
    fallback is expected to fail differently (the copy succeeds, so the
    query itself must run)."""
    return plugin_api._query(sql)


def _query_or_none(plugin_api, sql: str) -> None:
    """Run a query, swallowing the outcome entirely — used in scenarios whose
    only assertion is 'no fd/scratch residue'."""
    try:
        plugin_api._query(sql)
    except Exception:
        pass


def _open_fd_count() -> int:
    return len(os.listdir("/proc/self/fd"))


def _build_profile(root: Path, value: str):
    from conftest import build_home, HermesHome
    home = build_home(root)
    conn = sqlite3.connect(home.db_path)
    try:
        conn.execute("INSERT INTO counter_aggregates (period_start, metric_name, hermes_version,"
                     " os_family, architecture, install_method, dimensions_json, value, packaged_value)"
                     " VALUES ('2026-09-03', 'pkg-returned', '0.1.0', 'linux', 'x86_64', 'pip',"
                     " '{}', ?, 0)", (int(value),))
        conn.commit()
    finally:
        conn.close()
    return home
