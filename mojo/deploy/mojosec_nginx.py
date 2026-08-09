"""Settings-free nginx fragments for the MojoSec security event stream.

The standard EC2 deploy path and Edge renderer share these builders so their
wire logs cannot quietly drift.  Values are deliberately narrower than nginx
syntax: paths and proxy networks are operator config, never arbitrary text.
"""

import ipaddress
import os
import re


SCHEMA = "mojosec.nginx"
VERSION = 1
DEFAULT_LOG_PATH = "/var/log/nginx/mojosec.json.log"
RECEIVER_PATH = "/api/incident/mojosec/batch"
RECEIVER_BODY_LIMIT = "512k"

_PATH_RE = re.compile(r"^/[A-Za-z0-9_./-]{1,511}$")


class NginxConfigError(ValueError):
    pass


def safe_log_path(value):
    value = str(value or "")
    if (not os.path.isabs(value) or not _PATH_RE.fullmatch(value) or
            "//" in value or "/../" in value or value.endswith("/..")):
        raise NginxConfigError("MojoSec nginx log path must be a simple absolute path")
    return value


def trusted_proxy_cidrs(value):
    """Return canonical, duplicate-free networks from a list or CSV string."""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        value = [item.strip() for item in value.split(",") if item.strip()]
    if not isinstance(value, (list, tuple)) or len(value) > 64:
        raise NginxConfigError("trusted proxy CIDRs must be a list of at most 64 networks")
    result = []
    seen = set()
    for item in value:
        if not isinstance(item, str):
            raise NginxConfigError("trusted proxy CIDRs must be strings")
        try:
            network = str(ipaddress.ip_network(item, strict=True))
        except ValueError as err:
            raise NginxConfigError(f"invalid trusted proxy CIDR: {item!r}") from err
        if network not in seen:
            seen.add(network)
            result.append(network)
    return result


def render_http_log(log_path=DEFAULT_LOG_PATH, proxy_cidrs=None):
    """Render http-context JSON logging and an exact trusted-proxy boundary.

    The dedicated root-only stream retains a bounded request target and the two
    specifically approved diagnostic headers. It never logs bodies, cookies,
    authorization, or arbitrary headers. `$realip_remote_addr` preserves the
    socket peer after realip has resolved `$remote_addr`.
    """
    path = safe_log_path(log_path)
    networks = trusted_proxy_cidrs(proxy_cidrs)
    lines = [
        "# MojoSec security log v1 — generated; do not hand-edit",
    ]
    if networks:
        lines.extend(f"set_real_ip_from {network};" for network in networks)
        lines.extend([
            "real_ip_header X-Forwarded-For;",
            "real_ip_recursive on;",
        ])
    lines.extend([
        "log_format mojosec_v1 escape=json",
        "    '{\"schema\":\"mojosec.nginx\",\"version\":1,'",
        "    '\"time\":\"$time_iso8601\",\"method\":\"$request_method\",'",
        "    '\"request_uri\":\"$request_uri\",\"status\":$status,'",
        "    '\"host\":\"$host\",\"referrer\":\"$http_referer\",'",
        "    '\"user_agent\":\"$http_user_agent\",'",
        "    '\"request_time\":\"$request_time\",'",
        "    '\"upstream_status\":\"$upstream_status\",'",
        "    '\"upstream_response_time\":\"$upstream_response_time\",'",
        "    '\"remote_addr\":\"$remote_addr\",'",
        "    '\"peer_addr\":\"$realip_remote_addr\"}';",
        f"access_log {path} mojosec_v1;",
    ])
    return "\n".join(lines) + "\n"


def render_receiver_location(proxy_include="/etc/nginx/asgi.inc", indent="    "):
    """Render the standard EC2 exact receiver route with its wire-body cap."""
    include = str(proxy_include or "")
    if not _PATH_RE.fullmatch(include):
        raise NginxConfigError("receiver proxy include must be a simple absolute path")
    blocks = []
    for path in (RECEIVER_PATH, RECEIVER_PATH + "/"):
        blocks.append("\n".join([
            f"{indent}location = {path} {{",
            f"{indent}    client_max_body_size {RECEIVER_BODY_LIMIT};",
            f"{indent}    include {include};",
            f"{indent}    proxy_pass http://asgi_upstream;",
            f"{indent}}}",
        ]))
    return "\n".join(blocks)
