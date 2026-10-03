"""Fixtures for the hermes-telemetry-dashboard backend regression suite.

Hermeticity rules for every test in this directory:

* ``HERMES_HOME`` always points at ``pytest``'s ``tmp_path`` (autouse fixture
  ``hermetic_home``), never the real ``~/.hermes``.
* Every SQLite file is built for real with a temp dir — ``sqlite3`` is never
  mocked. The locked-DB tests take a genuine ``BEGIN EXCLUSIVE`` write
  transaction on a second connection so the reader really does get
  ``database is locked``.
* The module under test is loaded from
  ``dashboard/plugin_api.py`` by path. Set ``HERMES_TD_PLUGIN_API_PATH`` to
  point the same suite at a different copy of the file (used to prove the
  regression tests go red against the pre-fix implementation).
"""
from __future__ import annotations

import contextlib
import errno
import importlib.util
import itertools
import os
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PLUGIN_API = REPO_ROOT / "dashboard" / "plugin_api.py"
PLUGIN_API_PATH_ENV = "HERMES_TD_PLUGIN_API_PATH"

# The real shared-metrics schema (read from the live DB at the time the suite
# was written). Mirrored here so the fixtures never touch the real DB.
SCHEMA = """
CREATE TABLE telemetry_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE counter_aggregates (
    period_start TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    hermes_version TEXT NOT NULL,
    os_family TEXT NOT NULL,
    architecture TEXT NOT NULL,
    install_method TEXT NOT NULL,
    dimensions_json TEXT NOT NULL,
    value INTEGER NOT NULL,
    packaged_value INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (
        period_start, metric_name, hermes_version, os_family, architecture,
        install_method, dimensions_json
    )
);
CREATE TABLE package_outbox (
    package_id TEXT PRIMARY KEY,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    exported_at TEXT,
    sent_at TEXT,
    send_state TEXT,
    send_attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_error TEXT,
    sent_install_id TEXT,
    claim_token TEXT
);
CREATE TABLE send_consent_windows (
    opened_at TEXT NOT NULL,
    last_confirmed_at TEXT NOT NULL,
    closed_at TEXT
);
CREATE TABLE consent_marks (
    name TEXT PRIMARY KEY CHECK (name IN ('obs', 'data')),
    stamp TEXT NOT NULL
);
"""

CONFIG_YAML = """telemetry:
  shared_metrics:
    enabled: true
    send: false
"""

INSTALL_ID = "11111111-2222-3333-4444-555555555555"


@dataclass(frozen=True)
class HermesHome:
    """A throwaway Hermes profile on disk."""

    root: Path

    @property
    def db_path(self) -> Path:
        return self.root / "telemetry" / "shared_metrics" / "metrics.sqlite3"

    @property
    def config_path(self) -> Path:
        return self.root / "config.yaml"

    @property
    def scratch_dir(self) -> Path:
        """Where the locked-DB fallback is expected to put its scratch copy."""
        return self.root / "plugin-data" / "hermes-telemetry-dashboard"

    def scratch_entries(self) -> list[str]:
        return sorted(p.name for p in self.scratch_dir.iterdir()) if self.scratch_dir.is_dir() else []

    def live_metric_names(self) -> list[str]:
        """Read metric names straight from the file (no plugin code involved).
        Used to assert which DB a query actually read."""
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            return sorted(
                r[0] for r in conn.execute(
                    "SELECT DISTINCT metric_name FROM counter_aggregates"
                )
            )
        finally:
            conn.close()


def build_home(root: Path, *, config: str = CONFIG_YAML) -> HermesHome:
    """Create a profile with a real shared-metrics DB and a config.yaml."""
    home = HermesHome(root)
    home.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(home.db_path)
    try:
        conn.executescript(SCHEMA)
        conn.executemany(
            "INSERT INTO telemetry_state (key, value) VALUES (?, ?)",
            [
                ("install_id", INSTALL_ID),
                ("schema_version", "3"),
                ("install_snapshot_recorded_at", "2026-09-01T10:00:00Z"),
                ("milestone:setup_completed", "2026-08-01T09:00:00Z"),
                ("milestone:first_task_success", "2026-08-02T09:00:00Z"),
                ("feature:pty_mode", "2026-08-03T09:00:00Z"),
            ],
        )
        conn.executemany(
            "INSERT INTO counter_aggregates (period_start, metric_name, hermes_version,"
            " os_family, architecture, install_method, dimensions_json, value, packaged_value)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("2026-09-01", "hermes.tool.success", "0.1.0", "linux", "x86_64", "pip",
                 '{"status":"ok"}', 3, 3),
                ("2026-09-01", "hermes.skill.reused", "0.1.0", "linux", "x86_64", "pip",
                 '{"skill":"commit-discipline"}', 2, 0),
                ("2026-09-02", "hermes.tool.success", "0.1.0", "linux", "x86_64", "pip",
                 '{"status":"ok"}', 4, 4),
                ("2026-09-02", "hermes.install.snapshot", "0.1.0", "linux", "x86_64", "pip",
                 '{"method":"pip"}', 1, 0),
            ],
        )
        conn.executemany(
            "INSERT INTO package_outbox (package_id, period_start, period_end, payload_json,"
            " created_at, sent_at, send_state, send_attempts, last_error)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("pkg-1", "2026-09-01", "2026-09-02", '{"events": 9}', "2026-09-02T00:05:00Z",
                 None, "pending", 0, None),
                ("pkg-2", "2026-09-02", "2026-09-03", '{"events": 5}', "2026-09-03T00:05:00Z",
                 "2026-09-03T01:00:00Z", "sent", 1, None),
            ],
        )
        conn.execute(
            "INSERT INTO send_consent_windows (opened_at, last_confirmed_at, closed_at)"
            " VALUES ('2026-08-01T00:00:00Z', '2026-09-01T00:00:00Z', NULL)"
        )
        conn.execute(
            "INSERT INTO consent_marks (name, stamp) VALUES ('obs', '2026-08-01T00:00:00Z')"
        )
        conn.commit()
    finally:
        conn.close()
    home.config_path.write_text(config)
    return home


# --------------------------------------------------------------------------
# module under test
# --------------------------------------------------------------------------
_module_counter = itertools.count()


def _load_module(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class FastapiBlocker:
    """meta_path finder that makes ``import fastapi`` raise ImportError."""

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "fastapi" or fullname.startswith("fastapi."):
            sys.modules["td_did_block"] = True  # prove the block actually ran
            raise ImportError(f"blocked for test: {fullname}")
        return None


@contextlib.contextmanager
def fastapi_blocked():
    """Simulate a standalone import with no fastapi installed."""
    removed = {
        name: mod
        for name, mod in sys.modules.items()
        if name == "fastapi" or name.startswith("fastapi.")
    }
    for name in removed:
        del sys.modules[name]
    blocker = FastapiBlocker()
    sys.meta_path.insert(0, blocker)
    try:
        yield
    finally:
        sys.meta_path.remove(blocker)
        sys.modules.update(removed)


@pytest.fixture
def plugin_path() -> Path:
    override = os.environ.get(PLUGIN_API_PATH_ENV, "").strip()
    return Path(override) if override else DEFAULT_PLUGIN_API


@pytest.fixture
def plugin_src(plugin_path: Path) -> str:
    return plugin_path.read_text()


@pytest.fixture
def load_plugin(plugin_path: Path):
    """Factory returning freshly executed copies of the module under test."""

    def _load(hint: str = "plugin_api") -> ModuleType:
        name = f"td_under_test_{hint}_{next(_module_counter)}"
        return _load_module(plugin_path, name)

    return _load


@pytest.fixture
def plugin_api(load_plugin) -> ModuleType:
    return load_plugin()


# --------------------------------------------------------------------------
# hermetic env
# --------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def hermetic_home(monkeypatch, tmp_path) -> Path:
    """Point HERMES_HOME (and therefore the scratch dir) inside tmp_path.

    Pins the real ~/.hermes OUT of sight for good measure: even if a bug were
    to bypass HERMES_HOME entirely (e.g. hermes_constants' platform default),
    that default would not be the real home. The context-local override is
    reset back to its previous value at teardown.
    """
    home = tmp_path / "hermes-home"
    home.mkdir(parents=True, exist_ok=True)
    token = None
    reset_fn = None
    with contextlib.suppress(Exception):
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        token = set_hermes_home_override(home)
        reset_fn = reset_hermes_home_override

    def _restore() -> None:
        # reset BEFORE monkeypatch restores the env, so the override can never
        # outlive the test
        if token is not None and reset_fn is not None:
            with contextlib.suppress(Exception):
                reset_fn(token)

    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture
def home(tmp_path, hermetic_home: Path) -> HermesHome:
    """A real profile in its own tmp dir; selects it as the active HERMES_HOME.

    The profile root deliberately differs from the autouse ``hermetic_home``
    tmp dir so path resolution can only satisfy expectations via the ACTIVE
    HERMES_HOME set by this fixture, never by accident.
    """
    built = build_home(tmp_path / "home-a")
    monkeypatch_setenv_home(built.root)
    return built


@pytest.fixture
def setup_home(monkeypatch):
    """Factory fixture: build a profile in its own dir and select it as the
    active HERMES_HOME."""
    def _use(root: Path, **kwargs) -> HermesHome:
        built = build_home(root, **kwargs)
        monkeypatch_setenv_home(built.root)
        return built

    return _use


def monkeypatch_setenv_home(root: Path) -> None:
    """Point HERMES_HOME at a profile root (global process env + the
    context-local override hermes_constants consults first)."""
    os.environ["HERMES_HOME"] = str(root)
    with contextlib.suppress(Exception):
        from hermes_constants import set_hermes_home_override
        set_hermes_home_override(root)


def make_home(root: Path, **kwargs) -> HermesHome:
    """Plain callable version of ``setup_home`` (for tests that build their
    own profiles mid-test)."""
    built = build_home(root, **kwargs)
    monkeypatch_setenv_home(built.root)
    return built


@pytest.fixture
def exclusive_lock(home: HermesHome):
    """Hold a real BEGIN EXCLUSIVE write transaction on the telemetry DB."""
    conn = sqlite3.connect(home.db_path, isolation_level=None)
    conn.execute("BEGIN EXCLUSIVE")
    conn.execute("UPDATE counter_aggregates SET value = value + 1 WHERE metric_name = ?",
                 ("hermes.tool.success",))
    try:
        yield conn
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        conn.close()


# --------------------------------------------------------------------------
# spies
# --------------------------------------------------------------------------
def open_fd_count() -> int:
    return len(os.listdir("/proc/self/fd"))


def vanishing_open(target: Path):
    """A drop-in for ``open`` that pretends ``target`` was rotated away.

    Models the race the fd-leak fix defends against: the DB exists at the
    ``exists()`` check and is gone by the time the scratch copy opens it.
    """
    real_open = open

    def _open(file, *args, **kwargs):
        try:
            same = Path(os.fspath(file)) == target
        except TypeError:  # pragma: no cover - exotic file objects
            same = False
        if same:
            raise FileNotFoundError(errno.ENOENT, "telemetry DB rotated", str(file))
        return real_open(file, *args, **kwargs)

    return _open


class Spy:
    """Records what the module under test does, without changing behaviour.

    ``sqlite3.connect`` keeps working normally except that ``timeout`` is forced
    to 0 so a genuinely locked DB surfaces ``database is locked`` immediately
    instead of blocking for the module's hard-coded 5s busy timeout.
    """

    def __init__(self) -> None:
        self.connects: list[str] = []
        self.mkstemps: list[dict] = []
        self.fdopens: list[int] = []

    @property
    def copy_paths(self) -> list[str]:
        return [call["path"] for call in self.mkstemps]

    @property
    def read_only_connects(self) -> list[str]:
        return [c for c in self.connects if "mode=ro" in c]


@pytest.fixture
def spy(monkeypatch, plugin_api: ModuleType) -> Spy:
    recorder = Spy()

    real_connect = sqlite3.connect

    def connect(database, *args, **kwargs):
        recorder.connects.append(str(database))
        kwargs["timeout"] = 0.0
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)

    real_mkstemp = tempfile.mkstemp

    def mkstemp(*args, **kwargs):
        fd, path = real_mkstemp(*args, **kwargs)
        recorder.mkstemps.append({
            "args": args,
            "kwargs": dict(kwargs),
            "path": path,
            # mode of the descriptor mkstemp just handed out, before anything
            # else can touch it
            "mode": os.fstat(fd).st_mode & 0o777,
        })
        return fd, path

    monkeypatch.setattr(tempfile, "mkstemp", mkstemp)

    real_fdopen = os.fdopen

    def fdopen(fd, *args, **kwargs):
        recorder.fdopens.append(fd)
        return real_fdopen(fd, *args, **kwargs)

    monkeypatch.setattr(os, "fdopen", fdopen)

    def vanish(target_: Path):
        """Monkeypatch ``plugin_api.open`` so the telemetry DB 'disappears'
        between the exists() check and the fallback copy (rotation race)."""
        real_open = open

        def _open(file, *a, **kw):
            try:
                same = Path(os.fspath(file)) == target_
            except TypeError:  # pragma: no cover - exotic file objects
                same = False
            if same:
                raise FileNotFoundError(errno.ENOENT, "telemetry DB rotated", str(file))
            return real_open(file, *a, **kw)

        monkeypatch.setattr(plugin_api, "open", _open, raising=False)

    recorder.vanish = vanish  # type: ignore[attr-defined]
    return recorder


def _load_new_module(path: Path, name: str = "td_no_fastapi") -> ModuleType:
    """Fresh module executed from ``path`` under a unique name (bypasses
    sys.modules so the same source can be imported repeatedly)."""
    return _load_module(path, name)


def _build_profile(root: Path, value: str) -> HermesHome:
    """Build a profile whose data differs from ``home`` so cross-home queries
    are distinguishable."""
    home = make_home(root)
    conn = sqlite3.connect(home.db_path)
    try:
        conn.execute(
            "INSERT INTO counter_aggregates (period_start, metric_name, hermes_version,"
            " os_family, architecture, install_method, dimensions_json, value, packaged_value)"
            " VALUES ('2026-09-03', 'pkg-returned', '0.1.0', 'linux', 'x86_64', 'pip',"
            " '{}', ?, 0)",
            (int(value),),
        )
        conn.commit()
    finally:
        conn.close()
    return home
