"""Crash-report route limits (maestro #4125).

Serial for execution reasons, not state ones: these tests fill the per-IP
`incident_client_report` bucket that every caller from 127.0.0.1 shares, so a
parallel package posting to the same route could be throttled by them.
Nothing is patched and no settings change; the Redis outage case uses the
reporter's `connection=` seam.
"""
TESTIT = {
    "default_core": True,
    "serial": True,
    "requires_apps": ["mojo.apps.incident"],
}
