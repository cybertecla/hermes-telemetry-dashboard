Delta ready for re-review — @teknium1 the new pin is live.

`sha:` now points at **1399a77** (tagged `v0.1.1`).

### 1. Predictable `/tmp` scratch copy → private `mkstemp`

The fallback now creates the copy with
`tempfile.mkstemp(dir=<HERMES_HOME>/"plugin-data"/"hermes-telemetry-dashboard")`
— O_EXCL + 0600 inside our own 0700 dir — and copies bytes through the
descriptor with `copyfileobj`. `copy2`/`copy` are gone entirely (grep:
zero hits), as is `gettempdir`.

`copyfileobj` rather than `copy2` specifically because `copy2` follows a
symlink *and* re-applies the source's 0644 mode over our 0600 copy. The
scratch file is unlinked on every exit path, including the
query-exception path.

I deliberately did **not** use your `sqlite3 backup()` → `:memory:`
suggestion: this fallback fires precisely because the connection is
locked, so a backup on that same connection would hit the same lock. The
byte copy sidesteps SQLite locking entirely. `mkstemp` was your first
suggestion, so this follows it.

### 2. `category: desktop` → `general`

Confirmed empirically: the repo ships `dashboard/` only —
`find . -type d -name desktop` returns nothing.

On your "or the category other dashboard plugins use" — I looked for a
precedent and there isn't one to defer to. The only two plugins in the
catalog with a `dashboard/` tab (`hermes-achievements`, `kanban`) are
official built-ins and have no catalog entries at all. So I took your
explicit first suggestion, `general`, which is in the validator's
accepted vocabulary (`CATALOG_CATEGORIES` in
`website/scripts/extract-plugins.py`).

### 3. Non-blocking note — `HERMES_HOME` at import

Done. The import-time constants became per-call resolvers
(`_hermes_home()` / `_telemetry_db()` / `_config_yaml()`) using
`hermes_constants.get_hermes_home()`, with a call-time `HERMES_HOME` env
fallback for the case where that import fails in the dashboard runtime:

```python
def _hermes_home() -> Path:
    """Resolve the Hermes home at call time.

    A module-level constant froze the launch profile's home for the life of the
    process, so a multi-profile dashboard could only ever see one profile. The
    env var is read at call time too, so tests can flip it between requests.
    """
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except ImportError:
        # Standalone dashboard import (hermes_constants not on sys.path).
        val = os.environ.get("HERMES_HOME", "").strip()
        return Path(val) if val else Path.home() / ".hermes"
```

No import-time env reads remain; every call site is inside a route
handler, so each request resolves the current home independently.

### One bug found during the fix, worth flagging

The first pass at blocker 1 introduced an fd leak:

```python
with open(db_path, "rb") as src, os.fdopen(fd, "wb") as dst:   # enters src first
```

`with A, B:` enters A before evaluating B, so if `open()` raises,
`os.fdopen` never runs and the `mkstemp` descriptor is never closed — the
`finally` only unlinks the path. One leaked descriptor per failed copy,
for the life of the dashboard process. It triggers *under lock
contention*, which is exactly when this fallback runs, and the writer can
rotate the DB between the `exists()` check and the copy.

Measured 50 leaked descriptors over 50 iterations; now 0. Fixed by taking
fd ownership via `os.fdopen` before any fallible call, plus an explicit
`os.close` guard because `os.fdopen` itself does not close the fd when it
raises.

### Verification

- `pytest tests/ -q` → **19 passed**
- Non-vacuity: reconstructing the old `copy2` →
  `/tmp/metrics-ro-<pid>.sqlite3` shape makes **6 of 19 fail**, including
  `test_a4_symlink_at_old_predictable_path_not_followed` (a symlink planted
  at the old path cannot clobber a victim) and the fd-leak check
- Temp copy: `0600` file, `0700` dir, under `HERMES_HOME`, never `/tmp`;
  0 descriptors leaked over 50 iterations
- `hermes plugins validate .` → **passed**, security scan safe
- `scripts/validate_plugin_catalog.py` → **OK**
- Screenshots re-pinned to 1399a77 and confirmed reachable (HTTP 200)

Ready for the re-review whenever you are.