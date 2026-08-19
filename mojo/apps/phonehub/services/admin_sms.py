"""Service layer for the Admin portal's Text messages (SMS) page.

Owns the read model (summary), the zero-side-effect connection test, the
test-SMS send, and the verified system-row save. Everything imports phonehub
models lazily and degrades to a clean not-installed envelope when
``mojo.apps.phonehub`` is not in INSTALLED_APPS (D8, maestro #2189).

Security invariants (D1):

* Only the SYSTEM row (group=None) is writable here, and only by a literal
  superuser — the REST boundary proves that, and
  ``provider_setup.save_messaging_system_config`` re-proves it under lock.
* A save whose credentials do not verify is refused before anything is
  written. ``test_mode`` is NOT a verification bypass — verification runs the
  real provider check regardless, because SMS.send() never reads test_mode.
* No secret value ever leaves this module — only "is it set" booleans.
"""

from mojo import errors as merrors
from mojo.helpers import logit


PROVIDERS = ("twilio", "aws", "mojo")

DEFAULT_NAMES = {
    "twilio": "Twilio SMS",
    "aws": "AWS SNS SMS",
    "mojo": "Mojo Remote SMS",
}

OVERRIDE_LIMIT = 25

SECRET_FIELDS = (
    "twilio_account_sid", "twilio_auth_token",
    "aws_access_key_id", "aws_secret_access_key", "mojo_api_key")

# Browser-facing text for every stable error code the connection tests can
# produce (D4). Raw provider exception text never reaches the browser — it is
# logged where the test ran.
DIAGNOSED = {
    "missing_credentials": "Credentials are not configured, or only half a "
                           "credential pair is stored.",
    "missing_library": "The provider's client library is not installed on "
                       "this server.",
    "connection_failed": "The provider could not be reached or rejected the "
                         "credentials.",
    "timeout": "The provider did not answer in time.",
    "invalid_credentials": "The provider rejected the API key.",
    "insufficient_permission": "The credentials are valid but lack the "
                               "permission required to send SMS.",
    "invalid_provider": "The configured provider is not supported.",
    "config_error": "The configuration is incomplete or inconsistent.",
    "no_config": "No SMS configuration exists yet.",
}

# test_mode is a THIRD state — never collapsed into OK (D4), and never a
# verification bypass (D1): SMS.send() ignores test_mode entirely.
TEST_MODE_MESSAGE = "Test mode — provider not contacted"


def diagnose(code):
    """Fixed-vocabulary browser text for a stable error code."""
    return DIAGNOSED.get(code, "The connection test failed.")


def is_installed():
    from django.apps import apps
    return apps.is_installed("mojo.apps.phonehub")


def not_installed_envelope():
    return {
        "schema_version": 1,
        "installed": False,
        "error": "Text messaging is not installed on this platform",
        "error_code": "not_installed",
    }


def _system_row():
    from mojo.apps.phonehub.models import PhoneConfig
    return PhoneConfig.objects.filter(
        group__isnull=True, is_active=True).order_by("pk").first()


def _describe(row):
    return {
        "id": row.pk,
        "name": row.name,
        "provider": row.provider,
        "is_active": bool(row.is_active),
        "test_mode": bool(row.test_mode),
        "twilio_from_number": row.twilio_from_number or "",
        "aws_region": row.aws_region or "",
        "aws_sender_id": row.aws_sender_id or "",
        "mojo_remote_url": row.mojo_remote_url or "",
        # Which secrets are SET — never their values (R2).
        "secrets": {name: bool(row.get_secret(name)) for name in SECRET_FIELDS},
    }


def summary():
    """The system config, active group overrides, and verification state."""
    if not is_installed():
        return not_installed_envelope()
    from mojo.apps.phonehub.models import PhoneConfig
    from mojo.helpers.settings import settings

    system = _system_row()
    # Only ACTIVE overrides: get_for_group() ignores inactive rows, so an
    # inactive row must never be shown as governing a group.
    overrides = PhoneConfig.objects.filter(
        group__isnull=False, is_active=True).select_related("group")
    items = list(overrides.order_by("group__name")[:OVERRIDE_LIMIT])
    count = overrides.count()
    from mojo.apps.account.services import provider_setup
    return {
        "schema_version": 1,
        "installed": True,
        "system": _describe(system) if system else None,
        "group_overrides": {
            "items": [{
                "id": row.pk,
                "group": {"id": row.group_id, "name": row.group.name},
                "name": row.name,
                "provider": row.provider,
                "test_mode": bool(row.test_mode),
            } for row in items],
            "count": count,
            "truncated": count > len(items),
        },
        "verify_state": provider_setup.sms_verify_state(),
        "settings_fallback": {
            "twilio_number_configured": bool(settings.get("TWILIO_NUMBER")),
            "twilio_credentials_configured": bool(
                settings.get("TWILIO_ACCOUNT_SID") and
                settings.get("TWILIO_AUTH_TOKEN")),
        },
    }


def _diagnosed_result(result, row):
    """Post-process a PhoneConfig.test_connection() dict for the browser.

    Success keeps its provider-shaped message; failure text is replaced by the
    fixed vocabulary; test_mode stays a distinct third state.
    """
    result = dict(result)
    result.pop("details", None)  # defensively — no raw text rides along
    envelope = {
        "schema_version": 1,
        "installed": True,
        "config": {"id": row.pk, "provider": row.provider, "name": row.name},
        "success": bool(result.get("success")),
        "error": result.get("error"),
    }
    if result.get("test_mode"):
        envelope["state"] = "test_mode"
        envelope["message"] = TEST_MODE_MESSAGE
    elif envelope["success"]:
        envelope["state"] = "ok"
        envelope["message"] = result.get("message") or "Connection verified"
    else:
        envelope["state"] = "failed"
        envelope["message"] = diagnose(result.get("error"))
    return envelope


def test_connection(config_id=None):
    """Run PhoneConfig.test_connection() (D9) and diagnose the result (R4)."""
    if not is_installed():
        return not_installed_envelope()
    from mojo.apps.phonehub.models import PhoneConfig
    if config_id:
        try:
            row = PhoneConfig.objects.filter(pk=int(config_id)).first()
        except (TypeError, ValueError):
            raise merrors.ValueException("config_id must be an integer")
    else:
        row = _system_row()
    if row is None:
        return {
            "schema_version": 1, "installed": True, "success": False,
            "state": "failed", "error": "no_config",
            "message": diagnose("no_config"),
        }
    return _diagnosed_result(row.test_connection(), row)


def send_test(actor, to_number):
    """Send ONE test message through the effective system config (R5)."""
    if not is_installed():
        return not_installed_envelope()
    if not to_number or not isinstance(to_number, str):
        raise merrors.ValueException("A to_number is required")
    from mojo.apps.phonehub.models import SMS
    sms = SMS.send(
        "django-mojo Admin test message", to_number,
        metadata={"source": "admin_sms_test", "actor": actor.pk})
    if sms.is_test:
        message = "Test number — nothing was sent"
    elif sms.status == "failed":
        message = "The test message failed to send"
    else:
        message = "Test message handed to the provider"
    return {
        "schema_version": 1,
        "installed": True,
        "sent": sms.status == "sent",
        "test_number": bool(sms.is_test),
        "message": message,
        "sms": {
            "id": sms.pk,
            "status": sms.status,
            "provider": sms.provider,
            "from_number": sms.from_number,
            "to_number": sms.to_number,
            "is_test": bool(sms.is_test),
            "error_code": sms.error_code,
            "error_message": sms.error_message,
        },
    }


_COMMON_KEYS = {"action", "expected_revision", "provider", "name", "test_mode"}
_PROVIDER_KEYS = {
    "mojo": {"remote_url", "api_key", "clear_api_key"},
    "twilio": {"twilio_from_number", "twilio_account_sid", "twilio_auth_token",
               "clear_twilio_credentials"},
    "aws": {"aws_region", "aws_sender_id", "aws_access_key_id",
            "aws_secret_access_key", "clear_aws_credentials"},
}


def _text(data, name, max_length=255, required=False):
    value = data.get(name)
    if value in (None, ""):
        if required:
            raise merrors.ValueException(f"{name} is required")
        return None
    if not isinstance(value, str) or len(value) > max_length:
        raise merrors.ValueException(f"{name} is invalid")
    return value.strip()


def _normalize_save(data):
    """Validate exactly one provider's section, failing closed on extras."""
    provider = data.get("provider")
    if provider not in PROVIDERS:
        raise merrors.ValueException(
            "SMS provider must be twilio, aws, or mojo")
    allowed = _COMMON_KEYS | _PROVIDER_KEYS[provider]
    extra = set(data.keys()) - allowed
    if extra:
        raise merrors.ValueException(
            "SMS setup contains unsupported fields: " + ", ".join(sorted(extra)))
    normalized = {
        "provider": provider,
        "name": _text(data, "name", 100) or DEFAULT_NAMES[provider],
        "test_mode": data.get("test_mode") is True,
    }
    if provider == "mojo":
        from mojo.deploy import config_override
        remote_url = data.get("remote_url", "")
        if not config_override.https_origin(remote_url):
            raise merrors.ValueException("SMS remote URL must be one HTTPS origin")
        normalized.update({
            "remote_url": remote_url.rstrip("/"),
            "api_key": _text(data, "api_key", 4096),
            "clear_api_key": data.get("clear_api_key") is True,
        })
    elif provider == "twilio":
        normalized.update({
            "twilio_from_number": _text(data, "twilio_from_number", 20),
            "twilio_account_sid": _text(data, "twilio_account_sid", 256),
            "twilio_auth_token": _text(data, "twilio_auth_token", 256),
            "clear_twilio_credentials": data.get("clear_twilio_credentials") is True,
        })
        if bool(normalized["twilio_account_sid"]) != bool(normalized["twilio_auth_token"]):
            raise merrors.ValueException(
                "Supply both twilio_account_sid and twilio_auth_token, or neither")
    else:
        normalized.update({
            "aws_region": _text(data, "aws_region", 20),
            "aws_sender_id": _text(data, "aws_sender_id", 11),
            "aws_access_key_id": _text(data, "aws_access_key_id", 256),
            "aws_secret_access_key": _text(data, "aws_secret_access_key", 256),
            "clear_aws_credentials": data.get("clear_aws_credentials") is True,
        })
        if bool(normalized["aws_access_key_id"]) != bool(normalized["aws_secret_access_key"]):
            raise merrors.ValueException(
                "Supply both aws_access_key_id and aws_secret_access_key, or neither")
    return normalized


def _verify_candidate(normalized):
    """Verify the configuration a save WOULD store, without storing anything.

    Builds an unsaved in-memory PhoneConfig, merges stored secrets under the
    same rules the writer uses, and runs the real per-provider test directly —
    never test_connection(), whose test_mode short-circuit must not vouch for
    a system-row write (D1).
    """
    from mojo.apps.phonehub.models import PhoneConfig
    stored = _system_row()
    provider = normalized["provider"]
    candidate = PhoneConfig(group=None, provider=provider, name="candidate")
    candidate.test_mode = False
    if provider == "mojo":
        if normalized["clear_api_key"]:
            return {"success": True, "code": "clear_requested",
                    "message": "SMS credential will be cleared"}
        url = normalized["remote_url"]
        candidate.mojo_remote_url = url
        key = normalized["api_key"]
        # The stored key is reused ONLY for the URL it was stored against —
        # verification must never send the stored credential to a new host.
        if not key and stored and stored.provider == "mojo" and \
                (stored.mojo_remote_url or "").rstrip("/") == url:
            key = stored.get_mojo_api_key()
        if key:
            candidate.set_mojo_api_key(key)
        result = candidate._test_mojo()
    elif provider == "twilio":
        candidate.twilio_from_number = normalized["twilio_from_number"]
        sid = normalized["twilio_account_sid"]
        token = normalized["twilio_auth_token"]
        if not sid and stored and not normalized["clear_twilio_credentials"]:
            sid = stored.get_twilio_account_sid()
            token = stored.get_twilio_auth_token()
        if sid:
            candidate.set_secret("twilio_account_sid", sid)
        if token:
            candidate.set_secret("twilio_auth_token", token)
        from mojo.apps.phonehub.services import twilio as twilio_service
        resolution = twilio_service.resolve_credentials(candidate)
        if resolution.error:
            return {"success": False, "code": "config_error",
                    "message": resolution.error}
        result = candidate._test_twilio()
    else:
        candidate.aws_region = normalized["aws_region"] or "us-east-1"
        candidate.aws_sender_id = normalized["aws_sender_id"]
        key_id = normalized["aws_access_key_id"]
        secret = normalized["aws_secret_access_key"]
        if not key_id and stored and not normalized["clear_aws_credentials"]:
            key_id = stored.get_aws_access_key_id()
            secret = stored.get_aws_secret_access_key()
        if key_id:
            candidate.set_secret("aws_access_key_id", key_id)
        if secret:
            candidate.set_secret("aws_secret_access_key", secret)
        result = candidate._test_aws()
    if result.get("success"):
        return {"success": True, "code": None,
                "message": result.get("message") or "Connection verified"}
    code = result.get("error") or "connection_failed"
    return {"success": False, "code": code, "message": diagnose(code)}


def save_config(actor, data):
    """Verify, then write the SYSTEM PhoneConfig row through provider_setup.

    Verification is mandatory and runs BEFORE anything is written; a failed
    verification refuses the save outright. The write itself carries the full
    Settings-page safeguard stack — installation lock, live superuser
    re-check, revision token, unconditional revision bump, audit event.
    """
    if not is_installed():
        return not_installed_envelope()
    normalized = _normalize_save(data)
    verify_result = _verify_candidate(normalized)
    if not verify_result["success"]:
        logit.warning(
            f"admin_sms save refused for user={actor.pk}: verification failed "
            f"({verify_result['code']})")
        raise merrors.ValueException(
            "Provider verification failed: " + verify_result["message"])
    fields = {key: value for key, value in normalized.items()
              if key not in ("provider", "name")}
    from mojo.apps.account.services import provider_setup
    result = provider_setup.save_messaging_system_config(
        actor,
        provider=normalized["provider"],
        name=normalized["name"],
        expected_revision=data.get("expected_revision"),
        verify_result=verify_result,
        **fields)
    result["schema_version"] = 1
    result["installed"] = True
    result["results"] = {"sms": verify_result}
    return result
