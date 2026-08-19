"""Loopback-only deterministic support server for the Admin preview."""

import argparse
import email.utils
import http.client
import ipaddress
import json
import mimetypes
import secrets
import socket
import ssl
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

from .gallery import bootstrap, reset
from .features import activity, advanced, maintenance, platform, settings, webapps
from .features import dashboard


ROOT = Path(__file__).resolve().parents[2] / "mojo/apps/account/admin_portal"
HOST = "127.0.0.1"
PREVIEW_COOKIE = "mojo_admin_preview"
MAX_REQUEST_BODY = 2 * 1024 * 1024
MAX_RESPONSE_BODY = 8 * 1024 * 1024
PROXY_TIMEOUT = 10
PROXY_METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}
PROXY_PREFIXES = ("/api/", "/static/")
PROXY_ROUTES = ("/auth", "/register", "/passkey")
PREVIEW_SESSION_TTL = 8 * 60 * 60
PREVIEW_SESSION_CAP = 32


class PreviewProxyError(Exception):
    """Stable local proxy refusal which never includes upstream details."""


def parse_upstream(value):
    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError as error:
        raise PreviewProxyError("Upstream must be one public HTTPS hostname origin") from error
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or
            parsed.password or parsed.path not in ("", "/") or parsed.query or
            parsed.fragment or port not in (None, 443)):
        raise PreviewProxyError("Upstream must be one public HTTPS hostname origin")
    try:
        hostname = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as error:
        raise PreviewProxyError("Upstream must be one public HTTPS hostname origin") from error
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None or hostname == "localhost" or "." not in hostname:
        raise PreviewProxyError("Upstream must be one public HTTPS hostname origin")
    return {"origin": f"https://{hostname}", "hostname": hostname, "port": 443}


def resolve_public_addresses(hostname, port=443):
    try:
        answers = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise PreviewProxyError("Upstream hostname could not be resolved") from error
    addresses = []
    for family, _, _, _, sockaddr in answers:
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        text = sockaddr[0]
        try:
            address = ipaddress.ip_address(text)
        except ValueError as error:
            raise PreviewProxyError("Upstream DNS returned an invalid address") from error
        if getattr(address, "ipv4_mapped", None) is not None or not address.is_global:
            raise PreviewProxyError("Upstream DNS returned a non-public address")
        normalized = str(address)
        if normalized not in addresses:
            addresses.append(normalized)
    if not addresses:
        raise PreviewProxyError("Upstream hostname has no public address")
    return addresses


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname, address, port=443, timeout=PROXY_TIMEOUT):
        super().__init__(hostname, port=port, timeout=timeout,
                         context=ssl.create_default_context())
        self.pinned_address = address

    def connect(self):
        raw = socket.create_connection((self.pinned_address, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
            peer = ipaddress.ip_address(self.sock.getpeername()[0])
            expected = ipaddress.ip_address(self.pinned_address)
            if peer != expected:
                raise PreviewProxyError("Upstream peer did not match the pinned address")
        except Exception:
            raw.close()
            raise


def rewrite_location(value, upstream_origin, local_origin):
    if not value:
        return value
    absolute = urljoin(upstream_origin + "/", value)
    parsed = urlparse(absolute)
    if f"{parsed.scheme}://{parsed.netloc}" != upstream_origin:
        return value
    return local_origin + (parsed.path or "/") + (
        f"?{parsed.query}" if parsed.query else "") + (
        f"#{parsed.fragment}" if parsed.fragment else "")

USERS = [
    {"id": 1, "uuid": "11111111-1111-4111-8111-111111111111", "display_name": "Ian Smith", "email": "ian@example.com", "username": "ian@example.com", "phone_number": "+14155550101", "is_active": True, "is_email_verified": True, "is_phone_verified": True, "requires_password_change": False, "last_login": "2026-08-10T16:44:00Z", "last_activity": "2026-08-10T17:02:00Z", "permissions": {"manage_users": True, "manage_groups": True, "custom_operator": True}, "created": "2026-07-12T18:10:00Z"},
    {"id": 2, "uuid": "22222222-2222-4222-8222-222222222222", "display_name": "Avery Chen", "email": "avery@example.com", "username": "avery@example.com", "is_active": True, "is_email_verified": True, "is_phone_verified": False, "requires_password_change": True, "last_login": "2026-08-09T09:30:00Z", "last_activity": "2026-08-09T09:31:00Z", "permissions": {"view_users": True}, "created": "2026-07-27T09:30:00Z"},
    {"id": 3, "uuid": "33333333-3333-4333-8333-333333333333", "display_name": "Morgan Lee", "email": "morgan@example.com", "username": "morgan@example.com", "is_active": False, "is_email_verified": False, "is_phone_verified": False, "requires_password_change": False, "last_login": None, "last_activity": "2026-07-03T14:20:00Z", "permissions": {}, "created": "2026-06-03T14:20:00Z"},
]
GROUPS = [
    {"id": 7, "uuid": "77777777-7777-4777-8777-777777777777", "name": "MOJO Platform", "kind": "organization", "is_active": True, "parent": None, "member_count": 2, "last_activity": "2026-08-10T16:58:00Z", "metadata": {"region": "us-west"}},
    {"id": 9, "uuid": "99999999-9999-4999-8999-999999999999", "name": "Web Operations", "kind": "team", "is_active": True, "parent": {"id": 7, "name": "MOJO Platform"}, "member_count": 2, "last_activity": "2026-08-10T16:55:00Z", "metadata": {"pager": "web-ops"}},
]
MEMBERS = [
    {"id": 61, "group": {"id": 9, "name": "Web Operations"}, "user": USERS[0], "is_active": True, "permissions": {"manage_group": True}},
    {"id": 62, "group": {"id": 9, "name": "Web Operations"}, "user": USERS[1], "is_active": True, "permissions": {"guest": True}},
]
API_KEYS = [
    {"id": 91, "group": {"id": 9, "name": "Web Operations"}, "name": "deploy bot", "is_active": True, "created": "2026-07-20T12:00:00Z", "last_used": "2026-08-09T22:14:00Z", "permissions": {"manage_webapp": True}},
]
LOGIN_EVENTS = [
    {"id": 501, "user": 1, "created": "2026-08-10T16:44:00Z", "ip_address": "198.51.100.14", "country_code": "US", "region": "California", "city": "San Francisco", "device": {"id": 44, "name": "Desktop"}, "user_agent_info": {"browser": "Chrome", "platform": "macOS"}, "source": "password", "is_new_country": False, "is_new_region": False},
    {"id": 502, "user": 1, "created": "2026-08-08T08:12:00Z", "ip_address": "203.0.113.18", "country_code": "CA", "region": "Ontario", "city": "Toronto", "device": {"id": 45, "name": "Mobile"}, "user_agent_info": {"browser": "Safari", "platform": "iOS"}, "source": "passkey", "is_new_country": True, "is_new_region": True},
]
DOMAINS = [
    {"id": 11, "name": "nativemojo.com", "provider": "route53", "status": "active", "verified": True, "auto_renew": True, "privacy": True, "expires": "2027-07-20T00:00:00Z", "group": {"id": 7, "name": "MOJO Platform"}},
    {"id": 12, "name": "mojoapps.dev", "provider": "route53", "status": "active", "verified": True, "auto_renew": True, "privacy": True, "expires": "2027-02-14T00:00:00Z", "group": {"id": 9, "name": "Web Operations"}},
    {"id": 13, "name": "edge-preview.net", "provider": "godaddy", "status": "active", "verified": True, "expires": "2026-11-09T00:00:00Z", "group": {"id": 9, "name": "Web Operations"}},
]
CREDENTIALS = [
    {"id": 31, "name": "GoDaddy production", "provider": "godaddy", "is_active": True, "verified": True, "verified_at": "2026-08-08T19:00:00Z", "modified": "2026-08-08T19:00:00Z", "domain_count": 1, "api_key_masked": "********7F2A", "group": {"id": 9, "name": "Web Operations"}},
]
RECORDS = [
    {"type": "A", "name": "nativemojo.com", "record_values": ["203.0.113.42"], "ttl": 300},
    {"type": "CNAME", "name": "www.nativemojo.com", "record_values": ["nativemojo.com"], "ttl": 300},
    {"type": "MX", "name": "nativemojo.com", "record_values": ["10 inbound-smtp.us-west-2.amazonaws.com"], "ttl": 600},
    {"type": "TXT", "name": "nativemojo.com", "record_values": ["v=spf1 include:amazonses.com -all", "google-site-verification=mojo-preview"], "ttl": 300},
]
CERTIFICATES = [
    {"id": 21, "common_name": "nativemojo.com", "sans": ["nativemojo.com", "*.nativemojo.com"], "status": "active", "issuer": "Let's Encrypt", "days_remaining": 67, "not_after": "2026-10-16T08:00:00Z", "domain": {"id": 11, "name": "nativemojo.com"}},
    {"id": 22, "common_name": "mojoapps.dev", "sans": ["mojoapps.dev", "www.mojoapps.dev"], "status": "issuing", "issuer": None, "days_remaining": None, "domain": {"id": 12, "name": "mojoapps.dev"}},
]
UPSTREAMS = [
    {"id": 51, "name": "django-api", "kind": "http", "host": "127.0.0.1", "port": 8000, "socket_path": None, "is_enabled": True, "group": None},
    {"id": 52, "name": "realtime", "kind": "unix", "host": None, "port": None, "socket_path": "/run/mojo/realtime.sock", "is_enabled": True, "group": None},
    {"id": 53, "name": "legacy-api", "kind": "http", "host": "127.0.0.1", "port": 7000, "socket_path": None, "is_enabled": False, "group": {"id": 9, "name": "Web Operations"}},
]
VHOSTS = [
    {"id": 61, "server_name": "api.nativemojo.com", "label": "api", "kind": "api", "pool": "public-web", "is_enabled": True, "body_size_mb": 50, "domain": {"id": 11, "name": "nativemojo.com"}, "upstream": {"id": 51, "name": "django-api"}, "certificate": {"id": 21, "common_name": "nativemojo.com"}},
    {"id": 62, "server_name": "nativemojo.com", "label": "", "kind": "site_api", "pool": "public-web", "is_enabled": True, "body_size_mb": 50, "spa": True, "serve_static": False, "domain": {"id": 11, "name": "nativemojo.com"}, "certificate": {"id": 21, "common_name": "nativemojo.com"}},
    {"id": 63, "server_name": "www.nativemojo.com", "label": "www", "kind": "redirect", "pool": "public-web", "is_enabled": True, "redirect_to": "nativemojo.com", "domain": {"id": 11, "name": "nativemojo.com"}, "certificate": {"id": 21, "common_name": "nativemojo.com"}},
]
ROUTES = [
    {"id": 71, "path_prefix": "/api", "modified": "2026-08-09T21:00:00Z", "vhost": {"id": 62, "server_name": "nativemojo.com"}, "upstream": {"id": 51, "name": "django-api"}},
    {"id": 72, "path_prefix": "/ws", "modified": "2026-08-09T21:01:00Z", "vhost": {"id": 62, "server_name": "nativemojo.com"}, "upstream": {"id": 52, "name": "realtime"}},
]
WEBAPPS = [
    {"id": 42, "slug": "mojo-portal", "display_name": "MOJO Portal", "environment": "production", "github_repository": "NativeMojo/portal", "deployment_ref": "main", "build_output": "dist", "created": "2026-07-19T10:00:00Z", "current_release": {"id": 18, "version": "8d42ea1"}},
    {"id": 54, "slug": "docs", "display_name": "Documentation", "environment": "production", "github_repository": "NativeMojo/docs", "deployment_ref": "main", "build_output": "site", "created": "2026-07-30T16:40:00Z", "current_release": None},
]


def webapp_onboarding_operation(state="address"):
    cursors = {"address": "address", "github": "github", "verify": "verify",
               "complete": "complete", "lost_key": "complete",
               "new_group": "address"}
    cursor = cursors.get(state, "address")
    completed = ["app", "address", "github", "verify"].index(cursor) if cursor != "complete" else 4
    evidence = {}
    for key in ["app", "address", "github", "verify"][:completed]:
        evidence[key] = {"status": "verified"}
    if state == "lost_key":
        evidence["deployment_key"] = {"status": "secret_unavailable"}
    return {
        "schema_version": 1, "id": 1816,
        "operation_id": "00000000-0000-4000-8000-000000001816",
        "group": {"id": 109, "name": "Customer Portal"},
        "status": "succeeded" if cursor == "complete" else "waiting",
        "cursor": cursor, "revision": completed + 2,
        "profile": {"slug": "customer-portal", "display_name": "Customer Portal",
                    "environment": "production", "bucket": "mojo-releases",
                    "github_repository": "NativeMojo/customer-portal",
                    "deployment_ref": "main", "build_output": "dist"},
        "choices": {}, "evidence": evidence,
        "resources": {"webapp": 42 if completed else None, "domain": 11 if completed > 1 else None,
                      "certificate": 21 if completed > 1 else None, "vhost": 62 if completed > 1 else None},
        "attempts": 0, "last_error": "", "activity": [
            {"at": "2026-08-10T17:00:00Z", "kind": "info",
             "message": "Application intent reconciled"}],
    }


def readiness_sections():
    def section(code, label, status, checks):
        return {"code": code, "label": label, "status": status, "checks": checks}

    def check(code, status, explanation, remediation="", fixable=False):
        return {"code": code, "status": status, "explanation": explanation, "remediation": remediation, "fixable": fixable}

    return [
        section("django", "Django installation", "fail", [
            check("django.apps", "pass", "Django applications loaded successfully."),
            check("django.database", "pass", "The database answered a query."),
            check("django.local_request", "pass", "The local API answered over HTTP.",
                  fixable=False),
            check("django.base_url", "fail", "The public BASE_URL is not configured safely.",
                  "Choose the canonical public HTTPS origin in Fix Setup.", True),
            check("django.public_api", "pass", "The public API health probe succeeded."),
        ]),
        section("aws_identity", "AWS identity", "pass", [check("aws.identity", "pass", "AWS account 123456789012 in us-west-2 is active.")]),
        section("aws_s3", "System S3 storage", "pass", [check("aws.s3", "pass", "Private system FileManager uses mojo-media-production with direct-upload CORS.")]),
        section("aws_email", "SES email", "warn", [check("aws.sender", "warn", "The sender mailbox is awaiting final verification.", "Select a verified SES domain and sender in Fix Setup.", True)]),
        section("aws_monitoring", "SNS and CloudWatch", "pass", [check("aws.monitoring", "pass", "Operations topic, subscription, alarms, and delivery proof are ready.")]),
        section("hosting_dns", "Domains and certificates", "warn", [check("hosting.dns", "warn", "DNS and certificate readiness: 4 ready, 1 warning, 0 pending, 0 failed across 5 items.", "Open Certificates to finish the issuing certificate.")]),
        section("hosting_vhosts", "Vhosts and routes", "pass", [check("hosting.vhosts", "pass", "Vhost readiness: 3 ready, 0 warning, 0 pending, 0 failed across 3 items.")]),
        section("edge_fleet", "Fleet deployment readiness", "pass", [check("fleet.summary", "pass", "Fleet node/pool readiness: 4 ready, 0 warning, 0 pending, 0 failed across 4 node/pool pairs.")]),
        section("webapp_keys", "WebApp deployment keys", "warn", [check("webapp.keys", "warn", "WebApp deployment-key readiness: 1 ready, 1 warning, 0 pending, 0 failed across 2 items.", "Open WebApps to create or rotate MOJO_DEPLOY_KEY.")]),
    ]


def setup_choice_operation():
    step = {
        "id": "base_url", "definition_version": 1, "choice_revision": 0,
        "label": "Configure public BASE_URL", "state": "waiting_for_choice",
        "choice_schema": {"type": "object", "properties": {
            "base_url": {"type": "string", "format": "https-origin"},
        }, "required": ["base_url"], "additionalProperties": False},
    }
    return {"id": "preview-choice", "mode": "fix", "status": "waiting_for_choice",
            "cursor": 0, "steps": [step], "current_step": step,
            "log": [{"at": "2026-08-10T03:00:00Z",
                     "message": "Choice required for public BASE_URL"}]}


def setup_planned_operation(mode="fix"):
    step = {"id": "installation_identity", "definition_version": 1,
            "choice_revision": 0, "label": "Freeze installation identity",
            "state": "planned"}
    return {"id": "preview-setup", "mode": mode, "status": "planned",
            "cursor": 0, "steps": [step], "current_step": step, "log": []}


def setup_complete_operation(mode="fix"):
    step = {"id": "installation_identity", "definition_version": 1,
            "choice_revision": 0, "label": "Freeze installation identity",
            "state": "proven"}
    return {"id": "preview-setup", "mode": mode, "status": "succeeded",
            "cursor": 1, "steps": [step], "current_step": None,
            "report": {"overall": "warn", "summary": {
                "pass": 8, "warn": 2, "fail": 0, "pending": 0,
            }, "sections": readiness_sections()},
            "log": [{"at": "2026-08-10T03:00:00Z",
                     "message": "Installation identity proven"}]}


class PreviewHandler(BaseHTTPRequestHandler):
    key_state = "active"
    setup_operation = None
    setup_behavior = "idle"
    events = []
    records = []
    credentials = []
    vhosts = []
    routes = []
    onboarding_operation = None
    onboarding_state = "idle"
    onboarding_receipts = {}
    webapps = []
    users = []
    groups = []
    members = []
    api_keys = []
    permission_bundles = {}
    upstream = None
    preview_port = None
    preview_sessions = {}
    preview_session_lock = threading.Lock()

    def log_message(self, fmt, *args):
        return

    def _send(self, body, content_type="application/json", status=200,
              headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps({"status": True, "data": body}, default=str).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _local_origins(self):
        port = type(self).preview_port
        return {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    def _request_preview_token(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return None
        token = cookie.get(PREVIEW_COOKIE)
        return token.value if token is not None else None

    def _preview_cookie_ok(self):
        token = self._request_preview_token()
        if not token:
            return False
        now = time.time()
        with type(self).preview_session_lock:
            session = type(self).preview_sessions.get(token)
            if session is None or now - session["seen"] > PREVIEW_SESSION_TTL:
                type(self).preview_sessions.pop(token, None)
                return False
            session["seen"] = now
        self.preview_session_token = token
        return True

    def _proxy_gate(self):
        host = self.headers.get("Host", "").lower()
        allowed_hosts = {urlparse(value).netloc for value in self._local_origins()}
        if host not in allowed_hosts or not self._preview_cookie_ok():
            raise PreviewProxyError("Preview request gate rejected the request")
        fetch_site = self.headers.get("Sec-Fetch-Site", "").lower()
        if fetch_site and fetch_site not in {"same-origin", "none"}:
            raise PreviewProxyError("Preview request gate rejected the request")
        origin = self.headers.get("Origin")
        if origin and origin not in self._local_origins():
            raise PreviewProxyError("Preview request gate rejected the request")
        if self.command not in {"GET", "HEAD"} and origin not in self._local_origins():
            raise PreviewProxyError("Preview mutations require a same-origin browser request")

    def _proxy_allowed(self, path):
        if (not path.startswith("/") or "%" in path or "\\" in path or
                "//" in path or any(part in (".", "..") for part in path.split("/"))):
            return False
        if path == "/api" or any(path.startswith(prefix) for prefix in PROXY_PREFIXES):
            return True
        return any(path == route or path.startswith(route + "/")
                   for route in PROXY_ROUTES)

    def _proxy_body(self):
        transfer = self.headers.get("Transfer-Encoding", "").lower()
        if transfer:
            raise PreviewProxyError("Chunked preview requests are not supported")
        try:
            size = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError as error:
            raise PreviewProxyError("Invalid request size") from error
        if size < 0 or size > MAX_REQUEST_BODY:
            raise PreviewProxyError("Preview request body is too large")
        return self.rfile.read(size) if size else None

    def _proxy_headers(self):
        upstream = type(self).upstream
        headers = {"Host": upstream["hostname"], "Accept": self.headers.get("Accept", "*/*")}
        for name in ("Accept-Language", "Content-Type", "Authorization",
                     "User-Agent", "X-CSRFToken"):
            value = self.headers.get(name)
            if value:
                headers[name] = value
        if self.headers.get("Origin"):
            headers["Origin"] = upstream["origin"]
        if self.headers.get("Referer"):
            headers["Referer"] = upstream["origin"] + "/"
        cookies = self._cookies_for_request(self.path)
        if cookies:
            headers["Cookie"] = "; ".join(
                f"{name}={value}" for name, value in sorted(cookies.items()))
        return headers

    def _cookies_for_request(self, path):
        token = getattr(self, "preview_session_token", None)
        if not token:
            return {}
        now = time.time()
        upstream = type(self).upstream
        request_path = urlparse(path).path
        selected = {}
        with type(self).preview_session_lock:
            session = type(self).preview_sessions.get(token)
            if not session:
                return {}
            for cookie_key, record in list(session["cookies"].items()):
                if record["expires"] is not None and record["expires"] <= now:
                    session["cookies"].pop(cookie_key, None)
                    continue
                domain = record["domain"]
                if not (upstream["hostname"] == domain or
                        upstream["hostname"].endswith("." + domain)):
                    continue
                cookie_path = record["path"]
                if request_path == cookie_path or request_path.startswith(
                        cookie_path.rstrip("/") + "/"):
                    current = selected.get(record["name"])
                    if current is None or len(cookie_path) > len(current["path"]):
                        selected[record["name"]] = record
        return {name: record["value"] for name, record in selected.items()}

    def _remember_upstream_cookies(self, response):
        token = getattr(self, "preview_session_token", None)
        if not token:
            return
        upstream = type(self).upstream
        request_path = urlparse(self.path).path
        default_path = request_path.rsplit("/", 1)[0] or "/"
        found = []
        for name, value in response.getheaders():
            if name.lower() != "set-cookie":
                continue
            cookie = SimpleCookie()
            try:
                cookie.load(value)
            except Exception:
                continue
            for key, morsel in cookie.items():
                domain = (morsel["domain"] or upstream["hostname"]).lstrip(".").lower()
                if not (upstream["hostname"] == domain or
                        upstream["hostname"].endswith("." + domain)):
                    continue
                expires = None
                if morsel["max-age"]:
                    try:
                        expires = time.time() + int(morsel["max-age"])
                    except ValueError:
                        continue
                elif morsel["expires"]:
                    try:
                        expires = email.utils.parsedate_to_datetime(
                            morsel["expires"]).timestamp()
                    except (TypeError, ValueError, OverflowError):
                        continue
                cookie_path = morsel["path"] or default_path
                if not cookie_path.startswith("/"):
                    continue
                record = {"name": key, "value": morsel.value, "domain": domain,
                          "path": cookie_path, "expires": expires}
                found.append(((key, domain, cookie_path), record))
        if found:
            with type(self).preview_session_lock:
                session = type(self).preview_sessions.get(token)
                if not session:
                    return
                for key, record in found:
                    if record["expires"] is not None and record["expires"] <= time.time():
                        session["cookies"].pop(key, None)
                    else:
                        session["cookies"][key] = record

    def _proxy(self):
        try:
            if self.command not in PROXY_METHODS:
                raise PreviewProxyError("Preview method is not allowed")
            parsed = urlparse(self.path)
            if parsed.scheme or parsed.netloc:
                raise PreviewProxyError("Absolute proxy request targets are not allowed")
            if not self._proxy_allowed(parsed.path):
                raise PreviewProxyError("Preview path is not allowed")
            self._proxy_gate()
            body = self._proxy_body()
            upstream = type(self).upstream
            addresses = resolve_public_addresses(upstream["hostname"], upstream["port"])
            response = None
            last_error = None
            for address in addresses:
                connection = PinnedHTTPSConnection(upstream["hostname"], address,
                                                   upstream["port"])
                try:
                    connection.request(self.command, self.path, body=body,
                                       headers=self._proxy_headers())
                    response = connection.getresponse()
                    break
                except (OSError, ssl.SSLError, http.client.HTTPException) as error:
                    last_error = error
                    connection.close()
            if response is None:
                raise PreviewProxyError("Upstream request failed") from last_error
            declared = response.getheader("Content-Length")
            if declared and int(declared) > MAX_RESPONSE_BODY:
                response.close()
                raise PreviewProxyError("Upstream response body is too large")
            payload = response.read(MAX_RESPONSE_BODY + 1)
            if len(payload) > MAX_RESPONSE_BODY:
                raise PreviewProxyError("Upstream response body is too large")
            self._remember_upstream_cookies(response)
            local_origin = f"http://{self.headers['Host']}"
            response_headers = {}
            for name in ("Content-Type", "Cache-Control", "Pragma", "Expires",
                         "ETag", "Last-Modified", "Content-Disposition",
                         "WWW-Authenticate", "Content-Security-Policy",
                         "Referrer-Policy", "X-Content-Type-Options"):
                value = response.getheader(name)
                if value:
                    response_headers[name] = value
            location = response.getheader("Location")
            if location:
                response_headers["Location"] = rewrite_location(
                    location, upstream["origin"], local_origin)
            return self._send(payload, response_headers.pop(
                "Content-Type", "application/octet-stream"), response.status,
                response_headers)
        except PreviewProxyError as error:
            return self._send({"error": str(error)}, status=403)
        except (OSError, ssl.SSLError, http.client.HTTPException, ValueError):
            return self._send({"error": "Upstream request failed"}, status=502)

    def _serve_preview_context(self):
        try:
            if not type(self).upstream:
                raise PreviewProxyError("Live preview context is unavailable")
            self._proxy_gate()
            return self._send({
                "schema_version": 1,
                "suggested_base_url": type(self).upstream["origin"],
            })
        except PreviewProxyError as error:
            return self._send({"error": str(error)}, status=403)

    def _serve_admin(self, parsed):
        if parsed.path in ("/", "/admin", "/admin/"):
            target = ROOT / "index.html"
        else:
            relative = parsed.path.removeprefix("/admin/").lstrip("/")
            target = (ROOT / relative).resolve()
            if ROOT.resolve() not in target.parents:
                return self._send("Not found", "text/plain", 404)
        if not target.is_file():
            return self._send("Not found", "text/plain", 404)
        headers = None
        if type(self).upstream:
            token = self._request_preview_token()
            now = time.time()
            with type(self).preview_session_lock:
                session = type(self).preview_sessions.get(token)
                if session is None or now - session["seen"] > PREVIEW_SESSION_TTL:
                    token = secrets.token_urlsafe(32)
                    type(self).preview_sessions[token] = {"seen": now, "cookies": {}}
                else:
                    session["seen"] = now
                expired = sorted(type(self).preview_sessions.items(),
                                 key=lambda item: item[1]["seen"])
                while len(expired) > PREVIEW_SESSION_CAP:
                    old_token, _ = expired.pop(0)
                    type(self).preview_sessions.pop(old_token, None)
            headers = {"Set-Cookie": (
                f"{PREVIEW_COOKIE}={token}; Path=/; "
                "HttpOnly; SameSite=Strict")}
        return self._send(target.read_bytes(), mimetypes.guess_type(
            target.name)[0] or "application/octet-stream", headers=headers)

    def _key_status(self):
        states = {
            "missing": {"linked": False, "active": False, "api_key": None, "last_action": None},
            "active": {"linked": True, "active": True, "api_key": 91, "name": "webapp:mojo-portal", "created": "2026-07-20T12:00:00Z", "last_used": "2026-08-09T22:14:00Z", "last_action": "mint"},
            "rotated": {"linked": True, "active": True, "api_key": 94, "name": "webapp:mojo-portal", "created": "2026-08-09T23:00:00Z", "last_used": None, "last_action": "rotate"},
            "revoked": {"linked": False, "active": False, "api_key": None, "last_action": "revoke", "last_operation_at": "2026-08-09T23:30:00Z"},
        }
        return states[self.key_state]

    @classmethod
    def _safe_payload(cls, payload):
        hidden = {
            "api_key", "api_secret", "confirm_token", "token", "password",
            "temporary_password", "forced_password_token", "rotated_token",
        }
        if isinstance(payload, dict):
            return {key: "[redacted]" if key.lower() in hidden else cls._safe_payload(value)
                    for key, value in payload.items()}
        if isinstance(payload, list):
            return [cls._safe_payload(value) for value in payload]
        return payload

    def _record_event(self, path, payload):
        if path == "/api/account/admin/settings" and isinstance(payload, dict):
            payload = {key: ("[redacted]" if key == "value" else value)
                       for key, value in payload.items()
                       if key in {"action", "key", "value"}}
        type(self).events.append({"path": path,
                                  "payload": self._safe_payload(payload)})

    def _api(self, parsed):
        path = parsed.path
        if path == "/api/account/static/mojo-auth.js":
            return self._send("window.MojoAuth={init:function(){},getAuthHeader:function(){return 'Bearer preview';},getRefreshToken:function(){return null;},logout:function(){}};", "application/javascript; charset=utf-8")
        if path == "/api/account/admin/bootstrap":
            memberships = [] if self.onboarding_state == "new_group" else None
            return self._send(bootstrap(self.groups, membership_groups=memberships))
        activity_response = activity.get(self, parsed)
        if activity_response is not None:
            status, payload = activity_response
            return self._send(payload, status=status)
        for provider in (dashboard, webapps, platform, advanced, settings, maintenance):
            response = provider.get(self, parsed)
            if response is not None:
                status, payload = response
                return self._send(payload, status=status)
        if path == "/api/account/admin/setup/options":
            active = self.setup_operation
            if active and active.get("status") in {"succeeded", "failed", "cancelled"}:
                active = None
            return self._send({"schema_version": 1, "active_fix": active, "sections": [{"code": row["code"], "label": row["label"], "fixable": row["code"] in {"django", "aws_s3", "aws_email", "aws_monitoring"}, "choice_schema": None} for row in readiness_sections()]})
        if path == "/api/account/admin/setup/readiness":
            sections = readiness_sections()
            wanted = parse_qs(parsed.query).get("section", [None])[0]
            if wanted:
                sections = [row for row in sections if row["code"] == wanted]
            summary = {key: 0 for key in ("pass", "warn", "fail", "pending")}
            for row in sections:
                for item in row["checks"]:
                    summary[item["status"]] += 1
            overall = "fail" if summary["fail"] else "pending" if summary["pending"] else "warn" if summary["warn"] else "pass"
            return self._send({"schema_version": 1, "overall": overall, "summary": summary, "sections": sections})
        if path == "/api/account/admin/setup/detail":
            if not self.setup_operation:
                return self._send({"error": "Setup operation not found"}, status=404)
            return self._send(self.setup_operation)
        fixtures = {
            "/api/user": self.users, "/api/group": self.groups,
            "/api/group/member": self.members, "/api/group/apikey": self.api_keys,
            "/api/account/logins": LOGIN_EVENTS,
            "/api/dnsman/domain": DOMAINS,
            "/api/dnsman/credential": self.credentials, "/api/dnsman/certificate": CERTIFICATES,
            "/api/edge/upstream": UPSTREAMS, "/api/edge/vhost": self.vhosts,
            "/api/edge/route": self.routes, "/api/edge/webapp": self.webapps,
        }
        if path in fixtures:
            rows = fixtures[path]
            query = parse_qs(parsed.query)
            if path in ("/api/user", "/api/group") and query.get("search"):
                term = query["search"][0].lower()
                fields = ("display_name", "email", "username") if path == "/api/user" else ("name", "kind")
                rows = [row for row in rows if any(term in str(row.get(field, "")).lower() for field in fields)]
            if path in ("/api/group/member", "/api/group/apikey") and query.get("group"):
                group_id = int(query["group"][0])
                rows = [row for row in rows if row["group"]["id"] == group_id]
            if path == "/api/account/logins" and query.get("user"):
                user_id = int(query["user"][0])
                rows = [row for row in rows if row["user"] == user_id]
            return self._send(rows)
        if path == "/api/account/admin/people/permission-bundles":
            user_id = int(parse_qs(parsed.query).get("user", ["0"])[0])
            selected = self.permission_bundles.get(user_id, [])
            return self._send({"version": 1, "selected": selected, "bundles": [
                {"id": "people", "label": "People", "permissions": ["view_users", "manage_users", "view_groups", "manage_groups"]},
                {"id": "platform", "label": "Platform", "permissions": ["view_admin", "view_global", "manage_aws"]},
                {"id": "network_hosting", "label": "Network & Hosting", "permissions": ["view_dns", "manage_dns"]},
                {"id": "deployments", "label": "Deployments", "permissions": ["manage_webapp", "manage_deploy", "view_github", "manage_github"]},
                {"id": "security_incidents", "label": "Security & Incidents", "permissions": ["view_security", "manage_security"]},
                {"id": "logs_metrics", "label": "Logs & Metrics", "permissions": ["view_logs", "manage_logs", "view_metrics", "manage_metrics"]},
                {"id": "system_administration", "label": "System Administration", "permissions": ["manage_settings", "view_jobs", "manage_jobs", "view_taskqueue", "admin_compliance", "admin_verify"]},
            ]})
        for prefix, rows in (("/api/user/", self.users), ("/api/group/", self.groups)):
            if path.startswith(prefix):
                try:
                    pk = int(path.rsplit("/", 1)[-1])
                except ValueError:
                    continue
                return self._send(next((row for row in rows if row["id"] == pk), {}))
        for prefix, rows in (("/api/dnsman/domain/", DOMAINS), ("/api/edge/vhost/", self.vhosts)):
            if path.startswith(prefix):
                pk = int(path.rsplit("/", 1)[-1])
                return self._send(next((row for row in rows if row["id"] == pk), {}))
        if path == "/api/dnsman/dns":
            return self._send({"domain": "nativemojo.com", "provider": "route53", "records": self.records})
        if path == "/api/dnsman/config":
            return self._send({"purchase_enabled": True, "registrant_contact_configured": True, "max_domain_price": "50.00", "currency": "USD", "providers": [{"name": "route53", "purchase": True}, {"name": "godaddy", "requires_credential": True}]})
        if path == "/api/dnsman/registrar/discover":
            return self._send({"domains": [{"name": "adopt-me.example", "hosted": True, "registered": False}], "truncated": False})
        if path == "/api/edge/webapp/key_status":
            return self._send({"webapp": 42, "secret_name": "MOJO_DEPLOY_KEY", "status": self._key_status()})
        if path == "/api/edge/webapp/onboarding/options":
            new_intent = parse_qs(parsed.query).get("group_intent") == ["new"]
            return self._send({"schema_version": 1, "buckets": ["mojo-releases"],
                               "environments": ["production", "staging", "preview", "development"],
                               "cname_target": "edge.nativemojo.com",
                               "group_intent": "new" if new_intent else "existing",
                               "github_connected": False if new_intent else True,
                               "limits": {"attempts": 8, "lease_seconds": 90}})
        if path == "/api/edge/webapp/summary":
            return self._send({"schema_version": 1, "webapp": WEBAPPS[0],
                               "address": {"hostname": "portal.nativemojo.com", "https_origin": "https://portal.nativemojo.com"},
                               "onboarding": {"status": "succeeded", "cursor": "complete", "evidence": {}},
                               "deployment_key": {"linked": True, "active": True}})
        return self._send({"error": "Not found"}, status=404)

    def do_GET(self):
        parsed = urlparse(self.path)
        if type(self).upstream:
            if parsed.path == "/":
                return self._send("", "text/plain", 302,
                                  {"Location": "/admin/"})
            if parsed.path == "/__preview__/context":
                return self._serve_preview_context()
            if parsed.path in ("/admin", "/admin/") or parsed.path.startswith("/admin/assets/"):
                return self._serve_admin(parsed)
            return self._proxy()
        if parsed.path == "/__preview__/events":
            return self._send(self.events)
        if parsed.path.startswith("/api/"):
            return self._api(parsed)
        return self._serve_admin(parsed)

    def _read_body(self):
        size = int(self.headers.get("Content-Length", "0"))
        if not size:
            return {}
        try:
            return json.loads(self.rfile.read(size))
        except (ValueError, TypeError):
            return {}

    def do_POST(self):
        if type(self).upstream:
            return self._proxy()
        path = urlparse(self.path).path
        payload = self._read_body()
        self._record_event(path, payload)
        for provider in (webapps, platform, advanced, settings, maintenance):
            response = provider.post(self, path, payload)
            if response is not None:
                status, body = response
                return self._send(body, status=status)
        if path == "/api/edge/webapp/link_key":
            type(self).key_state = "rotated" if payload.get("action") == "rotate" else "active"
            return self._send({"webapp": 42, "secret_name": "MOJO_DEPLOY_KEY", "replayed": False, "operation_id": "preview", "token": "preview-token-shown-once", "status": self._key_status()})
        if path == "/api/edge/webapp/revoke_key":
            type(self).key_state = "revoked"
            return self._send({"webapp": 42, "secret_name": "MOJO_DEPLOY_KEY", "replayed": False, "operation_id": "preview", "status": self._key_status()})
        if path == "/api/edge/webapp/onboarding/choose":
            order = {"app": "address", "address": "github", "github": "verify", "verify": "complete"}
            current = self.onboarding_operation or webapp_onboarding_operation("address")
            next_state = order.get(current.get("cursor"), "complete")
            type(self).onboarding_operation = webapp_onboarding_operation(next_state)
            return self._send(self.onboarding_operation)
        if path == "/api/edge/webapp/onboarding/cancel":
            current = self.onboarding_operation or webapp_onboarding_operation("address")
            current.update({"status": "cancelled"})
            type(self).onboarding_operation = current
            return self._send(current)
        if path == "/api/edge/webapp/onboarding/workflow":
            return self._send({"schema_version": 1, "repository": "NativeMojo/portal",
                               "filename": ".github/workflows/deploy-webapp.yml",
                               "yaml": "name: Deploy WebApp\npermissions:\n  contents: read\njobs:\n  deploy:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - run: npm ci\n"})
        if path == "/api/account/admin/user/password/reset":
            return self._send({"user": payload.get("user"), "sent": True})
        if path == "/api/account/admin/user/password/temporary":
            return self._send({"user": payload.get("user"), "temporary_password": "Preview-Only-Temp-72!", "requires_password_change": True})
        if path == "/api/account/admin/people/permission-bundles":
            type(self).permission_bundles[int(payload.get("user"))] = list(payload.get("selected", []))
            return self._send({"version": payload.get("version", 1), "user": payload.get("user"), "selected": payload.get("selected", []), "bundles": []})
        if path == "/api/account/admin/apikey/action":
            key = next((row for row in self.api_keys if row["id"] == int(payload.get("api_key", 0))), None)
            if key is None:
                return self._send({"error": "API key not found"}, status=404)
            action = payload.get("action")
            if action == "rotate":
                return self._send({"id": key["id"], "action": action, "token": "preview-rotated-token-shown-once"})
            if action in ("deactivate", "reactivate"):
                key["is_active"] = action == "reactivate"
            elif action == "revoke":
                type(self).api_keys = [row for row in self.api_keys if row["id"] != key["id"]]
                return self._send({"id": key["id"], "action": action, "revoked": True})
            return self._send({"id": key["id"], "action": action, "is_active": key["is_active"]})
        if path == "/api/group/apikey":
            row = {"id": 100 + len(self.api_keys), "group": {"id": int(payload.get("group")), "name": "Web Operations"}, "name": payload.get("name"), "is_active": True, "last_used": None, "permissions": payload.get("permissions", {})}
            type(self).api_keys.append(row)
            return self._send({**row, "token": "preview-new-token-shown-once"})
        if path == "/api/group/member":
            row = {"id": 100 + len(self.members), "group": {"id": int(payload.get("group")), "name": "Web Operations"}, "user": next((user for user in self.users if user["id"] == int(payload.get("user"))), {}), "is_active": True, "permissions": payload.get("permissions", {})}
            type(self).members.append(row)
            return self._send(row)
        if path == "/api/user":
            row = {"id": 100 + len(self.users), "uuid": f"preview-user-{100 + len(self.users)}", "is_active": True, "is_email_verified": False, "is_phone_verified": False, "requires_password_change": False, "permissions": {}, "last_login": None, "last_activity": None, **payload}
            type(self).users.append(row)
            return self._send(row)
        if path == "/api/group":
            parent_id = payload.get("parent")
            parent = next(({"id": row["id"], "name": row["name"]} for row in self.groups if str(row["id"]) == str(parent_id)), None)
            row = {"id": 100 + len(self.groups), "uuid": f"preview-group-{100 + len(self.groups)}", "is_active": True, "member_count": 0, "last_activity": None, "metadata": {}, **payload, "parent": parent}
            type(self).groups.append(row)
            return self._send(row)
        for prefix, rows in (("/api/user/", self.users), ("/api/group/", self.groups)):
            if path.startswith(prefix):
                try:
                    pk = int(path.rsplit("/", 1)[-1])
                except ValueError:
                    continue
                row = next((item for item in rows if item["id"] == pk), None)
                if row is not None:
                    if "disable" in payload:
                        row["is_active"] = False
                    elif "reactivate" in payload:
                        row["is_active"] = True
                    else:
                        row.update(payload)
                    return self._send(row)
        if path == "/api/account/admin/setup/create":
            type(self).setup_operation = setup_planned_operation(payload.get("mode", "fix"))
            if self.setup_behavior == "delay":
                time.sleep(1.25)
            if self.setup_behavior == "error":
                type(self).setup_operation = None
                return self._send({"error": "Deterministic Setup failure"}, status=503)
            if self.setup_behavior == "fresh":
                return self._send({"error": "Fresh authentication required"}, status=440)
            if self.setup_behavior == "ambiguous":
                return self._send({"error": "Deterministic lost Setup response"}, status=503)
            return self._send(self.setup_operation)
        if path == "/api/account/admin/setup/advance":
            if self.setup_behavior == "delay":
                time.sleep(1.25)
            type(self).setup_operation = setup_complete_operation((self.setup_operation or {}).get("mode", "fix"))
            return self._send(self.setup_operation)
        if path == "/api/account/admin/setup/choose":
            type(self).setup_operation = setup_planned_operation("fix")
            self.setup_operation["log"] = [{"at": "2026-08-10T03:00:01Z",
                                             "message": "Public BASE_URL choice accepted"}]
            return self._send(self.setup_operation)
        if path == "/api/account/admin/setup/cancel":
            type(self).setup_operation = {"id": payload.get("operation", "preview-choice"),
                                          "mode": "fix", "status": "cancelled",
                                          "cursor": 0, "steps": [], "current_step": None,
                                          "log": []}
            return self._send(self.setup_operation)
        if path == "/api/dnsman/registrar/search":
            return self._send({"name": payload.get("domain", "preview.dev"), "available": True, "price": "14.00", "currency": "USD"})
        if path == "/api/dnsman/registrar/quote":
            return self._send({"purchase": 88, "name": payload.get("domain"), "price": "14.00", "currency": "USD", "years": 1, "token": "preview-quote-token", "expires": "2026-08-10T07:15:00Z"})
        if path == "/api/dnsman/credential/link":
            if payload.get("api_key") == "reject":
                return self._send({"error": "Credential verification failed"}, status=400)
            row = {"id": payload.get("credential", 99), "name": payload.get("name"),
                   "provider": payload.get("provider", "godaddy"), "is_active": True,
                   "verified": True, "modified": "2026-08-10T04:00:00Z",
                   "api_key_masked": "********SAFE", "domain_count": 0,
                   "group": {"id": int(payload.get("group", 9)), "name": "Web Operations"}}
            type(self).credentials = [item for item in self.credentials if item["id"] != row["id"]] + [row]
            return self._send(row)
        if path == "/api/dnsman/dns":
            identity = (str(payload.get("type", "")).upper(), str(payload.get("name", "")).rstrip(".").lower())
            if payload.get("name") == "unconfirmed":
                return self._send({"error": "Ambiguous provider result"}, status=503)
            row = {"type": identity[0], "name": payload.get("name"),
                   "record_values": payload.get("record_values", []),
                   "ttl": payload.get("ttl", 300)}
            type(self).records = [item for item in self.records if
                                  (item["type"], item["name"].rstrip(".").lower()) != identity] + [row]
            return self._send(row)
        if path == "/api/dnsman/dns/delete":
            identity = (str(payload.get("type", "")).upper(), str(payload.get("name", "")).rstrip(".").lower())
            type(self).records = [item for item in self.records if
                                  (item["type"], item["name"].rstrip(".").lower()) != identity]
            return self._send({"deleted": True})
        if path == "/api/edge/vhost":
            row = {"id": 99, "server_name": "preview.nativemojo.com", **payload,
                   "domain": {"id": payload.get("domain"), "name": "nativemojo.com"},
                   "certificate": {"id": payload.get("certificate"), "common_name": "nativemojo.com"}}
            type(self).vhosts.append(row)
            return self._send(row)
        if path == "/api/edge/route":
            if payload.get("path_prefix") == "/fail":
                return self._send({"error": "Deterministic partial route failure"}, status=400)
            row = {"id": 90 + len(self.routes), "modified": "2026-08-10T04:00:00Z", **payload}
            type(self).routes.append(row)
            return self._send(row)
        return self._send({"saved": True, "id": 999})

    def do_PUT(self):
        if type(self).upstream:
            return self._proxy()
        path = urlparse(self.path).path
        payload = self._read_body()
        self._record_event(path, payload)
        activity_response = activity.put(self, path, payload)
        if activity_response is not None:
            status, body = activity_response
            return self._send(body, status=status)
        return self._send({"saved": True})

    def do_DELETE(self):
        if type(self).upstream:
            return self._proxy()
        path = urlparse(self.path).path
        self._read_body()
        if path.startswith("/api/group/apikey/"):
            try:
                pk = int(path.rsplit("/", 1)[-1])
            except ValueError:
                return self._send({"error": "Not found"}, status=404)
            type(self).api_keys = [row for row in self.api_keys if row["id"] != pk]
        return self._send({"deleted": True})

    def do_PATCH(self):
        if type(self).upstream:
            return self._proxy()
        return self._send({"error": "Not found"}, status=404)

    def do_HEAD(self):
        return self.do_GET()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5608)
    parser.add_argument("--key-state", choices=("missing", "active", "rotated", "revoked"), default="active")
    parser.add_argument("--setup-state", choices=("idle", "choice", "delay", "error", "fresh", "ambiguous"), default="idle")
    parser.add_argument("--activity-state", choices=("full", "empty", "unavailable"), default="full")
    parser.add_argument("--dashboard-state", choices=("healthy", "degraded", "down", "denied", "unknown"), default="healthy")
    parser.add_argument("--onboarding-state", choices=("idle", "address", "github", "verify", "complete", "lost_key", "new_group"), default="idle")
    parser.add_argument("--settings-state", choices=("normal", "duplicate", "invalid", "delay", "error", "fresh"), default="normal")
    parser.add_argument("--metrics-state", choices=("live", "empty", "unconfigured", "denied", "partial"), default="live")
    parser.add_argument("--maintenance-state", choices=("findings", "denied", "in_flight", "stalled", "unavailable", "framework_pinned", "framework_none", "clear"), default="findings")
    parser.add_argument("--deployments-state", choices=("mixed", "converged", "failed", "empty"), default="mixed")
    parser.add_argument("--upstream", help="Public HTTPS django-mojo origin for live QA")
    args = parser.parse_args()
    upstream = None
    if args.upstream:
        try:
            upstream = parse_upstream(args.upstream)
            resolve_public_addresses(upstream["hostname"], upstream["port"])
        except PreviewProxyError as error:
            parser.error(str(error))
    reset(PreviewHandler, {
        "records": RECORDS, "credentials": CREDENTIALS,
        "vhosts": VHOSTS, "routes": ROUTES,
        "users": USERS, "groups": GROUPS, "members": MEMBERS,
        "api_keys": API_KEYS,
        "webapps": WEBAPPS,
        "setup_choice": setup_choice_operation,
        "webapp_onboarding": webapp_onboarding_operation,
    }, key_state=args.key_state, setup_state=args.setup_state,
       activity_state=args.activity_state,
       dashboard_state=args.dashboard_state,
       onboarding_state=args.onboarding_state,
       settings_state=args.settings_state,
       metrics_state=args.metrics_state,
       maintenance_state=args.maintenance_state,
       deployments_state=args.deployments_state)
    PreviewHandler.upstream = upstream
    PreviewHandler.setup_behavior = args.setup_state
    PreviewHandler.preview_port = args.port
    PreviewHandler.preview_sessions = {}
    if upstream:
        print(f"Admin live QA: http://localhost:{args.port}/admin/ -> {upstream['origin']}", flush=True)
        print("Password authentication is supported; external OAuth callbacks are not bridged.", flush=True)
    else:
        print(f"Admin visual fixture ({args.key_state} key): http://{HOST}:{args.port}/", flush=True)
    try:
        ThreadingHTTPServer((HOST, args.port), PreviewHandler).serve_forever()
    except KeyboardInterrupt:
        pass
