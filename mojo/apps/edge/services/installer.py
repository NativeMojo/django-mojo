"""
The node-side installer: render a generation, validate it, swap it in, reload.

This is the only module in django-mojo that writes files nginx reads and asks
systemd to reload a service. Read the privilege note at the bottom before
changing it.

## Why the sequence looks the way it does

```
fetch desired state (from the DB — see below)
if generation == installed: return                      idempotent no-op
render generations/<new>/, write certificate material 0600
    material unfetchable:  house vhost  -> abort
                           tenant vhost -> exclude it, report an incident
stage www/<vhost-id> links                              (#1435 fills these)
nginx -t -c generations/<new>/nginx.conf                cheap pre-filter
os.replace(current -> generations/<new>)                nothing has reloaded yet
nginx -t                                                against the REAL config
    fail, or "conflicting server name" on stderr -> revert current, incident, raise
    ok -> systemctl reload nginx, write installed.json, prune
```

**Validation happens after the swap, not only before.** A harness `nginx.conf`
only approximates the real one — a `map`, `upstream` or `limit_req_zone`
defined in the deployment's own config and referenced by generated output
passes or fails differently under it. The real test is the real config. Doing
it after the swap is safe because **nginx serves the running configuration
until something reloads it**: a bad `current` is reverted before any reload, so
it is never loaded. The pre-filter is kept because it catches most breakage
without touching `current` at all.

**`nginx -t` does NOT catch server_name collisions.** A duplicate is a
*warning* — `conflicting server name "x" on 0.0.0.0:443, ignored` — and nginx
still exits 0, having silently dropped one block. The platform's own API server
block also lives in the real config, not in the generated set, so the shadowing
attack is invisible to the harness. The defences that work are the Phase A
enabled-uniqueness constraint and `validators.validate_not_reserved`; the
stderr scan here is a third net, not the first.

**One tenant cannot freeze the fleet.** Certificate material can be unreadable
for reasons unrelated to the row (KMS down — `KSMSecrets` returns an empty
mapping). With a single abort path, one tenant's broken certificate would stop
every node in the pool from converging, including on an urgent renewal of the
platform's own certificate, and it would fail silent-but-serving until
something expired. nginx's all-or-nothing loading requires the *generation* to
be complete; it does not require the generation to contain every vhost.

## Where the desired state comes from

Directly from the database, through the SAME `render.desired_state()` the
node-facing REST endpoint serves. The job runner is the application, so it
already holds a consistent snapshot and needs no credential — and sharing the
function is what makes it impossible for the endpoint and the installer to
disagree about what a generation contains. `GET /api/edge/desired_state`
remains for out-of-process consumers (a standalone sync script, or a node whose
app is down but whose timer still runs).
"""

import json
import os
import shutil
import subprocess

from mojo.helpers import logit
from mojo.helpers.settings import settings

from mojo.apps.edge.services import render


class InstallError(Exception):
    """A generation could not be installed. `current` is unchanged."""


def keep_generations():
    return int(settings.get("EDGE_KEEP_GENERATIONS", 5))


def command_timeout():
    return int(settings.get("EDGE_COMMAND_TIMEOUT", 60))


# ----------------------------------------------------------------------
# the privileged boundary — the ONLY place this app shells out
# ----------------------------------------------------------------------

def _nginx_test_argv(config_path=None):
    """`nginx -t`, optionally against a specific config.

    Built from settings and a fixed shape — **never composed from row data**.
    A vhost's label, an upstream's host and a certificate's name are all
    validated, but none of them belongs anywhere near an argv list.
    """
    argv = list(settings.get("EDGE_NGINX_TEST_CMD", ["sudo", "-n", "nginx", "-t"],
                             kind="list"))
    if config_path:
        argv = argv + ["-c", str(config_path)]
    return argv


def _nginx_reload_argv():
    return list(settings.get(
        "EDGE_NGINX_RELOAD_CMD", ["sudo", "-n", "systemctl", "reload", "nginx"],
        kind="list"))


def _run(argv):
    """Run a command. The single seam the installer tests replace.

    Kept deliberately dumb: no shell, no string interpolation, no `cwd`. If you
    are tempted to add any of those, the thing you actually want is a new
    constant argv builder above.
    """
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=command_timeout())


# nginx reports a duplicate server_name as a WARNING and still exits 0. Treat
# it as a failure — a silently-dropped server block is a site that stopped
# being served with no error anywhere.
CONFLICT_MARKER = "conflicting server name"


def _nginx_check(config_path=None):
    """Run `nginx -t` and return (ok, combined output)."""
    try:
        result = _run(_nginx_test_argv(config_path))
    except Exception as err:
        return False, f"could not run nginx -t: {err}"
    output = f"{result.stdout or ''}{result.stderr or ''}"
    if result.returncode != 0:
        return False, output
    if CONFLICT_MARKER in output:
        return False, f"nginx reported a server_name collision: {output}"
    return True, output


# ----------------------------------------------------------------------
# paths
# ----------------------------------------------------------------------

def installed_path():
    return os.path.join(render.edge_root(), "installed.json")


def read_installed():
    try:
        with open(installed_path()) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def write_installed(generation, excluded=None):
    payload = dict(generation=generation, excluded=sorted(excluded or []))
    tmp = f"{installed_path()}.tmp"
    with open(tmp, "w") as handle:
        json.dump(payload, handle)
    os.replace(tmp, installed_path())
    return payload


def _symlink_swap(link_path, target):
    """Repoint a symlink atomically.

    `os.symlink` refuses an existing path, so the swap goes through a temporary
    link and `os.replace`, which is atomic on POSIX. A plain unlink-then-symlink
    would leave a window where the node has no `current` at all.
    """
    tmp = f"{link_path}.swap"
    if os.path.islink(tmp) or os.path.exists(tmp):
        os.unlink(tmp)
    os.symlink(target, tmp)
    os.replace(tmp, link_path)


def current_target():
    """What `current` points at now, or None when it has never been set."""
    link = render.current_link()
    try:
        return os.path.realpath(link) if os.path.islink(link) else None
    except OSError:
        return None


# ----------------------------------------------------------------------
# staging
# ----------------------------------------------------------------------

def _write_material(generation, certificate):
    """Write one certificate's material into the generation, 0600.

    Returns True when the material landed. A False here is NOT an error by
    itself — the caller decides whether the owning vhost is droppable.
    """
    private_key = certificate.private_key_pem
    if not private_key or not certificate.cert_pem:
        # KSMSecrets returns an empty mapping when KMS decryption fails, so
        # this means "custody unavailable", not "this certificate has no key".
        return False

    target = render.cert_dir(generation, certificate.pk)
    os.makedirs(target, mode=0o700, exist_ok=True)

    chain = certificate.chain_pem or ""
    fullchain = certificate.cert_pem
    if chain and not fullchain.endswith("\n"):
        fullchain += "\n"
    fullchain += chain

    for name, content in (("fullchain.pem", fullchain),
                          ("privkey.pem", private_key)):
        path = os.path.join(target, name)
        # Create with 0600 from the start rather than chmod'ing after — a
        # private key must never exist, even briefly, as world-readable.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
    return True


def stage_generation(vhosts, generation):
    """Build `generations/<generation>/` completely. Returns excluded vhost ids.

    Raises InstallError when a HOUSE vhost cannot be staged: that is the
    platform's own serving path, and converging without it is not a partial
    success.
    """
    from mojo.apps.incident import reporter

    gen_dir = render.generation_dir(generation)
    os.makedirs(os.path.join(gen_dir, "conf.d"), exist_ok=True)
    os.makedirs(os.path.join(gen_dir, "www"), exist_ok=True)

    installable = []
    excluded = []
    for vhost in vhosts:
        if _write_material(generation, vhost.certificate):
            installable.append(vhost)
            continue

        is_house = vhost.domain.group_id is None
        message = (
            f"edge: certificate {vhost.certificate_id} for "
            f"{vhost.server_name} has no readable material (KMS?)")
        if is_house:
            raise InstallError(f"{message} — it serves a platform vhost")
        logit.error(message)
        try:
            reporter.report_event(
                message, title="Vhost excluded from an edge generation",
                category="edge_install", level=6)
        except Exception:
            # An incident-reporting failure must not decide whether a fleet
            # converges. The exclusion is already in installed.json.
            logit.exception("edge: failed to report a vhost exclusion")
        excluded.append(vhost.pk)

    files = render.render_generation(installable, generation)
    for name, text in files.items():
        with open(os.path.join(gen_dir, "conf.d", name), "w") as handle:
            handle.write(text)

    with open(os.path.join(gen_dir, "nginx.conf"), "w") as handle:
        handle.write(render.render_nginx_harness(generation))

    # A web root that does not exist makes nginx answer 500 instead of 404.
    # #1435 replaces these directories with symlinks to an installed release.
    for vhost in installable:
        if vhost.kind in ("static", "spa"):
            os.makedirs(render.www_dir(generation, vhost.pk), exist_ok=True)

    return excluded


def prune_generations(keep=None):
    """Drop all but the most recent `keep` generations, never `current`."""
    keep = keep or keep_generations()
    root = os.path.join(render.edge_root(), "generations")
    # `current_target()` returns a REALPATH, so the candidate list has to be
    # realpath'd too before comparing. Without this, any EDGE_ROOT reached
    # through a symlink (/var -> /private/var on macOS, or a symlinked
    # /opt/api) makes the live generation compare unequal to itself — and the
    # prune deletes the directory nginx is currently serving from, taking the
    # rollback target with it. Caught by a test.
    live = current_target()
    try:
        entries = [os.path.join(root, name) for name in os.listdir(root)]
    except OSError:
        return []
    entries = [
        p for p in entries
        if os.path.isdir(p) and os.path.realpath(p) != live
    ]
    entries.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    removed = []
    # `keep` counts the LIVE generation too, so one slot is already spoken for.
    for path in entries[max(keep - 1, 0):]:
        shutil.rmtree(path, ignore_errors=True)
        removed.append(path)
    return removed


# ----------------------------------------------------------------------
# the install
# ----------------------------------------------------------------------

def install(pool="default", force=False):
    """Converge this node onto the current desired state for `pool`.

    Returns an objict-ish dict describing what happened. Raises InstallError on
    a failure that left `current` unchanged — which is every failure.
    """
    from objict import objict

    from mojo.apps.edge.rest.node import enabled_vhosts

    vhosts = enabled_vhosts(pool)
    payload = render.desired_state(vhosts)
    generation = payload["generation"]

    installed = read_installed()
    if not force and installed.get("generation") == generation:
        return objict(changed=False, generation=generation, reason="unchanged")

    os.makedirs(os.path.join(render.edge_root(), "generations"), exist_ok=True)
    previous = current_target()

    excluded = stage_generation(vhosts, generation)

    gen_dir = render.generation_dir(generation)
    ok, output = _nginx_check(os.path.join(gen_dir, "nginx.conf"))
    if not ok:
        _fail(generation, f"staged configuration failed nginx -t: {output}",
              reverted_to=previous)

    _symlink_swap(render.current_link(), gen_dir)

    # Nothing has reloaded yet, so the running configuration is still the old
    # one. This is the authoritative check — against the REAL nginx.conf.
    ok, output = _nginx_check()
    if not ok:
        if previous:
            _symlink_swap(render.current_link(), previous)
        else:
            try:
                os.unlink(render.current_link())
            except OSError:
                pass
        _fail(generation,
              f"generation failed nginx -t against the real config: {output}",
              reverted_to=previous)

    try:
        result = _run(_nginx_reload_argv())
        reload_ok = result.returncode == 0
        reload_output = f"{result.stdout or ''}{result.stderr or ''}"
    except Exception as err:
        reload_ok, reload_output = False, str(err)

    if not reload_ok:
        # The configuration validated, so `current` is left in place: nginx is
        # still serving the previous config and the next converge (or a manual
        # reload) picks this up. Reverting here would discard a good generation
        # over a transient systemd failure.
        _fail(generation, f"nginx reload failed: {reload_output}",
              reverted_to=None, revert_note="current left at the new generation")

    write_installed(generation, excluded)
    prune_generations()
    logit.info(
        f"edge: installed generation {generation} for pool {pool} "
        f"({len(vhosts) - len(excluded)} vhosts, {len(excluded)} excluded)")
    return objict(changed=True, generation=generation, excluded=excluded,
                  previous=previous)


def _fail(generation, message, reverted_to=None, revert_note=None):
    """Report an install failure as an incident, then raise.

    Always both: the raise makes the job show failed, and the incident is what
    a human actually sees. A node that silently fails to converge is the
    failure mode this whole design exists to prevent.
    """
    from mojo.apps.incident import reporter

    detail = message
    if revert_note:
        detail = f"{message} ({revert_note})"
    elif reverted_to:
        detail = f"{message} (reverted to {reverted_to})"

    logit.error(f"edge: {detail}")
    try:
        reporter.report_event(
            detail, title="Edge generation install failed",
            category="edge_install", level=7)
    except Exception:
        logit.exception("edge: failed to report an install failure")
    raise InstallError(detail)


# ----------------------------------------------------------------------
# Privilege boundary — read this before changing anything above
# ----------------------------------------------------------------------
#
# The installer runs as the APP USER in the job runner. Two operations need
# more than that: `nginx -t` and `systemctl reload nginx`. Both go through
# `_run` with a constant argv list built from settings, never from row data,
# behind a narrow sudoers rule.
#
# Writing that sudoers rule and the `/etc/nginx/conf.d/mojo.conf` include is
# django-mojo-skeleton work and is a cross-repo dependency of this file. It is
# also what bounds the risk this module introduces, so state it plainly:
#
#   The structured-model constraint (no free-text nginx) defends against a
#   malicious ADMIN. It does nothing about a compromised API PROCESS, which
#   now has a path to nginx configuration. What bounds THAT is the sudoers
#   narrowness plus the app user owning only EDGE_ROOT — not the renderer.
