"""Test-mode header gate.

Test-mode headers (X-Mojo-Test-*) let test suites override per-request behavior
without server reloads. The gate enforces three layers:

  1. **`MOJO_TEST_MODE = True` in Django settings**. Defaults to False.
     The framework follows the same settings-flag pattern used for other
     gates throughout the codebase.

  2. **Loopback-only**: REMOTE_ADDR must be 127.0.0.1 / ::1 / localhost.
     The peer-IP check uses the raw socket address, not the proxy-aware
     `request.ip` — production traffic that came through any reverse proxy
     fails this check even if MOJO_TEST_MODE were accidentally True.

  3. **No proxy chain**: HTTP_X_FORWARDED_FOR / HTTP_FORWARDED / HTTP_VIA
     must all be absent. Any presence indicates the request crossed a
     proxy and is therefore not a local test request.

If ALL three conditions are not satisfied, every X-Mojo-Test-* header is
silently ignored. This is the only function that should grant
header-override access anywhere in the framework. The defenses #2 and #3
make this safe even if MOJO_TEST_MODE accidentally leaks into a production
settings file — external attackers can't satisfy them.

The dotted-path handler headers (X-Mojo-Test-User-Registered-Handler etc.)
can load arbitrary importable callables — that's why #2 and #3 matter, not
just the flag.
"""
import sys

from mojo.helpers.settings import settings


_LOOPBACK_IPS = frozenset(("127.0.0.1", "::1", "localhost"))

# Track whether we've already emitted the startup warning so we only log once
# per process, not once per request.
_WARNED = False


def is_enabled():
    """Module-level: is test-mode enabled in this process at all?

    Read conf-file-only (get_static): the master switch for the X-Mojo-Test-*
    header plane must never be flippable via the DB/Redis settings plane, which
    is remotely writable (generic /api/settings REST, or Redis access) — ITEM-031.
    """
    return settings.get_static("MOJO_TEST_MODE", False, kind="bool")


def is_test_request(request):
    """Per-request gate. Returns True only when ALL defenses pass.

    Call this before reading ANY X-Mojo-Test-* header. If False, the header
    must be ignored — fall back to normal settings.
    """
    if not is_enabled():
        return False
    _warn_once()
    if request is None:
        return False
    # Defense 2 + 3: peer must be loopback AND no proxy chain.
    meta = getattr(request, "META", None) or {}
    if meta.get("HTTP_X_FORWARDED_FOR"):
        return False
    if meta.get("HTTP_FORWARDED"):
        return False
    if meta.get("HTTP_VIA"):
        return False
    remote = meta.get("REMOTE_ADDR", "")
    return remote in _LOOPBACK_IPS


def _warn_once():
    """Loud one-shot stderr warning the first time the gate is consulted
    with MOJO_TEST_MODE=True. Surfaces accidental production-settings
    leaks in logs (uvicorn, gunicorn, systemd, k8s all capture stderr)."""
    global _WARNED
    if _WARNED:
        return
    _WARNED = True
    print(
        "================================================================\n"
        "MOJO_TEST_MODE=True — server honors X-Mojo-Test-* override headers\n"
        "from loopback connections. This MUST NOT be True in production.\n"
        "================================================================",
        file=sys.stderr, flush=True,
    )
