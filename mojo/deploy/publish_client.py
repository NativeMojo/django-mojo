"""Unprivileged client for the root-owned publish broker.

Wrap a content publish in `publish_window(...)` and the writes inside it become
explainable to MojoSec. The write itself still runs as the application, as
itself — this only opens and closes the annotation window around it.

ANNOTATION IS NEVER A GATE. Every failure here — broker absent, node not
enrolled for content, sudo refused, malformed reply, timeout — is reported to
stderr and swallowed. A publish must not fail because its explanation could
not be recorded; the writes simply stay unexplained and MojoSec reports them,
which is the outcome this module exists to reduce, not to enforce.

Diagnostics are stderr prints, not logit: `mojo.deploy` runs before Django
settings exist and `mojo.helpers` is off-limits here (see the package
docstring).
"""

import contextlib
import json
import subprocess
import sys


SUDO = "/usr/bin/sudo"
BROKER_PATH = "/usr/local/sbin/mojo-publish-broker"
DEFAULT_TTL_SECONDS = 900
BEGIN_TIMEOUT_SECONDS = 60
END_TIMEOUT_SECONDS = 120
MAX_REPLY_BYTES = 1024 * 1024


def _warn(message):
    print(f"mojo publish: {message}", file=sys.stderr)


def _default_runner(request, timeout):
    """Send one bounded JSON request to the broker over sudo."""
    payload = json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8")
    done = subprocess.run(
        [SUDO, BROKER_PATH, ""], input=payload, capture_output=True,
        timeout=timeout)
    if done.returncode != 0:
        raise RuntimeError(
            (done.stderr or b"").decode("utf-8", "replace").strip()[:256] or
            f"broker exited {done.returncode}")
    return done.stdout[:MAX_REPLY_BYTES]


def _request(request, timeout, runner=None):
    """Return the broker's parsed reply, or None. Never raises."""
    call = _default_runner if runner is None else runner
    try:
        reply = call(request, timeout)
    except Exception as err:
        _warn(f"{request['operation']} failed ({err}); "
              "this publish stays unexplained to MojoSec")
        return None
    try:
        if isinstance(reply, (bytes, bytearray)):
            reply = reply.decode("utf-8")
        value = json.loads(reply) if isinstance(reply, str) else reply
    except (UnicodeError, ValueError) as err:
        _warn(f"{request['operation']} reply was unreadable ({err})")
        return None
    if not isinstance(value, dict) or value.get("ok") is not True:
        _warn(f"{request['operation']} was refused by the broker")
        return None
    return value


@contextlib.contextmanager
def publish_window(root, subtrees, ttl_seconds=DEFAULT_TTL_SECONDS, *, runner=None):
    """Open an annotation window around this tenant's own publish.

    `root` is the tenant root (an immediate child of an enrolled content root)
    and `subtrees` names its direct children being published — `rev-7`,
    `current`. Root derives every path inside them and computes every digest.

    Open the window BEFORE the first write, including before a delete: the
    broker snapshots `before` at begin, and a path removed before the window
    opened has no before-state to compare against and stays unexplained.
    """
    begun = _request(
        {"operation": "begin", "root": root, "subtrees": list(subtrees),
         "ttl_seconds": ttl_seconds},
        BEGIN_TIMEOUT_SECONDS, runner=runner)
    operation_id = begun.get("operation_id") if begun else None
    try:
        yield operation_id
    except BaseException:
        if operation_id:
            _request({"operation": "abort", "operation_id": operation_id},
                     BEGIN_TIMEOUT_SECONDS, runner=runner)
        raise
    if operation_id:
        _request({"operation": "end", "operation_id": operation_id},
                 END_TIMEOUT_SECONDS, runner=runner)
