"""Small trusted, default-off project hook for the existing snapshot cycle.

Callbacks must use cooperative bounded I/O. A deadline rejects late results;
it cannot forcibly stop arbitrary Python supplied by the project.
"""
import importlib
import json
import re
import time
from types import MappingProxyType

from mojo.apps.account.services import health_evidence as evidence
from mojo.helpers.settings import settings

SETTING = "HEALTH_SNAPSHOT_PROJECT_SERVICES"
MAX_CHECKS = 8
CHECK_SECONDS = 5
ID = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
MODULE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$")
REASONS = ("configuration_unavailable", "credential_unavailable", "authentication_failed",
           "protocol_invalid", "deadline_exceeded", "provider_unavailable", "unsupported",
           "cleanup_failed", "dns_mismatch", "dns_no_answer")


def _frozen(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _frozen(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_frozen(item) for item in value)
    return value


def _module(name, loader):
    module = loader(name)
    if not callable(getattr(module, "configuration", None)) or not callable(getattr(module, "collect", None)):
        raise ValueError()
    return module


def _json_config(value):
    if type(value) in (str, int, float, bool, type(None)):
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        children = value.values()
    elif isinstance(value, list):
        children = value
    else:
        raise ValueError()
    for child in children:
        _json_config(child)


def freeze(deadline, *, budget=None, get_static=None, loader=None, clock=None):
    """Freeze actual offline, non-secret config; never resolve credentials/I/O.

    Invalid rosters have one reserved obligation. Once a roster is valid,
    import/configuration failures retain every named obligation as Unknown.
    Only a trusted file setting chooses executable code.
    """
    clock, loader = clock or time.monotonic, loader or importlib.import_module
    declared = (get_static or settings.get_static)(SETTING, None)
    if declared is None:
        return None
    checks, module_name, reason = [], None, None
    try:
        if not isinstance(declared, dict) or set(declared) != {"module", "checks"}:
            raise ValueError()
        roster = declared["checks"]
        if not isinstance(roster, (list, tuple)) or not 0 < len(roster) <= MAX_CHECKS:
            raise ValueError()
        for row in roster:
            if (not isinstance(row, dict) or set(row) != {"id", "impact"}
                    or not isinstance(row["id"], str) or len(row["id"]) > 64
                    or not ID.fullmatch(row["id"]) or row["id"] == "project-checks"
                    or row["impact"] not in ("availability", "operational")
                    or row["id"] in {item["id"] for item in checks}):
                raise ValueError()
            checks.append({"id": row["id"], "impact": row["impact"], "config": None})
        checks.sort(key=lambda row: row["id"])
    except Exception:
        checks = [{"id": "project-checks", "impact": "operational", "config": None}]
        reason = "project_declaration_invalid"
    if reason is None:
        try:
            module_name = declared["module"]
            if not isinstance(module_name, str) or len(module_name) > 256 or not MODULE.fullmatch(module_name):
                module_name = None
                raise ValueError()
            if clock() >= deadline:
                raise TimeoutError()
            module = _module(module_name, loader)
            if clock() >= deadline:
                raise TimeoutError()
            roster = _frozen([{key: row[key] for key in ("id", "impact")} for row in checks])
            values = module.configuration(roster, deadline, budget or evidence.CycleBudget(deadline, clock=clock))
            if clock() >= deadline:
                raise TimeoutError()
            if not isinstance(values, dict) or set(values) != {row["id"] for row in checks}:
                raise ValueError()
            # A canonical round trip rejects non-JSON/non-finite data and
            # detaches mutable callback objects before publication identity.
            _json_config(values)
            encoded = evidence.canonical(values)
            if len(encoded.encode()) > 32 * 1024:
                raise ValueError()
            values = json.loads(encoded)
            for row in checks:
                if len(evidence.canonical(values[row["id"]]).encode()) > 4096:
                    raise ValueError()
            checks = [{**row, "config": values[row["id"]]} for row in checks]
        except TimeoutError:
            reason = "project_configuration_late"
        except Exception:
            reason = "project_configuration_unavailable"
    result = {"module": module_name, "checks": checks, "reason": reason}
    result["fingerprint"] = evidence.digest(result)
    return result


def source_identity(project, check):
    return {"module": project["module"], "check_id": check["id"], "impact": check["impact"],
            "project_fingerprint": project["fingerprint"]}


def collect(project, check, deadline, budget, *, loader=None, clock=None, now=None):
    """One <=5s project call; only the three-field sanitized result escapes."""
    clock, now = clock or time.monotonic, now or evidence.utcnow
    end = min(deadline, clock() + CHECK_SECONDS)
    def unknown(reason):
        return {"outcome": "unknown", "observed_at": None, "reason": reason}
    if project["reason"]:
        return unknown(project["reason"])
    try:
        budget.take("project_services")
        if clock() >= end:
            return unknown("project_check_late")
        module = _module(project["module"], loader or importlib.import_module)
        if clock() >= end:
            return unknown("project_check_late")
        result = module.collect(_frozen(check), end, budget)
        if clock() >= end:
            return unknown("project_check_late")
        if (not isinstance(result, dict) or set(result) != {"outcome", "observed_at", "reason"}
                or result["outcome"] not in ("pass", "fail", "unknown")
                or result["reason"] is not None and result["reason"] not in REASONS):
            return unknown("project_result_invalid")
        at = result["observed_at"]
        if result["outcome"] == "unknown":
            if at is not None:
                return unknown("project_result_invalid")
        elif not isinstance(at, str) or evidence.valid_time(at, now()) is None:
            return unknown("project_result_invalid")
        return {key: result[key] for key in ("outcome", "observed_at", "reason")}
    except TimeoutError:
        return unknown("not_collected_budget")
    except Exception:
        return unknown("project_check_unavailable")


def cached_matches(value, deadline, *, configuration_reader=None, get_static=None, clock=None):
    """Offline narrow fence: never collect or rewrite same-generation facts."""
    clock = clock or time.monotonic
    components = [row for row in value["components"] if row["kind"] == "service"]
    get_static = get_static or settings.get_static
    if configuration_reader is None and not components and get_static(SETTING, None) is None:
        return True
    project = (configuration_reader or (lambda end: freeze(end, get_static=get_static, clock=clock)))(deadline)
    if clock() >= deadline or project is not None and project["reason"]:
        return False
    expected = {"service:" + row["id"]: source_identity(project, row) for row in project["checks"]} if project else {}
    actual = {}
    for row in components:
        if len(row["evidence"]) != 1 or row["evidence"][0]["slot_id"] != "check":
            return False
        actual[row["component_id"]] = row["evidence"][0]["source_identity"]
    return actual == expected
