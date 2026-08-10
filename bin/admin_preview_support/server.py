"""Loopback-only deterministic support server for the Admin preview."""

import argparse
import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .gallery import bootstrap, reset
from .features import activity


ROOT = Path(__file__).resolve().parents[2] / "mojo/apps/account/admin_portal"
HOST = "127.0.0.1"

USERS = [
    {"id": 1, "display_name": "Ian Smith", "email": "ian@example.com", "username": "ian@example.com", "is_active": True, "created": "2026-07-12T18:10:00Z"},
    {"id": 2, "display_name": "Avery Chen", "email": "avery@example.com", "username": "avery@example.com", "is_active": True, "created": "2026-07-27T09:30:00Z"},
    {"id": 3, "display_name": "Morgan Lee", "email": "morgan@example.com", "username": "morgan@example.com", "is_active": False, "created": "2026-06-03T14:20:00Z"},
]
GROUPS = [
    {"id": 7, "name": "MOJO Platform", "kind": "organization", "is_active": True},
    {"id": 9, "name": "Web Operations", "kind": "team", "is_active": True},
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
    {"id": 42, "slug": "mojo-portal", "created": "2026-07-19T10:00:00Z", "current_release": {"id": 18, "version": "8d42ea1"}},
    {"id": 54, "slug": "docs", "created": "2026-07-30T16:40:00Z", "current_release": None},
]


def readiness_sections():
    def section(code, label, status, checks):
        return {"code": code, "label": label, "status": status, "checks": checks}

    def check(code, status, explanation, remediation="", fixable=False):
        return {"code": code, "status": status, "explanation": explanation, "remediation": remediation, "fixable": fixable}

    return [
        section("django", "Django installation", "pass", [
            check("django.apps", "pass", "Django applications loaded successfully."),
            check("django.database", "pass", "The database answered a query."),
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
    events = []
    records = []
    credentials = []
    vhosts = []
    routes = []

    def log_message(self, fmt, *args):
        return

    def _send(self, body, content_type="application/json", status=200):
        if isinstance(body, (dict, list)):
            body = json.dumps({"status": True, "data": body}, default=str).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _key_status(self):
        states = {
            "missing": {"linked": False, "active": False, "api_key": None, "last_action": None},
            "active": {"linked": True, "active": True, "api_key": 91, "name": "webapp:mojo-portal", "created": "2026-07-20T12:00:00Z", "last_used": "2026-08-09T22:14:00Z", "last_action": "mint"},
            "rotated": {"linked": True, "active": True, "api_key": 94, "name": "webapp:mojo-portal", "created": "2026-08-09T23:00:00Z", "last_used": None, "last_action": "rotate"},
            "revoked": {"linked": False, "active": False, "api_key": None, "last_action": "revoke", "last_operation_at": "2026-08-09T23:30:00Z"},
        }
        return states[self.key_state]

    @staticmethod
    def _safe_payload(payload):
        hidden = {"api_key", "api_secret", "confirm_token", "token", "password"}
        return {key: "[redacted]" if key in hidden else value
                for key, value in payload.items()}

    def _record_event(self, path, payload):
        type(self).events.append({"path": path,
                                  "payload": self._safe_payload(payload)})

    def _api(self, parsed):
        path = parsed.path
        if path == "/api/account/static/mojo-auth.js":
            return self._send("window.MojoAuth={init:function(){},getAuthHeader:function(){return 'Bearer preview';},getRefreshToken:function(){return null;},logout:function(){}};", "application/javascript; charset=utf-8")
        if path == "/api/account/admin/bootstrap":
            return self._send(bootstrap(GROUPS))
        activity_response = activity.get(self, parsed)
        if activity_response is not None:
            status, payload = activity_response
            return self._send(payload, status=status)
        if path == "/api/account/admin/setup/options":
            active = self.setup_operation
            if active and active.get("status") in {"succeeded", "failed", "cancelled"}:
                active = None
            return self._send({"schema_version": 1, "active_fix": active, "sections": [{"code": row["code"], "label": row["label"], "fixable": row["code"] in {"aws_s3", "aws_email", "aws_monitoring"}, "choice_schema": None} for row in readiness_sections()]})
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
        fixtures = {
            "/api/user": USERS, "/api/group": GROUPS, "/api/dnsman/domain": DOMAINS,
            "/api/dnsman/credential": self.credentials, "/api/dnsman/certificate": CERTIFICATES,
            "/api/edge/upstream": UPSTREAMS, "/api/edge/vhost": self.vhosts,
            "/api/edge/route": self.routes, "/api/edge/webapp": WEBAPPS,
        }
        if path in fixtures:
            return self._send(fixtures[path])
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
        return self._send({"error": "Not found"}, status=404)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/__preview__/events":
            return self._send(self.events)
        if parsed.path.startswith("/api/"):
            return self._api(parsed)
        if parsed.path in ("/", "/admin", "/admin/"):
            target = ROOT / "index.html"
        else:
            relative = parsed.path.removeprefix("/admin/").lstrip("/")
            target = (ROOT / relative).resolve()
            if ROOT.resolve() not in target.parents:
                return self._send("Not found", "text/plain", 404)
        if not target.is_file():
            return self._send("Not found", "text/plain", 404)
        self._send(target.read_bytes(), mimetypes.guess_type(target.name)[0] or "application/octet-stream")

    def _read_body(self):
        size = int(self.headers.get("Content-Length", "0"))
        if not size:
            return {}
        try:
            return json.loads(self.rfile.read(size))
        except (ValueError, TypeError):
            return {}

    def do_POST(self):
        path = urlparse(self.path).path
        payload = self._read_body()
        self._record_event(path, payload)
        if path == "/api/edge/webapp/link_key":
            type(self).key_state = "rotated" if payload.get("action") == "rotate" else "active"
            return self._send({"webapp": 42, "secret_name": "MOJO_DEPLOY_KEY", "replayed": False, "operation_id": "preview", "token": "preview-token-shown-once", "status": self._key_status()})
        if path == "/api/edge/webapp/revoke_key":
            type(self).key_state = "revoked"
            return self._send({"webapp": 42, "secret_name": "MOJO_DEPLOY_KEY", "replayed": False, "operation_id": "preview", "status": self._key_status()})
        if path == "/api/account/admin/setup/create":
            type(self).setup_operation = setup_planned_operation(payload.get("mode", "fix"))
            return self._send(self.setup_operation)
        if path == "/api/account/admin/setup/advance":
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
        path = urlparse(self.path).path
        payload = self._read_body()
        self._record_event(path, payload)
        activity_response = activity.put(self, path, payload)
        if activity_response is not None:
            status, body = activity_response
            return self._send(body, status=status)
        return self._send({"saved": True})

    def do_DELETE(self):
        self._read_body()
        return self._send({"deleted": True})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5608)
    parser.add_argument("--key-state", choices=("missing", "active", "rotated", "revoked"), default="active")
    parser.add_argument("--setup-state", choices=("idle", "choice"), default="idle")
    parser.add_argument("--activity-state", choices=("full", "empty", "unavailable"), default="full")
    args = parser.parse_args()
    reset(PreviewHandler, {
        "records": RECORDS, "credentials": CREDENTIALS,
        "vhosts": VHOSTS, "routes": ROUTES,
        "setup_choice": setup_choice_operation,
    }, key_state=args.key_state, setup_state=args.setup_state,
        activity_state=args.activity_state)
    print(f"Admin visual fixture ({args.key_state} key): http://{HOST}:{args.port}/", flush=True)
    try:
        ThreadingHTTPServer((HOST, args.port), PreviewHandler).serve_forever()
    except KeyboardInterrupt:
        pass
