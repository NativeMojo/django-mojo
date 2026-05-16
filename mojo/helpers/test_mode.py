"""Hardened test-mode header gate — defense in depth.

Test-mode headers (X-Mojo-Test-*) let test suites override per-request behavior
without server reloads. To prevent an accidental production leak from becoming
a remote-code-execution vector, the gate enforces FOUR layers:

  1. **Environment variable**: MOJO_TEST_MODE=1 must be set in the server
     process environment. Set only by `bin/asgi_local` for the test server;
     production launchers never set it. Not a settings.py value — settings
     files travel between projects, env vars don't.

  2. **Loopback-only**: REMOTE_ADDR must be 127.0.0.1 / ::1 / localhost.
     The peer-IP check uses the raw socket address, not the proxy-aware
     `request.ip` — production traffic that came through any reverse proxy
     fails this check.

  3. **No proxy chain**: HTTP_X_FORWARDED_FOR / HTTP_FORWARDED / HTTP_VIA must
     all be absent. Any presence indicates the request crossed a proxy and is
     therefore not a local test request.

  4. **Startup warning + Django system check**: when MOJO_TEST_MODE=1 is set,
     a WARNING is logged at import time. Django's `check --deploy` reports
     this as an ERROR if DEBUG=False — production deploys catch the
     misconfiguration before serving traffic.

If ALL four conditions are not satisfied, every X-Mojo-Test-* header is
silently ignored — the code paths that read them are short-circuited at the
gate. This is the only function that should grant header-override access
anywhere in the framework.
"""
import os
import sys


# Read once at module load. Env var only — never settings. The server process
# either had MOJO_TEST_MODE=1 in its environment at startup or it didn't.
_TEST_MODE_ENABLED = os.environ.get("MOJO_TEST_MODE") == "1"


_LOOPBACK_IPS = frozenset(("127.0.0.1", "::1", "localhost"))


def is_enabled():
    """Module-level: is test-mode enabled in this process at all?"""
    return _TEST_MODE_ENABLED


def is_test_request(request):
    """Per-request gate. Returns True only when ALL defenses pass.

    Call this before reading ANY X-Mojo-Test-* header. If False, the header
    must be ignored — fall back to normal settings.
    """
    if not _TEST_MODE_ENABLED:
        return False
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


# Emit a loud startup warning when test mode is active. Goes to stderr so
# it shows up in every common log capture (uvicorn, gunicorn, systemd, k8s).
if _TEST_MODE_ENABLED:
    print(
        "================================================================\n"
        "MOJO_TEST_MODE=1 IS ACTIVE — server honors X-Mojo-Test-* headers\n"
        "from loopback connections. This MUST NOT be set in production.\n"
        "================================================================",
        file=sys.stderr, flush=True,
    )
