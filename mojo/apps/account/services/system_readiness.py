"""Versioned, extensible readiness report registry for System Setup."""

from collections import OrderedDict
import http.client
import ipaddress
import json
import socket
import ssl
from urllib.parse import urlsplit

from django.utils import timezone

from mojo.apps.account.services import setup_safety
from mojo.apps.account.services.setup_safety import sanitize
from mojo.helpers.settings import settings


SCHEMA_VERSION = 1
STATUSES = ("pass", "warn", "fail", "pending")
LOCAL_API_SOURCES = ("configured_static", "request_server_port", "default_80")
SECTION_CHECK_LIMIT = 64
_SECTIONS = OrderedDict()


def register_section(code, label, check, fix=None, reconcile=None,
                     choice_schema=None, order=100, definition_version=1,
                     reconciliation_adapters=None):
    if not isinstance(code, str) or not code or not callable(check):
        raise ValueError("readiness sections need a stable code and check callable")
    current_version = int(definition_version)
    adapters = {}
    for version, callback in dict(reconciliation_adapters or {}).items():
        version = int(version)
        if version == current_version or not callable(callback):
            raise ValueError("reconciliation adapters need old versions and callables")
        adapters[version] = callback
    _SECTIONS[code] = {
        "code": code, "label": str(label), "check": check, "fix": fix,
        "reconcile": reconcile, "choice_schema": choice_schema,
        "order": int(order), "definition_version": current_version,
        "reconciliation_adapters": adapters,
    }


def register_reconciliation_adapter(code, definition_version, reconcile):
    """Register a read-only reconciler for one persisted old step version."""
    entry = _SECTIONS.get(code)
    if entry is None or not callable(reconcile):
        raise ValueError("reconciliation adapter needs a registered section and callable")
    version = int(definition_version)
    if version == entry["definition_version"]:
        raise ValueError("current definition uses the section's normal reconciler")
    entry["reconciliation_adapters"][version] = reconcile


def sections():
    return sorted(_SECTIONS.values(), key=lambda item: (item["order"], item["code"]))


def get_section(code):
    return _SECTIONS.get(code)


def result(code, status, explanation, remediation="", fixable=False,
           required_choice=None, details=None):
    if status not in STATUSES:
        raise ValueError(f"invalid readiness status: {status}")
    passing = status == "pass"
    safe = {
        "code": str(code)[:120],
        "status": status,
        "explanation": str(explanation)[:500],
        "remediation": "" if passing else str(remediation)[:500],
        "fixable": False if passing else bool(fixable),
        "required_choice": (sanitize(required_choice)
                            if not passing and isinstance(required_choice, dict) else None),
    }
    if isinstance(details, dict):
        safe["details"] = sanitize({
            str(key)[:80]: value for key, value in list(details.items())[:16]
            if isinstance(value, (str, int, float, bool, type(None)))
        })
    return sanitize(safe)


def _section_status(checks):
    values = [item.get("status") for item in checks]
    for status in ("fail", "pending", "warn"):
        if status in values:
            return status
    return "pass"


def _status_from_counts(counts):
    for status in ("fail", "pending", "warn"):
        if counts.get(status, 0):
            return status
    return "pass"


def _item_cost(value):
    """Mirror the sanitizer's item accounting for an already-safe report."""
    if isinstance(value, dict):
        return 1 + sum(_item_cost(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return 1 + sum(_item_cost(item) for item in value)
    return 1


def _fits_report(report):
    # The sanitizer reserves 4096 bytes for structural overhead. Keep another
    # 4096 bytes for escaping and key names so it never has to silently choose
    # which typed row survives.
    return (_item_cost(report) <= setup_safety.MAX_ITEMS - 4 and
            len(json.dumps(report, separators=(",", ":"), default=str).encode("utf-8"))
            <= setup_safety.MAX_SERIALIZED_BYTES - 8192)


def _normalize_checks(entry, raw_checks):
    """Count every result while retaining only bounded severity-first detail."""
    counts = {status: 0 for status in STATUSES}
    buckets = {status: [] for status in STATUSES}
    actionable = False
    for raw in raw_checks:
        if not isinstance(raw, dict):
            raise ValueError("section check rows must be objects")
        check = result(**raw)
        status = check["status"]
        counts[status] += 1
        actionable = actionable or bool(check["fixable"] and status != "pass")
        if len(buckets[status]) < SECTION_CHECK_LIMIT:
            buckets[status].append(check)
    retained = []
    for status in ("fail", "pending", "warn", "pass"):
        retained.extend(buckets[status][:SECTION_CHECK_LIMIT - len(retained)])
        if len(retained) >= SECTION_CHECK_LIMIT:
            break
    can_fix = bool(entry.get("fix") or entry["code"] == "django")
    return counts, retained, bool(can_fix and actionable)


def _sanitize_report(report):
    """Keep the bounded readiness envelope schema-safe at collection edges."""
    safe = sanitize(report)
    if not isinstance(safe, dict):
        return {
            "schema_version": SCHEMA_VERSION,
            "overall": "pending",
            "summary": {status: 0 for status in STATUSES},
            "sections": [],
            "truncated": True,
        }
    partial = safe.get("truncated") is True
    raw_sections = safe.get("sections")
    if not isinstance(raw_sections, list):
        raw_sections = []
        partial = True
    sections = []
    for section_report in raw_sections:
        if (not isinstance(section_report, dict) or
                section_report.get("status") not in STATUSES or
                not isinstance(section_report.get("checks"), list)):
            partial = True
            continue
        checks = []
        for check in section_report["checks"]:
            if not isinstance(check, dict) or check.get("status") not in STATUSES:
                partial = True
                continue
            checks.append(check)
        clean_section = dict(section_report)
        clean_section["checks"] = checks
        sections.append(clean_section)
    safe["sections"] = sections
    returned_checks = 0
    for section_report in sections:
        returned = len(section_report["checks"])
        returned_checks += returned
        coverage = section_report.get("coverage")
        if isinstance(coverage, dict) and isinstance(coverage.get("total"), int):
            total = max(returned, coverage["total"])
            section_report["coverage"] = {
                "total": total, "returned": returned, "omitted": total - returned}
            if total != returned:
                partial = True
    coverage = safe.get("coverage")
    if isinstance(coverage, dict):
        check_coverage = coverage.get("checks")
        if isinstance(check_coverage, dict) and isinstance(check_coverage.get("total"), int):
            total = max(returned_checks, check_coverage["total"])
            coverage["checks"] = {
                "total": total, "returned": returned_checks,
                "omitted": total - returned_checks}
            if total != returned_checks:
                partial = True
    if partial:
        safe["truncated"] = True
    return safe


def run(section=None, context=None):
    selected = sections()
    if section:
        selected = [item for item in selected if item["code"] == section]
        if not selected:
            raise ValueError(f"unknown readiness section: {section}")
    collected = []
    summary = {status: 0 for status in STATUSES}
    for entry in selected:
        try:
            checks = entry["check"](context or {})
            if isinstance(checks, dict):
                checks = [checks]
            if not isinstance(checks, list):
                raise ValueError("section check must return a result or result list")
            counts, retained, fixable = _normalize_checks(entry, checks)
        except Exception:
            retained = [result(
                f"{entry['code']}.check_error", "fail",
                "The readiness check could not complete safely.",
                "Inspect the server log for this check and rerun it.")]
            counts = {status: int(status == "fail") for status in STATUSES}
            fixable = False
        for status in STATUSES:
            summary[status] += counts[status]
        collected.append({
            "code": entry["code"], "label": entry["label"],
            "status": _status_from_counts(counts),
            "fixable": fixable, "checks": [],
            "coverage": {
                "total": sum(counts.values()), "returned": 0,
                "omitted": sum(counts.values()),
            },
            "_candidates": retained,
        })

    total_checks = sum(summary.values())
    report = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": timezone.now().isoformat(),
        "overall": _status_from_counts(summary),
        "summary": summary,
        "sections": [],
        "coverage": {
            "sections": {"total": len(collected), "returned": 0,
                         "omitted": len(collected)},
            "checks": {"total": total_checks, "returned": 0,
                       "omitted": total_checks},
        },
    }

    # Represent as many sections as the unchanged global safety budget permits
    # before spending any of that budget on optional check detail.
    for item in collected:
        section_report = {key: value for key, value in item.items()
                          if key != "_candidates"}
        candidate = dict(report)
        candidate["sections"] = [*report["sections"], section_report]
        candidate["coverage"] = {
            **report["coverage"],
            "sections": {
                "total": len(collected),
                "returned": len(candidate["sections"]),
                "omitted": len(collected) - len(candidate["sections"]),
            },
        }
        if not _fits_report(candidate):
            break
        report = candidate

    # Round-robin gives every returned section one severity-first row before a
    # verbose provider can consume the remaining budget. Details are optional;
    # if one detail object would crowd out typed rows, retain the finding
    # without details and announce that coverage is partial.
    candidates = [item["_candidates"] for item in collected[:len(report["sections"])]]
    for index in range(SECTION_CHECK_LIMIT):
        made_progress = False
        for section_index, rows in enumerate(candidates):
            if index >= len(rows):
                continue
            check = rows[index]
            trial_check = check
            for compact in (False, True):
                trial = json.loads(json.dumps(report, default=str))
                if compact and "details" in check:
                    trial_check = {key: value for key, value in check.items()
                                   if key != "details"}
                trial["sections"][section_index]["checks"].append(trial_check)
                returned = sum(len(item["checks"]) for item in trial["sections"])
                trial["coverage"]["checks"]["returned"] = returned
                trial["coverage"]["checks"]["omitted"] = total_checks - returned
                coverage = trial["sections"][section_index]["coverage"]
                coverage["returned"] = len(trial["sections"][section_index]["checks"])
                coverage["omitted"] = coverage["total"] - coverage["returned"]
                if _fits_report(trial):
                    report = trial
                    made_progress = True
                    break
            else:
                return _sanitize_report({**report, "truncated": True})
        if not made_progress:
            break

    partial = (report["coverage"]["sections"]["omitted"] > 0 or
               report["coverage"]["checks"]["omitted"] > 0)
    if partial:
        report["truncated"] = True
    return _sanitize_report(report)


class UnsafePublicProbe(ValueError):
    pass


class DefinitiveSetupFailure(Exception):
    """A fixer may raise this only when it proved no mutation occurred."""

    pass


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname, port, address, timeout):
        super().__init__(hostname, port=port, timeout=timeout,
                         context=ssl.create_default_context())
        self._pinned_address = address

    def connect(self):
        sock = socket.create_connection(
            (self._pinned_address, self.port), self.timeout, self.source_address)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _resolve_public_address(hostname, port):
    try:
        answers = socket.getaddrinfo(
            hostname, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafePublicProbe("Public hostname could not be resolved") from exc
    addresses = []
    for answer in answers:
        address = ipaddress.ip_address(answer[4][0])
        if not address.is_global:
            raise UnsafePublicProbe("Public hostname resolved to a non-public address")
        if str(address) not in addresses:
            addresses.append(str(address))
    if not addresses:
        raise UnsafePublicProbe("Public hostname had no usable address")
    return addresses[0]


def probe_public_api(origin, timeout=2.0):
    """Probe one public HTTPS origin through a DNS-pinned TLS connection."""
    parsed = urlsplit(origin)
    if parsed.scheme != "https" or not parsed.hostname:
        raise UnsafePublicProbe("Public probe requires an HTTPS origin")
    port = parsed.port or 443
    address = _resolve_public_address(parsed.hostname, port)
    connection = _PinnedHTTPSConnection(
        parsed.hostname, port, address, float(timeout))
    host_header = parsed.hostname
    if ":" in host_header:
        host_header = f"[{host_header}]"
    if port != 443:
        host_header = f"{host_header}:{port}"
    try:
        connection.request(
            "GET", "/api/version",
            headers={"Host": host_header, "Accept": "application/json", "Connection": "close"})
        response = connection.getresponse()
        # Redirects are intentionally not followed: a provider-controlled
        # Location must never become a second SSRF destination.
        return response.status == 200
    finally:
        connection.close()


def probe_public_api_details(origin, timeout=2.0):
    """SSRF-safe bounded version probe with status/body-size metadata."""
    import json
    import time

    parsed = urlsplit(origin)
    if parsed.scheme != "https" or not parsed.hostname:
        raise UnsafePublicProbe("Public probe requires an HTTPS origin")
    port = parsed.port or 443
    address = _resolve_public_address(parsed.hostname, port)
    connection = _PinnedHTTPSConnection(
        parsed.hostname, port, address, float(timeout))
    host_header = parsed.hostname
    if ":" in host_header:
        host_header = f"[{host_header}]"
    if port != 443:
        host_header = f"{host_header}:{port}"
    started = time.monotonic()
    try:
        connection.request(
            "GET", "/api/version",
            headers={"Host": host_header, "Accept": "application/json",
                     "Connection": "close"})
        response = connection.getresponse()
        raw = response.read(4097)
        too_large = len(raw) > 4096
        payload = {}
        if not too_large:
            try:
                parsed_body = json.loads(raw.decode("utf-8"))
                payload = parsed_body if isinstance(parsed_body, dict) else {}
            except (UnicodeDecodeError, ValueError):
                pass
        body = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        return {
            "ok": response.status == 200 and not too_large,
            "http_status": response.status,
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
            "version": str(body.get("version") or "")[:64],
            "body_truncated": too_large,
        }
    finally:
        connection.close()


def trusted_local_api_target(request=None):
    """Return the validated local probe URL and its bounded provenance."""
    configured = str(settings.get_static("SYSTEM_SETUP_LOCAL_API_URL", "") or "").strip()
    if configured:
        parsed = urlsplit(configured)
        try:
            address = ipaddress.ip_address(parsed.hostname or "")
            port = parsed.port
        except ValueError:
            raise ValueError("SYSTEM_SETUP_LOCAL_API_URL must use a loopback address")
        if (parsed.scheme not in ("http", "https") or not address.is_loopback or
                parsed.username or parsed.password or parsed.query or parsed.fragment or
                parsed.path not in ("", "/")):
            raise ValueError("SYSTEM_SETUP_LOCAL_API_URL must be a loopback HTTP(S) origin")
        display = f"[{address}]" if address.version == 6 else str(address)
        default_port = 443 if parsed.scheme == "https" else 80
        netloc = display if port in (None, default_port) else f"{display}:{port}"
        return {
            "url": f"{parsed.scheme}://{netloc}/api/version",
            "source": "configured_static",
        }
    raw_port = request.META.get("SERVER_PORT", "") if request is not None else ""
    valid_request_port = False
    try:
        port = int(raw_port)
        valid_request_port = 1 <= port <= 65535
    except (TypeError, ValueError):
        port = 80
    source = "request_server_port" if request is not None and valid_request_port else "default_80"
    if not 1 <= port <= 65535:
        port = 80
    return {"url": f"http://127.0.0.1:{port}/api/version", "source": source}


def trusted_local_api_url(request=None):
    """String-compatible local probe destination for existing callers."""
    return trusted_local_api_target(request)["url"]


def _core_check(context):
    from mojo.apps.account.services import system_settings
    from mojo.apps.edge.services import sanity

    rows = []
    sanity_options = {
        "url": context.get("local_url", "http://127.0.0.1/api/version"),
        "timeout": context.get("timeout", 2.0),
        "retries": context.get("retries", 1),
        "delay": 0,
    }
    local_source = context.get("local_source")
    local_remediation = {
        "configured_static": (
            "Change the SYSTEM_SETUP_LOCAL_API_URL deployment setting or restore "
            "that loopback listener, then rerun."),
        "request_server_port": (
            "Restore the application listener on this request's server port and "
            "the /api/version route, then rerun."),
        "default_80": (
            "Set SYSTEM_SETUP_LOCAL_API_URL for this deployment or restore the "
            "loopback listener on port 80, then rerun."),
    }
    messages = {
        "django apps": ("Django applications loaded successfully.", "Repair the Django installation and restart the service."),
        "database": ("The database answered a query.", "Check database connectivity and credentials."),
        "migrations": ("The database schema matches installed migrations.", "Run the deployment migration step, then rerun this check."),
        "redis": ("Redis answered a health probe.", "Restore Redis connectivity for cache, jobs, and scheduler services."),
        "local request": ("The local API answered over HTTP.", "Restore the local application listener and /api/version route."),
    }
    for item in sanity.run(sanity_options):
        if item["name"] == "local request" and local_source != "configured_static":
            continue
        explanation, remediation = messages[item["name"]]
        if not item["ok"]:
            explanation = f"{item['name'].title()} is not ready."
        remediation = local_remediation.get(local_source, remediation) \
            if item["name"] == "local request" else remediation
        details = {"target_source": local_source} \
            if item["name"] == "local request" and local_source in LOCAL_API_SOURCES else None
        rows.append(result(
            "django." + item["name"].replace(" ", "_"),
            "pass" if item["ok"] else "fail", explanation, remediation,
            details=details))

    base_url = system_settings.get_value(system_settings.BASE_URL)
    try:
        canonical = system_settings.validate_base_url(base_url)
        rows.append(result("django.base_url", "pass",
                           "The canonical public BASE_URL is configured.",
                           details={"origin": canonical}))
        if context.get("probe_public", True):
            try:
                public_ok = probe_public_api(
                    canonical, timeout=float(context.get("timeout", 2.0)))
            except UnsafePublicProbe:
                rows.append(result(
                    "django.public_api", "pending",
                    "The public API probe was withheld because its destination could not be pinned safely.",
                    "Verify public DNS resolves only to global addresses, then rerun."))
                return rows
            except (OSError, ssl.SSLError, http.client.HTTPException):
                public_ok = False
            rows.append(result(
                "django.public_api", "pass" if public_ok else "fail",
                "The public API health probe succeeded." if public_ok else
                "The public BASE_URL did not answer the API health probe.",
                "Verify public DNS, TLS, routing, and /api/version, then rerun."))
        else:
            rows.append(result(
                "django.public_api", "pending",
                "The public API probe was not requested in this report.",
                "Run the complete readiness report to probe the public API."))
    except ValueError:
        rows.append(result(
            "django.base_url", "fail", "The public BASE_URL is not configured safely.",
            "Choose the canonical public HTTPS origin in Fix Setup.", True,
            {"type": "string", "format": "https-origin", "name": "base_url"}))
        rows.append(result(
            "django.public_api", "pending",
            "The public API probe is waiting for a valid BASE_URL.",
            "Configure BASE_URL, then rerun the public health probe."))
    return rows


register_section("django", "Django installation", _core_check, order=10)
