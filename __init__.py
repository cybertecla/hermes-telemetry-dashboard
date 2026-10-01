"""Hermes Telemetry Dashboard — standalone dashboard plugin.

The dashboard extension is discovered from dashboard/manifest.json. The
standalone plugin entry point is intentionally a no-op so `hermes plugins
install ... --enable` can manage this dashboard-only plugin cleanly.
"""


def register(ctx):
    """Register no agent tools; the dashboard tab is loaded by the Hermes dashboard."""
    return None
