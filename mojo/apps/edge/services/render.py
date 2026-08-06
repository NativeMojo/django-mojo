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
   literal `proxy_pass` appears only inside `_render_proxy`; `root ` appears
   only inside the file-serving builders. There is no branch that could leak
   one into the other, because the strings are not in scope.

**No port-80 block is emitted.** Certificates come from DNS-01, so nothing here
needs an HTTP challenge path, and a redirect belongs in the deployment's base
config as one catch-all server. Emitting a second block per vhost would also
make "exactly one `server {` per vhost" — the assertion that pins the injection
case — stop meaning anything.
"""

import hashlib
import json

from mojo import errors as me
from mojo.helpers.settings import settings

from mojo.apps.edge import validators
from mojo.apps.edge.models.upstream import KIND_UNIX
from mojo.apps.edge.models.vhost import KIND_PROXY, KIND_SPA, KIND_STATIC


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

    No model field reaches this — there is no field that could. That is the
    whole reason `Vhost` has no free-form options.
    """
    certs = cert_dir(generation, certificate_id)
    return "\n".join([
        f"    ssl_certificate     {certs}/fullchain.pem;",
        f"    ssl_certificate_key {certs}/privkey.pem;",
        f"    ssl_protocols {tls_protocols()};",
        f"    ssl_ciphers {tls_ciphers()};",
        "    ssl_prefer_server_ciphers off;",
        "    ssl_session_timeout 1d;",
        "    ssl_session_cache shared:EdgeSSL:10m;",
        "    ssl_stapling on;",
        "    ssl_stapling_verify on;",
    ])


def _header_block():
    """Security headers, from the base config, identical for every vhost.

    `headers` is deliberately NOT a model field in v1 — every structured field
    is another renderer input to test, and there is no requirement yet that
    these differ per site.
    """
    return "\n".join([
        "    add_header X-Content-Type-Options nosniff always;",
        "    add_header X-Frame-Options SAMEORIGIN always;",
        "    add_header Referrer-Policy strict-origin-when-cross-origin always;",
        "    add_header Strict-Transport-Security "
        "\"max-age=31536000; includeSubDomains\" always;",
    ])


def _open(server_name):
    return "\n".join([
        "server {",
        "    listen 443 ssl;",
        "    listen [::]:443 ssl;",
        "    http2 on;",
        f"    server_name {server_name};",
    ])


def _render_static(vhost, generation, server_name):
    """Static files. `proxy_pass` is not in this function's scope."""
    root = www_dir(generation, vhost.pk)
    return "\n".join([
        _open(server_name),
        "",
        _tls_block(generation, vhost.certificate_id),
        "",
        _header_block(),
        "",
        f"    root {root};",
        "    index index.html;",
        "",
        "    location / {",
        "        try_files $uri $uri/ =404;",
        "    }",
        "}",
        "",
    ])


def _render_spa(vhost, generation, server_name):
    """Single-page app: same as static, with a history fallback."""
    root = www_dir(generation, vhost.pk)
    return "\n".join([
        _open(server_name),
        "",
        _tls_block(generation, vhost.certificate_id),
        "",
        _header_block(),
        "",
        f"    root {root};",
        "    index index.html;",
        "",
        "    location / {",
        "        try_files $uri $uri/ /index.html;",
        "    }",
        "}",
        "",
    ])


def _render_proxy(vhost, generation, server_name):
    """Reverse proxy. No filesystem `root` is in this function's scope."""
    target = _safe_upstream_target(vhost.upstream)
    # nginx names a unix socket as `http://unix:/path/to.sock:/` — the trailing
    # `:/` is the URI part and is NOT optional; without it nginx refuses the
    # directive at parse time.
    if target.startswith("unix:"):
        destination = f"http://{target}:/"
    else:
        destination = f"http://{target}"
    return "\n".join([
        _open(server_name),
        "",
        _tls_block(generation, vhost.certificate_id),
        "",
        _header_block(),
        "",
        "    location / {",
        f"        proxy_pass {destination};",
        "        proxy_http_version 1.1;",
        "        proxy_set_header Host $host;",
        "        proxy_set_header X-Real-IP $remote_addr;",
        "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
        "        proxy_set_header X-Forwarded-Proto $scheme;",
        "        proxy_set_header Upgrade $http_upgrade;",
        "        proxy_set_header Connection $connection_upgrade;",
        "    }",
        "}",
        "",
    ])


_BUILDERS = {
    KIND_STATIC: _render_static,
    KIND_SPA: _render_spa,
    KIND_PROXY: _render_proxy,
}


def render_vhost(vhost, generation):
    """One vhost, one `server` block."""
    server_name = _safe_server_name(vhost.server_name)
    builder = _BUILDERS.get(vhost.kind)
    if builder is None:
        raise me.ValueException(f"edge renderer has no builder for {vhost.kind!r}")
    return builder(vhost, generation, server_name)


def render_nginx_harness(generation):
    """A minimal wrapper used ONLY by the staging `nginx -t` pre-filter.

    This is an approximation of the real config and is documented as such: a
    `map`, `upstream` or `limit_req_zone` defined in the deployment's own
    nginx.conf is absent here, so a generated file that references one passes
    or fails differently. The authoritative check is `nginx -t` against the
    REAL config after the swap — see services/installer.py.
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
        f"pid {gen}/nginx.pid;",
        f"error_log {gen}/error.log;",
        "events { worker_connections 64; }",
        "http {",
        f"    access_log {gen}/access.log;",
        f"    client_body_temp_path {gen}/tmp/client_body;",
        f"    proxy_temp_path {gen}/tmp/proxy;",
        f"    fastcgi_temp_path {gen}/tmp/fastcgi;",
        f"    uwsgi_temp_path {gen}/tmp/uwsgi;",
        f"    scgi_temp_path {gen}/tmp/scgi;",
        "    map $http_upgrade $connection_upgrade {",
        "        default upgrade;",
        "        '' close;",
        "    }",
        f"    include {gen}/conf.d/*.conf;",
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
        upstream = dict(
            kind=vhost.upstream.kind,
            host=vhost.upstream.host,
            port=vhost.upstream.port,
            socket_path=vhost.upstream.socket_path,
        )
    return dict(
        id=vhost.pk,
        server_name=vhost.server_name,
        kind=vhost.kind,
        pool=vhost.pool,
        certificate=vhost.certificate_id,
        upstream=upstream,
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
    """The payload a node polls for, and the thing the hash covers."""
    rows = sorted((vhost_payload(v) for v in vhosts), key=lambda r: r["id"])
    payload = dict(vhosts=rows, webapps=webapps or [])
    payload["generation"] = generation_id(payload)
    return payload


def render_generation(vhosts, generation):
    """Every enabled vhost, rendered into `{filename: text}`.

    The caller supplies `generation` because the id must be computed from the
    desired-state payload BEFORE rendering — the file contents reference the
    generation directory by absolute path, which is what lets `nginx -t`
    validate the new certificates rather than the currently-installed ones.
    """
    files = {}
    for vhost in vhosts:
        files[f"{int(vhost.pk)}.conf"] = render_vhost(vhost, generation)
    return files
