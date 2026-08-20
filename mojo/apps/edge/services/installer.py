"""
The node-side installer: render a generation, validate it, swap it in, reload.

This is the only module in django-mojo that writes files nginx reads and asks
systemd to reload a service. Read the privilege note at the bottom before
changing it.

## Why the sequence looks the way it does

```
fetch desired state (from the DB — see below)
if generation == installed AND nothing is www_pending: return   idempotent no-op
fetch promoted release bytes from S3, verified per file (www_sync)
    unfetchable: degrade THAT vhost only (below), never the pool
render generations/<new>/, write certificate material 0600
    material unfetchable:  house vhost  -> abort
                           tenant vhost -> exclude it, report an incident
stage www/<vhost-id> links                              release, or the fallback
nginx -t -c generations/<new>/nginx.conf                cheap pre-filter
os.replace(current -> generations/<new>)                nothing has reloaded yet
nginx -t                                                against the REAL config
    fail, or "conflicting server name" on stderr -> revert current, incident, raise
    ok -> systemctl reload nginx, write installed.json, prune
```

**An unfetchable release degrades one vhost, and heals itself.** If the bytes
cannot be pulled, a vhost that was already serving keeps serving the EXACT
release it served before the promote (`current`'s own `www/<id>` target), and
one that never served is excluded — dark beats a live vhost pointing at
nothing. Either way the vhost and its desired version are recorded in
`installed.json` as `www_pending`, which **defeats the generation
short-circuit**: the next converge re-runs the fetch, and the one after a
transient S3 failure clears re-points the symlink at the real release with no
operator action. The incident is reported once, when the failure is new — not
every ten minutes for as long as it lasts.

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
block also lives in the real config, not in the generated set, so a collision
with it is invisible to the harness. The two defences that work are the Phase A
enabled-uniqueness constraint (row vs row, in the database) and the
`conflicting server name` stderr scan below (row vs a hand-written block).

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

from mojo.apps.edge.services import render, www_sync


class InstallError(Exception):
    """A generation could not be installed. `current` is unchanged."""


def keep_generations():
    return int(settings.get_static("EDGE_KEEP_GENERATIONS", 5))


def command_timeout():
    return int(settings.get_static("EDGE_COMMAND_TIMEOUT", 60))


# ----------------------------------------------------------------------
# the privileged boundary — the ONLY place this app shells out
# ----------------------------------------------------------------------

# Why there are TWO test commands, and why only one of them uses sudo.
#
# `nginx -t` processes `load_module` while parsing, and `dlopen()`s the named
# object with the privileges it is running under. So ANY sudoers rule of the
# shape `nginx -t -c <path the app user can write>` is a root escalation, not a
# config check: the installer writes `generations/<gen>/nginx.conf`, and one
# `load_module /tmp/evil.so;` line in it would run attacker code as root. A
# wildcard is unavoidable in such a rule, because the generation hash is in the
# path — so the rule cannot be narrowed into safety.
#
# The staged pre-filter runs unprivileged: every file it reads (the rendered
# trees, the staged certificates, the harness) is owned by the app user. What
# unprivileged does NOT survive is the binds — `nginx -t` attempts bind() on
# every listen it parses (only EADDRINUSE is tolerated in test mode; EACCES on
# 443/80 is a fatal [emerg]) — which is why the harness includes the staging/
# listen-remapped copies instead of the real trees (render_staged_variant).
# `-e stderr` keeps nginx from opening its compiled-in default error log,
# whose root-owned path otherwise leads every failure's output with a
# harmless-but-misleading permission alert.
#
# The authoritative check reads /etc/nginx/nginx.conf, which is root-owned, so
# it does need sudo — but it takes NO arguments, which leaves no injection
# surface and lets the sudoers entry be an exact command with no wildcard.
#
# Keeping them as separate settings is deliberate: it means neither can grow an
# argument later without someone editing this comment.

def _nginx_staged_test_argv(config_path):
    """Validate a staged generation. **Unprivileged** — see above."""
    argv = list(settings.get_static(
        "EDGE_NGINX_STAGED_TEST_CMD",
        ["nginx", "-e", "stderr", "-t", "-c"], kind="list"))
    return argv + [str(config_path)]


def _nginx_test_argv():
    """Validate the REAL config. Root, and deliberately argument-free.

    `get_static`, not `get`, and that is load-bearing. `settings.get` resolves
    a DB-backed `Setting` row FIRST (mojo/helpers/settings/helper.py), and
    `Setting` is REST-writable by any holder of a global `manage_settings` or
    `groups` grant. That would make this argv row data: write
    `EDGE_NGINX_TEST_CMD = ["/bin/sh","-c","..."]` globally, wait up to ten
    minutes for the convergence broadcast, and the command runs on EVERY node.
    Same precedent as `_return_real_error` in mojo/decorators/http.py.
    """
    return list(settings.get_static(
        "EDGE_NGINX_TEST_CMD", ["sudo", "-n", "nginx", "-t"], kind="list"))


def _nginx_reload_argv():
    return list(settings.get_static(
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
    """Run `nginx -t` and return (ok, combined output).

    A `config_path` selects the unprivileged staged check; without one this is
    the root check against the real config.
    """
    argv = (_nginx_staged_test_argv(config_path) if config_path
            else _nginx_test_argv())
    try:
        result = _run(argv)
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

def installed_path(pool="default"):
    """Per-pool convergence evidence path.

    The historical root ``installed.json`` remains a read-only fallback for
    the default pool. New writes always land under ``installed/<pool>.json``.
    """
    from mojo.apps.edge import validators
    pool = validators.validate_pool(pool)
    return os.path.join(render.edge_root(), "installed", f"{pool}.json")


def read_installed(pool="default"):
    try:
        with open(installed_path(pool)) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        if pool == "default":
            try:
                with open(os.path.join(render.edge_root(), "installed.json")) as handle:
                    return json.load(handle)
            except (OSError, ValueError):
                pass
        return {}


def pending_releases(installed=None):
    """{vhost id (str): desired version} the last install could not fetch.

    Named apart from `write_installed`'s `www_pending` argument so the reader
    and the JSON key are never confused for each other.
    """
    if installed is None:
        installed = read_installed()
    rows = installed.get("www_pending") or {}
    return {str(key): value for key, value in rows.items()}


def pending_certs(installed=None):
    """Vhost ids the last install excluded for unreadable key material.

    Same retry contract as `pending_releases`, for the other transient
    failure: KMS comes back, and nothing else about the desired state has to
    change for that vhost to install.
    """
    if installed is None:
        installed = read_installed()
    return [int(pk) for pk in (installed.get("cert_pending") or [])]


def write_installed(generation, excluded=None, www_pending=None,
                    cert_pending=None, pool="default", serving_generation=None):
    """Record what this node installed.

    `www_pending` is {vhost id: desired version} for vhosts whose release
    bytes could not be fetched; `cert_pending` is the vhost ids whose key
    material would not read. Both are written only when non-empty, so a
    healthy node's installed.json keeps exactly the shape it always had — and
    either one's presence is what makes the next converge retry instead of
    short-circuiting.
    """
    payload = dict(generation=generation, excluded=sorted(excluded or []))
    if serving_generation:
        payload["serving_generation"] = serving_generation
    if www_pending:
        # JSON object keys are strings either way; normalise here so a reader
        # never has to guess which side of a round-trip it is holding.
        payload["www_pending"] = {
            str(key): value for key, value in www_pending.items()}
    if cert_pending:
        payload["cert_pending"] = sorted(cert_pending)
    path = installed_path(pool)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as handle:
        json.dump(payload, handle)
    os.replace(tmp, path)
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


def _disabled_upstreams(vhost):
    """The disabled upstreams this vhost would proxy to, direct or via route."""
    rows = []
    if vhost.upstream_id and not vhost.upstream.is_enabled:
        rows.append(vhost.upstream)
    for route in vhost.routes.all():
        if not route.upstream.is_enabled:
            rows.append(route.upstream)
    return rows


def _report_incident(message, title, key=None):
    """Best-effort incident. An outage in the reporter never decides a converge.

    With `key`, the report repeats at most hourly instead of once per call.
    Reach for it wherever a condition PERSISTS across converges: the caller's
    own "have I reported this already" check stops the ten-minute repeat, but
    on its own it also means a vhost stuck on one bad release files exactly one
    incident ever and then looks healthy forever, which is the silent
    non-convergence this module exists to make loud.
    """
    from mojo.apps.incident import reporter

    try:
        if key is not None:
            # Never raises, by contract.
            reporter.report_event_suppressed(
                message, key, title=title, category="edge_install", level=6,
                window=3600)
            return
        reporter.report_event(
            message, title=title, category="edge_install", level=6)
    except Exception:
        # An incident-reporting failure must not decide whether a fleet
        # converges. The exclusion is already in installed.json.
        logit.exception("edge: failed to report a vhost incident")


def _exclude_or_abort(vhost, message, excluded, report=True, key=None,
                      allow_house=False):
    """The one fork for a vhost that cannot be staged.

    House vhost -> abort the install (the platform's own serving path;
    converging without it is not a partial success). Tenant vhost -> exclude
    it and report an incident, so one tenant's broken row cannot freeze the
    fleet.

    **`allow_house=True` degrades a house vhost instead of aborting**, and the
    REASON is what earns it, not the owner. A missing release is one vhost's
    content problem however that vhost is owned; a house vhost carrying a
    webapp is ordinary (a portal served by the platform's own group), not a
    statement that the platform cannot serve. Without this the ownership fork
    silently decides a content failure: point webapps at house-owned vhosts and
    every first-promote fetch failure aborts the pool — the fleet-wide freeze
    the fetch degrade exists to remove, reintroduced for every webapp. Genuine
    platform config errors (unreadable certificate material, a retired
    upstream) keep the abort.

    `report=False` is for a failure the previous install already reported —
    a fetch that is still pending is retried every converge, and one incident
    per ten minutes for one broken release is noise, not signal. It picks the
    log level; `key` (a persisting condition) hands the repeat decision to the
    suppressed reporter, which re-notifies hourly rather than never. The
    exclusion itself is unconditional.
    """
    if vhost.domain.group_id is None and not allow_house:
        raise InstallError(f"{message} — it serves a platform vhost")
    if report:
        logit.error(message)
    else:
        logit.info(f"{message} (already reported; still retrying)")
    if report or key is not None:
        _report_incident(
            message, "Vhost excluded from an edge generation", key=key)
    excluded.append(vhost.pk)


def _previous_web_root(previous, vhost_id):
    """The release directory this vhost is serving RIGHT NOW, or None.

    Read off the live generation's own `www/<id>` symlink rather than
    recomputed from the desired state, because those are different answers
    precisely when it matters: after a promote whose bytes did not arrive, the
    desired version has no directory and the served one does. Containment
    under the vhost's `releases/` tree is re-checked here — this value becomes
    a symlink target in the next generation.
    """
    from mojo.apps.edge import validators

    if not previous:
        return None
    link = os.path.join(previous, "www", str(int(vhost_id)))
    if not os.path.islink(link):
        return None
    try:
        resolved = os.path.realpath(link)
    except OSError:
        return None
    if not os.path.isdir(resolved):
        return None
    base = os.path.join(
        validators.www_base(), str(int(vhost_id)), "releases")
    if not validators.contained_under(base, resolved):
        return None
    return resolved


def stage_generation(vhosts, generation, webapps=None, fetch_failures=None,
                     previous=None, pool="default", http=None):
    """Build `generations/<generation>/` completely.

    Returns `objict(excluded, cert_excluded)`: every vhost left out of the
    generation, and the subset left out because its key material would not
    read. The second list is what makes a certificate failure retryable —
    `install()` records it so the next converge re-stages instead of
    short-circuiting on an unchanged generation.

    Raises InstallError when a HOUSE vhost cannot be staged for a PLATFORM
    reason (unreadable material, a retired upstream): that is the platform's
    own serving path, and converging without it is not a partial success. A
    missing release is not such a reason — see `_exclude_or_abort`.

    `fetch_failures` is {vhost id: message} from `www_sync.fetch_webapps`, and
    `previous` is what `current` pointed at before this install. Together they
    decide the degrade: a vhost that was already serving keeps its bytes, one
    that never served is excluded.
    """
    from objict import objict

    gen_dir = render.generation_dir(generation)
    os.makedirs(os.path.join(gen_dir, "conf.d"), exist_ok=True)
    os.makedirs(os.path.join(gen_dir, "http.d"), exist_ok=True)
    os.makedirs(os.path.join(gen_dir, "www"), exist_ok=True)
    # The staged check runs unprivileged, so nginx needs writable scratch
    # paths inside the generation — the harness names these.
    from mojo.deploy.nginx_runtime import TEMP_PATHS
    for _directive, leaf in TEMP_PATHS:
        os.makedirs(os.path.join(gen_dir, "tmp", leaf), exist_ok=True)
    # The base's access_log (and the stage-4 watch log) point here, and
    # `nginx -t` opens log files while validating — the directory has to
    # exist before the staged check runs.
    os.makedirs(render.log_dir(), exist_ok=True)

    fetch_failures = fetch_failures or {}
    by_vhost = {row["vhost"]: row for row in (webapps or [])}
    # What the LAST install already reported as unfetchable. A retry that is
    # still failing logs; only a new failure raises an incident.
    installed = read_installed(pool)
    reported = pending_releases(installed)
    reported_certs = set(pending_certs(installed))

    installable = []
    excluded = []
    cert_excluded = []
    fallbacks = {}
    for vhost in vhosts:
        disabled = _disabled_upstreams(vhost)
        if disabled:
            names = ", ".join(sorted(row.name for row in disabled))
            _exclude_or_abort(
                vhost,
                f"edge: {vhost.server_name} proxies to retired upstream(s) "
                f"{names}", excluded)
            continue

        failure = fetch_failures.get(vhost.pk)
        if failure:
            row = by_vhost.get(vhost.pk) or {}
            version = (row.get("release") or {}).get("version")
            is_new = reported.get(str(vhost.pk)) != version
            # Keyed per vhost+version: the same broken release stays one
            # incident an hour however often the converge retries it, and a
            # NEW version failing reports immediately.
            key = f"webapp-fetch:{vhost.pk}:{version}"
            fallback = _previous_web_root(previous, vhost.pk)
            if fallback is None:
                # It has never served anything, so there is nothing to keep
                # serving. Dark beats a live vhost pointing at nothing —
                # `allow_house` because that is true of a house-owned vhost
                # too, and aborting the pool over one vhost's missing content
                # is the freeze this whole path exists to prevent.
                _exclude_or_abort(
                    vhost, f"edge: {failure}", excluded, report=is_new,
                    key=key, allow_house=True)
                continue
            fallbacks[vhost.pk] = fallback
            message = (f"edge: {failure} — {vhost.server_name} is serving its "
                       f"previous release from {fallback}")
            if is_new:
                logit.error(message)
            else:
                logit.info(f"{message} (already reported; still retrying)")
            _report_incident(
                message,
                "Edge release fetch failed; serving the previous release",
                key=key)

        if _write_material(generation, vhost.certificate):
            installable.append(vhost)
            continue

        # Unreadable key material is usually transient (KMS), so this vhost is
        # recorded as retryable the same way an unfetched release is: the next
        # converge tries again instead of leaving it excluded until something
        # unrelated moves the generation.
        cert_excluded.append(vhost.pk)
        _exclude_or_abort(
            vhost,
            f"edge: certificate {vhost.certificate_id} for "
            f"{vhost.server_name} has no readable material (KMS?)", excluded,
            report=vhost.pk not in reported_certs,
            key=f"cert-material:{vhost.pk}:{vhost.certificate_id}")

    files = render.render_generation(installable, generation, knobs=http)
    for name, text in files.items():
        # Two copies of every rendered file: the real tree (what `current`
        # serves after the swap) and the staging/ listen-remapped copy the
        # unprivileged pre-filter validates. The staged HTTP base alone owns
        # the rewritten scratch directives — never the main harness too.
        for target, body in ((name, text),
                             (f"staging/{name}",
                              render.render_staged_variant(
                                  text,
                                  temp_root=os.path.join(gen_dir, "tmp")
                                  if name == "http.d/00_base.conf" else None))):
            path = os.path.join(gen_dir, target)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as handle:
                handle.write(body)

    with open(os.path.join(gen_dir, "nginx.conf"), "w") as handle:
        handle.write(render.render_nginx_harness(generation))

    stage_web_roots(generation, installable, webapps or [], fallbacks)
    return objict(excluded=excluded, cert_excluded=cert_excluded)


def release_dir(vhost_id, version):
    """Where a release's files live, OUTSIDE any generation.

    Retained across generations on purpose: that is what makes a rollback a
    symlink flip rather than a re-download.
    """
    from mojo.apps.edge import validators

    validators.validate_release_version(version)
    return os.path.join(validators.www_base(), str(int(vhost_id)),
                        "releases", version)


def _placeholder_page(server_name):
    """The page a release-less vhost serves: live, secure, nothing deployed.

    Fully self-contained static HTML — inline styles, no scripts, no external
    assets — and the only dynamic value (the vhost's server_name) is
    html-escaped, never trusted as markup.
    """
    import html

    name = html.escape(str(server_name or "").strip(), quote=True)
    heading = name or "This address"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{heading} is live</title>
<style>
body {{ margin: 0; font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
       background: #f5f6f8; color: #1f2933; display: flex; min-height: 100vh;
       align-items: center; justify-content: center; }}
main {{ max-width: 26rem; padding: 2rem; text-align: center; }}
h1 {{ font-size: 1.25rem; margin: 0 0 0.75rem; }}
p {{ margin: 0.5rem 0; color: #52606d; line-height: 1.5; }}
.ok {{ color: #14803c; font-weight: 600; }}
</style>
</head>
<body>
<main>
<h1>{heading} is live</h1>
<p class="ok">HTTPS is working.</p>
<p>Nothing is deployed here yet.</p>
<p>Deploy your first release from the Admin portal.</p>
</main>
</body>
</html>
"""


def stage_web_roots(generation, vhosts, webapps, fallbacks=None):
    """Point every file-serving vhost's web root at its installed release.

    **The pointer lives INSIDE the generation.** An earlier design put a
    `current` symlink next to the release, outside the atomic swap — so a
    failed `nginx -t` abandoned the config change but left the content already
    moved, and the node served a new bundle under old config: a state neither
    generation describes. Staging it here means one `os.replace` of `current`
    swaps configuration and content together, and a rollback reverts both.

    `fallbacks` names the vhosts whose desired release could not be fetched
    but which were already serving something — they get their PREVIOUS release
    directory, not the desired one.
    """
    by_vhost = {row["vhost"]: row for row in webapps}
    fallbacks = fallbacks or {}

    for vhost in vhosts:
        if vhost.kind not in ("site", "site_api"):
            continue

        link = render.www_dir(generation, vhost.pk)
        fallback = fallbacks.get(vhost.pk)
        if fallback:
            # Deliberately NOT the desired release: those bytes are not on
            # disk, and this vhost keeps serving exactly what it served before
            # the promote until a later converge fetches them.
            _symlink_swap(link, fallback)
            continue

        row = by_vhost.get(vhost.pk)
        if row is None:
            # No release yet. A real directory (never a release symlink) with
            # one static placeholder page: the address is live and HTTPS
            # works, which beats both a 404 and a 500, and it tells the owner
            # what happens next. Written inside the generation, so the same
            # atomic swap that would replace it with a release also governs it.
            os.makedirs(link, exist_ok=True)
            with open(os.path.join(link, "index.html"), "w") as handle:
                handle.write(_placeholder_page(
                    getattr(vhost, "server_name", "")))
            continue

        target = release_dir(vhost.pk, row["release"]["version"])
        if not os.path.isdir(target):
            # The safety net BEHIND the fetch, not the fetch's error path —
            # www_sync already degraded anything it could not pull, so
            # reaching here means the directory went missing some other way
            # (a hand-deleted release, a full disk mid-install). Retained
            # deliberately: a live vhost pointing at nothing is worse than a
            # refused generation.
            raise InstallError(
                f"release {row['release']['version']} for vhost {vhost.pk} "
                f"is not on disk at {target}")
        _symlink_swap(link, target)


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
    # Guarded: prune runs after the reload and after installed.json is
    # written, so a directory vanishing under us here would fail a job whose
    # install actually succeeded.
    entries.sort(key=www_sync.safe_mtime, reverse=True)
    removed = []
    # `keep` counts the LIVE generation too, so one slot is already spoken for.
    for path in entries[max(keep - 1, 0):]:
        shutil.rmtree(path, ignore_errors=True)
        removed.append(path)
    return removed


# ----------------------------------------------------------------------
# the install
# ----------------------------------------------------------------------

def install(pool="default", force=False, pools=None):
    """Converge this node onto the current desired state for `pool`.

    Returns an objict-ish dict describing what happened. Raises InstallError on
    a failure that left `current` unchanged — which is every failure.
    """
    from objict import objict

    from mojo.apps.edge import validators
    from mojo.apps.edge.rest.node import enabled_vhosts

    from mojo.apps.edge.services import releases

    selected_pools = pools if pools is not None else [pool]
    selected_pools = sorted({validators.validate_pool(item) for item in selected_pools})
    if not selected_pools:
        raise InstallError("at least one edge pool must be assigned to this node")
    pool_vhosts = {item: enabled_vhosts(item) for item in selected_pools}
    vhosts = [vhost for item in selected_pools for vhost in pool_vhosts[item]]
    # Built exactly as the REST endpoint builds it — same two calls, same
    # order. If these ever diverge, a node installs one thing while the fleet's
    # desired-state answer describes another.
    webapps = releases.desired_webapps(vhosts)
    payload = render.desired_state(vhosts, webapps=webapps)
    generation = payload["generation"]
    pool_generations = {}
    installed_by_pool = {}
    for item in selected_pools:
        item_vhosts = pool_vhosts[item]
        item_webapps = releases.desired_webapps(item_vhosts)
        pool_generations[item] = render.desired_state(
            item_vhosts, webapps=item_webapps)["generation"]
        installed_by_pool[item] = read_installed(item)
    installed = installed_by_pool[selected_pools[0]]
    pending = any(pending_releases(row) for row in installed_by_pool.values())
    cert_pending = any(pending_certs(row) for row in installed_by_pool.values())
    live = current_target()
    live_generation = live.rsplit("/", 1)[-1] if live else None
    # A recorded failure DEFEATS the short-circuit on purpose: a node that
    # could not fetch a release, or could not read key material, is degraded,
    # and the only thing that heals either is running the install again. Both
    # causes are transient (S3, KMS) and neither changes the desired state, so
    # without this a vhost stays degraded until something unrelated moves the
    # generation. A healthy node still does zero S3 work on an unchanged
    # generation, which is what keeps the ten-minute sweep cheap.
    evidence_current = all(
        installed_by_pool[item].get("generation") == pool_generations[item]
        and installed_by_pool[item].get("serving_generation") == generation
        for item in selected_pools)
    if (not force and evidence_current and live_generation == generation
            and not pending and not cert_pending):
        return objict(changed=False, generation=generation, reason="unchanged",
                      pools=selected_pools, pool_generations=pool_generations)

    os.makedirs(os.path.join(render.edge_root(), "generations"), exist_ok=True)
    previous = live

    # Pull the promoted bytes BEFORE staging, so the web-root links point at
    # releases that are verified on disk. Never raises: one unfetchable
    # release degrades its own vhost and nothing else.
    fetch_failures = www_sync.fetch_webapps(webapps)

    staged = stage_generation(vhosts, generation, webapps,
                              fetch_failures=fetch_failures,
                              previous=previous, pool=selected_pools[0],
                              http=payload["http"])
    excluded = staged.excluded

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

    # Every fetch-failed vhost, whether it is serving stale bytes or is dark.
    # This is what makes the next converge retry rather than short-circuit.
    still_pending = {
        str(row["vhost"]): row["release"]["version"]
        for row in webapps if row["vhost"] in fetch_failures
    }
    vhost_pools = {row.pk: row.pool for row in vhosts}
    for item in selected_pools:
        item_excluded = [
            pk for pk in excluded if vhost_pools.get(pk) == item]
        item_www_pending = {
            pk: version for pk, version in still_pending.items()
            if vhost_pools.get(int(pk)) == item}
        item_cert_pending = [
            pk for pk in staged.cert_excluded if vhost_pools.get(pk) == item]
        write_installed(
            pool_generations[item], item_excluded,
            www_pending=item_www_pending, cert_pending=item_cert_pending,
            pool=item, serving_generation=generation)
    prune_generations()
    www_sync.prune_releases(webapps)
    logit.info(
        f"edge: installed combined generation {generation} for pools "
        f"{','.join(selected_pools)} "
        f"({len(vhosts) - len(excluded)} vhosts, {len(excluded)} excluded, "
        f"{len(still_pending)} awaiting release bytes, "
        f"{len(staged.cert_excluded)} awaiting key material)")
    return objict(changed=True, generation=generation, excluded=excluded,
                  previous=previous, www_pending=still_pending,
                  cert_pending=staged.cert_excluded, pools=selected_pools,
                  pool_generations=pool_generations)


def install_pools(pools, force=False):
    """Atomically serve the union assigned to this node in one generation."""
    return install(force=force, pools=pools)


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
# Writing that sudoers rule and the `/etc/nginx/nginx.conf` bootstrap (the
# ~12-line form with the current/ includes — see
# docs/django_developer/edge/templates.md) is django-mojo-skeleton work and is
# a cross-repo dependency of this file. It is also what bounds the risk this
# module introduces, so state it plainly:
#
#   The structured-model constraint (no free-text nginx) defends against a
#   malicious ADMIN. It does nothing about a compromised API PROCESS, which
#   now has a path to nginx configuration. What bounds THAT is the sudoers
#   narrowness plus the app user owning only EDGE_ROOT — not the renderer.
