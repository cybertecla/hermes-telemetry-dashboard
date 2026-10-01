"""Hermes Telemetry Dashboard — read-only backend.

Mounted at /api/plugins/hermes-telemetry-dashboard/ by the Hermes dashboard.

READ-ONLY GUARANTEE:
- Opens the shared-metrics DB with sqlite3 mode=ro + busy_timeout.
- Never writes to the telemetry DB, never touches config.yaml telemetry.*
  (enabled/send toggles belong to the consent UI elsewhere), never calls
  the Nous sender.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

try:
    from fastapi import APIRouter, HTTPException
except Exception:  # dashboard-venv ImportError guard (see skill)
    class APIRouter:
        def get(self, *a, **kw):
            return lambda fn: fn

    class HTTPException(Exception):
        def __init__(self, status_code: int = 500, detail: str = "error"):
            self.status_code = status_code
            self.detail = detail


router = APIRouter()

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
TELEMETRY_DB = HERMES_HOME / "telemetry" / "shared_metrics" / "metrics.sqlite3"
CONFIG_YAML = HERMES_HOME / "config.yaml"

_MILESTONE_KEYS = (
    "milestone:setup_completed",
    "milestone:first_task_started",
    "milestone:first_task_success",
    "milestone:first_tool_success",
    "milestone:first_skill_reused",
    "milestone:first_mcp_tool_success",
)


# --------------------------------------------------------------------------
# DB access (strictly read-only, locked-DB tolerant)
# --------------------------------------------------------------------------
def _query(sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    """Run a read-only query against the telemetry DB.

    Falls back to a scratch copy of the file when a live writer holds the
    SQLite lock (see hermes-dashboard-plugins skill: Hermes-owned DBs are
    live; mode=ro + timeout may still hit `database is locked`).
    """
    if not TELEMETRY_DB.exists():
        raise HTTPException(404, f"telemetry DB not found: {TELEMETRY_DB}")
    try:
        conn = sqlite3.connect(f"file:{TELEMETRY_DB}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        raise HTTPException(500, f"cannot open telemetry DB read-only: {exc}")
    conn.row_factory = sqlite3.Row
    try:
        try:
            return conn.execute(sql, args).fetchall()
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower():
                raise HTTPException(500, f"query failed: {exc}")
    finally:
        conn.close()
    # Live writer holds the lock: read a consistent scratch copy instead.
    tmp = Path(tempfile.gettempdir()) / f"metrics-ro-{os.getpid()}.sqlite3"
    try:
        shutil.copy2(TELEMETRY_DB, tmp)
        conn2 = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True, timeout=5.0)
        conn2.row_factory = sqlite3.Row
        try:
            return conn2.execute(sql, args).fetchall()
        finally:
            conn2.close()
    finally:
        tmp.unlink(missing_ok=True)


def _parse_dims(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _telemetry_config() -> dict[str, Any]:
    """Read telemetry.shared_metrics from config.yaml (read-only; never writes)."""
    info: dict[str, Any] = {"enabled": None, "send": None}
    try:
        import yaml
    except Exception:
        info["source"] = "unavailable"
        return info
    try:
        raw = yaml.safe_load(CONFIG_YAML.read_text()) or {}
    except Exception:
        info["source"] = "unreadable"
        return info
    block = (raw.get("telemetry") or {}).get("shared_metrics") or {}
    info["enabled"] = block.get("enabled")
    info["send"] = block.get("send")
    info["source"] = str(CONFIG_YAML)
    return info


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------
@router.get("/status")
def status() -> dict[str, Any]:
    """Health endpoint — proves the plugin mounted and can see the DB."""
    db_found = TELEMETRY_DB.exists()
    state: dict[str, Any] = {"ok": True, "db_found": db_found, "db_path": str(TELEMETRY_DB)}
    if db_found:
        try:
            tables = [r[0] for r in _query(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )]
            state["tables"] = tables
            state["aggregate_rows"] = _query(
                "SELECT COUNT(*) AS n FROM counter_aggregates"
            )[0]["n"]
        except HTTPException as exc:
            state["ok"] = False
            state["error"] = exc.detail
        except Exception as exc:  # pragma: no cover - defensive
            state["ok"] = False
            state["error"] = str(exc)
    return state


@router.get("/summary")
def summary() -> dict[str, Any]:
    """Today's overview: metric families, total events, queued packages, send state."""
    periods = []
    for r in _query(
        "SELECT period_start, COUNT(DISTINCT metric_name) AS families,"
        " COUNT(*) AS rows, COALESCE(SUM(value), 0) AS total,"
        " COALESCE(SUM(packaged_value), 0) AS packaged"
        " FROM counter_aggregates GROUP BY period_start ORDER BY period_start"
    ):
        periods.append({
            "period": r["period_start"],
            "metric_families": r["families"],
            "aggregate_rows": r["rows"],
            "total_events": r["total"],
            "packaged_events": r["packaged"],
        })
    outbox = _query(
        "SELECT COUNT(*) AS n,"
        " COALESCE(SUM(CASE WHEN sent_at IS NULL THEN 1 ELSE 0 END), 0) AS queued"
        " FROM package_outbox"
    )[0]
    consent = _query("SELECT COUNT(*) AS n FROM send_consent_windows WHERE closed_at IS NULL")
    marks = [dict(r) for r in _query("SELECT name, stamp FROM consent_marks ORDER BY stamp")]
    return {
        "periods": periods,
        "metric_families_total": len(_query("SELECT DISTINCT metric_name FROM counter_aggregates")),
        "events_total": sum(p["total_events"] for p in periods),
        "packages_total": outbox["n"],
        "packages_queued": outbox["queued"],
        "telemetry": _telemetry_config(),
        "consent": {
            "open_windows": consent[0]["n"],
            "marks": marks,
        },
    }


@router.get("/metrics")
def metrics(period: str | None = None) -> dict[str, Any]:
    """Metric families + totals, optionally filtered to one period."""
    sql = (
        "SELECT metric_name, COUNT(*) AS rows, COALESCE(SUM(value), 0) AS total,"
        " COALESCE(SUM(packaged_value), 0) AS packaged,"
        " MIN(period_start) AS first_period, MAX(period_start) AS last_period"
        " FROM counter_aggregates"
    )
    args: tuple = ()
    if period:
        sql += " WHERE period_start = ?"
        args = (period,)
    sql += " GROUP BY metric_name ORDER BY metric_name"
    return {
        "period": period,
        "metrics": [dict(r) for r in _query(sql, args)],
    }


@router.get("/metrics/{name}")
def metric_drilldown(name: str) -> dict[str, Any]:
    """Drilldown for one metric: dimensions breakdown + values per period."""
    rows = _query(
        "SELECT period_start, value, packaged_value, dimensions_json"
        " FROM counter_aggregates WHERE metric_name = ? ORDER BY period_start",
        (name,),
    )
    if not rows:
        raise HTTPException(404, f"unknown metric family: {name}")
    per_period: dict[str, dict[str, Any]] = {}
    dims: dict[str, dict[str, int]] = {}
    total = 0
    packaged = 0
    for r in rows:
        period = r["period_start"]
        entry = per_period.setdefault(period, {"period": period, "value": 0, "packaged_value": 0})
        entry["value"] += r["value"]
        entry["packaged_value"] += r["packaged_value"]
        total += r["value"]
        packaged += r["packaged_value"]
        for k, v in _parse_dims(r["dimensions_json"]).items():
            bucket = dims.setdefault(k, {})
            bucket[str(v)] = bucket.get(str(v), 0) + r["value"]
    return {
        "name": name,
        "total": total,
        "packaged_total": packaged,
        "periods": list(per_period.values()),
        "dimensions": dims,
    }


@router.get("/timeline")
def timeline() -> dict[str, Any]:
    """Milestones (setup -> first task -> first success -> ...) + feature flags."""
    state = {r["key"]: r["value"] for r in _query("SELECT key, value FROM telemetry_state")}
    milestones = []
    for key, stamp in state.items():
        if key.startswith("milestone:") and stamp:
            milestones.append({"name": key.split(":", 1)[1], "stamp": stamp})
    milestones.sort(key=lambda m: m["stamp"])
    features = []
    for key, stamp in state.items():
        if key.startswith("feature:") and stamp:
            features.append({"name": key.split(":", 1)[1], "stamp": stamp})
    features.sort(key=lambda f: f["stamp"])
    return {
        "milestones": milestones,
        "features": features,
        "install_id": state.get("install_id"),
        "install_snapshot_recorded_at": state.get("install_snapshot_recorded_at"),
        "schema_version": state.get("schema_version"),
    }


@router.get("/install")
def install() -> dict[str, Any]:
    """Install snapshot: version/os/arch/install method (resource) + bucket dimensions."""
    state = {r["key"]: r["value"] for r in _query("SELECT key, value FROM telemetry_state")}
    rows = _query(
        "SELECT hermes_version, os_family, architecture, install_method, dimensions_json"
        " FROM counter_aggregates WHERE metric_name = 'hermes.install.snapshot' LIMIT 1"
    )
    if not rows:
        return {"install_id": state.get("install_id")}
    r = rows[0]
    resource = {
        "hermes_version": r["hermes_version"],
        "os_family": r["os_family"],
        "architecture": r["architecture"],
        "install_method": r["install_method"],
    }
    dims = _parse_dims(r["dimensions_json"])
    return {
        "install_id": state.get("install_id"),
        "recorded_at": state.get("install_snapshot_recorded_at"),
        "resource": resource,
        "buckets": dims,
    }


@router.get("/package/next")
def package_next() -> dict[str, Any]:
    """The next Nous-bound package: exactly what WOULD be sent + its send state."""
    rows = _query(
        "SELECT package_id, period_start, period_end, payload_json, created_at,"
        " sent_at, send_state, send_attempts, last_error"
        " FROM package_outbox WHERE sent_at IS NULL"
        " ORDER BY created_at LIMIT 1"
    )
    outbox = _query(
        "SELECT COUNT(*) AS n,"
        " COALESCE(SUM(CASE WHEN sent_at IS NULL THEN 1 ELSE 0 END), 0) AS queued"
        " FROM package_outbox"
    )[0]
    if not rows:
        return {
            "package": None,
            "packages_total": outbox["n"],
            "packages_queued": outbox["queued"],
            "telemetry": _telemetry_config(),
        }
    r = rows[0]
    try:
        payload = json.loads(r["payload_json"])
    except Exception:
        payload = None
    return {
        "package": {
            "package_id": r["package_id"],
            "period_start": r["period_start"],
            "period_end": r["period_end"],
            "created_at": r["created_at"],
            "send_state": r["send_state"],
            "send_attempts": r["send_attempts"],
            "last_error": r["last_error"],
            "payload": payload,
        },
        "packages_total": outbox["n"],
        "packages_queued": outbox["queued"],
        "telemetry": _telemetry_config(),
    }