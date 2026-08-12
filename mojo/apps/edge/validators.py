"""
Every value that can reach a generated nginx file passes through here first.

The design rule this app is built on: an admin never types nginx syntax, and no
operator-controllable string is ever escaped into config. Values are either
**derived from a foreign key** (so ownership decides them) or **matched against
a whitelist regex** (so they cannot carry a metacharacter at all). There is no
third category, and there is deliberately no escaping helper — an escape
function is an invitation to route a new field around this module.

Read `mojo/apps/edge/services/render.py` alongside this: the renderer re-asserts
every check here at the point of substitution. That duplication is intentional.
A service-layer write that skips `save()` still cannot put a `;` into a file.
"""

import ipaddress
import os
import re

from mojo import errors as me
from mojo.helpers.settings import settings


# NOTE ON `\Z`, and why it is not `$`.
#
# In Python `$` matches at the end of the string OR just before a trailing
# newline. Every regex here was written with `$` and every one of them accepted
# a trailing "\n" — `LABEL_RE.match("www\n")` was True, which is a newline
# injected straight into an nginx config file. `\Z` is the real end of string.
# A test caught this on the version field; the same bug was present in all six.
#
# A single DNS label. No dots (a label cannot climb into another zone), and by
# construction no `;`, `{`, `}`, `#`, `$`, quote, space, CR or LF.
LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\Z")

# Upstream hosts. No scheme, no path, no port smuggling, no newline. IPv6
# literals are NOT supported in v1 — they would need bracketing in the rendered
# config, and every real upstream so far is a hostname, an IPv4 address, or a
# unix socket. Widening this is a deliberate change with a renderer change
# beside it, not a regex tweak.
UPSTREAM_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?\Z")

# Upstream / vhost names used as filename components.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}\Z")

# A request-path a vhost may quiet or route. Leading slash, then a charset
# with no `;`, `{`, `}`, `#`, `$`, quote, backslash, space, CR or LF — the
# same "cannot carry a metacharacter at all" rule as every other whitelist
# here. `..` segments and `//` runs are refused separately below: the charset
# admits `.` and `/` individually, and spelling the traversal check out keeps
# it reviewable.
QUIET_PATH_RE = re.compile(r"^/[A-Za-z0-9._/\-]{0,127}\Z")
ROUTE_PREFIX_RE = QUIET_PATH_RE

# The TLS floor values. Settings, not row data — but `settings.get` resolves a
# DB-backed Setting row first, and these land verbatim inside `ssl_protocols`
# / `ssl_ciphers` directives, so the renderer re-asserts them like everything
# else. The charset covers every real protocol and OpenSSL cipher-string
# token (`TLSv1.3`, `ECDHE-...`, `:`, `!aNULL`, `+`, `_`) and cannot carry
# `;`, `{`, `}`, `#`, quotes, or a control character.
TLS_VALUE_RE = re.compile(r"^[A-Za-z0-9 .:+!_-]{1,1024}\Z")

# A user-agent blocklist pattern. Rendered ONLY inside double quotes as
# `"~*<value>" <id>;` in a map block, so the whitelist must make breaking out
# of that quoted string unspellable: no `"`, no backtick, no `{`/`}`/`;`, no
# whitespace, no control characters, no newline — while still permitting the
# regex metacharacters real bot patterns use (`( ) [ ] | ? ^ . * + -`, `/`,
# `_`, and backslash for escapes like `\.`). `$` is deliberately absent:
# nginx expands `$name` inside quoted strings in enough contexts that an
# end-anchor is not worth the ambiguity — anchor with `^` or match a literal.
UA_PATTERN_RE = re.compile(r"^[A-Za-z0-9()\[\]|?^.*+\\/_-]{1,256}\Z")


def www_base():
    # get_static throughout this pair of helpers: these define the containment
    # boundaries themselves, and a boundary a DB row can move is not a
    # boundary. See services/installer.py::_nginx_test_argv.
    return settings.get_static("EDGE_WWW_BASE", "/opt/www")


def socket_base():
    return settings.get_static("EDGE_SOCKET_BASE", "/run/mojo")


# ----------------------------------------------------------------------
# server names
# ----------------------------------------------------------------------

def validate_label(label):
    """A vhost's subdomain label: "" (apex), "*" (wildcard), or one DNS label."""
    if label in ("", "*"):
        return label
    if not isinstance(label, str) or not LABEL_RE.match(label):
        raise me.ValueException(
            "label must be empty (apex), '*', or a single DNS label "
            "of letters, digits and hyphens")
    return label


def server_name_for(domain_name, label):
    """The vhost's server_name, DERIVED — never stored as free text.

    Ownership of the name is the `Domain` FK, and `Domain` is tenant-scoped, so
    a caller cannot claim a name under a zone they do not hold. This is why the
    workspec's canonical injection case is unreachable rather than filtered:
    `example.com; } server { ...` is not a valid `Domain.name` (that model
    normalises to lowercase IDNA) and is not a valid label.
    """
    if not domain_name:
        raise me.ValueException("a vhost requires a domain")
    if label == "":
        return domain_name
    return f"{label}.{domain_name}"


def validate_server_name(server_name):
    """Every label of a derived server name, checked.

    **Do not assume `Domain.name` is already safe.** It is normalised in
    `Domain.on_rest_pre_save` — the REST path only — so a row created through
    the ORM, a service, a data migration or a shell can hold anything the
    column's `max_length` allows, including nginx syntax. Scoping this item
    asserted the opposite and was wrong; a test caught it.

    So the vhost layer validates the WHOLE derived name rather than trusting
    the domain half of it.
    """
    if not isinstance(server_name, str) or not server_name:
        raise me.ValueException("a vhost requires a server name")
    if len(server_name) > 253:
        raise me.ValueException("server name is too long")
    candidate = server_name
    if candidate.startswith("*."):
        candidate = candidate[2:]
    parts = candidate.split(".")
    if len(parts) < 2:
        raise me.ValueException(
            f"{server_name} is not a fully qualified domain name")
    for part in parts:
        if not LABEL_RE.match(part):
            raise me.ValueException(
                f"{server_name} is not a valid server name")
    return server_name


# ----------------------------------------------------------------------
# upstreams
# ----------------------------------------------------------------------

def validate_upstream_host(host):
    """A proxy destination's host.

    Loopback and RFC1918 are ALLOWED here, deliberately: the real upstream on
    every node is 127.0.0.1:<asgi port>, and internal services legitimately sit
    on private addresses. What stops a tenant reaching them is not this
    function — it is that only a platform admin may create an `Upstream` row at
    all (see mojo/apps/edge/rest/upstream.py). A blanket RFC1918 ban would
    forbid the primary use case while adding nothing, because a tenant never
    supplies one of these values.

    Link-local IS refused for everybody, including a platform admin: there is
    no legitimate reverse proxy to a cloud instance-metadata service.
    """
    if not isinstance(host, str) or not UPSTREAM_HOST_RE.match(host):
        raise me.ValueException(
            "upstream host must be a hostname or IPv4 address "
            "(letters, digits, dots and hyphens only)")
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return host
    if addr.is_link_local:
        # 169.254.0.0/16 covers 169.254.169.254 — every cloud's IMDS.
        raise me.ValueException(
            "link-local addresses cannot be an upstream")
    if str(addr) == "fd00:ec2::254":
        raise me.ValueException(
            "the instance metadata service cannot be an upstream")
    return host


def validate_upstream_port(port):
    if not isinstance(port, int) or isinstance(port, bool):
        raise me.ValueException("upstream port must be an integer")
    if not 1 <= port <= 65535:
        raise me.ValueException("upstream port must be between 1 and 65535")
    return port


def contained_under(base, candidate):
    """True when `candidate` normalises to a path under `base`.

    `os.path.commonpath` on realpath'd absolute paths — NOT `startswith`, which
    treats /opt/wwwevil as living under /opt/www, and NOT a `..` scan, which
    misses symlinks.
    """
    base_real = os.path.realpath(base)
    cand_real = os.path.realpath(candidate)
    try:
        return os.path.commonpath([base_real, cand_real]) == base_real
    except ValueError:
        # Different drives / one relative — cannot be contained.
        return False


def validate_socket_path(path):
    base = socket_base()
    if not isinstance(path, str) or not path:
        raise me.ValueException("a unix upstream requires a socket path")
    if "\n" in path or "\r" in path or ";" in path or "\x00" in path:
        raise me.ValueException("socket path contains an illegal character")
    if not contained_under(base, path):
        raise me.ValueException(
            f"socket path must resolve under {base}")
    return path


def validate_upstream(upstream):
    """Whole-row validation for an Upstream. Called from save()."""
    from mojo.apps.edge.models.upstream import KIND_HTTP, KIND_UNIX

    if not upstream.name or not NAME_RE.match(upstream.name):
        raise me.ValueException(
            "upstream name must be lowercase letters, digits, '-' or '_'")
    if upstream.kind == KIND_HTTP:
        if upstream.socket_path:
            raise me.ValueException("an http upstream has no socket path")
        validate_upstream_host(upstream.host or "")
        validate_upstream_port(upstream.port)
    elif upstream.kind == KIND_UNIX:
        if upstream.host or upstream.port:
            raise me.ValueException("a unix upstream has no host or port")
        validate_socket_path(upstream.socket_path or "")
    else:
        raise me.ValueException(f"unknown upstream kind {upstream.kind!r}")
    return upstream


# ----------------------------------------------------------------------
# certificates
# ----------------------------------------------------------------------

def certificate_covers(certificate, server_name):
    """Whether this certificate's CN or SANs cover `server_name`.

    A wildcard covers exactly one label — `*.example.com` matches
    `www.example.com` and NOT `example.com` or `a.b.example.com`, which is the
    rule TLS clients apply.
    """
    names = []
    if certificate.common_name:
        names.append(certificate.common_name)
    for san in (certificate.sans or []):
        if isinstance(san, str):
            names.append(san)
    target = server_name.lower()
    for name in names:
        candidate = name.lower()
        if candidate == target:
            return True
        if candidate.startswith("*."):
            suffix = candidate[1:]          # ".example.com"
            if target.endswith(suffix):
                remainder = target[: -len(suffix)]
                if remainder and "." not in remainder:
                    return True
            # A wildcard vhost (`*.example.com`) is covered by the identical
            # wildcard certificate; the label-count rule above cannot express
            # that, so match it directly.
            if target == candidate:
                return True
    return False


def validate_certificate_covers(certificate, domain_id, server_name):
    """The certificate must belong to this domain AND cover this name.

    Without the ownership half, a tenant could attach any certificate they can
    see to any vhost they own and make a node serve it — which is how you get a
    valid TLS session for a name whose key you were never issued.
    """
    if certificate is None:
        raise me.ValueException("a vhost requires a certificate")
    if certificate.domain_id != domain_id:
        raise me.ValueException(
            "the certificate must belong to this vhost's domain")
    if not certificate_covers(certificate, server_name):
        raise me.ValueException(
            f"certificate {certificate.common_name} does not cover {server_name}")
    return certificate


# ----------------------------------------------------------------------
# vhosts
# ----------------------------------------------------------------------

def declared_pools():
    return settings.get("EDGE_POOLS", ["default"], kind="list") or ["default"]


def validate_pool(pool):
    """A vhost's pool must be one this deployment declared.

    Charset alone is not enough (post-build security review): `pool` is
    tenant-writable, and an arbitrary value lets a tenant move their vhost into
    a pool name they invented — including a dedicated or isolated pool, whose
    nodes would then fetch and install that tenant's certificate. Restricting
    to the declared set keeps the pool an operator's decision.
    """
    if not pool or not NAME_RE.match(pool):
        raise me.ValueException(
            "pool must be lowercase letters, digits, '-' or '_'")
    allowed = declared_pools()
    if pool not in allowed:
        raise me.ValueException(
            f"{pool} is not a declared pool ({', '.join(sorted(allowed))})")
    return pool


# ----------------------------------------------------------------------
# web apps and releases
# ----------------------------------------------------------------------

# A build id or git SHA. No slashes, no dots-only, nothing that could climb an
# S3 prefix — this string becomes part of an object key.
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")

# A path INSIDE a release bundle. Relative, forward slashes only. ``!`` and
# ``~`` are ordinary POSIX/S3 filename characters emitted by Next.js static
# exports for route segments and Turbopack chunks; neither can traverse.
RELEASE_PATH_RE = re.compile(r"^[A-Za-z0-9._!~-]+(/[A-Za-z0-9._!~-]+)*\Z")

SHA256_RE = re.compile(r"^[a-f0-9]{64}\Z")
GITHUB_REPOSITORY_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9])?/"
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9])?\Z")
DEPLOYMENT_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}\Z")
BUILD_OUTPUT_RE = re.compile(
    r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*\Z")
DISPLAY_NAME_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,120}\Z")


def release_buckets():
    """Buckets a release may be uploaded to. Fail closed when undeclared.

    The API signs uploads with the platform's own AWS credentials, so a
    caller-chosen bucket would mean signing writes into anything those
    credentials can reach.
    """
    return settings.get("EDGE_RELEASE_BUCKETS", [], kind="list") or []


def validate_release_bucket(bucket):
    allowed = release_buckets()
    if not allowed:
        raise me.ValueException(
            "No release buckets are declared (EDGE_RELEASE_BUCKETS); "
            "refusing to register a web app")
    if bucket not in allowed:
        raise me.ValueException(f"{bucket} is not a declared release bucket")
    return bucket


def validate_release_version(version):
    if not isinstance(version, str) or not VERSION_RE.match(version):
        raise me.ValueException(
            "version must be letters, digits, '.', '_' or '-'")
    if version in (".", ".."):
        raise me.ValueException("version is not a usable name")
    return version


def validate_web_app(web_app):
    if not web_app.slug or not NAME_RE.match(web_app.slug):
        raise me.ValueException(
            "slug must be lowercase letters, digits, '-' or '_'")
    validate_release_bucket(web_app.bucket or "")
    if not web_app.group_id:
        raise me.ValueException("a web app requires a group")
    if web_app.display_name and not DISPLAY_NAME_RE.match(web_app.display_name):
        raise me.ValueException(
            "display name must be 1-120 printable characters")
    if web_app.environment not in (
            "production", "staging", "preview", "development"):
        raise me.ValueException("unknown WebApp environment")
    if web_app.github_repository:
        validate_github_repository(web_app.github_repository)
    validate_deployment_ref(web_app.deployment_ref)
    validate_build_output(web_app.build_output)

    # The linked vhost must belong to this web app's group, or to a group
    # ABOVE it. Found by the post-build security review: `vhost` is
    # caller-writable, and the framework's FK-attach gate resolves a HOUSE
    # vhost (group-less domain) against the caller's global permissions — so a
    # global manage_dns holder could attach the platform's own vhost to a web
    # app in a group they control, promote a release, and serve their own
    # content on the platform's hostname. The same check stops one group
    # serving its build output on an unrelated group's name.
    #
    # The ancestor allowance is what lets one domain carry several teams: a
    # parent group owns `example.com` and its wildcard certificate, and each
    # team's web app sits in its own child group. Siblings still cannot reach
    # each other — neither is above the other — so teams stay isolated without
    # a domain record and a certificate per team. A house domain is nobody's
    # ancestor (group_id is None matches nothing), so the hijack above stays
    # refused.
    if web_app.vhost_id:
        vhost_group_id = web_app.vhost.domain.group_id
        if not _group_at_or_below(web_app.group, vhost_group_id):
            raise me.ValueException(
                "the vhost must belong to this web app's group or a parent "
                "of it")
    return web_app


def validate_github_repository(value):
    if not isinstance(value, str) or not GITHUB_REPOSITORY_RE.match(value):
        raise me.ValueException(
            "GitHub repository must be an OWNER/REPOSITORY identifier")
    if any(part in (".", "..") for part in value.split("/")):
        raise me.ValueException("GitHub repository contains an unusable name")
    return value


def validate_deployment_ref(value):
    if not isinstance(value, str) or not DEPLOYMENT_REF_RE.match(value):
        raise me.ValueException("deployment ref contains an illegal character")
    if value.startswith("/") or value.endswith("/") or "//" in value:
        raise me.ValueException("deployment ref is not usable")
    if any(part in (".", "..") for part in value.split("/")):
        raise me.ValueException("deployment ref contains an unusable segment")
    return value


def validate_build_output(value):
    if not isinstance(value, str) or not BUILD_OUTPUT_RE.match(value):
        raise me.ValueException(
            "build output must be a relative directory using safe characters")
    if any(part in (".", "..") for part in value.split("/")):
        raise me.ValueException("build output cannot traverse directories")
    return value


def _group_at_or_below(group, owner_group_id):
    """True when `owner_group_id` is `group` itself or one of its ancestors.

    Walks the chain here, depth-capped, rather than calling
    `Group.get_parents()`: that helper has no cycle guard, and this runs on
    every WebApp save. The cap matches `get_member_for_user`'s.
    """
    if owner_group_id is None or group is None:
        return False
    if owner_group_id == group.pk:
        return True
    current = group.parent
    depth = 0
    while current is not None and depth < 8:
        if current.pk == owner_group_id:
            return True
        current = current.parent
        depth += 1
    return False


def validate_manifest(manifest):
    """Check a CI-declared manifest before a single upload URL is minted.

    Each entry becomes an S3 object key and a presigned URL, so an unvalidated
    `path` is a write primitive pointed at the platform's own credentials.
    """
    max_files = int(settings.get("EDGE_RELEASE_MAX_FILES", 5000))
    max_bytes = int(settings.get("EDGE_RELEASE_MAX_BYTES", 1073741824))

    if not isinstance(manifest, list) or not manifest:
        raise me.ValueException("manifest must be a non-empty list")
    if len(manifest) > max_files:
        raise me.ValueException(
            f"manifest has {len(manifest)} files; the limit is {max_files}")

    seen = set()
    cleaned = []
    for entry in manifest:
        if not isinstance(entry, dict):
            raise me.ValueException("each manifest entry must be an object")
        path = entry.get("path")
        sha256 = (entry.get("sha256") or "").lower()
        size = entry.get("size")

        if not isinstance(path, str) or not RELEASE_PATH_RE.match(path):
            raise me.ValueException(f"illegal path in manifest: {path!r}")
        # RELEASE_PATH_RE cannot match a leading slash or an empty segment, but
        # it CAN match ".." as a whole segment — spell that out rather than
        # relying on a reader to notice.
        if any(part in (".", "..") for part in path.split("/")):
            raise me.ValueException(f"illegal path in manifest: {path!r}")
        if path in seen:
            raise me.ValueException(f"duplicate path in manifest: {path!r}")
        seen.add(path)

        if not SHA256_RE.match(sha256):
            raise me.ValueException(
                f"manifest entry {path!r} needs a hex sha256")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise me.ValueException(
                f"manifest entry {path!r} needs a non-negative integer size")

        cleaned.append(dict(path=path, sha256=sha256, size=size))

    # A file count cap does not bound bytes: 5000 entries can still declare a
    # terabyte. Every node in the pool fetches a promoted release onto its own
    # disk, so an unbounded release is a fleet-wide disk exhaustion — checked
    # here rather than only at fetch time so CI learns at register, and so a
    # node re-validating a row it did not mint gets the same answer.
    total = sum(entry["size"] for entry in cleaned)
    if total > max_bytes:
        raise me.ValueException(
            f"manifest declares {total} bytes; the limit is {max_bytes}")
    return cleaned


def validate_body_size_mb(value):
    """client_max_body_size, in megabytes. Always rendered, so always checked."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise me.ValueException("body_size_mb must be an integer")
    if not 1 <= value <= 4096:
        raise me.ValueException("body_size_mb must be between 1 and 4096")
    return value


def _validate_request_path(path, what):
    if not isinstance(path, str) or not QUIET_PATH_RE.match(path):
        raise me.ValueException(
            f"{what} must start with '/' and use only letters, digits, "
            f"'.', '_', '-' and '/' (max 128 characters)")
    if "//" in path:
        raise me.ValueException(f"{what} may not contain '//'")
    if any(part == ".." for part in path.split("/")):
        raise me.ValueException(f"{what} may not contain a '..' segment")
    return path


def validate_quiet_path(path):
    """A request path whose hits stay out of the main access log."""
    return _validate_request_path(path, "a quiet path")


def validate_route_prefix(prefix):
    """A site_api proxied prefix. `/` alone is refused — a whole-host proxy
    is what kind=api is for, and allowing it here would make the two kinds'
    guarantees indistinguishable."""
    _validate_request_path(prefix, "a route prefix")
    if prefix == "/":
        raise me.ValueException(
            "a route prefix cannot be '/' — use kind=api for a whole-host "
            "proxy")
    return prefix


def validate_redirect_target(target):
    """A redirect destination is a HOST, never a free URL.

    Delegates to the server-name whitelist: same charset, same label rules,
    so it cannot smuggle a scheme, a path, a port or a metacharacter.
    """
    if not isinstance(target, str) or not target:
        raise me.ValueException("a redirect vhost requires redirect_to")
    if target.startswith("*."):
        raise me.ValueException("a redirect target cannot be a wildcard")
    return validate_server_name(target)


def validate_route(route):
    """Whole-row validation for a VhostRoute. Called from save()."""
    from mojo.apps.edge.models.vhost import KIND_SITE_API

    validate_route_prefix(route.path_prefix or "")

    if not route.vhost_id:
        raise me.ValueException("a route requires a vhost")
    if route.vhost.kind != KIND_SITE_API:
        raise me.ValueException(
            f"routes belong to site_api vhosts, not {route.vhost.kind}")
    if not route.upstream_id:
        raise me.ValueException("a route requires an upstream")

    # The upstream must be a house row or belong to THIS vhost's tenant.
    # Mirrors the WebApp.vhost cross-tenant check: `upstream` is
    # caller-writable, and without this an admin of two tenants could proxy
    # group A's prefix into group B's upstream.
    if route.upstream.group_id not in (None, route.vhost.domain.group_id):
        raise me.ValueException(
            "the upstream must be a shared one or belong to this vhost's "
            "group")
    return route


def validate_blocklist_entry(entry):
    """Whole-row validation for a BlocklistEntry. Called from save() AND
    called explicitly by the seed migration (historical models drop save()).

    `ip` values are STORED normalized (`ipaddress.ip_network(strict=False)`),
    which is what makes the unique constraint and the render-time dedupe
    mean something — `10.0.0.1/8` and `10.0.0.0/8` are the same network, and
    two spellings of one network rendered into a `geo` block is an nginx
    [emerg] that would freeze fleet convergence.
    """
    from mojo.apps.edge.models.blocklist import KINDS, MODES

    if entry.kind not in dict(KINDS):
        raise me.ValueException(f"unknown blocklist kind {entry.kind!r}")
    if entry.mode not in dict(MODES):
        raise me.ValueException(f"unknown blocklist mode {entry.mode!r}")

    value = entry.value
    if not isinstance(value, str) or not value:
        raise me.ValueException("a blocklist entry requires a value")

    if entry.kind == "ip":
        try:
            network = ipaddress.ip_network(value, strict=False)
        except ValueError:
            raise me.ValueException(
                f"{value!r} is not an IP address or CIDR network")
        entry.value = str(network)
        return entry

    # kind == "ua": a regex fragment, rendered only as `"~*<value>" <id>;`.
    if not UA_PATTERN_RE.match(value):
        raise me.ValueException(
            "a user-agent pattern may use letters, digits and the regex "
            "characters ()[]|?^.*+-/_\\ only (max 256 characters, no "
            "spaces, quotes or braces)")
    # A trailing ODD run of backslashes would escape the closing quote of
    # the rendered `"~*<value>"` string — the exact break-out the charset
    # exists to prevent.
    trailing = len(value) - len(value.rstrip("\\"))
    if trailing % 2 == 1:
        raise me.ValueException(
            "a user-agent pattern cannot end with an unescaped backslash")
    try:
        re.compile(value)
    except re.error as err:
        raise me.ValueException(
            f"user-agent pattern does not compile: {err}")
    return entry


def validate_vhost(vhost):
    """Whole-row validation for a Vhost. Called from save()."""
    from mojo.apps.edge.models.vhost import (
        KIND_API, KIND_REDIRECT, KIND_SITE, KIND_SITE_API,
    )

    validate_label(vhost.label or "")
    validate_pool(vhost.pool)
    validate_body_size_mb(vhost.body_size_mb)

    if vhost.kind not in (KIND_API, KIND_SITE, KIND_SITE_API, KIND_REDIRECT):
        raise me.ValueException(f"unknown vhost kind {vhost.kind!r}")

    # --- the kind matrix: which knobs each product shape may carry ---

    if vhost.kind == KIND_API:
        if not vhost.upstream_id:
            raise me.ValueException("an api vhost requires an upstream")
    elif vhost.upstream_id:
        raise me.ValueException(
            f"a {vhost.kind} vhost has no whole-host upstream "
            f"(site_api proxies per-route)")

    if vhost.kind == KIND_REDIRECT:
        validate_redirect_target(vhost.redirect_to)
    elif vhost.redirect_to:
        raise me.ValueException(
            f"a {vhost.kind} vhost has no redirect target")

    if vhost.spa and vhost.kind not in (KIND_SITE, KIND_SITE_API):
        raise me.ValueException(
            f"spa applies to site and site_api vhosts, not {vhost.kind}")

    if vhost.serve_static and vhost.kind not in (KIND_API, KIND_SITE_API):
        raise me.ValueException(
            f"serve_static applies to api and site_api vhosts, not "
            f"{vhost.kind}")

    quiet_paths = vhost.quiet_paths or []
    if not isinstance(quiet_paths, list):
        raise me.ValueException("quiet_paths must be a list of paths")
    if quiet_paths and vhost.kind not in (KIND_API, KIND_SITE_API):
        raise me.ValueException(
            f"quiet_paths applies to api and site_api vhosts, not "
            f"{vhost.kind}")
    for path in quiet_paths:
        validate_quiet_path(path)
    if len(set(quiet_paths)) != len(quiet_paths):
        raise me.ValueException("quiet_paths contains a duplicate")

    if vhost.kind == KIND_SITE_API:
        # A quiet path only means something on a PROXIED endpoint here — the
        # site half logs normally. Requiring coverage catches the typo where
        # a quiet path silences nothing at all.
        prefixes = []
        if vhost.pk:
            prefixes = list(
                vhost.routes.values_list("path_prefix", flat=True))
        for path in quiet_paths:
            if not any(path.startswith(prefix) for prefix in prefixes):
                declared = ", ".join(sorted(prefixes)) or "none declared"
                raise me.ValueException(
                    f"quiet path {path} is not under any route prefix "
                    f"({declared})")
    elif vhost.pk:
        # Routes belong to site_api rows only; a kind change must not leave
        # orphaned prefixes behind that a change back would silently revive.
        if vhost.routes.exists():
            raise me.ValueException(
                f"a {vhost.kind} vhost cannot carry routes — delete them "
                f"before changing kind")

    if not vhost.domain_id:
        raise me.ValueException("a vhost requires a domain")

    server_name = server_name_for(vhost.domain.name, vhost.label or "")
    # Checked unconditionally, enabled or not: an unvalidatable name is a
    # broken row whichever way its flag is set, and storing one would leave a
    # landmine for whoever enables it later.
    validate_server_name(server_name)

    # The certificate-coverage check gates ENABLING. A disabled row is inert
    # — it renders nothing — and refusing to store one would make a vhost
    # impossible to park while its certificate is reissued.
    if vhost.is_enabled:
        validate_certificate_covers(
            vhost.certificate, vhost.domain_id, server_name)
    return vhost
