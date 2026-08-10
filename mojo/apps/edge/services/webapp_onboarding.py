"""Crash-safe App -> Address -> GitHub -> Verify onboarding orchestration."""

import hashlib
import json
import uuid
from datetime import timedelta
from urllib.parse import quote, urlsplit

import requests
from django.db import transaction
from django.utils import timezone

from mojo import errors as me
from mojo.helpers.settings import settings

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
    if not operation.group.is_active:
        raise me.PermissionDeniedException("The onboarding group is no longer active")
    WebAppOnboardingOperation.rest_check_permission_or_raise(
        request, ["SAVE_PERMS", "VIEW_PERMS"] if mutate else ["VIEW_PERMS"],
        operation)
    if mutate and operation.origin != request_origin(request):
        raise me.PermissionDeniedException("Onboarding must continue on its original origin")


def serialize(operation):
    evidence = _safe(operation.evidence or {})
    profile = _safe((operation.state or {}).get("profile") or {})
    choices = _safe((operation.state or {}).get("choices") or {})
    return {
        "schema_version": SCHEMA_VERSION,
        "id": operation.pk,
        "operation_id": str(operation.operation_id),
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


def options(group):
    from mojo.apps.github.models import GitHubInstall

    buckets = validators.release_buckets()
    return {
        "schema_version": SCHEMA_VERSION,
        "buckets": buckets,
        "environments": ["production", "staging", "preview", "development"],
        "cname_target": str(settings.get_static(
            "EDGE_WEBAPP_CNAME_TARGET", "") or ""),
        "github_connected": GitHubInstall.objects.filter(group=group).exists(),
        "limits": {"attempts": MAX_ATTEMPTS, "lease_seconds": LEASE_SECONDS},
    }


@transaction.atomic
def create(group, actor, origin, payload):
    slug = str(payload.get("slug") or "").strip().lower()
    profile = {
        "slug": slug,
        "display_name": str(payload.get("display_name") or slug).strip(),
        "environment": str(payload.get("environment") or "production").strip(),
        "bucket": str(payload.get("bucket") or "").strip(),
        "github_repository": str(payload.get("github_repository") or "").strip(),
        "deployment_ref": str(payload.get("deployment_ref") or "main").strip(),
        "build_output": str(payload.get("build_output") or "dist").strip(),
    }
    candidate = WebApp(group=group, prefix="pending", **profile)
    validators.validate_web_app(candidate)
    fingerprint = _fingerprint(group, profile)
    current = WebAppOnboardingOperation.objects.select_for_update().filter(
        group=group, replay_fingerprint=fingerprint,
        status__in=(STATUS_ACTIVE, STATUS_WAITING)).first()
    if current is not None:
        if current.actor_id != actor.pk or current.origin != origin:
            raise me.PermissionDeniedException(
                "Matching onboarding is already owned by another session")
        return current, False
    operation = WebAppOnboardingOperation.objects.create(
        group=group, actor=actor, origin=origin,
        replay_fingerprint=fingerprint,
        state={"profile": profile, "choices": {}, "intent": {}},
        evidence={})
    _activity(operation, "Onboarding created; application intent frozen")
    _save_state(operation, operation.state)
    return operation, True


def choose(operation, request, payload):
    raw_choice = payload.get("choice") or {}
    purchase_confirmation = None
    with transaction.atomic():
        locked = WebAppOnboardingOperation.objects.select_for_update().select_related(
            "group", "actor").get(pk=operation.pk)
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
        _activity(locked, f"Choice accepted for {step}")
        fields = ["status", "last_error", "next_attempt_at"]
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
    locked = WebAppOnboardingOperation.objects.select_for_update().select_related(
        "group", "actor").get(pk=operation.pk)
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
                not operation.group.is_active or not
                operation.group.user_has_permission(
                    operation.actor, ["manage_webapp", "security"])):
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
        if wait and choice and operation.attempts < MAX_ATTEMPTS:
            operation.attempts += 1
            retry_delay = min(300, 2 ** operation.attempts)
            operation.next_attempt_at = timezone.now() + timedelta(
                seconds=retry_delay)
        elif wait and choice and operation.attempts >= MAX_ATTEMPTS:
            operation.status = STATUS_FAILED
            operation.finished = timezone.now()
            operation.last_error = "Onboarding recovery attempts were exhausted"
            _activity(operation, operation.last_error, "error")
        released = _release(operation, owner, wait=wait)
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

    domain = None
    if choice.get("domain"):
        domain = Domain.objects.filter(pk=choice["domain"],
                                       group=operation.group).first()
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
    operation.domain = domain
    operation.dns_provider = domain.provider

    hostname = f"{label}.{domain.name}"
    target = str(settings.get_static("EDGE_WEBAPP_CNAME_TARGET", "") or "").strip().rstrip(".")
    validators.validate_server_name(target)
    if target == hostname:
        raise me.ValueException("The WebApp CNAME target cannot point to itself")
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
    if not cnames:
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
    certificate = None
    if existing_vhost is not None:
        existing_certificate = existing_vhost.certificate
        if (existing_vhost.kind == "site" and
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
            state = dict(operation.state or {})
            intent = dict(state.get("intent") or {})
            intent["certificate"] = {
                "provider": "acme", "domain": domain.pk,
                "names": [hostname],
            }
            state["intent"] = intent
            _save_state(operation, state, ["certificate"])
            pending = certs.request_certificate(domain, names=[hostname])
        operation.certificate = pending
        operation.evidence = dict(operation.evidence or {}, address={
            "status": "waiting", "hostname": hostname,
            "dns": "verified", "certificate": pending.status})
        _activity(operation, "DNS is reconciled; certificate issuance is pending")
        return True

    operation.certificate = certificate
    vhost = existing_vhost
    if vhost is not None:
        if (vhost.kind != "site" or vhost.certificate_id != certificate.pk):
            raise me.ValueException(
                "The hostname is already owned by an incompatible vhost")
    else:
        vhost = Vhost.objects.create(
            domain=domain, label=label, kind="site", certificate=certificate,
            pool=str(choice.get("pool") or "default"), spa=True)
    operation.vhost = vhost
    web_app = WebApp.objects.get(pk=operation.web_app_id)
    if web_app.vhost_id not in (None, vhost.pk):
        raise me.ValueException("The WebApp is already linked to another vhost")
    web_app.vhost = vhost
    web_app.save(update_fields=["vhost", "modified"])
    operation.evidence = dict(operation.evidence or {}, address={
        "status": "verified", "hostname": hostname, "dns": "verified",
        "certificate": "reused" if certificate.created < operation.created else "issued",
        "vhost": vhost.pk})
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


def workflow(web_app):
    """Return a secret-free workflow whose inputs never become shell syntax."""
    repository = validators.validate_github_repository(web_app.github_repository)
    ref_name = validators.validate_deployment_ref(web_app.deployment_ref)
    output = validators.validate_build_output(web_app.build_output)
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
    env:
      MOJO_WEBAPP_ID: \"__WEBAPP_ID__\"
      BUILD_OUTPUT: \"__OUTPUT__\"
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: \"22\"
      - run: npm ci
      - run: npm run build
      - name: Register release
        env:
          MOJO_DEPLOY_KEY: ${{ secrets.MOJO_DEPLOY_KEY }}
        run: |
          test -n \"${MOJO_DEPLOY_KEY}\"
          test -d \"${BUILD_OUTPUT}\"
          python -m mojo_webapp deploy --webapp \"${MOJO_WEBAPP_ID}\" --directory \"${BUILD_OUTPUT}\"
"""
    yaml = yaml.replace("__REF__", ref_name)
    yaml = yaml.replace("__WEBAPP_ID__", str(web_app.pk))
    yaml = yaml.replace("__OUTPUT__", output)
    return {"schema_version": SCHEMA_VERSION, "repository": repository,
            "filename": ".github/workflows/deploy-webapp.yml", "yaml": yaml}


def summary_for(web_app):
    """Frozen v1 contract for item 1818: group-scoped and secret-free."""
    from mojo.apps.edge.services import webapp_keys

    operation = WebAppOnboardingOperation.objects.filter(
        web_app=web_app, group=web_app.group).first()
    key_status = webapp_keys.status(web_app)
    hostname = web_app.vhost.server_name if web_app.vhost_id else None
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
        "address": {"hostname": hostname, "https_origin": f"https://{hostname}" if hostname else None},
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
