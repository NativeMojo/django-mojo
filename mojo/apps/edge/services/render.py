"""
Turn validated `Vhost` rows into nginx configuration.

Three rules hold this file together, and all three are structural rather than
careful:

1. **No template engine.** Django's autoescaping is HTML-shaped and does
   nothing for nginx; using it here would imply a safety that does not exist.
   These are plain string builders over values that were validated before they
   were stored.

2. **Every substitution is re-asserted.** `_safe()` runs the same whitelist the
   model layer ran. That duplication is deliberate: a future service-layer
   write that skips `Model.save()` still cannot put a `;` into a file.

3. **Kind isolation is a property of the code shape, not of an `if`.** The
   literal `proxy_pass` appears only inside `_proxy_location_body`, which only
   the proxying builders call; `root ` appears only inside the file-serving
   builders. There is no branch that could leak one into the other, because
   the strings are not in scope.

**Every vhost renders exactly TWO server blocks**: the 443 block that carries
the kind's contract, and a per-name port-80 block (ACME webroot location plus
a 301 to https). This supersedes the earlier "no port-80 block" stance — the
fleet now terminates HTTP per vhost rather than in a hand-managed catch-all,
and HTTP-01 issuance needs the challenge path served per name. The injection
pin moves with it: the assertion is now *exactly two* `server {` per vhost.
"""

import hashlib
import ipaddress
import json
import os

from mojo import errors as me
from mojo.helpers.settings import settings
from mojo.deploy.mojosec_nginx import (
    RECEIVER_BODY_LIMIT as MOJOSEC_BODY_LIMIT,
    RECEIVER_PATH as MOJOSEC_RECEIVER_PATH,
    render_http_log as render_mojosec_http_log,
    safe_log_path,
    trusted_proxy_cidrs,
)

from mojo.apps.edge import validators
from mojo.apps.edge.models.upstream import KIND_UNIX
from mojo.apps.edge.models.vhost import (
    KIND_API, KIND_REDIRECT, KIND_SITE, KIND_SITE_API,
)


def edge_root():
    # get_static: a DB-backed Setting must not be able to redirect where the
    # installer writes certificate material. See _nginx_test_argv in
    # services/installer.py for the full reasoning.
    return settings.get_static("EDGE_ROOT", "/opt/api/var/edge")


def tls_protocols():
    return settings.get("EDGE_TLS_PROTOCOLS", "TLSv1.2 TLSv1.3")


def tls_ciphers():
    return settings.get(
        "EDGE_TLS_CIPHERS",
        "ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:"
        "ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:"
        "ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305")


def acme_webroot():
    # get_static: this is a filesystem path the port-80 blocks serve from,
    # and a DB row must not be able to repoint it. Same rule as EDGE_ROOT.
    return settings.get_static("EDGE_ACME_WEBROOT", "/var/www/certbot")


def django_static_root():
    return settings.get_static(
        "EDGE_DJANGO_STATIC_ROOT", "/opt/api/django/static")


def _bounded_int(name, default, low, high):
    """A DB-settable integer knob, clamped rather than trusted.

    A bad `Setting` row must degrade to the default, never freeze fleet
    convergence with a render-time raise — these knobs are tuning, not
    structure.
    """
    try:
        value = int(settings.get(name, default))
    except (TypeError, ValueError):
        value = default
    return min(max(value, low), high)


def proxy_read_timeout():
    return _bounded_int("EDGE_PROXY_READ_TIMEOUT", 3600, 60, 86400)


def keepalive_timeout():
    return _bounded_int("EDGE_HTTP_KEEPALIVE_TIMEOUT", 65, 5, 300)


def mime_types_path():
    return settings.get_static("EDGE_MIME_TYPES", "/etc/nginx/mime.types")


def log_dir():
    # App-owned: the base's access_log (and stage 4's watch log) live here,
    # NOT under /var/log/nginx — the whole include graph must stay writable
    # by the app user so the staged, unprivileged `nginx -t` can open it.
    return settings.get_static("EDGE_LOG_DIR", f"{edge_root()}/log")


def default_server_enabled():
    # get_static and default OFF: flipping this changes which server answers
    # every unmatched name on the node — a cutover step (see the migration
    # order in docs), not a tuning knob a DB row may toggle.
    return bool(settings.get_static("EDGE_HTTP_DEFAULT_SERVER", False))


def mojosec_log_path():
    root = safe_log_path(log_dir())
    value = safe_log_path(settings.get_static(
        "MOJOSEC_NGINX_LOG_PATH", f"{root}/mojosec.json.log"))
    if os.path.commonpath((value, root)) != root:
        raise me.ValueException(
            f"Edge MojoSec log must stay beneath EDGE_LOG_DIR: {value}")
    return value


def mojosec_mode():
    value = str(settings.get_static("MOJOSEC_MODE", "off")).strip().lower()
    if value not in ("off", "observe"):
        raise me.ValueException(
            f"MOJOSEC_MODE must be off or observe, got {value!r}")
    return value


def mojosec_trusted_proxy_cidrs():
    # File-only: trusting an address range changes the authoritative client IP
    # used by both nginx and the security sensor.
    return trusted_proxy_cidrs(
        settings.get_static("MOJOSEC_TRUSTED_PROXY_CIDRS", ""))


def _staged_port_value(name, default):
    raw = settings.get_static(name, default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        # The same named-refusal style as every other bad input here — a bare
        # int() traceback would not say which setting to fix.
        raise me.ValueException(
            f"edge staged listen port is not an integer: {name}={raw!r}")


def _staged_ports():
    """The (http, https) ports the STAGED copies listen on.

    Refused, not clamped: these are file-only settings (get_static), so a bad
    value is an operator error worth stopping on. A port ≤1023 silently
    reintroduces the unprivileged-bind EACCES failure the staged remap exists
    to avoid, and equal ports collapse each vhost's 443/80 block pair onto one
    addr:port — which surfaces as a baffling `conflicting server name`
    failure instead of a named refusal. Defaults sit above Linux's default
    ephemeral ceiling (60999), though a collision could not fail the check
    anyway — `nginx -t` tolerates EADDRINUSE.
    """
    http = _staged_port_value("EDGE_STAGED_HTTP_PORT", 61080)
    https = _staged_port_value("EDGE_STAGED_HTTPS_PORT", 61443)
    for port in (http, https):
        if not 1024 <= port <= 65535:
            raise me.ValueException(
                f"edge staged listen port out of range (1024-65535): {port}")
    if http == https:
        raise me.ValueException(
            f"edge staged listen ports must differ, both are {http}")
    return http, https


def staged_http_port():
    return _staged_ports()[0]


def staged_https_port():
    return _staged_ports()[1]


def http_knobs():
    """Every resolved http-level knob, as one dict.

    This is BOTH what `render_http_base` renders from and what
    `desired_state` embeds under its `http` key — which is what makes a TLS
    or logging change move the generation hash. Before this, changing
    EDGE_TLS_PROTOCOLS never converged: the values were substituted at render
    time but absent from the hashed payload.
    """
    return dict(
        mime_types=mime_types_path(),
        log_dir=log_dir(),
        keepalive_timeout=keepalive_timeout(),
        proxy_read_timeout=proxy_read_timeout(),
        default_server=default_server_enabled(),
        tls_protocols=tls_protocols(),
        tls_ciphers=tls_ciphers(),
        mojosec_log_path=mojosec_log_path(),
        mojosec_trusted_proxy_cidrs=mojosec_trusted_proxy_cidrs(),
        mojosec_mode=mojosec_mode(),
    )


def blocklist_payload():
    """Every blocklist row that RENDERS, as hashable dicts.

    `off` rows are absent on purpose — they render nowhere, so flipping a
    row to `off` must move the generation hash by dropping it from here,
    and flipping it back must bring it back.
    """
    from mojo.apps.edge.models import BlocklistEntry

    return [
        dict(id=row.pk, kind=row.kind, value=row.value, mode=row.mode)
        for row in BlocklistEntry.objects.exclude(mode="off")
        .order_by("kind", "value")
    ]


# ----------------------------------------------------------------------
# paths
# ----------------------------------------------------------------------

def generation_dir(generation):
    return f"{edge_root()}/generations/{generation}"


def cert_dir(generation, certificate_id):
    return f"{generation_dir(generation)}/certs/{int(certificate_id)}"


def www_dir(generation, vhost_id):
    return f"{generation_dir(generation)}/www/{int(vhost_id)}"


def current_link():
    return f"{edge_root()}/current"


# ----------------------------------------------------------------------
# the re-assertion boundary
# ----------------------------------------------------------------------

def _safe(value, pattern, what):
    """Refuse anything the model layer should already have refused.

    Raises rather than escaping. There is no escape hatch here on purpose — an
    escaping helper is how a new field eventually routes around validation.
    """
    if not isinstance(value, str) or not pattern.match(value):
        raise me.ValueException(
            f"edge renderer refused an unsafe {what}: {value!r}")
    return value


def _safe_server_name(server_name):
    """A server_name is a domain, optionally wildcard-prefixed."""
    candidate = server_name
    if candidate.startswith("*."):
        candidate = candidate[2:]
    for part in candidate.split("."):
        _safe(part, validators.LABEL_RE, "server name label")
    return server_name


def _safe_upstream_target(upstream):
    if upstream.kind == KIND_UNIX:
        validators.validate_socket_path(upstream.socket_path or "")
        return f"unix:{upstream.socket_path}"
    validators.validate_upstream_host(upstream.host or "")
    validators.validate_upstream_port(upstream.port)
    return f"{upstream.host}:{upstream.port}"


# ----------------------------------------------------------------------
# blocks
# ----------------------------------------------------------------------

def _tls_block(generation, certificate_id):
    """The TLS floor. Emitted unconditionally, fed by settings only.

    No model field reaches this — there is no field that could. The values
    still pass `_safe`: `settings.get` resolves a DB-backed Setting row
    first, and a Setting is REST-writable by holders of global
    manage_settings, so "it's a setting" is not "it's not operator input".
    This was the file's one unasserted substitution; it no longer is.
    """
    certs = cert_dir(generation, certificate_id)
    protocols = _safe(tls_protocols(), validators.TLS_VALUE_RE, "TLS protocols")
    ciphers = _safe(tls_ciphers(), validators.TLS_VALUE_RE, "TLS ciphers")
    return "\n".join([
        f"    ssl_certificate     {certs}/fullchain.pem;",
        f"    ssl_certificate_key {certs}/privkey.pem;",
        f"    ssl_protocols {protocols};",
        f"    ssl_ciphers {ciphers};",
        "    ssl_prefer_server_ciphers off;",
        "    ssl_session_timeout 1d;",
        "    ssl_session_cache shared:EdgeSSL:10m;",
        "    ssl_stapling on;",
        "    ssl_stapling_verify on;",
    ])


def _header_block(indent="    "):
    """Security headers, identical for every vhost.

    `headers` is deliberately NOT a model field in v1 — every structured field
    is another renderer input to test, and there is no requirement yet that
    these differ per site.

    `indent` exists because nginx's `add_header` inheritance is
    replace-not-merge: a location that declares ANY `add_header` of its own
    (the no-cache posture on proxied locations, the immutable caching on
    assets) silently drops every inherited header — so those locations
    re-emit this block inside themselves.
    """
    return "\n".join([
        f"{indent}add_header X-Content-Type-Options nosniff always;",
        f"{indent}add_header X-Frame-Options DENY always;",
        f"{indent}add_header Referrer-Policy strict-origin-when-cross-origin always;",
        f"{indent}add_header Strict-Transport-Security "
        "\"max-age=31536000; includeSubDomains\" always;",
        f"{indent}add_header Permissions-Policy "
        "\"camera=(), microphone=(), geolocation=()\" always;",
    ])


def _open(server_name):
    return "\n".join([
        "server {",
        "    listen 443 ssl;",
        "    listen [::]:443 ssl;",
        "    http2 on;",
        f"    server_name {server_name};",
    ])


def _safe_body_size(vhost):
    """client_max_body_size, re-asserted at substitution like every value."""
    validators.validate_body_size_mb(vhost.body_size_mb)
    return f"    client_max_body_size {int(vhost.body_size_mb)}m;"


def _blocklist_guards():
    """The per-server blocklist guards. Structural — rendered in every 443
    block whether rows exist or not; with no rows the maps default to 0 and
    the guards never fire."""
    return "\n".join([
        "    if ($edge_block_ip) { return 444; }",
        "    if ($edge_block_ua) { return 444; }",
    ])


def _port80_block(server_name):
    """The per-name HTTP block: ACME webroot, then a 301 to https.

    Emitted for EVERY kind — HTTP-01 issuance needs the challenge path served
    on the exact name being validated, and everything else on port 80 is a
    redirect, never content.
    """
    return "\n".join([
        "server {",
        "    listen 80;",
        "    listen [::]:80;",
        f"    server_name {server_name};",
        "",
        "    location /.well-known/acme-challenge/ {",
        f"        root {acme_webroot()};",
        "    }",
        "",
        "    location / {",
        "        return 301 https://$host$request_uri;",
        "    }",
        "}",
        "",
    ])


def _proxy_destination(upstream):
    """The proxy_pass target: a NAMED upstream block, never an inline address.

    The name is derived from the row's pk — an integer — but the target is
    still re-asserted here: a row that cannot validate must not converge just
    because only its name is substituted. The block itself is rendered once
    in http.d/10_upstreams.conf by `render_upstreams`.
    """
    _safe_upstream_target(upstream)
    return f"http://edge_up_{int(upstream.pk)}"


def _proxy_location_body(upstream, indent="        "):
    """The one canonical proxied-location body (asgi.inc parity).

    The literal `proxy_pass` lives here and nowhere else. WebSocket upgrade
    headers ride on every proxied location — the `$connection_upgrade` map is
    defined once in the rendered http base. The no-cache posture declares
    `add_header`, which is why the security headers are re-emitted first.
    """
    destination = _proxy_destination(upstream)
    return "\n".join([
        f"{indent}proxy_pass {destination};",
        f"{indent}proxy_http_version 1.1;",
        f"{indent}proxy_set_header Host $host;",
        f"{indent}proxy_set_header X-Real-IP $remote_addr;",
        f"{indent}proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
        f"{indent}proxy_set_header X-Forwarded-Proto $scheme;",
        f"{indent}proxy_set_header Upgrade $http_upgrade;",
        f"{indent}proxy_set_header Connection $connection_upgrade;",
        f"{indent}proxy_connect_timeout 10s;",
        f"{indent}proxy_send_timeout 120s;",
        f"{indent}proxy_read_timeout {proxy_read_timeout()}s;",
        f"{indent}proxy_buffering off;",
        f"{indent}proxy_redirect off;",
        f"{indent}proxy_no_cache 1;",
        f"{indent}proxy_cache_bypass $http_pragma $http_authorization;",
        _header_block(indent),
        f"{indent}add_header Cache-Control "
        "\"no-store, no-cache, must-revalidate, max-age=0\" always;",
        f"{indent}add_header Pragma \"no-cache\" always;",
        f"{indent}add_header Expires \"0\" always;",
    ])


def _quiet_location(path, upstream):
    """An exact-match location keeping a health check out of the MAIN log.

    Any `access_log` directive in a location REPLACES the inherited set, so
    declaring only the watch log here switches the main access log off for
    this path while keeping the security watch on — a quiet path must never
    be a blind spot for a watched client. The path is re-asserted at
    substitution, same as every other value.
    """
    validators.validate_quiet_path(path)
    return "\n".join([
        f"    location = {path} {{",
        f"        access_log {log_dir()}/edge_watch.log edge_watch "
        "if=$edge_watch;",
        "        log_not_found off;",
        _proxy_location_body(upstream),
        "    }",
    ])


def _mojosec_receiver_location(upstream):
    """The exact machine receiver has a wire cap below a vhost's normal cap."""
    blocks = []
    for path in (MOJOSEC_RECEIVER_PATH, MOJOSEC_RECEIVER_PATH + "/"):
        blocks.append("\n".join([
            f"    location = {path} {{",
            f"        client_max_body_size {MOJOSEC_BODY_LIMIT};",
            _proxy_location_body(upstream),
            "    }",
        ]))
    return "\n".join(blocks)


# Hashed build assets — safe to cache forever because their names change when
# their bytes do. This location declares add_header, so it re-emits the
# security header block (nginx add_header inheritance is replace-not-merge).
_ASSET_EXTENSIONS = (
    r"css|js|mjs|map|ico|svg|gif|png|jpe?g|webp|avif|woff2?|ttf|otf|eot")


def _asset_cache_location():
    return "\n".join([
        f"    location ~* \\.({_ASSET_EXTENSIONS})$ {{",
        _header_block("        "),
        "        add_header Cache-Control "
        "\"public, max-age=31536000, immutable\" always;",
        "        try_files $uri =404;",
        "    }",
    ])


def _render_api(vhost, generation, server_name):
    """Whole-host reverse proxy. No filesystem `root` is in this scope —
    the optional /static/ alias serves the FLEET's Django static root, a
    get_static path no row can point anywhere else."""
    parts = [
        _open(server_name),
        "",
        _tls_block(generation, vhost.certificate_id),
        "",
        _header_block(),
        "",
        _safe_body_size(vhost),
        "",
        _blocklist_guards(),
        "",
    ]
    for path in sorted(vhost.quiet_paths or []):
        if path == MOJOSEC_RECEIVER_PATH:
            continue
        parts.append(_quiet_location(path, vhost.upstream))
        parts.append("")
    if mojosec_mode() == "observe":
        parts.extend([_mojosec_receiver_location(vhost.upstream), ""])
    if vhost.serve_static:
        parts.extend([
            "    location /static/ {",
            f"        alias {django_static_root()}/;",
            "    }",
            "",
        ])
    parts.extend([
        "    location / {",
        _proxy_location_body(vhost.upstream),
        "    }",
        "}",
        "",
    ])
    return "\n".join(parts)


def _site_body(vhost, generation):
    """The file-serving half shared by site and site_api."""
    root = www_dir(generation, vhost.pk)
    if vhost.spa:
        fallback = "        try_files $uri $uri/ /index.html;"
    else:
        fallback = "        try_files $uri $uri/ =404;"
    parts = [
        f"    root {root};",
        "    index index.html;",
        "",
    ]
    if not vhost.spa:
        parts.extend([
            "    error_page 404 /404.html;",
            "",
        ])
    parts.extend([
        _asset_cache_location(),
        "",
        "    location / {",
        fallback,
        "    }",
    ])
    return parts


def _render_site(vhost, generation, server_name):
    """Static site / SPA. `proxy_pass` is not in this function's scope."""
    parts = [
        _open(server_name),
        "",
        _tls_block(generation, vhost.certificate_id),
        "",
        _header_block(),
        "",
        _safe_body_size(vhost),
        "",
        _blocklist_guards(),
        "",
    ]
    parts.extend(_site_body(vhost, generation))
    parts.extend(["}", ""])
    return "\n".join(parts)


def _sorted_routes(vhost):
    return sorted(vhost.routes.all(), key=lambda r: r.path_prefix)


def _render_site_api(vhost, generation, server_name):
    """A site plus proxied path prefixes, one location per VhostRoute.

    Quiet paths attach to their LONGEST covering route prefix — they only
    mean something on a proxied endpoint here, and the vhost validator
    enforces coverage at save time. A quiet path orphaned later (its route
    deleted) is SKIPPED rather than raised on: rendering must not freeze the
    pool over a row that merely logs more than intended.
    """
    routes = _sorted_routes(vhost)
    parts = [
        _open(server_name),
        "",
        _tls_block(generation, vhost.certificate_id),
        "",
        _header_block(),
        "",
        _safe_body_size(vhost),
        "",
        _blocklist_guards(),
        "",
    ]
    for path in sorted(vhost.quiet_paths or []):
        if path == MOJOSEC_RECEIVER_PATH:
            continue
        covering = None
        for route in routes:
            validators.validate_route_prefix(route.path_prefix)
            if path.startswith(route.path_prefix):
                if covering is None or len(route.path_prefix) > len(
                        covering.path_prefix):
                    covering = route
        if covering is None:
            continue
        parts.append(_quiet_location(path, covering.upstream))
        parts.append("")
    receiver_route = None
    for route in routes:
        prefix = validators.validate_route_prefix(route.path_prefix)
        if MOJOSEC_RECEIVER_PATH.startswith(prefix):
            if receiver_route is None or len(prefix) > len(receiver_route.path_prefix):
                receiver_route = route
    if receiver_route is not None and mojosec_mode() == "observe":
        parts.extend([_mojosec_receiver_location(receiver_route.upstream), ""])
    if vhost.serve_static:
        parts.extend([
            "    location ^~ /static/ {",
            f"        alias {django_static_root()}/;",
            "    }",
            "",
        ])
    for route in routes:
        prefix = validators.validate_route_prefix(route.path_prefix)
        parts.extend([
            f"    location ^~ {prefix} {{",
            _proxy_location_body(route.upstream),
            "    }",
            "",
        ])
    parts.extend(_site_body(vhost, generation))
    parts.extend(["}", ""])
    return "\n".join(parts)


def _render_redirect(vhost, generation, server_name):
    """A host-to-host permanent redirect. Neither `root` nor `proxy_pass`
    is in this function's scope — the target is a validated FQDN, re-asserted
    at substitution."""
    target = validators.validate_redirect_target(vhost.redirect_to)
    _safe_server_name(target)
    return "\n".join([
        _open(server_name),
        "",
        _tls_block(generation, vhost.certificate_id),
        "",
        _header_block(),
        "",
        _safe_body_size(vhost),
        "",
        _blocklist_guards(),
        "",
        f"    return 301 https://{target}$request_uri;",
        "}",
        "",
    ])


_BUILDERS = {
    KIND_SITE: _render_site,
    KIND_API: _render_api,
    KIND_SITE_API: _render_site_api,
    KIND_REDIRECT: _render_redirect,
}


def render_vhost(vhost, generation):
    """One vhost, exactly TWO `server` blocks: its 443 contract and its
    port-80 ACME/redirect shell."""
    server_name = _safe_server_name(vhost.server_name)
    builder = _BUILDERS.get(vhost.kind)
    if builder is None:
        raise me.ValueException(f"edge renderer has no builder for {vhost.kind!r}")
    return builder(vhost, generation, server_name) + "\n" + _port80_block(server_name)


def _safe_blocklist_ip(value):
    """Re-assert an ip row at substitution: it must BE a network, and it is
    rendered from the normalized parse, never the stored string."""
    try:
        return str(ipaddress.ip_network(value, strict=False))
    except (TypeError, ValueError):
        raise me.ValueException(
            f"edge renderer refused an unsafe blocklist network: {value!r}")


def _safe_blocklist_ua(value):
    """Re-assert a ua row at substitution — the whitelist plus the
    trailing-backslash rule that keeps `"~*<value>"` unescapable."""
    _safe(value, validators.UA_PATTERN_RE, "blocklist pattern")
    if (len(value) - len(value.rstrip("\\"))) % 2 == 1:
        raise me.ValueException(
            f"edge renderer refused an unsafe blocklist pattern: {value!r}")
    return value


def _dedupe_rows(rows):
    """Render-time dedupe by (already normalized) value.

    The unique constraint makes duplicates unreachable through save(), but a
    bypass write could still smuggle two spellings of one network into a
    `geo` block — and a duplicate `geo` entry is an nginx [emerg] that would
    freeze fleet convergence. First occurrence wins, deterministically.
    """
    seen = set()
    out = []
    for row in rows:
        if row["value"] in seen:
            continue
        seen.add(row["value"])
        out.append(row)
    return out


def _blocklist_section(knobs, security):
    """The four blocklist maps, the watch combiner, and the watch log.

    ALLOW ROWS RENDER FIRST in every map they join: nginx `map` regexes
    match in order of appearance, so an early `0` beats any later pattern
    that would also match (Lynx's UA contains `libwww-FM`; the allow row is
    what keeps the unanchored `libwww` token off it). In `geo` blocks order
    is cosmetic — most-specific network wins — but the allow-first rule is
    kept for one readable convention.

    Values carry the ROW ID, so a watch-log line names the exact rule that
    matched it.
    """
    ip_rows = _dedupe_rows([r for r in security if r["kind"] == "ip"])
    ua_rows = _dedupe_rows([r for r in security if r["kind"] == "ua"])

    def geo_block(variable, active_mode):
        lines = [f"geo ${variable} {{", "    default 0;"]
        for row in ip_rows:
            if row["mode"] == "allow":
                lines.append(f"    {_safe_blocklist_ip(row['value'])} 0;")
        for row in ip_rows:
            if row["mode"] == active_mode:
                lines.append(
                    f"    {_safe_blocklist_ip(row['value'])} {int(row['id'])};")
        lines.append("}")
        return lines

    def ua_map(variable, active_mode):
        lines = [f"map $http_user_agent ${variable} {{", "    default 0;"]
        for row in ua_rows:
            if row["mode"] == "allow":
                lines.append(f"    \"~*{_safe_blocklist_ua(row['value'])}\" 0;")
        for row in ua_rows:
            if row["mode"] == active_mode:
                lines.append(
                    f"    \"~*{_safe_blocklist_ua(row['value'])}\" "
                    f"{int(row['id'])};")
        lines.append("}")
        return lines

    parts = []
    parts.extend(geo_block("edge_block_ip", "enforce"))
    parts.append("")
    parts.extend(geo_block("edge_watch_ip", "log"))
    parts.append("")
    parts.extend(ua_map("edge_block_ua", "enforce"))
    parts.append("")
    parts.extend(ua_map("edge_watch_ua", "log"))
    parts.extend([
        "",
        "map \"$edge_watch_ip:$edge_watch_ua\" $edge_watch {",
        "    default 1;",
        "    \"0:0\" 0;",
        "}",
        "",
        "log_format edge_watch '$remote_addr - [$time_local] '",
        "                      '\"$request_method "
        "$scheme://$host$request_uri\" '",
        "                      '$status ip=$edge_watch_ip "
        "ua=$edge_watch_ua '",
        "                      '\"$http_user_agent\"';",
        "",
        f"access_log {knobs['log_dir']}/edge_watch.log edge_watch "
        "if=$edge_watch;",
    ])
    return parts


def render_http_base(knobs=None, security=None):
    """`http.d/00_base.conf` — everything the server blocks lean on.

    Rendered per generation from `http_knobs()` and the blocklist rows, so
    the maps every server block references (`$connection_upgrade`, the
    blocklist guards), the log plumbing, and the hardening knobs converge
    with the vhosts instead of living in a hand-managed file. `default_type`
    and `types_hash_max_size` deliberately do NOT render here: they live in
    the provision-time bootstrap (see
    docs/django_developer/edge/templates.md), and a second copy at the same
    http level would be a duplicate-directive [emerg].
    """
    knobs = knobs or http_knobs()
    if security is None:
        security = blocklist_payload()
    parts = [
        "# edge http base — rendered per generation, never hand-edited",
        f"include {knobs['mime_types']};",
        "",
        "log_format main '$remote_addr - - [$time_local] '",
        "                '\"$request_method $scheme://$host$request_uri "
        "$server_protocol\" '",
        "                '$status $body_bytes_sent \"$http_referer\" '",
        "                '\"$http_user_agent\" $request_time $server_port';",
        "",
        "map $http_user_agent $loggable {",
        "    ~*ELB-HealthChecker 0;",
        "    default 1;",
        "}",
        "",
        f"access_log {knobs['log_dir']}/access.log main if=$loggable;",
        "",
    ]
    if knobs["mojosec_mode"] == "observe":
        parts.extend(render_mojosec_http_log(
            knobs["mojosec_log_path"], knobs["mojosec_trusted_proxy_cidrs"]
        ).rstrip().splitlines())
        parts.append("")
    parts.extend([
        "map $http_upgrade $connection_upgrade {",
        "    default upgrade;",
        "    '' close;",
        "}",
        "",
        "sendfile on;",
        f"keepalive_timeout {int(knobs['keepalive_timeout'])};",
        "server_tokens off;",
        "gzip on;",
        "gzip_vary on;",
        "gzip_comp_level 5;",
        "gzip_min_length 256;",
        "gzip_proxied any;",
        "gzip_types text/plain text/css application/json "
        "application/javascript text/xml application/xml image/svg+xml;",
        "",
    ])
    parts.extend(_blocklist_section(knobs, security))
    if knobs["default_server"]:
        parts.extend([
            "",
            "# Catch-alls for names no vhost claims. 443 rejects the TLS",
            "# handshake outright (no certificate needed, no name leaked);",
            "# 80 drops the connection. Flag-gated by EDGE_HTTP_DEFAULT_SERVER",
            "# so a migrating deployment can keep its file-managed default",
            "# server until the cutover step.",
            "server {",
            "    listen 443 ssl default_server;",
            "    listen [::]:443 ssl default_server;",
            "    ssl_reject_handshake on;",
            "}",
            "",
            "server {",
            "    listen 80 default_server;",
            "    listen [::]:80 default_server;",
            "    return 444;",
            "}",
        ])
    parts.append("")
    return "\n".join(parts)


def referenced_upstreams(vhosts):
    """Every upstream the given vhosts reach, directly or via routes.

    De-duplicated by pk and sorted, so the rendered upstreams file is
    deterministic for a given desired state.
    """
    seen = {}
    for vhost in vhosts:
        if vhost.upstream_id:
            seen[vhost.upstream_id] = vhost.upstream
        for route in vhost.routes.all():
            seen[route.upstream_id] = route.upstream
    return [seen[key] for key in sorted(seen)]


def render_upstreams(upstreams):
    """`http.d/10_upstreams.conf` — one named block per referenced upstream.

    Vhost files reference these by name (`edge_up_<pk>`); the literal
    host:port / socket path appears here and nowhere else in the generation.
    """
    parts = ["# edge upstreams — named blocks; vhost files reference these only"]
    for upstream in sorted(upstreams, key=lambda row: row.pk):
        target = _safe_upstream_target(upstream)
        parts.extend([
            "",
            f"upstream edge_up_{int(upstream.pk)} {{",
            f"    server {target};",
            "}",
        ])
    parts.append("")
    return "\n".join(parts)


def _staged_listen_map():
    """Stripped real listen body -> its staged replacement.

    EXHAUSTIVE over the bodies the builders emit: `_open` (443 pair),
    `_port80_block` (80 pair), and the flag-gated default servers in
    `render_http_base`. A listen line not in this map refuses the render —
    see render_staged_variant.
    """
    http, https = _staged_ports()
    entries = {}
    for real, staged, params in (("443", https, " ssl"), ("80", http, "")):
        for addr in ("", "[::]:"):
            for suffix in ("", " default_server"):
                entries[f"listen {addr}{real}{params}{suffix};"] = (
                    f"listen {addr}{staged}{params}{suffix};")
    return entries


def render_staged_variant(text):
    """The same rendered file with every listen port remapped unprivileged.

    The staged `nginx -t` runs as the app user, and nginx DOES attempt
    `bind()` on every listen during `-t` — only EADDRINUSE is tolerated in
    test mode; EACCES on 443/80 is a fatal `[emerg]`, which froze fleet
    convergence on the first standard node (item #1623). The staged copies
    keep every byte identical except the listen ports, so the pre-filter
    still validates the real certificate material, maps and upstreams while
    binding only unprivileged ports. `ssl`, `default_server` and the address
    family are preserved — stripping `listen` instead would drop the ssl
    context and skip certificate validation, the pre-filter's chief value.

    Fails CLOSED: a listen line the map does not know means a builder grew a
    new listen shape without teaching the staged remap. Refusing here aborts
    the install before anything swaps, same as every other render refusal.
    """
    remap = _staged_listen_map()
    out = []
    for line in text.split("\n"):
        body = line.strip()
        # First-token match, not startswith: `listen\t443;` must reach the
        # refusal below, not slip through as a non-listen line.
        tokens = body.split()
        if not tokens or tokens[0] != "listen":
            out.append(line)
            continue
        replacement = remap.get(body)
        if replacement is None:
            raise me.ValueException(
                f"edge staged remap refused an unknown listen line: {line!r}")
        indent = line[:len(line) - len(line.lstrip())]
        out.append(indent + replacement)
    return "\n".join(out)


def render_nginx_harness(generation):
    """A minimal wrapper used ONLY by the staging `nginx -t` pre-filter.

    This approximates the real bootstrap and is documented as such — but
    since the generation now carries its own http base (maps, log format,
    upstreams), the approximation is close: the harness contributes only the
    main context, the scratch paths, and the two directives the bootstrap
    owns (`default_type`, `types_hash_max_size`). It includes the `staging/`
    listen-remapped copies rather than the real trees: `nginx -t` attempts
    `bind()` on every listen (EADDRINUSE tolerated in test mode, anything
    else fatal), and the unprivileged check cannot bind 443/80. Certificate,
    root and log paths in those copies are absolute into the real
    generation, so the material is still what gets validated. The
    authoritative check is `nginx -t` against the REAL config after the
    swap — see services/installer.py.
    """
    gen = generation_dir(generation)
    return "\n".join([
        "# staging harness — nginx -t pre-filter only, never loaded to serve",
        "#",
        "# Every path below points INSIDE the generation because this check runs",
        "# UNPRIVILEGED (see services/installer.py: a sudo'd `nginx -t -c` over an",
        "# app-writable file is a root escalation via load_module). An app user",
        "# cannot write nginx's packaged prefix, so leaving these at their",
        "# defaults would fail the check for permissions rather than for config.",
        "# The staging/ trees are listen-remapped copies of http.d/ and conf.d/:",
        "# nginx -t binds every listen it parses, and 443/80 need root.",
        f"pid {gen}/nginx.pid;",
        f"error_log {gen}/error.log;",
        "events { worker_connections 64; }",
        "http {",
        "    default_type application/octet-stream;",
        "    types_hash_max_size 4096;",
        f"    client_body_temp_path {gen}/tmp/client_body;",
        f"    proxy_temp_path {gen}/tmp/proxy;",
        f"    fastcgi_temp_path {gen}/tmp/fastcgi;",
        f"    uwsgi_temp_path {gen}/tmp/uwsgi;",
        f"    scgi_temp_path {gen}/tmp/scgi;",
        f"    include {gen}/staging/http.d/*.conf;",
        f"    include {gen}/staging/conf.d/*.conf;",
        "}",
        "",
    ])


# ----------------------------------------------------------------------
# generations
# ----------------------------------------------------------------------

def vhost_payload(vhost):
    """The canonical description of one vhost.

    This is BOTH what the generation hash is computed over and what the
    desired-state endpoint serves, so a node and the renderer can never
    disagree about what a generation contains.
    """
    upstream = None
    if vhost.upstream_id:
        upstream = _upstream_payload(vhost.upstream)
    routes = [
        dict(id=route.pk,
             path_prefix=route.path_prefix,
             upstream=_upstream_payload(route.upstream))
        for route in _sorted_routes(vhost)
    ]
    return dict(
        id=vhost.pk,
        server_name=vhost.server_name,
        kind=vhost.kind,
        pool=vhost.pool,
        spa=vhost.spa,
        body_size_mb=vhost.body_size_mb,
        quiet_paths=sorted(vhost.quiet_paths or []),
        serve_static=vhost.serve_static,
        redirect_to=vhost.redirect_to,
        certificate=vhost.certificate_id,
        certificate_serial=vhost.certificate.serial,
        upstream=upstream,
        routes=routes,
    )


def _upstream_payload(upstream):
    # `id` is in here because the rendered text names the upstream by pk
    # (`edge_up_<pk>`) — two upstreams with an identical target must still
    # move the hash when a vhost switches between them. `is_enabled` is here
    # because the installer's disabled-upstream fork depends on it: a retire
    # must move the hash so nodes converge onto the exclusion.
    return dict(
        id=upstream.pk,
        kind=upstream.kind,
        host=upstream.host,
        port=upstream.port,
        socket_path=upstream.socket_path,
        is_enabled=upstream.is_enabled,
    )


def generation_id(payload):
    """sha256 over the canonical serialisation of a desired-state payload.

    Computed over the INPUTS, never the rendered text — the rendered text
    embeds the generation directory, which embeds this id. Item #1435 adds a
    `webapps` key to the same payload, which is why this takes the whole dict
    rather than a vhost list: a promote must move the hash, or nodes will not
    install it.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                           default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def desired_state(vhosts, webapps=None):
    """The payload a node polls for, and the thing the hash covers.

    The `http` key is the resolved knob set (TLS values included) — its
    presence here is what makes a settings change converge: the hash moves,
    nodes reinstall.
    """
    rows = sorted((vhost_payload(v) for v in vhosts), key=lambda r: r["id"])
    payload = dict(vhosts=rows, webapps=webapps or [], http=http_knobs(),
                   security=blocklist_payload())
    payload["generation"] = generation_id(payload)
    return payload


def render_generation(vhosts, generation):
    """Every enabled vhost plus the generation's http base and upstreams,
    rendered into `{relative_path: text}`.

    The caller supplies `generation` because the id must be computed from the
    desired-state payload BEFORE rendering — the file contents reference the
    generation directory by absolute path, which is what lets `nginx -t`
    validate the new certificates rather than the currently-installed ones.
    """
    files = {
        "http.d/00_base.conf": render_http_base(),
        "http.d/10_upstreams.conf": render_upstreams(
            referenced_upstreams(vhosts)),
    }
    for vhost in vhosts:
        files[f"conf.d/{int(vhost.pk)}.conf"] = render_vhost(vhost, generation)
    return files
