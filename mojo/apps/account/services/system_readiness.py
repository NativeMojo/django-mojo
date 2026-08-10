"""Versioned, extensible readiness report registry for System Setup."""

from collections import OrderedDict

from django.utils import timezone


SCHEMA_VERSION = 1
STATUSES = ("pass", "warn", "fail", "pending")
_SECTIONS = OrderedDict()


def register_section(code, label, check, fix=None, reconcile=None,
                     choice_schema=None, order=100):
    if not isinstance(code, str) or not code or not callable(check):
        raise ValueError("readiness sections need a stable code and check callable")
    _SECTIONS[code] = {
        "code": code, "label": str(label), "check": check, "fix": fix,
        "reconcile": reconcile, "choice_schema": choice_schema,
        "order": int(order),
    }


def sections():
    return sorted(_SECTIONS.values(), key=lambda item: (item["order"], item["code"]))


def get_section(code):
    return _SECTIONS.get(code)


def result(code, status, explanation, remediation="", fixable=False,
           required_choice=None, details=None):
    if status not in STATUSES:
        raise ValueError(f"invalid readiness status: {status}")
    safe = {
        "code": str(code)[:120],
        "status": status,
        "explanation": str(explanation)[:500],
        "remediation": str(remediation)[:500],
        "fixable": bool(fixable),
        "required_choice": required_choice if isinstance(required_choice, dict) else None,
    }
    if isinstance(details, dict):
        safe["details"] = {
            str(key)[:80]: value for key, value in list(details.items())[:16]
            if isinstance(value, (str, int, float, bool, type(None)))
        }
    return safe


def _section_status(checks):
    values = [item.get("status") for item in checks]
    for status in ("fail", "pending", "warn"):
        if status in values:
            return status
    return "pass"


def run(section=None, context=None):
    selected = sections()
    if section:
        selected = [item for item in selected if item["code"] == section]
        if not selected:
            raise ValueError(f"unknown readiness section: {section}")
    reports = []
    for entry in selected:
        try:
            checks = entry["check"](context or {})
            if isinstance(checks, dict):
                checks = [checks]
            if not isinstance(checks, list):
                raise ValueError("section check must return a result or result list")
            checks = [result(**item) for item in checks[:64]]
        except Exception:
            checks = [result(
                f"{entry['code']}.check_error", "fail",
                "The readiness check could not complete safely.",
                "Inspect the server log for this check and rerun it.")]
        status = _section_status(checks)
        reports.append({
            "code": entry["code"], "label": entry["label"], "status": status,
            "fixable": bool(entry["fix"]), "checks": checks,
        })
    overall = _section_status(reports)
    summary = {status: 0 for status in STATUSES}
    for section_report in reports:
        for check in section_report["checks"]:
            summary[check["status"]] += 1
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": timezone.now().isoformat(),
        "overall": overall,
        "summary": summary,
        "sections": reports,
    }


def _core_check(context):
    import requests
    from mojo.apps.account.services import system_settings
    from mojo.apps.edge.services import sanity

    rows = []
    sanity_options = {
        "url": context.get("local_url", "http://127.0.0.1/api/version"),
        "timeout": context.get("timeout", 2.0),
        "retries": context.get("retries", 1),
        "delay": 0,
    }
    messages = {
        "django apps": ("Django applications loaded successfully.", "Repair the Django installation and restart the service."),
        "database": ("The database answered a query.", "Check database connectivity and credentials."),
        "migrations": ("The database schema matches installed migrations.", "Run the deployment migration step, then rerun this check."),
        "redis": ("Redis answered a health probe.", "Restore Redis connectivity for cache, jobs, and scheduler services."),
        "local request": ("The local API answered over HTTP.", "Restore the local application listener and /api/version route."),
    }
    for item in sanity.run(sanity_options):
        explanation, remediation = messages[item["name"]]
        if not item["ok"]:
            explanation = f"{item['name'].title()} is not ready."
        rows.append(result(
            "django." + item["name"].replace(" ", "_"),
            "pass" if item["ok"] else "fail", explanation, remediation))

    try:
        sanity.check_static_directories({})
        rows.append(result("django.static_directories", "pass",
                           "Configured static directories are present."))
    except RuntimeError:
        rows.append(result(
            "django.static_directories", "warn",
            "One or more configured static directories are not present.",
            "Create the configured directories and run collectstatic."))

    base_url = system_settings.get_value(system_settings.BASE_URL)
    try:
        canonical = system_settings.validate_base_url(base_url)
        rows.append(result("django.base_url", "pass",
                           "The canonical public BASE_URL is configured.",
                           details={"origin": canonical}))
        if context.get("probe_public", True):
            try:
                response = requests.get(
                    f"{canonical}/api/version",
                    timeout=float(context.get("timeout", 2.0)),
                    allow_redirects=False)
                public_ok = response.status_code == 200
            except requests.RequestException:
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
