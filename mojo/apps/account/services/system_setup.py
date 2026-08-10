"""Durable System Setup operation orchestration.

Each mutating step persists intent before calling its fixer. A resumed
``mutation_attempted`` step only reconciles authoritative state; it never
blindly repeats the mutation.
"""

import hashlib
import json
import secrets
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from mojo import errors as merrors
from mojo.apps.account.models import SystemSetupOperation
from mojo.apps.account.services import system_readiness, system_settings
from mojo.apps.account.services.setup_safety import is_sensitive_name, sanitize
from mojo.helpers import logit
from mojo.helpers.request import is_key_backed_session, is_request_user


STEP_DEFINITION_VERSION = 1
LEASE_SECONDS = 120
MAX_LOG_ENTRIES = 200


def require_request_admin(request):
    if (not is_request_user(request) or is_key_backed_session(request) or
            getattr(request, "bearer", None) != "bearer"):
        raise merrors.PermissionDeniedException(
            "System Setup requires an interactive superuser session")
    return system_settings.require_system_admin(request.user)


def request_origin(request):
    try:
        origin = system_settings.validate_browser_origin(
            request.META.get("HTTP_ORIGIN", ""))
        expected = system_settings.validate_browser_origin(
            f"{'https' if request.is_secure() else 'http'}://{request.get_host()}")
    except (ValueError, merrors.ValueException):
        raise merrors.PermissionDeniedException("A valid same-origin request is required")
    if origin != expected:
        raise merrors.PermissionDeniedException("System Setup Origin does not match this Admin")
    return origin


def _assert_bound_origin(request, operation):
    if request_origin(request) != operation.bound_origin:
        raise merrors.PermissionDeniedException(
            "This setup operation is bound to a different Admin origin")


def _log(operation, code, message):
    entries = list(operation.operation_log or [])[-(MAX_LOG_ENTRIES - 1):]
    entries.append(sanitize({
        "at": timezone.now().isoformat(),
        "code": code,
        "message": message,
    }))
    operation.operation_log = entries


def _step(step_id, label, kind, choice_schema=None, section="",
          definition_version=STEP_DEFINITION_VERSION):
    return {
        "id": step_id, "definition_version": int(definition_version),
        "choice_revision": 0, "label": label,
        "kind": kind, "section": section, "state": "planned",
        "choice_schema": sanitize(choice_schema) if choice_schema else None,
    }


def _choice_schema(entry):
    schema = entry.get("choice_schema")
    return schema({}) if callable(schema) else schema


def _build_steps(mode, section=""):
    if mode == "check":
        return [_step("readiness", "Run readiness checks", "readiness", section=section)]
    steps = []
    if not section or section == "django":
        steps.extend([
            _step("installation_identity", "Freeze installation identity", "identity"),
            _step("base_url", "Configure public BASE_URL", "base_url", {
                "type": "object",
                "properties": {"base_url": {"type": "string", "format": "https-origin"}},
                "required": ["base_url"], "additionalProperties": False,
            }),
        ])
    for entry in system_readiness.sections():
        if not entry.get("fix") or (section and entry["code"] != section):
            continue
        steps.append(_step(
            f"section:{entry['code']}", f"Repair {entry['label']}", "section",
            _choice_schema(entry), section=entry["code"],
            definition_version=entry["definition_version"]))
    steps.append(_step("final_readiness", "Prove setup readiness", "final", section=section))
    return steps


def _fingerprint(user, mode, section, origin, replay_key):
    raw = json.dumps({
        "user": user.pk, "mode": mode, "section": section,
        "origin": origin, "replay_key": replay_key,
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def options(request):
    require_request_admin(request)
    entries = []
    for entry in system_readiness.sections():
        entries.append({
            "code": entry["code"], "label": entry["label"],
            "fixable": bool(entry["fix"]),
            "choice_schema": sanitize(_choice_schema(entry)),
        })
    active = SystemSetupOperation.objects.filter(
        mode="fix", status__in=SystemSetupOperation.ACTIVE_STATUSES).first()
    return sanitize({
        "schema_version": system_readiness.SCHEMA_VERSION,
        "sections": entries,
        "active_fix": serialize(active) if active else None,
    })


def create(request, mode, section="", replay_key=""):
    actor = require_request_admin(request)
    origin = request_origin(request)
    mode = str(mode or "").strip().lower()
    section = str(section or "").strip()
    replay_key = str(replay_key or secrets.token_urlsafe(18))
    if mode not in ("check", "fix"):
        raise merrors.ValueException("mode must be check or fix")
    if len(replay_key) > 128:
        raise merrors.ValueException("replay_key is too long")
    if section and system_readiness.get_section(section) is None:
        raise merrors.ValueException("Unknown readiness section")
    fingerprint = _fingerprint(actor, mode, section, origin, replay_key)
    existing = SystemSetupOperation.objects.filter(
        replay_fingerprint=fingerprint).first()
    if existing:
        return existing, True
    if mode == "fix":
        active = SystemSetupOperation.objects.filter(
            mode="fix", status__in=SystemSetupOperation.ACTIVE_STATUSES).first()
        if active:
            raise merrors.ValueException(
                "Another Fix Setup operation is already active", status=409, code=409)
    try:
        operation = SystemSetupOperation.objects.create(
            created_by=actor, mode=mode, section=section, status="planned",
            replay_fingerprint=fingerprint, bound_origin=origin,
            steps=_build_steps(mode, section))
    except IntegrityError:
        existing = SystemSetupOperation.objects.filter(
            replay_fingerprint=fingerprint).first()
        if existing:
            return existing, True
        raise merrors.ValueException(
            "Another Fix Setup operation is already active", status=409, code=409)
    _log(operation, "operation.created", f"{mode.title()} operation created")
    operation.save(update_fields=["operation_log", "modified"])
    return operation, False


def get(request, operation_id):
    require_request_admin(request)
    try:
        return SystemSetupOperation.objects.get(pk=operation_id)
    except (SystemSetupOperation.DoesNotExist, ValueError, TypeError):
        raise merrors.ValueException("Unknown setup operation", status=404, code=404)


def _validate_choice(schema, value):
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise merrors.ValueException("This step does not accept a choice")
    if not isinstance(value, dict):
        raise merrors.ValueException("choice must be an object")
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    if schema.get("additionalProperties") is False and set(value) - set(properties):
        raise merrors.ValueException("choice contains unsupported fields")
    for name in required:
        if name not in value:
            raise merrors.ValueException(f"choice.{name} is required")
    clean = {}
    for name, item in value.items():
        if is_sensitive_name(name):
            raise merrors.ValueException("Secret choices are not supported by System Setup")
        field = properties.get(name, {})
        if not isinstance(field, dict):
            raise merrors.ValueException(f"choice.{name} has an invalid schema")
        field_type = field.get("type")
        if field_type == "string" and not isinstance(item, str):
            raise merrors.ValueException(f"choice.{name} must be a string")
        if field_type == "integer" and (isinstance(item, bool) or not isinstance(item, int)):
            raise merrors.ValueException(f"choice.{name} must be an integer")
        if field_type == "boolean" and not isinstance(item, bool):
            raise merrors.ValueException(f"choice.{name} must be a boolean")
        if "enum" in field and item not in field["enum"]:
            raise merrors.ValueException(f"choice.{name} is not an allowed value")
        if field.get("format") == "https-origin":
            try:
                item = system_settings.validate_base_url(item)
            except ValueError as exc:
                raise merrors.ValueException(str(exc))
        clean[name] = sanitize(item)
    return sanitize(clean)


def _expected_definition_version(step):
    if step.get("kind") == "section":
        entry = system_readiness.get_section(step.get("section"))
        return entry.get("definition_version") if entry else None
    if step.get("kind") in ("identity", "base_url", "readiness", "final"):
        return STEP_DEFINITION_VERSION
    return None


def _assert_current_definition(step):
    expected = _expected_definition_version(step)
    if expected is None or step.get("definition_version") != expected:
        raise merrors.ValueException(
            "Setup step definition changed; cancel and start a new operation",
            status=409, code=409)


def _definition_is_current(step):
    return step.get("definition_version") == _expected_definition_version(step)


def _reconcile_identity_v1(operation, context, choice):
    """Retained v1 adapter: prove identity without creating or changing it."""
    identity = system_settings.read_installation_identity()
    return {"status": "proven" if identity else "pending"}


def _reconcile_base_url_v1(operation, context, choice):
    """Retained v1 adapter: compare the intended canonical BASE_URL only."""
    return {"status": "proven" if (
        system_settings.get_value(system_settings.BASE_URL) == choice.get("base_url"))
        else "pending"}


def _reconcile_readiness_v1(operation, context, choice):
    """Retained v1 adapter: rerun the original read-only proof."""
    report = system_readiness.run(operation.section or None, context)
    return {"status": "proven", "report": report}


# Never rewrite these adapters in place. A future definition increment adds a
# new implementation while retaining each old read-only proof by exact version.
_BUILTIN_RECONCILIATION_ADAPTERS = {
    ("identity", 1): _reconcile_identity_v1,
    ("base_url", 1): _reconcile_base_url_v1,
    ("readiness", 1): _reconcile_readiness_v1,
    ("final", 1): _reconcile_readiness_v1,
}


def _versioned_reconciler(step):
    if step.get("kind") != "section":
        if _definition_is_current(step):
            return None
        return _BUILTIN_RECONCILIATION_ADAPTERS.get((
            step.get("kind"), step.get("definition_version")))
    entry = system_readiness.get_section(step.get("section"))
    if entry is None:
        return None
    if _definition_is_current(step):
        return entry.get("reconcile")
    return entry.get("reconciliation_adapters", {}).get(step.get("definition_version"))


def choose(request, operation_id, step_id, definition_version, choice_revision, choice):
    require_request_admin(request)
    with transaction.atomic():
        try:
            operation = SystemSetupOperation.objects.select_for_update().get(pk=operation_id)
        except (SystemSetupOperation.DoesNotExist, ValueError, TypeError):
            raise merrors.ValueException("Unknown setup operation", status=404, code=404)
        _assert_bound_origin(request, operation)
        if operation.status not in SystemSetupOperation.ACTIVE_STATUSES:
            raise merrors.ValueException("Setup operation is already terminal", status=409, code=409)
        if operation.cursor >= len(operation.steps):
            raise merrors.ValueException("Setup operation has no current step", status=409, code=409)
        steps = list(operation.steps)
        step = dict(steps[operation.cursor])
        _assert_current_definition(step)
        if (step.get("id") != step_id or
                step.get("definition_version") != definition_version or
                step.get("choice_revision") != choice_revision):
            raise merrors.ValueException("Stale or cross-step choice", status=409, code=409)
        if step.get("state") != "waiting_for_choice":
            raise merrors.ValueException("Current step is not waiting for a choice", status=409, code=409)
        clean = _validate_choice(step.get("choice_schema"), choice)
        choices = dict(operation.choices or {})
        if step_id in choices:
            raise merrors.ValueException("Choice has already been supplied", status=409, code=409)
        choices[step_id] = clean
        step["choice_revision"] = int(step["choice_revision"]) + 1
        step["state"] = "planned"
        steps[operation.cursor] = step
        operation.steps = steps
        operation.choices = choices
        operation.status = "planned"
        _log(operation, "choice.accepted", f"Choice accepted for {step_id}")
        operation.save(update_fields=[
            "steps", "choices", "status", "operation_log", "modified"])
    return operation


def _context(operation, actor, local_url):
    return {
        "operation": operation,
        "actor": actor,
        "local_url": local_url,
        "timeout": 2.0, "retries": 1,
    }


def _execute_planned(operation, step, context):
    kind = step["kind"]
    choice = (operation.choices or {}).get(step["id"], {})
    if kind == "identity":
        system_settings.installation_identity(context["actor"])
        return {"status": "mutation_attempted"}
    if kind == "base_url":
        system_settings.set_value(
            context["actor"], system_settings.BASE_URL, choice["base_url"])
        return {"status": "mutation_attempted"}
    if kind == "section":
        entry = system_readiness.get_section(step["section"])
        if entry is None or not entry.get("fix"):
            raise system_readiness.DefinitiveSetupFailure(
                "Registered setup fixer is unavailable")
        entry["fix"](context, choice)
        return {"status": "mutation_attempted"}
    if kind in ("readiness", "final"):
        report = system_readiness.run(operation.section or None, context)
        return {"status": "proven", "report": report}
    raise system_readiness.DefinitiveSetupFailure("Unknown setup step")


def _execute_reconcile(operation, step, context):
    kind = step["kind"]
    choice = (operation.choices or {}).get(step["id"], {})
    if not _definition_is_current(step):
        adapter = _versioned_reconciler(step)
        if adapter is None:
            return {"status": "pending"}
        if kind == "section":
            outcome = adapter(context, choice)
            if isinstance(outcome, dict):
                status = outcome.get("status", "pending")
                if status not in ("proven", "pending"):
                    raise ValueError("reconciler returned an unsupported status")
                return {"status": status}
            return {"status": "proven" if outcome else "pending"}
        return adapter(operation, context, choice)
    if kind == "identity":
        identity = system_settings.read_installation_identity()
        return {"status": "proven" if identity else "pending"}
    if kind == "base_url":
        return {"status": "proven" if (
            system_settings.get_value(system_settings.BASE_URL) == choice.get("base_url"))
            else "pending"}
    if kind == "section":
        reconcile = _versioned_reconciler(step)
        if not reconcile:
            return {"status": "pending"}
        outcome = reconcile(context, choice)
        if isinstance(outcome, dict):
            status = outcome.get("status", "pending")
            if status not in ("proven", "pending"):
                raise ValueError("reconciler returned an unsupported status")
            return {"status": status}
        return {"status": "proven" if outcome else "pending"}
    raise system_readiness.DefinitiveSetupFailure("Unknown setup step")


def advance(request, operation_id):
    actor = require_request_admin(request)
    local_url = system_readiness.trusted_local_api_url(request)
    lease = secrets.token_hex(16)
    now = timezone.now()
    with transaction.atomic():
        try:
            operation = SystemSetupOperation.objects.select_for_update().select_related(
                "created_by").get(pk=operation_id)
        except (SystemSetupOperation.DoesNotExist, ValueError, TypeError):
            raise merrors.ValueException("Unknown setup operation", status=404, code=404)
        _assert_bound_origin(request, operation)
        if operation.status not in SystemSetupOperation.ACTIVE_STATUSES:
            return operation
        if (operation.lease_owner and operation.lease_expires_at and
                operation.lease_expires_at > now):
            raise merrors.ValueException("Setup operation is currently advancing", status=409, code=409)
        if operation.cursor >= len(operation.steps):
            operation.status = "succeeded" if (
                operation.mode == "check" or operation.report.get("overall") == "pass") else "failed"
            operation.finished_at = now
            operation.save(update_fields=["status", "finished_at", "modified"])
            return operation
        steps = list(operation.steps)
        step = dict(steps[operation.cursor])
        if not _definition_is_current(step):
            uncertain = step.get("state") in ("mutation_attempted", "reconciling")
            if not uncertain or _versioned_reconciler(step) is None:
                _assert_current_definition(step)
        if step["state"] == "waiting_for_choice":
            return operation
        if step["kind"] == "base_url" and step["id"] not in (operation.choices or {}):
            try:
                system_settings.validate_base_url(
                    system_settings.get_value(system_settings.BASE_URL))
            except ValueError:
                pass
            else:
                step["state"] = "proven"
                steps[operation.cursor] = step
                operation.steps = steps
                operation.cursor += 1
                operation.status = "planned"
                _log(operation, "step.proven", "Existing BASE_URL is already valid")
                operation.save(update_fields=[
                    "steps", "cursor", "status", "operation_log", "modified"])
                return operation
        if step.get("choice_schema") and step["id"] not in (operation.choices or {}):
            step["state"] = "waiting_for_choice"
            steps[operation.cursor] = step
            operation.steps = steps
            operation.status = "waiting_for_choice"
            _log(operation, "choice.required", f"Choice required for {step['id']}")
            operation.save(update_fields=[
                "steps", "status", "operation_log", "modified"])
            return operation
        was_attempted = step["state"] in ("mutation_attempted", "reconciling")
        step["state"] = "reconciling" if was_attempted else "mutation_attempted"
        steps[operation.cursor] = step
        operation.steps = steps
        operation.status = "reconciling" if was_attempted else "running"
        operation.lease_owner = lease
        operation.lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)
        _log(operation, "step.started", f"Started {step['id']}")
        operation.save(update_fields=[
            "steps", "status", "lease_owner", "lease_expires_at",
            "operation_log", "modified"])

    try:
        context = _context(operation, actor, local_url)
        outcome = (_execute_reconcile(operation, step, context) if was_attempted
                   else _execute_planned(operation, step, context))
    except system_readiness.DefinitiveSetupFailure as exc:
        logit.error("system_setup", f"step {step['id']} failed ({exc.__class__.__name__})")
        outcome = {"status": "fail", "exception_class": exc.__class__.__name__}
    except Exception as exc:
        logit.error("system_setup", f"step {step['id']} pending ({exc.__class__.__name__})")
        outcome = {"status": "pending", "exception_class": exc.__class__.__name__}

    with transaction.atomic():
        operation = SystemSetupOperation.objects.select_for_update().get(pk=operation_id)
        if operation.lease_owner != lease:
            raise merrors.ValueException("Setup operation lease changed", status=409, code=409)
        steps = list(operation.steps)
        step_index = operation.cursor
        step = dict(steps[step_index])
        status = outcome.get("status")
        if outcome.get("exception_class"):
            _log(operation, "step.exception",
                 f"{outcome['exception_class']} left this step {status}")
        operation.lease_owner = ""
        operation.lease_expires_at = None
        if status == "mutation_attempted":
            step["state"] = "reconciling"
            operation.status = "reconciling"
            _log(operation, "step.reconciling", f"Reconciling {step['id']}")
        elif status == "proven":
            step["state"] = "proven"
            if "report" in outcome:
                operation.report = sanitize(outcome["report"])
            operation.cursor += 1
            operation.status = "planned"
            _log(operation, "step.proven", f"Proven {step['id']}")
            if operation.cursor >= len(steps):
                operation.status = "succeeded" if (
                    operation.mode == "check" or
                    operation.report.get("overall") == "pass") else "failed"
                operation.finished_at = timezone.now()
        elif status == "pending":
            step["state"] = "reconciling"
            operation.status = "reconciling"
            _log(operation, "step.pending", f"Still reconciling {step['id']}")
        else:
            step["state"] = "failed"
            operation.status = "failed"
            operation.finished_at = timezone.now()
            _log(operation, "step.failed", f"Failed {step['id']}")
        steps[step_index] = step
        operation.steps = steps
        operation.save(update_fields=[
            "steps", "cursor", "status", "report", "lease_owner",
            "lease_expires_at", "operation_log", "finished_at", "modified"])
    return operation


def cancel(request, operation_id):
    require_request_admin(request)
    with transaction.atomic():
        try:
            operation = SystemSetupOperation.objects.select_for_update().get(pk=operation_id)
        except (SystemSetupOperation.DoesNotExist, ValueError, TypeError):
            raise merrors.ValueException("Unknown setup operation", status=404, code=404)
        _assert_bound_origin(request, operation)
        if operation.status not in SystemSetupOperation.ACTIVE_STATUSES:
            return operation
        current = (operation.steps[operation.cursor]
                   if operation.cursor < len(operation.steps or []) else None)
        if current and current.get("state") not in ("planned", "waiting_for_choice"):
            raise merrors.ValueException(
                "Setup can only be cancelled between steps or while waiting for a choice",
                status=409, code=409)
        if (operation.lease_owner and operation.lease_expires_at and
                operation.lease_expires_at > timezone.now()):
            raise merrors.ValueException(
                "Setup can only be cancelled between steps", status=409, code=409)
        operation.status = "cancelled"
        operation.finished_at = timezone.now()
        _log(operation, "operation.cancelled", "Operation cancelled between steps")
        operation.save(update_fields=[
            "status", "finished_at", "operation_log", "modified"])
    return operation


def serialize(operation):
    if operation is None:
        return None
    current = None
    if operation.cursor < len(operation.steps or []):
        current = operation.steps[operation.cursor]
    return sanitize({
        "id": str(operation.pk),
        "mode": operation.mode,
        "section": operation.section or None,
        "status": operation.status,
        "cursor": operation.cursor,
        "steps": operation.steps or [],
        "current_step": current,
        "choices": operation.choices or {},
        "report": operation.report or {},
        "log": operation.operation_log or [],
        "created": operation.created.isoformat() if operation.created else None,
        "modified": operation.modified.isoformat() if operation.modified else None,
        "finished_at": operation.finished_at.isoformat() if operation.finished_at else None,
    })
