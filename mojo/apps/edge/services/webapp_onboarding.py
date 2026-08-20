"""Crash-safe App -> Address -> GitHub -> Verify onboarding orchestration."""

import hashlib
import json
import uuid
from datetime import timedelta
from urllib.parse import quote, urlsplit

import requests
from django.db import IntegrityError, transaction
from django.utils import timezone

from mojo import errors as me
from mojo.apps.account.services import webapp_authority

from mojo.apps.edge import validators
from mojo.apps.edge.models import WebApp, WebAppOnboardingOperation
from mojo.apps.edge.models.web_app_onboarding_operation import (
    STATUS_ACTIVE, STATUS_CANCELLED, STATUS_FAILED, STATUS_SUCCEEDED,
    STATUS_WAITING, TERMINAL_STATUSES,
)


SCHEMA_VERSION = 1
MAX_ATTEMPTS = 8
MAX_STATE_BYTES = 32768
MAX_ACTIVITY = 80
LEASE_SECONDS = 90
# A third _advance_* outcome beyond True (retry) / False (advance): the step is
# waiting on the USER to publish or repair DNS records. advance() parks it as
# waiting with no error budget and schedules no auto-retry — the user re-checks
# by re-submitting the choice, which resets the attempt count.
WAIT_FOR_USER = "wait-for-user"
SECRET_KEYS = {
    "api_key", "api_secret", "authorization", "confirm_token", "password",
    "private_key", "secret", "token", "mojo_deploy_key",
}


def _safe(value, depth=0):
    """Recursively redact secret-shaped keys and bound provider evidence."""
    if depth > 6:
        return "[truncated]"
    if isinstance(value, dict):
        result = {}
        for key, item in list(value.items())[:100]:
            name = str(key)[:100]
            lowered = name.lower()
            if lowered in SECRET_KEYS or any(
                    marker in lowered for marker in ("password", "secret", "token")):
                result[name] = "[redacted]"
            else:
                result[name] = _safe(item, depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe(item, depth + 1) for item in list(value)[:100]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:500]


def _bounded(value, what):
    cleaned = _safe(value)
    if len(json.dumps(cleaned, sort_keys=True, default=str).encode()) > MAX_STATE_BYTES:
        raise me.ValueException(f"{what} is too large")
    return cleaned


def _fingerprint(group, payload):
    stable = json.dumps(
        _safe(payload), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{group.pk}:{stable}".encode()).hexdigest()


def _operation_uuid(value, required=False):
    if value in (None, "") and not required:
        return None
    try:
        return uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError):
        raise me.ValueException("operation_id must be a UUID")


def _profile(payload, require_display_name=False):
    slug = str(payload.get("slug") or "").strip().lower()
    display_name = str(payload.get("display_name") or "").strip()
    if require_display_name and not display_name:
        raise me.ValueException("display_name is required for a new group")
    profile = {
        "slug": slug,
        "display_name": display_name or slug,
        "environment": str(payload.get("environment") or "production").strip(),
        "bucket": str(payload.get("bucket") or "").strip(),
        "github_repository": str(payload.get("github_repository") or "").strip(),
        "deployment_ref": str(payload.get("deployment_ref") or "main").strip(),
        "build_output": str(payload.get("build_output") or "dist").strip(),
    }
    if not profile["slug"]:
        raise me.ValueException("slug is required")
    validators.validate_release_bucket(profile["bucket"])
    if not validators.NAME_RE.match(profile["slug"]):
        raise me.ValueException(
            "slug must be lowercase letters, digits, '-' or '_'")
    if (profile["display_name"] and
            not validators.DISPLAY_NAME_RE.match(profile["display_name"])):
        raise me.ValueException(
            "display name must be 1-120 printable characters")
    if profile["environment"] not in (
            "production", "staging", "preview", "development"):
        raise me.ValueException("unknown WebApp environment")
    if profile["github_repository"]:
        validators.validate_github_repository(profile["github_repository"])
    validators.validate_deployment_ref(profile["deployment_ref"])
    validators.validate_build_output(profile["build_output"])
    return profile


def _intent(operation):
    value = (operation.state or {}).get("group_intent")
    if value == "new":
        return "new"
    return "existing"


def _assert_receipt(operation, actor, origin, profile, group_intent, group=None):
    if operation.actor_id != actor.pk or operation.origin != origin:
        raise me.PermissionDeniedException(
            "operation_id is owned by another administrator or origin")
    if _intent(operation) != group_intent:
        raise me.ValueException("operation_id was already used for another group intent")
    if ((operation.state or {}).get("profile") or {}) != profile:
        raise me.ValueException("operation_id was already used for another WebApp profile")
    if group_intent == "existing" and operation.group_id != group.pk:
        raise me.ValueException("operation_id was already used for another group")
    if not webapp_authority.can_manage_group_webapps(actor, operation.group):
        raise me.PermissionDeniedException(
            "WebApp and DNS management are no longer granted in this group")
    return operation


def request_origin(request):
    raw = str(request.META.get("HTTP_ORIGIN") or "").strip().rstrip("/")
    request_scheme = "https" if request.is_secure() else "http"
    if not raw:
        raw = f"{request_scheme}://{request.get_host()}"
    parsed = urlsplit(raw)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or
            parsed.username or parsed.password or parsed.path not in ("", "/") or
            parsed.query or parsed.fragment):
        raise me.PermissionDeniedException("A valid same-origin request is required")
    expected = urlsplit(f"{request_scheme}://{request.get_host()}")
    try:
        origin_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        request_port = expected.port or (443 if request_scheme == "https" else 80)
    except ValueError:
        raise me.PermissionDeniedException("A valid same-origin request is required")
    if (parsed.hostname.lower() != (expected.hostname or "").lower() or
            parsed.scheme != request_scheme or origin_port != request_port):
        raise me.PermissionDeniedException("Cross-origin onboarding is refused")
    return raw


def _activity(operation, message, kind="info"):
    rows = list(operation.activity or [])
    rows.append({"at": timezone.now().isoformat(), "kind": kind,
                 "message": str(message)[:300]})
    operation.activity = rows[-MAX_ACTIVITY:]


def _save_state(operation, state, fields=None):
    operation.state = _bounded(state, "onboarding state")
    operation.revision += 1
    names = list(fields or []) + ["state", "revision", "activity", "modified"]
    operation.save(update_fields=list(dict.fromkeys(names)))


def _assert_current(operation, request, mutate=False):
    if getattr(request, "group_token", None) is not None:
        raise me.PermissionDeniedException("Group tokens cannot operate onboarding")
    if operation.actor_id != getattr(request.user, "pk", None):
        raise me.PermissionDeniedException("Only the initiating administrator may continue")
    if not operation.group.is_effectively_active():
        raise me.PermissionDeniedException("The onboarding group is no longer active")
    if not webapp_authority.can_manage_group_webapps(
            request.user, operation.group):
        raise me.PermissionDeniedException(
            "WebApp and DNS management are no longer granted in this group")
    # These dedicated endpoints deliberately use the stricter actor + current
    # WebApp/DNS group-authority contract above.  RestMeta's generic request
    # group inference is a separate boundary and would turn a valid scoped
    # membership into an accidental global-permission requirement here.
    if operation.origin != request_origin(request):
        raise me.PermissionDeniedException("Onboarding must continue on its original origin")


def serialize(operation):
    evidence = _safe(operation.evidence or {})
    profile = _safe((operation.state or {}).get("profile") or {})
    choices = _safe((operation.state or {}).get("choices") or {})
    return {
        "schema_version": SCHEMA_VERSION,
        "id": operation.pk,
        "operation_id": str(operation.operation_id),
        "group": {"id": operation.group_id, "name": operation.group.name},
        "status": operation.status,
        "cursor": operation.cursor,
        "revision": operation.revision,
        "profile": profile,
        "choices": choices,
        "evidence": evidence,
        "activity": _safe(operation.activity or []),
        "resources": {
            "webapp": operation.web_app_id, "domain": operation.domain_id,
            "certificate": operation.certificate_id, "vhost": operation.vhost_id,
        },
        "attempts": operation.attempts,
        "next_attempt_at": operation.next_attempt_at,
        "last_error": operation.last_error,
        "created": operation.created, "modified": operation.modified,
        "finished": operation.finished,
    }


def _is_label(value):
    try:
        validators.validate_label(str(value).strip().lower())
        return True
    except me.MojoException:
        return False


def _is_ip_literal(host):
    import ipaddress

    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def _domain_summary(domain):
    return {"id": domain.pk, "name": domain.name, "provider": domain.provider}


def _verify_external_cname(hostname, target):
    """Authoritatively confirm the user published ``hostname`` CNAME -> target.

    Unlike delegated ACME's one-hop ``_acme-challenge`` proof, the app CNAME
    target (``EDGE_WEBAPP_CNAME_TARGET``) may itself be a CNAME — e.g. to an
    ELB — so this deliberately does NOT use ``probe.verify_one_hop_cname``'s
    one-hop restriction on the target side.
    """
    from mojo.helpers.dns import probe

    result = probe.query_cname(hostname)
    targets = sorted(str(value).rstrip(".") for value in (result.targets or []))
    return targets == [target.rstrip(".")]


def _wildcard_synthesized(domain, targets):
    """Whether a probed CNAME answer came from a ``*.{domain}`` record.

    A wildcard answers every otherwise-empty name in its zone, so a random
    sibling label returning the identical target set proves the answer was
    synthesized rather than served by a host-specific record. A specific
    record always takes precedence over the wildcard, so a synthesized answer
    never blocks the platform from routing one hostname itself.
    """
    from mojo.helpers.dns import probe

    sibling = f"mojo-wildcard-check-{uuid.uuid4().hex[:12]}.{domain.name}"
    result = probe.query_cname(sibling)
    sibling_targets = sorted(
        str(value).rstrip(".") for value in (result.targets or []))
    return bool(sibling_targets) and sibling_targets == targets


def _external_records(hostname, target, domain, include_challenge=False):
    """Records a user publishes at their own DNS host for an external domain.

    The app CNAME is always required. The ``_acme-challenge`` CNAME is listed
    only when ``include_challenge`` is set — the failed-certificate recovery
    case, where that record needs re-adding. In the normal flow the delegation
    was verified *before* the ``mojo`` Domain existed, so the challenge record
    is already published and is not the user's job at the address step. That is
    also what makes a second app on the same domain a single-record add: it
    reuses the delegation and the wildcard certificate.
    """
    if not target:
        # The render invariant: never hand back a record the operator would
        # copy with a blank destination. The destination resolver makes this
        # unreachable in the normal flow; this is the backstop for a direct or
        # future caller.
        raise me.ValueException(
            "No serving destination is configured for this app address")
    records = [{"type": "CNAME", "name": hostname, "value": target, "ttl": 300}]
    if include_challenge:
        from mojo.apps.dnsman.services import delegation

        row = delegation.for_domain(domain)
        if row is not None and row.source_name and row.target_name:
            records.append({"type": "CNAME", "name": row.source_name,
                            "value": row.target_name, "ttl": 300})
    return records


def precheck(group, raw_url, group_intent="existing"):
    """Stateless URL-first pre-flight for the wizard, before any operation.

    Given the address a user typed, work out what onboarding will require and
    steer un-serveable shapes. Never touches provider credentials: the only
    network call is one authoritative CNAME probe (never ``dns.list_records``,
    which would enumerate a whole provider zone on shared credentials per click).
    """
    from urllib.parse import urlsplit

    from mojo.apps.dnsman.models.domain import PROVIDER_MOJO
    from mojo.apps.dnsman.services import delegation, naming
    from mojo.apps.edge.models import Vhost
    from mojo.apps.edge.services import webapp_destination
    from mojo.helpers.dns import probe

    def result(verdict, **extra):
        payload = {"schema_version": SCHEMA_VERSION, "verdict": verdict}
        payload.update(extra)
        return payload

    raw = str(raw_url or "").strip()
    if not raw:
        return result("invalid", reason="Enter the web address you want")
    candidate = raw if "://" in raw else f"https://{raw}"
    parsed = urlsplit(candidate)
    scheme = (parsed.scheme or "https").lower()
    if scheme not in ("http", "https"):
        return result("invalid", reason="Use a web address like https://app.example.com")
    if parsed.username or parsed.password:
        return result("invalid", reason="Remove the username or password from the address")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return result("invalid", reason="That address has no hostname")
    trailing = (parsed.path or "").strip("/")
    if trailing:
        segment = trailing.split("/")[0]
        return result(
            "path", host=host,
            suggestion=f"{segment}.{host}" if _is_label(segment) else None,
            reason="Apps are served on a whole address, not a path under one")
    if parsed.port and parsed.port not in (80, 443):
        return result("invalid", reason="Leave the port off the address")
    if _is_ip_literal(host):
        return result("invalid", reason="Use a domain name, not an IP address")
    if host.startswith("*"):
        return result("invalid", reason="Wildcard addresses are not supported")

    scheme_note = "http upgraded to https" if scheme == "http" else None
    try:
        hostname = naming.normalize_domain(host)
    except me.MojoException:
        return result("invalid", reason="That is not a valid domain name")
    normalized = {"url": f"https://{hostname}", "hostname": hostname,
                  "scheme_note": scheme_note}

    # Longest-suffix match against the domains this group may serve under —
    # its own and its ancestors' (never a sibling's; an unrelated group's
    # domain stays domain_unknown). A new group has none, so it always
    # resolves to domain_unknown.
    from mojo.apps.edge.services import webapp_apps_domain

    domain = None
    if group_intent != "new" and group is not None:
        for candidate_domain in webapp_apps_domain.owned_by_group_or_ancestor(
                group).filter(status="active"):
            base = candidate_domain.name
            if hostname == base or hostname.endswith(f".{base}"):
                if domain is None or len(base) > len(domain.name):
                    domain = candidate_domain

    if domain is None:
        return result("domain_unknown", normalized=normalized, options={
            "external_available": delegation.is_available(),
            "purchase_available": True,
            "godaddy_available": True,
        })

    normalized["domain_name"] = domain.name
    if hostname == domain.name:
        return result("apex", normalized=normalized, domain=_domain_summary(domain),
                      suggestion=f"www.{domain.name}",
                      reason="Apps are served on a subdomain, not the bare domain")
    remainder = hostname[: -(len(domain.name) + 1)]
    if "." in remainder:
        leaf = remainder.split(".")[-1]
        return result("deep_label", normalized=normalized,
                      domain=_domain_summary(domain),
                      suggestion=f"{leaf}.{domain.name}",
                      reason="Use a single label under your domain")
    label = remainder
    if not _is_label(label):
        return result("invalid", normalized=normalized,
                      reason="That subdomain name is not usable")
    normalized["label"] = label

    # Conflicts are DB checks plus one authoritative CNAME probe — never a
    # provider record listing.
    existing = Vhost.objects.filter(
        domain=domain, label=label, is_enabled=True).first()
    if existing is not None:
        app = getattr(existing, "web_app", None)
        # The occupying app's slug is workspace-internal detail: disclose it
        # only when the requesting group itself owns the domain. Through the
        # ancestor rule the verdict stands, the slug does not.
        own_domain = group is not None and domain.group_id == group.pk
        return result("taken", normalized=normalized,
                      domain=_domain_summary(domain),
                      app=app.slug if (app is not None and own_domain) else None,
                      reason="That address is already serving a site")

    def destination_or_block():
        # Managed and external addresses alike need a serving destination. An
        # installation that has none is configuration-required (fix Setup); a
        # hostname that resolves to the platform's own address is plain-invalid.
        try:
            return webapp_destination.resolve(hostname), None
        except webapp_destination.DestinationUnavailable as err:
            return None, result(
                "configuration_required", normalized=normalized,
                domain=_domain_summary(domain), reason=str(err),
                setup_check="webapp_destination")
        except me.ValueException as err:
            return None, result("invalid", normalized=normalized,
                                domain=_domain_summary(domain), reason=str(err))

    if domain.provider == PROVIDER_MOJO:
        # The delegation (and its _acme-challenge record) was already verified
        # to make this a mojo Domain, so the only record the user still adds is
        # the app CNAME — one record, whether it is their first app here or
        # their tenth.
        destination, blocked = destination_or_block()
        if blocked is not None:
            return blocked
        records = _external_records(hostname, destination.value, domain)
        published = _verify_external_cname(hostname, destination.value)
        return result("ready" if published else "records_needed",
                      normalized=normalized, domain=_domain_summary(domain),
                      records=records)

    # A managed domain (route53/godaddy): the platform will write the record,
    # but a foreign existing CNAME still blocks. One probe, no zone listing.
    destination, blocked = destination_or_block()
    if blocked is not None:
        return blocked
    probe_result = probe.query_cname(hostname)
    targets = sorted(str(v).rstrip(".") for v in (probe_result.targets or []))
    if (targets and targets != [destination.value.rstrip(".")]
            and not _wildcard_synthesized(domain, targets)):
        return result("conflict", normalized=normalized,
                      domain=_domain_summary(domain),
                      reason="That address already points somewhere else")
    # The platform writes this record itself, so a managed ready carries the
    # resolved destination and its provenance — never a record to copy.
    return result("ready", normalized=normalized, domain=_domain_summary(domain),
                  destination={"type": destination.type,
                               "value": destination.value,
                               "provenance": destination.provenance})


def options(group, group_intent="existing"):
    from mojo.apps.github.models import GitHubInstall
    from mojo.apps.edge.services import webapp_destination

    buckets = validators.release_buckets()
    # The installation-level serving destination the wizard shows and gates the
    # identity step on. Its absence (destination_error) is what steers the
    # connected-domain and purchase paths, which never pass through precheck.
    destination = None
    destination_error = None
    try:
        resolved = webapp_destination.resolve()
        destination = {"type": resolved.type, "value": resolved.value,
                       "provenance": resolved.provenance}
    except webapp_destination.DestinationUnavailable as err:
        destination_error = str(err)
    # The workspace's apps domain — where a new app can go live instantly with
    # no DNS step at all. None plus a plain-language reason otherwise.
    from mojo.apps.edge.services import webapp_apps_domain

    apps_domain = None
    apps_domain_error = None
    resolved_apps_domain = webapp_apps_domain.resolve(group)
    if resolved_apps_domain is not None:
        apps_domain = {"id": resolved_apps_domain.pk,
                       "name": resolved_apps_domain.name,
                       "provider": resolved_apps_domain.provider}
    else:
        apps_domain_error = webapp_apps_domain.no_domain_reason(group)
    return {
        "schema_version": SCHEMA_VERSION,
        "buckets": buckets,
        "environments": ["production", "staging", "preview", "development"],
        "destination": destination,
        "destination_error": destination_error,
        "apps_domain": apps_domain,
        "apps_domain_error": apps_domain_error,
        "group_intent": group_intent,
        "github_connected": bool(
            group is not None and
            GitHubInstall.objects.filter(group=group).exists()),
        "limits": {"attempts": MAX_ATTEMPTS, "lease_seconds": LEASE_SECONDS},
    }


def create(group, actor, origin, payload, group_intent="existing"):
    from mojo.apps.account.models import Group

    is_new = group_intent == "new"
    if group_intent not in ("existing", "new"):
        raise me.ValueException("Unknown WebApp group intent")
    if is_new:
        if not webapp_authority.can_create_webapp_group(actor):
            raise me.PermissionDeniedException(
                "Creating a WebApp group is not granted globally")
    elif not webapp_authority.can_manage_group_webapps(actor, group):
        raise me.PermissionDeniedException(
            "WebApp and DNS management are not granted in this group")
    profile = _profile(payload, require_display_name=is_new)
    operation_id = _operation_uuid(
        payload.get("operation_id"), required=is_new)

    if operation_id is not None:
        receipt = WebAppOnboardingOperation.objects.select_related(
            "group", "actor").filter(operation_id=operation_id).first()
        if receipt is not None:
            return _assert_receipt(
                receipt, actor, origin, profile, group_intent, group), False

    if not is_new:
        candidate = WebApp(group=group, prefix="pending", **profile)
        validators.validate_web_app(candidate)
        fingerprint = _fingerprint(group, profile)
        with transaction.atomic():
            current = WebAppOnboardingOperation.objects.select_for_update().filter(
                group=group, replay_fingerprint=fingerprint,
                status__in=(STATUS_ACTIVE, STATUS_WAITING)).first()
            if current is not None:
                return _assert_receipt(
                    current, actor, origin, profile, "existing", group), False
            try:
                with transaction.atomic():
                    operation = WebAppOnboardingOperation.objects.create(
                        operation_id=operation_id or uuid.uuid4(),
                        group=group, actor=actor, origin=origin,
                        replay_fingerprint=fingerprint,
                        state={"profile": profile, "choices": {}, "intent": {},
                               "group_intent": "existing"}, evidence={})
            except IntegrityError:
                if operation_id is not None:
                    receipt = WebAppOnboardingOperation.objects.select_related(
                        "group", "actor").filter(
                            operation_id=operation_id).first()
                    if receipt is not None:
                        return _assert_receipt(
                            receipt, actor, origin, profile, "existing", group), False
                operation = WebAppOnboardingOperation.objects.select_related(
                    "group", "actor").filter(
                        group=group, replay_fingerprint=fingerprint,
                        status__in=(STATUS_ACTIVE, STATUS_WAITING)).first()
                if operation is None:
                    raise
                return _assert_receipt(
                    operation, actor, origin, profile, "existing", group), False
            _activity(operation, "Onboarding created; application intent frozen")
            _save_state(operation, operation.state)
            return operation, True

    # The inner savepoint owns the complete first commit. A UUID loser rolls
    # back its Group and WebApp before the winning receipt is inspected.
    try:
        with transaction.atomic():
            group = Group.objects.create(
                name=profile["display_name"], kind="organization", is_active=True)
            fingerprint = _fingerprint(group, profile)
            operation = WebAppOnboardingOperation.objects.create(
                operation_id=operation_id, group=group, actor=actor,
                origin=origin, replay_fingerprint=fingerprint,
                state={"profile": profile, "choices": {}, "intent": {},
                       "group_intent": "new"}, evidence={})
            _activity(operation, "Group and application intent frozen")
            _advance_app(operation)
            operation.revision += 1
            operation.save(update_fields=[
                "state", "activity", "evidence", "web_app", "cursor",
                "attempts", "next_attempt_at", "revision", "modified"])
            return operation, True
    except IntegrityError:
        receipt = WebAppOnboardingOperation.objects.select_related(
            "group", "actor").filter(operation_id=operation_id).first()
        if receipt is None:
            raise
        return _assert_receipt(
            receipt, actor, origin, profile, "new"), False


def choose(operation, request, payload):
    raw_choice = payload.get("choice") or {}
    purchase_confirmation = None
    with transaction.atomic():
        locked = WebAppOnboardingOperation.objects.select_for_update().select_related(
            "group").get(pk=operation.pk)
        _assert_current(locked, request, mutate=True)
        if locked.status in TERMINAL_STATUSES:
            raise me.ValueException("This onboarding operation is already finished")
        if (locked.lease_expires_at and
                locked.lease_expires_at > timezone.now()):
            raise me.ValueException(
                "Onboarding is reconciling; reload before choosing again")
        revision = payload.get("revision")
        try:
            revision = int(revision)
        except (TypeError, ValueError):
            raise me.ValueException("revision is required")
        if revision != locked.revision:
            raise me.ValueException("Onboarding changed; reload before continuing")
        step = str(payload.get("step") or "").strip()
        if step != locked.cursor:
            raise me.ValueException(f"The current onboarding step is {locked.cursor}")
        choice = _bounded(raw_choice, "onboarding choice")
        state = dict(locked.state or {})
        choices = dict(state.get("choices") or {})
        if step == "address" and choice.get("purchase"):
            token = str(raw_choice.get("confirm_token") or "")
            if not token:
                raise me.ValueException("The one-use purchase confirmation is required")
            purchase_confirmation = {
                "purchase": choice.get("purchase"), "token": token,
                "domain": choice.get("confirm_domain"),
                "price": choice.get("confirm_price"),
            }
            choice.pop("confirm_token", None)
            intent = dict(state.get("intent") or {})
            intent["purchase"] = {
                "provider": "route53", "purchase": choice.get("purchase"),
                "domain": str(choice.get("confirm_domain") or ""),
                "price": str(choice.get("confirm_price") or ""),
                "confirmation_fingerprint": hashlib.sha256(token.encode()).hexdigest(),
            }
            state["intent"] = intent
            locked.registrar_provider = "route53"
        choices[step] = choice
        state["choices"] = choices
        locked.status = STATUS_ACTIVE
        locked.last_error = ""
        locked.next_attempt_at = timezone.now()
        # Every accepted choice is a fresh user action: reset the attempt budget
        # so a step parked at WAIT_FOR_USER (or exhausted retries) resumes with a
        # clean slate when the user presses Check.
        locked.attempts = 0
        _activity(locked, f"Choice accepted for {step}")
        fields = ["status", "last_error", "next_attempt_at", "attempts"]
        if purchase_confirmation:
            fields.append("registrar_provider")
        _save_state(locked, state, fields)

    # Queue recovery before the synchronous provider call. If this request is
    # killed after the registrar accepts money but before sending a response,
    # the worker has durable intent and can reconcile the ledger/domain. It has
    # no raw confirmation and therefore cannot repeat the purchase itself.
    publish(locked.pk)

    # The raw confirmation exists only in this fresh, synchronous request.
    # Durable intent above committed before the provider can move money.
    if purchase_confirmation:
        from mojo.apps.dnsman.models import Domain
        from mojo.apps.dnsman.services import registrar

        domain = Domain.objects.filter(
            metadata__purchase=purchase_confirmation["purchase"],
            group=locked.group).first()
        if domain is None:
            try:
                result = registrar.purchase(
                    locked.group, locked.actor,
                    purchase_confirmation["purchase"],
                    purchase_confirmation["token"],
                    purchase_confirmation["domain"],
                    purchase_confirmation["price"])
                domain = Domain.objects.get(pk=result.domain)
            except Exception:
                # Registrar detail belongs in its permission-scoped ledger and
                # logs. The onboarding response must neither replay nor echo a
                # provider body, which can contain account/contact metadata.
                from mojo.apps.dnsman.models import DomainPurchase

                purchase_status = DomainPurchase.objects.filter(
                    pk=purchase_confirmation["purchase"],
                    group=locked.group).values_list("status", flat=True).first()
                if purchase_status in ("quoted", "expired"):
                    message = "Domain purchase confirmation was refused"
                    status_code = 400
                elif purchase_status == "failed":
                    message = "Domain registration failed; review the purchase ledger"
                    status_code = 503
                else:
                    message = "Domain purchase outcome is being reconciled"
                    status_code = 503
                with transaction.atomic():
                    failed = WebAppOnboardingOperation.objects.select_for_update().get(
                        pk=locked.pk)
                    if purchase_status in ("quoted", "expired"):
                        failed_state = dict(failed.state or {})
                        failed_choices = dict(failed_state.get("choices") or {})
                        failed_choices.pop("address", None)
                        failed_intent = dict(failed_state.get("intent") or {})
                        failed_intent.pop("purchase", None)
                        failed_state["choices"] = failed_choices
                        failed_state["intent"] = failed_intent
                        failed.state = failed_state
                    failed.status = (
                        STATUS_FAILED if purchase_status == "failed"
                        else STATUS_WAITING)
                    failed.finished = (
                        timezone.now() if purchase_status == "failed" else None)
                    failed.last_error = message
                    failed.revision += 1
                    failed.save(update_fields=[
                        "state", "status", "finished", "last_error",
                        "revision", "modified"])
                raise me.ValueException(
                    f"{message}; reload status",
                    code=status_code, status=status_code)
        with transaction.atomic():
            locked = WebAppOnboardingOperation.objects.select_for_update().get(
                pk=locked.pk)
            locked.domain = domain
            locked.revision += 1
            locked.save(update_fields=["domain", "revision", "modified"])

    publish(locked.pk)
    return locked


@transaction.atomic
def cancel(operation, request):
    # ``actor`` is nullable, so joining it under FOR UPDATE creates a nullable
    # outer-join lock that PostgreSQL rejects.  _assert_current only needs the
    # actor id and the concrete group.
    locked = WebAppOnboardingOperation.objects.select_for_update().select_related(
        "group").get(pk=operation.pk)
    _assert_current(locked, request, mutate=True)
    if locked.status in TERMINAL_STATUSES:
        return locked
    locked.status = STATUS_CANCELLED
    locked.finished = timezone.now()
    locked.lease_owner = ""
    locked.lease_expires_at = None
    _activity(locked, "Onboarding cancelled; completed resources were preserved",
              "warning")
    locked.revision += 1
    locked.save(update_fields=[
        "status", "finished", "lease_owner", "lease_expires_at", "activity",
        "revision", "modified"])
    return locked


def publish(operation_id, delay=0):
    from mojo.apps import jobs

    operation = WebAppOnboardingOperation.objects.filter(pk=operation_id).only(
        "revision").first()
    if operation is None:
        return None

    return jobs.publish(
        func="mojo.apps.edge.asyncjobs.webapp_onboarding_advance",
        payload={"operation": operation_id}, channel="default",
        delay=int(delay) or None,
        idempotency_key=f"webapp-onboard-{operation_id}-{operation.revision}"[:64],
        max_retries=0, expires_in=600 + int(delay), max_exec_seconds=120)


@transaction.atomic
def _lease(operation_id, owner):
    now = timezone.now()
    operation = WebAppOnboardingOperation.objects.select_for_update().filter(
        pk=operation_id).first()
    if operation is None or operation.status in TERMINAL_STATUSES:
        return None
    if (operation.lease_expires_at and operation.lease_expires_at > now and
            operation.lease_owner != owner):
        return None
    if operation.next_attempt_at and operation.next_attempt_at > now:
        return None
    operation.lease_owner = owner
    operation.lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)
    operation.save(update_fields=["lease_owner", "lease_expires_at", "modified"])
    return operation.pk


@transaction.atomic
def _release(operation, owner, wait=False):
    """Publish worker state only while this exact lease/revision still owns it."""
    current = WebAppOnboardingOperation.objects.select_for_update().get(
        pk=operation.pk)
    if (current.lease_owner != owner or
            current.revision != operation.revision):
        return False
    operation.lease_owner = ""
    operation.lease_expires_at = None
    if wait and operation.status not in TERMINAL_STATUSES:
        operation.status = STATUS_WAITING
    operation.revision += 1
    operation.save(update_fields=[
        "lease_owner", "lease_expires_at", "status", "revision", "activity",
        "evidence", "last_error", "attempts", "next_attempt_at", "cursor",
        "web_app", "domain", "certificate", "vhost", "finished", "modified"])
    return True


def advance(operation_id, owner=None):
    owner = str(owner or uuid.uuid4())[:64]
    if _lease(operation_id, owner) is None:
        return "not-acquired"
    operation = WebAppOnboardingOperation.objects.select_related(
        "group", "actor", "web_app", "domain", "certificate", "vhost").get(
            pk=operation_id)
    try:
        if (operation.actor is None or not operation.actor.is_active or
                not webapp_authority.can_manage_group_webapps(
                    operation.actor, operation.group)):
            raise me.PermissionDeniedException("Onboarding authority is no longer current")
        if operation.cursor == "app":
            wait = _advance_app(operation)
        elif operation.cursor == "address":
            wait = _advance_address(operation)
        elif operation.cursor == "github":
            wait = _advance_github(operation)
        elif operation.cursor == "verify":
            wait = _advance_verify(operation)
        else:
            wait = False
        retry_delay = None
        choice = ((operation.state or {}).get("choices") or {}).get(
            operation.cursor)
        if wait is WAIT_FOR_USER:
            # Waiting on the user to publish or repair DNS records. No error
            # budget and no auto-retry: the user re-checks by re-submitting the
            # choice, which resets attempts. _release parks it as waiting.
            operation.attempts = 0
            operation.next_attempt_at = None
        elif wait and choice and operation.attempts < MAX_ATTEMPTS:
            operation.attempts += 1
            retry_delay = min(300, 2 ** operation.attempts)
            operation.next_attempt_at = timezone.now() + timedelta(
                seconds=retry_delay)
        elif wait and choice and operation.attempts >= MAX_ATTEMPTS:
            # A long-but-legitimate wait (certificate issuance, DNS
            # propagation) is not a failure. Park it so the user can re-check,
            # instead of terminally failing an onboarding that just took a
            # while.
            operation.attempts = 0
            operation.next_attempt_at = None
            _activity(operation,
                      "Paused waiting on DNS or certificate; press Check to retry")
        released = _release(operation, owner, wait=bool(wait))
        if not released:
            return "superseded"
        if retry_delay:
            publish(operation.pk, delay=retry_delay)
        return f"{operation.status}:{operation.cursor}"
    except Exception as exc:
        operation.attempts += 1
        operation.last_error = _safe_error(exc)
        _activity(operation, operation.last_error, "error")
        if operation.attempts >= MAX_ATTEMPTS:
            operation.status = STATUS_FAILED
            operation.finished = timezone.now()
            operation.next_attempt_at = None
        else:
            operation.status = STATUS_WAITING
            delay = min(300, 2 ** operation.attempts)
            operation.next_attempt_at = timezone.now() + timedelta(seconds=delay)
        released = _release(operation, owner, wait=True)
        if not released:
            return "superseded"
        if operation.status not in TERMINAL_STATUSES:
            publish(operation.pk, delay=delay)
        return f"{operation.status}:{operation.last_error}"


def _safe_error(exc):
    if isinstance(exc, me.MojoException):
        return str(exc)[:500]
    return f"Provider operation is unavailable ({type(exc).__name__})"


def _advance_app(operation):
    profile = (operation.state or {}).get("profile") or {}
    existing = WebApp.objects.filter(group=operation.group,
                                     slug=profile["slug"]).first()
    if existing is not None:
        compatible = all(getattr(existing, field) == profile[field] for field in (
            "display_name", "environment", "bucket", "github_repository",
            "deployment_ref", "build_output"))
        if not compatible:
            raise me.ValueException(
                "A WebApp with this slug exists with a different profile")
        web_app = existing
        adopted = True
    else:
        web_app = WebApp.objects.create(
            group=operation.group, prefix="pending", **profile)
        web_app.prefix = web_app.storage_prefix()
        web_app.save(update_fields=["prefix", "modified"])
        adopted = False
    operation.web_app = web_app
    operation.evidence = dict(operation.evidence or {}, app={
        "status": "verified", "adopted": adopted, "webapp": web_app.pk})
    operation.cursor = "address"
    operation.attempts = 0
    operation.next_attempt_at = None
    _activity(operation, "Application profile reconciled")
    return True


def _record_values(record):
    return sorted(str(value).rstrip(".") for value in (
        getattr(record, "record_values", None) or
        (record.get("record_values") if isinstance(record, dict) else []) or []))


def _advance_address(operation):
    from mojo.apps.dnsman.models import Certificate, Domain, DomainPurchase
    from mojo.apps.dnsman.models.certificate import STATUS_ACTIVE as CERT_ACTIVE
    from mojo.apps.dnsman.models.domain import PROVIDER_MOJO
    from mojo.apps.dnsman.services import certs, dns

    choice = ((operation.state or {}).get("choices") or {}).get("address")
    if not choice:
        return True
    label = str(choice.get("label") or "").strip().lower()
    if not label or label == "*":
        raise me.ValueException(
            "Apex and wildcard WebApp onboarding are not supported; "
            "choose one subdomain")
    validators.validate_label(label)

    from mojo.apps.edge.services import webapp_apps_domain

    domain = None
    if choice.get("domain"):
        # Own group or an ancestor of it — a team's child group may serve
        # under the organization's domain. Never a sibling's or an unrelated
        # group's, which keep the same non-disclosing refusal.
        domain = webapp_apps_domain.owned_by_group_or_ancestor(
            operation.group).filter(pk=choice["domain"]).first()
        if domain is None:
            raise me.PermissionDeniedException("The selected domain is outside this group")
    elif choice.get("purchase"):
        purchase_id = choice.get("purchase")
        purchase = DomainPurchase.objects.filter(
            pk=purchase_id, group=operation.group).first()
        if purchase is None:
            raise me.PermissionDeniedException("The selected quote is outside this group")
        domain = operation.domain or Domain.objects.filter(
            metadata__purchase=purchase.pk, group=operation.group).first()
        if domain is None:
            if purchase.status == "failed":
                raise me.ValueException("The domain purchase failed")
            # A submitted purchase with no Domain is the registrar's bounded
            # reconciliation window. A worker never has the raw token and must
            # wait for authoritative registrar recovery.
            return True
    if domain is None or domain.status != "active" or not domain.verified:
        return True
    if domain.group_id != operation.group_id:
        # The domain was matched through the ancestor rule. READING it is the
        # inheritance contract; WRITING to it (the per-host CNAME, the
        # wildcard converge backstop, certificate requests) requires authority
        # in the group that owns the domain. Grants inherit downward, so an
        # organization that grants manage at the parent keeps working; a
        # child-only grant fails closed before any provider write.
        if not webapp_authority.can_manage_group_webapps(
                operation.actor, domain.group):
            raise me.PermissionDeniedException(
                f"Managing DNS for {domain.name} requires access in the "
                "workspace that owns it")
    operation.domain = domain
    operation.dns_provider = domain.provider

    hostname = f"{label}.{domain.name}"
    # One resolver decides the serving destination for every provider: it
    # validates the value, rejects a self-referential hostname, and raises
    # DestinationUnavailable when the installation has none. A raise here is a
    # plain retried failure (never WAIT_FOR_USER, never the external panel);
    # precheck and create already block the normal paths up front.
    from mojo.apps.edge.services import webapp_destination
    destination = webapp_destination.resolve(hostname)
    target = destination.value

    def address_evidence(**extra):
        base = {"hostname": hostname, "provider": domain.provider,
                "writable": domain.provider != PROVIDER_MOJO,
                "destination": {"type": destination.type, "value": destination.value,
                                "provenance": destination.provenance}}
        base.update(extra)
        return base

    if domain.provider == PROVIDER_MOJO:
        # External DNS: the platform holds no credential for this domain, so it
        # cannot write the record (dns.list_records/upsert_record both raise for
        # provider "mojo"). The user publishes the CNAME at their own host and
        # we confirm it authoritatively — no error budget while they do.
        if not (target and _verify_external_cname(hostname, target)):
            state = dict(operation.state or {})
            intent = dict(state.get("intent") or {})
            intent["dns"] = {"provider": "mojo", "type": "CNAME",
                             "name": hostname, "values": [target], "ttl": 300}
            state["intent"] = intent
            _save_state(operation, state, ["domain", "dns_provider"])
            operation.evidence = dict(
                operation.evidence or {}, address=address_evidence(
                    status="waiting", dns="unpublished",
                    records=_external_records(hostname, target, domain)))
            _activity(operation, "Waiting for the app address record at your DNS host")
            return WAIT_FOR_USER
    else:
        records = dns.list_records(domain)
        same_name = [row for row in records
                     if str(getattr(row, "name", row.get("name", ""))).rstrip(".") == hostname]
        cnames = [row for row in same_name
                  if str(getattr(row, "type", row.get("type", ""))).upper() == "CNAME"]
        conflicts = [row for row in same_name if row not in cnames]
        if conflicts or len(cnames) > 1:
            raise me.ValueException(
                "The selected hostname has ambiguous or non-CNAME DNS records")
        if cnames and _record_values(cnames[0]) != [target]:
            raise me.ValueException(
                "The selected hostname has a foreign CNAME value")
        # A `*.{domain}` CNAME already pointing at the destination routes every
        # subdomain, this one included — writing a per-host record would be a
        # no-op in resolution and pure clutter in the zone.
        wildcard_covers = any(
            str(getattr(row, "name", row.get("name", ""))).rstrip(".") == f"*.{domain.name}"
            and str(getattr(row, "type", row.get("type", ""))).upper() == "CNAME"
            and _record_values(row) == [target]
            for row in records)
        if not cnames and not wildcard_covers:
            apps_domain = webapp_apps_domain.resolve(operation.group)
            if apps_domain is not None and apps_domain.pk == domain.pk:
                # The workspace's apps domain: instead of one more per-host
                # record, converge the wildcard coverage (a `*.{domain}` CNAME
                # plus the apex+wildcard certificate request) so THIS app and
                # every later one are routed. Idempotent, and the certificate
                # scan below picks up whatever converge found or requested —
                # the pass proceeds to cert/vhost work exactly as if the
                # wildcard had already covered the name.
                webapp_apps_domain.converge(domain)
            else:
                state = dict(operation.state or {})
                intent = dict(state.get("intent") or {})
                intent["dns"] = {
                    "provider": domain.provider, "type": "CNAME", "name": hostname,
                    "values": [target], "ttl": 300,
                }
                state["intent"] = intent
                _save_state(operation, state, ["domain", "dns_provider"])
                try:
                    dns.upsert_record(domain, "CNAME", hostname, [target], ttl=300)
                except Exception:
                    proven = [row for row in dns.list_records(domain)
                              if str(getattr(row, "name", row.get("name", ""))).rstrip(".") == hostname
                              and str(getattr(row, "type", row.get("type", ""))).upper() == "CNAME"
                              and _record_values(row) == [target]]
                    if len(proven) != 1:
                        raise me.ValueException(
                            "DNS provider outcome is ambiguous; reconciliation will retry",
                            code=503, status=503)

    from mojo.apps.edge.models import Vhost
    existing_vhost = Vhost.objects.filter(
        domain=domain, label=label, is_enabled=True).select_related(
            "certificate").first()
    if existing_vhost is not None:
        # An enabled vhost already bound to a DIFFERENT WebApp is another
        # app's live address — refuse plainly here, before any certificate or
        # repoint work, instead of letting the one-to-one link raise a DB
        # IntegrityError further down.
        occupant = getattr(existing_vhost, "web_app", None)
        if occupant is not None and occupant.pk != operation.web_app_id:
            raise me.PermissionDeniedException(
                "That address is already used by another app")
        # An ALIAS address is equally taken — including one belonging to THIS
        # app. Adopting it as the primary would leave one vhost owned twice
        # (validate_vhost refuses that outright), so refuse plainly here.
        if existing_vhost.alias_of_id is not None:
            raise me.PermissionDeniedException(
                "That address is already used by another app")
    certificate = None
    if existing_vhost is not None:
        existing_certificate = existing_vhost.certificate
        if (existing_vhost.kind in ("site", "site_api") and
                existing_certificate.status == CERT_ACTIVE and
                validators.certificate_covers(existing_certificate, hostname) and
                (existing_certificate.renew_after is None or
                 existing_certificate.renew_after > timezone.now())):
            certificate = existing_certificate
    for candidate in Certificate.objects.filter(domain=domain, status=CERT_ACTIVE):
        if certificate is not None:
            break
        if (validators.certificate_covers(candidate, hostname) and
                (candidate.renew_after is None or
                 candidate.renew_after > timezone.now())):
            certificate = candidate
            break
    if certificate is None:
        pending = None
        for candidate in Certificate.objects.filter(
                domain=domain, status__in=("pending", "issuing")):
            if validators.certificate_covers(candidate, hostname):
                pending = candidate
                break
        if pending is None:
            if domain.provider == PROVIDER_MOJO and operation.attempts > 0:
                # A delegated certificate was requested this cycle and is now
                # neither pending nor active — it failed, almost always because
                # the _acme-challenge CNAME is missing or broken. Do not spin
                # requesting fresh certificates; wait for the user to repair the
                # record and press Check (which resets attempts and re-requests).
                operation.evidence = dict(
                    operation.evidence or {}, address=address_evidence(
                        status="waiting", dns="verified", certificate="failed",
                        records=_external_records(
                            hostname, target, domain, include_challenge=True)))
                _activity(
                    operation,
                    "Certificate issuance is waiting on the _acme-challenge record",
                    "warning")
                return WAIT_FOR_USER
            # Every provider issues the apex-plus-wildcard profile — one
            # certificate per domain, ever. The first app pays the issuance
            # wait; every later app (and any admin-created vhost) reuses it via
            # the covering-certificate scan above.
            cert_names = [domain.name, f"*.{domain.name}"]
            state = dict(operation.state or {})
            intent = dict(state.get("intent") or {})
            intent["certificate"] = {
                "provider": "acme", "domain": domain.pk, "names": cert_names,
            }
            state["intent"] = intent
            _save_state(operation, state, ["certificate"])
            pending = certs.request_certificate(domain)
        operation.certificate = pending
        operation.evidence = dict(
            operation.evidence or {}, address=address_evidence(
                status="waiting", dns="verified", certificate=pending.status))
        _activity(operation, "DNS is reconciled; certificate issuance is pending")
        return True

    operation.certificate = certificate
    vhost = existing_vhost
    if vhost is not None:
        if (vhost.kind not in ("site", "site_api") or
                vhost.certificate_id != certificate.pk):
            raise me.ValueException(
                "The hostname is already owned by an incompatible vhost")
    else:
        vhost = Vhost.objects.create(
            domain=domain, label=label, kind="site_api", certificate=certificate,
            pool=str(choice.get("pool") or "default"), spa=True)
    from mojo.apps.edge.services import webapp_auth_routes
    vhost, _, _ = webapp_auth_routes.reconcile(vhost)
    operation.vhost = vhost
    web_app = WebApp.objects.get(pk=operation.web_app_id)
    old_site_vhost = None
    if web_app.vhost_id not in (None, vhost.pk):
        previous = Vhost.objects.filter(pk=web_app.vhost_id).first()
        if previous is None or previous.kind not in ("site", "site_api"):
            raise me.ValueException(
                "The WebApp is already linked to an incompatible address")
        # Change-address: the old address kept serving until the new vhost and
        # certificate were ready (they are, here). Retire it now — its delete
        # publishes convergence so nodes drop the old server block.
        old_site_vhost = previous
    web_app.vhost = vhost
    web_app.save(update_fields=["vhost", "modified"])
    if old_site_vhost is not None:
        old_site_vhost.delete()
    operation.evidence = dict(
        operation.evidence or {}, address=address_evidence(
            status="verified", dns="verified",
            certificate=("reused" if certificate.created < operation.created
                         else "issued"),
            vhost=vhost.pk))
    operation.cursor = "github"
    operation.attempts = 0
    operation.next_attempt_at = None
    _activity(operation, "Address, DNS, certificate, and serving vhost reconciled")
    return True


def _github_evidence(operation, repository, ref_name, output, attested=False):
    from mojo.apps.github.models import GitHubInstall
    from mojo.apps.github.services import github_app

    install = GitHubInstall.objects.filter(group=operation.group).first()
    if install is None:
        return {"status": "attested" if attested else "unavailable",
                "reason": "No group-scoped GitHub installation is connected"}
    try:
        token = github_app.get_install_token(install)
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        repo_url = f"{github_app.GITHUB_API_BASE}/repos/{repository}"
        repo_response = requests.get(repo_url, headers=headers, timeout=15)
        if repo_response.status_code != 200:
            return {"status": "unavailable", "reason": "Repository is not visible to the group installation"}
        repo_data = repo_response.json()
        if str(repo_data.get("full_name") or "").lower() != repository.lower():
            return {"status": "unavailable", "reason": "GitHub returned a different repository identity"}
        ref_response = requests.get(
            f"{repo_url}/commits/{quote(ref_name, safe='')}",
            headers=headers, timeout=15)
        return {
            "status": "verified" if ref_response.status_code == 200 else "unavailable",
            "installation": install.pk, "repository": repo_data.get("full_name"),
            "ref": "verified" if ref_response.status_code == 200 else "unavailable",
            "output": "validated",
        }
    except Exception as exc:
        return {"status": "attested" if attested else "unavailable",
                "reason": f"GitHub evidence unavailable ({type(exc).__name__})"}


def _advance_github(operation):
    choice = ((operation.state or {}).get("choices") or {}).get("github") or {}
    profile = (operation.state or {}).get("profile") or {}
    repository = str(choice.get("repository") or profile.get("github_repository") or "").strip()
    if choice.get("skip") or not repository:
        # GitHub deploys are optional: an explicit skip, or simply no
        # repository anywhere, moves straight to verification. The WebApp's
        # github fields stay untouched so connecting a repo later starts clean.
        operation.evidence = dict(operation.evidence or {},
                                  github={"status": "skipped"})
        operation.cursor = "verify"
        operation.attempts = 0
        operation.next_attempt_at = None
        _activity(operation,
                  "GitHub deploys skipped; deploy any time from the app's page")
        return True
    ref_name = str(choice.get("ref") or profile.get("deployment_ref") or "main").strip()
    output = str(choice.get("output") or profile.get("build_output") or "dist").strip()
    validators.validate_github_repository(repository)
    validators.validate_deployment_ref(ref_name)
    validators.validate_build_output(output)
    evidence = _github_evidence(
        operation, repository, ref_name, output,
        attested=bool(choice.get("attest_unavailable")))
    if evidence["status"] == "unavailable":
        operation.evidence = dict(operation.evidence or {}, github=evidence)
        _activity(operation, evidence.get("reason", "GitHub evidence unavailable"),
                  "warning")
        return True
    web_app = WebApp.objects.get(pk=operation.web_app_id)
    web_app.github_repository = repository
    web_app.deployment_ref = ref_name
    web_app.build_output = output
    web_app.save(update_fields=[
        "github_repository", "deployment_ref", "build_output", "modified"])
    operation.evidence = dict(operation.evidence or {}, github=evidence)
    operation.cursor = "verify"
    operation.attempts = 0
    operation.next_attempt_at = None
    _activity(operation, f"GitHub repository evidence {evidence['status']}")
    return True


def _advance_verify(operation):
    from mojo.apps.edge.services.public_probe import probe_https_root

    if operation.vhost is None:
        raise me.ValueException("No serving vhost is linked")
    origin = f"https://{operation.vhost.server_name}"
    result = probe_https_root(origin)
    operation.evidence = dict(operation.evidence or {}, verify=_safe(result))
    if not result.get("ok"):
        _activity(operation, "Public HTTPS root is not ready", "warning")
        return True
    operation.status = STATUS_SUCCEEDED
    operation.cursor = "complete"
    operation.finished = timezone.now()
    operation.attempts = 0
    operation.next_attempt_at = None
    _activity(operation, "Public HTTPS root verified; onboarding complete")
    return False


def workflow(web_app, api_origin=None):
    """The single GitHub Actions workflow a user drops into their repo.

    GitHub-centric by design: the user installs one secret (``MOJO_DEPLOY_KEY``)
    and one file. The deploy step references django-mojo's public composite
    action straight from ``@main`` — the repo is public, so there is no
    version-tag machinery — and that action runs the release client on the
    GitHub runner. Nothing (no Python, no django-mojo) is installed on the
    user's machine. The old generator told users to run ``python -m
    mojo_webapp``, a module that exists nowhere; this is the real contract.
    """
    # A repository is optional — the yaml is a file the user drops into
    # whichever repo they later choose, and it only needs ref/output/api-url/
    # webapp-id. Validate one only when the app actually has one.
    repository = str(web_app.github_repository or "").strip() or None
    if repository is not None:
        repository = validators.validate_github_repository(repository)
    ref_name = validators.validate_deployment_ref(web_app.deployment_ref)
    output = validators.validate_build_output(web_app.build_output)
    origin = str(api_origin or "").strip().rstrip("/") or "https://YOUR-PLATFORM-ORIGIN"
    yaml = """name: Deploy WebApp
on:
  push:
    branches:
      - __REF__
permissions:
  contents: read
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: \"22\"
      - run: npm ci
      - run: npm run build
      - name: Deploy to django-mojo
        uses: NativeMojo/django-mojo/examples/github/actions/deploy-webapp@main
        with:
          api-url: \"__API_URL__\"
          webapp-id: \"__WEBAPP_ID__\"
          artifact-dir: \"__OUTPUT__\"
          version: ${{ github.sha }}
        env:
          MOJO_DEPLOY_KEY: ${{ secrets.MOJO_DEPLOY_KEY }}
"""
    yaml = yaml.replace("__REF__", ref_name)
    yaml = yaml.replace("__API_URL__", origin)
    yaml = yaml.replace("__WEBAPP_ID__", str(web_app.pk))
    yaml = yaml.replace("__OUTPUT__", output)
    return {"schema_version": SCHEMA_VERSION, "repository": repository,
            "filename": ".github/workflows/deploy-webapp.yml", "yaml": yaml}


def summary_for(web_app):
    """Frozen v1 contract for item 1818: group-scoped and secret-free.

    v1 is additive-only. Item 2099 added, without changing any existing
    meaning: `address.domain`, top-level `current_release`, and
    `latest_deployment` — the three facts a day-2 management view needs to
    answer "is my app live and serving X?" in one call. Item 2158 added
    `address.certificate` ({status, not_after}; null only when there is no
    vhost) so the same view can answer "is SSL healthy?" without a second
    read — Vhost.certificate is a non-null FK.
    """
    from mojo.apps.edge.models import Vhost, WebAppDeployment
    from mojo.apps.edge.services import webapp_keys

    operation = WebAppOnboardingOperation.objects.filter(
        web_app=web_app, group=web_app.group).first()
    key_status = webapp_keys.status(web_app)
    vhost = web_app.vhost if web_app.vhost_id else None
    hostname = vhost.server_name if vhost else None
    domain = vhost.domain if vhost else None
    certificate = vhost.certificate if vhost and vhost.certificate_id else None
    release = web_app.current_release
    deployment = WebAppDeployment.objects.filter(webapp=web_app).first()
    aliases = [
        {"hostname": alias.server_name,
         "certificate": ({
             "status": alias.certificate.status,
             "not_after": (alias.certificate.not_after.isoformat()
                           if alias.certificate.not_after else None),
         } if alias.certificate_id else None)}
        for alias in Vhost.objects.filter(alias_of=web_app).select_related(
            "domain", "certificate").order_by("pk")
    ]
    return {
        "schema_version": 1,
        "webapp": {
            "id": web_app.pk, "slug": web_app.slug,
            "display_name": web_app.display_name,
            "environment": web_app.environment,
            "repository": web_app.github_repository,
            "deployment_ref": web_app.deployment_ref,
            "build_output": web_app.build_output,
        },
        "address": {
            "hostname": hostname,
            "https_origin": f"https://{hostname}" if hostname else None,
            "domain": ({"id": domain.pk, "name": domain.name,
                        "provider": domain.provider} if domain else None),
            "certificate": ({
                "status": certificate.status,
                "not_after": (certificate.not_after.isoformat()
                              if certificate.not_after else None),
            } if certificate else None),
            # Additive (item 2226): the app's extra custom addresses. Empty
            # list, never null, so a reader never branches on absence.
            # schema_version stays 1 — v1 is additive-only.
            "aliases": aliases,
        },
        "current_release": ({
            "id": release.pk, "version": release.version,
            "status": release.status, "created": release.created.isoformat(),
        } if release else None),
        "latest_deployment": ({
            "id": deployment.pk, "status": deployment.status,
            "created": deployment.created.isoformat(),
            "finished": deployment.finished.isoformat() if deployment.finished else None,
        } if deployment else None),
        "onboarding": {
            "status": operation.status if operation else "not_started",
            "cursor": operation.cursor if operation else None,
            "evidence": _safe(operation.evidence if operation else {}),
        },
        "deployment_key": {
            "linked": bool(key_status.get("linked")),
            "active": bool(key_status.get("active")),
        },
    }
