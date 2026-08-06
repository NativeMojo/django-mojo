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
from mojo.helpers import logit
from mojo.helpers.settings import settings


# A single DNS label. No dots (a label cannot climb into another zone), and by
# construction no `;`, `{`, `}`, `#`, `$`, quote, space, CR or LF.
LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")

# Upstream hosts. No scheme, no path, no port smuggling, no newline. IPv6
# literals are NOT supported in v1 — they would need bracketing in the rendered
# config, and every real upstream so far is a hostname, an IPv4 address, or a
# unix socket. Widening this is a deliberate change with a renderer change
# beside it, not a regex tweak.
UPSTREAM_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$")

# Upstream / vhost names used as filename components.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def www_base():
    return settings.get("EDGE_WWW_BASE", "/opt/www")


def socket_base():
    return settings.get("EDGE_SOCKET_BASE", "/run/mojo")


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


def reserved_server_names():
    """Names no vhost may ever claim — the API's own hostnames.

    Union of Django's ALLOWED_HOSTS (concrete entries only) and the explicit
    EDGE_RESERVED_SERVER_NAMES setting. Returns None to mean "this deployment
    cannot name itself", which callers MUST treat as fail-closed.
    """
    from django.conf import settings as django_settings

    explicit = settings.get("EDGE_RESERVED_SERVER_NAMES", [], kind="list") or []
    allowed = [
        h.lower().lstrip(".") for h in getattr(django_settings, "ALLOWED_HOSTS", [])
        if h and h not in ("*",) and not h.startswith(".")
    ]
    names = {n.lower() for n in list(explicit) + allowed if n}
    if not names:
        return None
    return names


def validate_not_reserved(server_name):
    """Refuse a name that shadows the API itself.

    **Fail closed.** A deployment whose ALLOWED_HOSTS is `["*"]` (or empty) and
    which has not set EDGE_RESERVED_SERVER_NAMES cannot say which name is its
    own — so it cannot protect it, and allowing every name IS the shadowing
    attack. Refuse rather than silently permit, and say what to configure.
    """
    reserved = reserved_server_names()
    if reserved is None:
        logit.error(
            "edge: refusing to enable a vhost — this deployment cannot name its "
            "own hostname. Set EDGE_RESERVED_SERVER_NAMES (or a concrete "
            "ALLOWED_HOSTS) so a tenant vhost cannot shadow the API.")
        raise me.ValueException(
            "This deployment has not declared its own hostnames "
            "(EDGE_RESERVED_SERVER_NAMES); refusing to enable a vhost")
    if server_name.lower() in reserved:
        raise me.ValueException(
            f"{server_name} is reserved by this deployment")
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

def validate_pool(pool):
    if not pool or not NAME_RE.match(pool):
        raise me.ValueException(
            "pool must be lowercase letters, digits, '-' or '_'")
    return pool


def validate_vhost(vhost):
    """Whole-row validation for a Vhost. Called from save()."""
    from mojo.apps.edge.models.vhost import KIND_PROXY, KIND_SPA, KIND_STATIC

    validate_label(vhost.label or "")
    validate_pool(vhost.pool)

    if vhost.kind not in (KIND_STATIC, KIND_SPA, KIND_PROXY):
        raise me.ValueException(f"unknown vhost kind {vhost.kind!r}")

    if vhost.kind == KIND_PROXY:
        if not vhost.upstream_id:
            raise me.ValueException("a proxy vhost requires an upstream")
    elif vhost.upstream_id:
        raise me.ValueException(
            f"a {vhost.kind} vhost serves files and has no upstream")

    if not vhost.domain_id:
        raise me.ValueException("a vhost requires a domain")

    server_name = server_name_for(vhost.domain.name, vhost.label or "")
    # Checked unconditionally, enabled or not: an unvalidatable name is a
    # broken row whichever way its flag is set, and storing one would leave a
    # landmine for whoever enables it later.
    validate_server_name(server_name)

    # The reserved-name and certificate-coverage checks gate ENABLING. A
    # disabled row is inert — it renders nothing — and refusing to store one
    # would make a vhost impossible to park while its certificate is reissued.
    if vhost.is_enabled:
        validate_not_reserved(server_name)
        validate_certificate_covers(
            vhost.certificate, vhost.domain_id, server_name)
    return vhost
