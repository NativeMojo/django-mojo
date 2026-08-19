"""Superuser setup for fleet GeoIP and the system Mojo SMS provider.

Every write is addressed to exactly one **topic**.  GeoIP and SMS share an
edit token but nothing else: one failing integration must not hold the other
hostage, and an SMS credential must be savable on an installation that never
publishes fleet configuration at all.
"""

import json
import uuid
from urllib.parse import urlsplit

from django.db import transaction
from django.utils import timezone

from mojo import errors as merrors
from mojo.deploy import config_override
from mojo.helpers.settings import settings


FLEET_KEYS = tuple(config_override.DEFAULTS)
FILENAME = "django.override.json"
SETUP_REVISION_KEY = "ADMIN_PROVIDER_SETUP_REVISION"
VERIFY_STATE_KEY = "ADMIN_PROVIDER_VERIFY_STATE"
TOPICS = ("geoip", "sms")


def _static(name, default=None):
    return settings.get_static(name, default)


def _allowed_keys():
    from mojo.apps.account.services import admin_settings
    registered = {
        row.key for row in admin_settings.descriptors()
        if row.storage == "fleet_config" and row.writable == "fleet_config"
    }
    configured = _static("ADMIN_FLEET_CONFIG_ALLOWED_KEYS", [])
    return (config_override.normalize_allowed(configured) &
            frozenset(config_override.VALIDATORS) & registered)


def _location():
    bucket = _static("ADMIN_FLEET_CONFIG_BUCKET", _static("AWS_CONFIG_BUCKET", ""))
    prefix = _static("ADMIN_FLEET_CONFIG_PREFIX", _static("AWS_CONFIG_PREFIX", ""))
    filename = _static("ADMIN_FLEET_CONFIG_FILENAME", FILENAME)
    bucket = str(bucket or "").strip()
    prefix = str(prefix or "").strip("/")
    filename = str(filename or "").strip()
    if (not bucket or not prefix or not filename or len(filename) > 128 or
            "/" in filename or filename in (".", "..")):
        return bucket, ""
    key = f"{prefix}/{filename}"
    return str(bucket or ""), key


def _kms_key():
    return _static("ADMIN_FLEET_CONFIG_KMS_KEY_ID", _static("KMS_KEY_ID", ""))


def _restart_enabled():
    value = _static("ADMIN_FLEET_CONFIG_RESTART_ENABLED",
                    _static("CONFIG_SYNC_RESTART", False))
    return value is True or str(value).strip().lower() in ("1", "true", "yes", "on")


def _s3_client():
    from mojo.helpers.aws.client import get_client
    return get_client("s3", region=_static("AWS_REGION", None))


def _superuser(actor, lock=False):
    from mojo.apps.account.services.admin_settings import require_catalog_writer
    actor = require_catalog_writer(actor, lock=lock)
    if not actor.is_superuser:
        raise merrors.PermissionDeniedException(
            "A superuser is required to publish fleet configuration")
    return actor


def _loaded_values():
    return {
        key: _static(key, default)
        for key, default in config_override.DEFAULTS.items()
    }


def _published(s3, bucket, key, allowed):
    if not bucket or not key:
        return None
    from botocore.exceptions import ClientError
    try:
        response = s3.get_object(Bucket=bucket, Key=key)
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            return None
        raise
    if response.get("ContentLength", 0) > config_override.MAX_DOCUMENT_BYTES:
        raise merrors.ValueException("Published fleet configuration is too large")
    body = response["Body"]
    try:
        payload = body.read(config_override.MAX_DOCUMENT_BYTES + 1)
    finally:
        close = getattr(body, "close", None)
        if close:
            close()
    advertised = response.get("Metadata", {}).get("sha256")
    if not advertised or advertised != config_override.sha256(payload):
        raise merrors.ValueException("Published fleet configuration failed integrity verification")
    return {
        "document": config_override.decode_document(payload, allowed),
        "etag": response.get("ETag"),
        "version_id": response.get("VersionId"),
    }


def _key_hint(value):
    """Last four characters of a stored key, or "" when that would say too much.

    Four characters of an eight-character-or-longer credential identify which
    key is installed without narrowing a guess; a shorter value gets no hint at
    all rather than a proportionally large fragment of itself.  Never a prefix
    (provider keys are prefixed by convention) and never a length.
    """
    if not isinstance(value, str) or len(value) < 8:
        return ""
    return value[-4:]


def known_providers():
    """GeoIP provider identifiers this installation can actually dispatch to."""
    try:
        from mojo.helpers import geoip
        return sorted(geoip.PROVIDERS)
    except Exception:
        # A picker is convenience only; validate_settings remains the authority
        # on what may be saved, so an unavailable registry costs a list, not a
        # working page.
        return []


def state(include_remote=True):
    from django.apps import apps
    from mojo.apps.account.models import Setting
    allowed = _allowed_keys()
    bucket, key = _location()
    loaded_revision = _static(config_override.REVISION_KEY, "")
    published = None
    remote_error = None
    if include_remote and bucket and key and allowed:
        try:
            published = _published(_s3_client(), bucket, key, allowed)
        except Exception as error:
            remote_error = error.__class__.__name__
    # One bounded row, decrypted once, sliced immediately: the hint is what lets
    # the browser say "key set · ····9f2c" without a value ever leaving here.
    secret_row = Setting.objects.filter(
        key="GEOIP_API_KEY_MOJO", group=None, is_secret=True).order_by("pk").first()
    secret_configured = secret_row is not None
    secret_hint = _key_hint(secret_row.get_value()) if secret_row else ""
    sms = {"configured": False, "remote_url": "", "api_key_configured": False,
           "api_key_hint": ""}
    if apps.is_installed("mojo.apps.phonehub"):
        from mojo.apps.phonehub.models import PhoneConfig
        row = PhoneConfig.objects.filter(group=None, provider="mojo", is_active=True).first()
        if row:
            stored_key = row.get_mojo_api_key()
            sms = {
                "configured": True,
                "remote_url": row.mojo_remote_url or "",
                "api_key_configured": bool(stored_key),
                "api_key_hint": _key_hint(stored_key),
                "test_mode": bool(row.test_mode),
            }
    desired_geoip = dict(_loaded_values())
    if published:
        desired_geoip.update(published["document"]["settings"])
    published_revision = (published["document"].get("revision")
                          if published else None)
    configuration_revision = _configuration_token(
        _configuration_revision(), published_revision)
    from mojo.apps.account.services import admin_settings
    by_key = {row.key: row for row in admin_settings.descriptors()}
    fleet_fields = [{
        "key": key,
        "label": by_key[key].label,
        "description": by_key[key].description,
        "value_type": by_key[key].value_type,
        "constraints": by_key[key].constraints,
    } for key in FLEET_KEYS if key in allowed and key in by_key]
    complete_delegation = frozenset(FLEET_KEYS) <= allowed
    return {
        "available": bool(bucket and key and complete_delegation and _kms_key() and
                          _restart_enabled()),
        "bucket_configured": bool(bucket),
        "restart_configured": _restart_enabled(),
        "object_key": key,
        "delegated_keys": sorted(allowed),
        "fleet_fields": fleet_fields,
        "loaded_revision": loaded_revision or None,
        "published_revision": published_revision,
        "configuration_revision": configuration_revision,
        "published_version": published.get("version_id") if published else None,
        "pending_restart": bool(
            published and published["document"].get("revision") != loaded_revision),
        "remote_error": remote_error,
        "geoip": {
            **desired_geoip,
            "GEOIP_API_KEY_MOJO_CONFIGURED": secret_configured,
            "GEOIP_API_KEY_MOJO_HINT": secret_hint,
        },
        "geoip_providers": known_providers(),
        "sms": sms,
        "verify_state": _read_verify_state(),
    }


def require_topic(topic):
    """Fail closed on anything but a named topic; there is no default."""
    if topic not in TOPICS:
        raise merrors.ValueException("Provider setup requires topic geoip or sms")
    return topic


def _normalize_geoip(section):
    extra = set(section) - set(config_override.DEFAULTS) - {
        "GEOIP_API_KEY_MOJO", "clear_api_key"}
    if extra:
        raise merrors.ValueException("GeoIP setup contains unsupported fields")
    static_values = {key: section.get(key, default)
                     for key, default in config_override.DEFAULTS.items()}
    try:
        static_values = config_override.validate_settings(static_values, _allowed_keys())
    except ValueError as error:
        raise merrors.ValueException(str(error)) from None
    api_key = section.get("GEOIP_API_KEY_MOJO")
    if api_key is not None and (not isinstance(api_key, str) or len(api_key) > 4096):
        raise merrors.ValueException("GeoIP API key is invalid")
    return {"static_values": static_values, "api_key": api_key,
            "clear_api_key": section.get("clear_api_key") is True}


def _normalize_sms(section):
    # phonehub is optional, and SMS is now addressable on its own. Refuse with
    # a sentence rather than letting the model import fail deep in a writer.
    from django.apps import apps
    if not apps.is_installed("mojo.apps.phonehub"):
        raise merrors.ValueException(
            "Text messaging is not installed on this platform")
    if set(section) - {"remote_url", "api_key", "clear_api_key", "test_mode"}:
        raise merrors.ValueException("SMS setup contains unsupported fields")
    remote_url = section.get("remote_url", "")
    if not config_override.https_origin(remote_url):
        raise merrors.ValueException("SMS remote URL must be one HTTPS origin")
    api_key = section.get("api_key")
    if api_key is not None and (not isinstance(api_key, str) or len(api_key) > 4096):
        raise merrors.ValueException("SMS API key is invalid")
    return {"remote_url": remote_url.rstrip("/"), "api_key": api_key,
            "clear_api_key": section.get("clear_api_key") is True,
            "test_mode": section.get("test_mode") is True}


def _normalize_payload(payload, topic):
    """Validate exactly one topic's section.

    The other topic's section is a rejection rather than an ignored extra: a
    save must fail alone, and silently accepting a section this call will never
    write is how a browser ends up believing it saved something it did not.
    """
    require_topic(topic)
    if not isinstance(payload, dict) or set(payload) - {topic, "expected_revision"}:
        raise merrors.ValueException(
            f"Provider setup accepts only {topic} and expected_revision")
    section = payload.get(topic) or {}
    if not isinstance(section, dict):
        raise merrors.ValueException("Provider setup sections must be objects")
    expected_revision = payload.get("expected_revision")
    if expected_revision is not None and (
            not isinstance(expected_revision, str) or
            not config_override.REVISION_RE.fullmatch(expected_revision)):
        raise merrors.ValueException("Provider setup revision is invalid")
    normalized = (_normalize_geoip(section) if topic == "geoip"
                  else _normalize_sms(section))
    return normalized, expected_revision


def _write_secret(key, value, clear):
    from mojo.apps.account.models import Setting
    with transaction.atomic():
        rows = list(Setting.objects.select_for_update().filter(
            key=key, group=None).order_by("pk"))
        if clear:
            Setting.objects.filter(pk__in=[row.pk for row in rows]).delete()
            def clear_cache():
                redis = Setting._redis()
                if redis:
                    redis.hdel(Setting._redis_key(), key)
            transaction.on_commit(clear_cache)
            return
        if value in (None, ""):
            return
        row = rows[0] if rows else Setting(key=key, group=None, is_secret=True)
        row.is_secret = True
        row.set_value(value)
        row.save(_protected_writer="GEOIP_API_KEY_MOJO", _skip_cache=True)
        if len(rows) > 1:
            Setting.objects.filter(pk__in=[item.pk for item in rows[1:]]).delete()
        transaction.on_commit(row.push_to_cache)


def _configuration_revision(*, lock=False):
    """Return the installation-wide provider edit revision from the database."""
    from mojo.apps.account.models import Setting
    rows = Setting.objects.filter(key=SETUP_REVISION_KEY, group=None).order_by("pk")
    if lock:
        rows = rows.select_for_update()
    rows = list(rows)
    if len(rows) > 1:
        raise merrors.ValueException(
            "Conflicting provider configuration revisions must be repaired")
    if not rows:
        return None
    value = rows[0].value
    if not isinstance(value, str) or not config_override.REVISION_RE.fullmatch(value):
        raise merrors.ValueException("Provider configuration revision is invalid")
    return value


def _configuration_token(edit_revision, published_revision):
    """Bind the browser's edit token to both database and S3 state."""
    if not edit_revision:
        return published_revision
    if edit_revision == published_revision:
        return edit_revision
    value = f"{edit_revision}:{published_revision or 'none'}".encode("ascii")
    return config_override.sha256(value)[:32]


def _write_configuration_revision(revision):
    """Persist the edit revision while the installation lock is held."""
    from mojo.apps.account.models import Setting
    updated = Setting.objects.filter(
        key=SETUP_REVISION_KEY, group=None).update(
            value=revision, is_secret=False, mojo_secrets=None,
            modified=timezone.now())
    if not updated:
        Setting.objects.bulk_create([Setting(
            key=SETUP_REVISION_KEY, group=None, is_secret=False, value=revision)])


def _read_verify_state():
    """Return the persisted per-topic verification state of the STORED config.

    Tolerates the duplicate global rows PostgreSQL still permits: the oldest row
    wins rather than the read failing, because a settings list that cannot
    render is worse than one showing a stale verification.
    """
    from mojo.apps.account.models import Setting
    row = Setting.objects.filter(
        key=VERIFY_STATE_KEY, group=None).order_by("pk").first()
    if row is None or row.is_secret:
        return {}
    value = row.value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "ignore")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    if not isinstance(value, dict):
        return {}
    state = {}
    for topic in TOPICS:
        entry = value.get(topic)
        if not isinstance(entry, dict):
            continue
        state[topic] = {
            "ok": entry.get("ok") is True,
            "code": str(entry.get("code") or "")[:64] or None,
            "message": str(entry.get("message") or "")[:200],
            "at": str(entry.get("at") or "")[:40],
        }
    return state


def _write_verify_state(topic, result):
    """Record how the STORED configuration for one topic last verified.

    Only ever called for a credential that is, or has just become, the stored
    one.  A rejected candidate was never stored, so recording it would make the
    settings list describe a configuration the installation is not running.
    Written through the queryset for the same reason as the edit revision:
    Setting.save/set/remove and the REST surface all refuse this protected key.
    """
    from mojo.apps.account.models import Setting, User
    require_topic(topic)
    entry = {
        "ok": bool(result.get("success")),
        "code": result.get("code") or None,
        # Messages are built from a host and fixed vocabulary; no credential,
        # request body, or exception repr ever reaches this row.
        "message": str(result.get("message") or "")[:200],
        "at": timezone.now().isoformat(),
    }
    with transaction.atomic():
        User.objects.select_for_update().order_by("pk").first()
        state = _read_verify_state()
        state[topic] = entry
        serialized = json.dumps(state)
        updated = Setting.objects.filter(
            key=VERIFY_STATE_KEY, group=None).update(
                value=serialized, is_secret=False, mojo_secrets=None,
                modified=timezone.now())
        if not updated:
            Setting.objects.bulk_create([Setting(
                key=VERIFY_STATE_KEY, group=None, is_secret=False,
                value=serialized)])


def _save_geoip_secret(geoip):
    _write_secret("GEOIP_API_KEY_MOJO", geoip["api_key"], geoip["clear_api_key"])


def save_system_config(provider, name, remote_url=None, api_key=None,
                       clear_api_key=False, test_mode=False,
                       twilio_from_number=None, twilio_account_sid=None,
                       twilio_auth_token=None, clear_twilio_credentials=False,
                       aws_region=None, aws_sender_id=None,
                       aws_access_key_id=None, aws_secret_access_key=None,
                       clear_aws_credentials=False, bump_revision=True):
    """Parameterized writer for the system PhoneConfig row (D5, maestro #2189).

    Extracted verbatim from the Settings-page SMS writer: same row-selection
    precedence (first active system row, else first provider="mojo" row, else
    create), same deactivate-other-active-rows update, same select_for_update.
    ``_save_sms_config`` keeps the Settings page byte-identical by always
    passing provider="mojo" / name="Mojo Remote SMS".

    ``bump_revision`` (default True) writes a fresh edit revision inside the
    same transaction so any concurrently open editor's ``expected_revision``
    fails instead of silently clobbering. ``apply()`` alone passes False — it
    writes its own revision, bound to the published S3 document, immediately
    after. Returns ``(row, revision)`` with revision None when not bumped.
    """
    from mojo.apps.phonehub.models import PhoneConfig
    revision = None
    with transaction.atomic():
        system_rows = list(PhoneConfig.objects.select_for_update().filter(
            group=None).order_by("pk"))
        row = next((item for item in system_rows if item.is_active), None)
        if row is None:
            row = next((item for item in system_rows if item.provider == "mojo"), None)
        if row is None:
            row = PhoneConfig(
                group=None, provider=provider, name=name, is_active=True)
        else:
            PhoneConfig.objects.filter(
                group=None, is_active=True).exclude(pk=row.pk).update(is_active=False)
            row.provider = provider
            row.name = name
        row.test_mode = test_mode
        row.is_active = True
        if provider == "mojo":
            row.mojo_remote_url = remote_url
            if clear_api_key:
                row.set_mojo_api_key(None)
            elif api_key not in (None, ""):
                row.set_mojo_api_key(api_key)
        elif provider == "twilio":
            row.twilio_from_number = twilio_from_number
            if clear_twilio_credentials:
                row.set_secret("twilio_account_sid", None)
                row.set_secret("twilio_auth_token", None)
            elif twilio_account_sid and twilio_auth_token:
                row.set_twilio_credentials(twilio_account_sid, twilio_auth_token)
        elif provider == "aws":
            if aws_region:
                row.aws_region = aws_region
            row.aws_sender_id = aws_sender_id
            if clear_aws_credentials:
                row.set_secret("aws_access_key_id", None)
                row.set_secret("aws_secret_access_key", None)
            elif aws_access_key_id and aws_secret_access_key:
                row.set_aws_credentials(aws_access_key_id, aws_secret_access_key)
        else:
            raise merrors.ValueException("Unknown SMS provider")
        row.save()
        if bump_revision:
            revision = uuid.uuid4().hex
            _write_configuration_revision(revision)
    return row, revision


def _save_sms_config(sms):
    # The Settings page's contract is unchanged by the refactor: always the
    # mojo provider under its fixed name, and no revision bump here because
    # apply() writes its own S3-bound revision in the same transaction.
    save_system_config(
        "mojo", "Mojo Remote SMS", remote_url=sms["remote_url"],
        api_key=sms["api_key"], clear_api_key=sms["clear_api_key"],
        test_mode=sms["test_mode"], bump_revision=False)


def sms_verify_state():
    """The persisted verification state of the stored SMS configuration."""
    return _read_verify_state().get("sms")


def sms_revision_token():
    """The edit token a Messaging-page save must echo back.

    Same (edit_revision, published_revision) binding state() and apply() use.
    Raises when the published document exists but cannot be read — a writer
    caller must fail closed; a reader should catch and degrade.
    """
    allowed = _allowed_keys()
    bucket, key = _location()
    current = _token_document("sms", bucket, key, allowed, None)
    published_revision = current["document"]["revision"] if current else None
    return _configuration_token(_configuration_revision(), published_revision)


def save_messaging_system_config(actor, *, provider, name, expected_revision,
                                 verify_result, **fields):
    """Write the system PhoneConfig row with the full Settings safeguard stack.

    The Messaging-page counterpart of apply()'s SMS topic: installation lock,
    live superuser re-check under that lock, revision-token compare,
    unconditional revision bump, audit event, and the verified result recorded
    as the stored row's state. Credential VERIFICATION happens in the caller
    (phonehub.services.admin_sms.save_config) before this runs — this function
    trusts ``verify_result`` only as the record of that check, never as a gate
    it can skip: callers must refuse before calling when verification failed.
    """
    actor = _superuser(actor)
    allowed = _allowed_keys()
    bucket, key = _location()
    old_revision = revision = None
    row = None
    try:
        with transaction.atomic():
            from mojo.apps.account.models import User
            User.objects.select_for_update().order_by("pk").first()
            actor = _superuser(actor, lock=True)
            current = _token_document("sms", bucket, key, allowed, None)
            published_revision = (current["document"]["revision"]
                                  if current else None)
            old_revision = _configuration_token(
                _configuration_revision(lock=True), published_revision)
            if expected_revision != old_revision:
                raise merrors.ValueException(
                    "Provider configuration changed; reload before publishing")
            row, revision = save_system_config(provider, name, **fields)
            _write_verify_state("sms", verify_result)
    except Exception as error:
        _audit(actor, "failed", revision, old_revision=old_revision,
               error=error, topic="sms")
        raise
    _audit(actor, "saved", revision, old_revision=old_revision, topic="sms")
    return {"saved": True, "topic": "sms", "provider": provider,
            "config_id": row.pk, "revision": revision}


def _loaded_geoip_url():
    return str(_static(
        "GEOIP_MOJO_PROVIDER_URL",
        config_override.DEFAULTS["GEOIP_MOJO_PROVIDER_URL"]) or "").rstrip("/")


def _stored_geoip_key(submitted_url):
    from mojo.apps.account.models import Setting
    if submitted_url.rstrip("/") != _loaded_geoip_url():
        return ""
    row = Setting.objects.filter(
        key="GEOIP_API_KEY_MOJO", group=None, is_secret=True).order_by("pk").first()
    return row.get_value() if row else ""


def _stored_sms_url():
    from mojo.apps.phonehub.models import PhoneConfig
    row = PhoneConfig.objects.filter(group=None, is_active=True).order_by("pk").first()
    if not row or row.provider != "mojo":
        return ""
    return (row.mojo_remote_url or "").rstrip("/")


def _stored_sms_key(submitted_url):
    from mojo.apps.phonehub.models import PhoneConfig
    row = PhoneConfig.objects.filter(group=None, is_active=True).order_by("pk").first()
    if (not row or row.provider != "mojo" or
            (row.mojo_remote_url or "").rstrip("/") != submitted_url.rstrip("/")):
        return ""
    return row.get_mojo_api_key()


def _host(url):
    try:
        return urlsplit(url).hostname or "The remote provider"
    except ValueError:
        return "The remote provider"


def _credential_result(url, api_key, required):
    """Verify one credential and say which host said what.

    The host is the whole diagnosis an operator needs — "api.mojoverify.com
    rejected the API key" is actionable where "the remote" is not.  The key
    itself never enters a message, and neither does an exception repr.
    """
    from mojo.apps.phonehub.services import mojo_provider
    host = _host(url)
    response = mojo_provider.verify_credentials(url, api_key)
    if not response.ok:
        code = response.code or "connection_failed"
        if code in ("http_401", "http_403"):
            message = f"{host} rejected the API key"
        elif code == "http_404":
            message = f"{host} does not expose API-key verification"
        elif code == "timeout":
            message = f"{host} did not answer in time"
        else:
            message = f"{host} could not be reached"
        return {"success": False, "code": code, "message": message}
    permissions = response.permissions or {}
    if required and not any(permissions.get(name) is True for name in required):
        names = " or ".join(required)
        return {"success": False, "code": "insufficient_permission",
                "message": f"{host} accepted the key, but it is missing {names}"}
    return {"success": True, "code": None, "message": "Connection verified"}


def _test_provider_credentials(topic, section):
    """Verify one topic's credential.

    Returns ``(results, tested_stored)``.  ``tested_stored`` is the single
    decision point for persisted verify state: it is true only when both the
    credential and the target came from storage, so a draft nobody saved can
    never be recorded as the state of the stored configuration.
    """
    require_topic(topic)
    if topic == "geoip":
        static_values = section["static_values"]
        url = static_values["GEOIP_MOJO_PROVIDER_URL"]
        if section["clear_api_key"]:
            return {"geoip": {"success": True, "code": "clear_requested",
                              "message": "GeoIP credential will be cleared"}}, False
        typed = section["api_key"]
        sync = static_values["GEOIP_MOJO_SYNC_ENABLED"]
        stored = bool(
            not typed and url.rstrip("/") == _loaded_geoip_url() and
            sync == bool(_static("GEOIP_MOJO_SYNC_ENABLED",
                                 config_override.DEFAULTS["GEOIP_MOJO_SYNC_ENABLED"])))
        result = _credential_result(
            url, typed or _stored_geoip_key(url),
            ("geoip_sync",) if sync else ())
        return {"geoip": result}, stored
    url = section["remote_url"]
    if section["clear_api_key"]:
        return {"sms": {"success": True, "code": "clear_requested",
                        "message": "SMS credential will be cleared"}}, False
    typed = section["api_key"]
    stored_url = _stored_sms_url()
    stored = bool(not typed and stored_url and url.rstrip("/") == stored_url)
    result = _credential_result(
        url, typed or _stored_sms_key(url), ("send_sms", "comms"))
    return {"sms": result}, stored


def test(actor, topic, payload):
    _superuser(actor)
    require_topic(topic)
    section, _ = _normalize_payload(payload, topic)
    results, tested_stored = _test_provider_credentials(topic, section)
    if tested_stored:
        # Testing what is already stored IS an observation of the stored
        # configuration, so it is worth persisting.  A draft is not.
        _write_verify_state(topic, results[topic])
    return {"tested": True, "topic": topic, "results": results,
            "success": all(item["success"] for item in results.values())}


def _audit(actor, outcome, revision, old_revision=None, version_id=None,
           error=None, topic=""):
    from mojo.apps.incident import report_event_suppressed
    keys = ",".join(FLEET_KEYS) if topic == "geoip" else "phonehub.PhoneConfig"
    message = (f"Admin provider configuration {outcome} by user={actor.pk} "
               f"topic={topic or 'none'} keys={keys} "
               f"old_revision={old_revision or 'none'} "
               f"new_revision={revision or 'none'} version={version_id or 'none'}")
    if error:
        message += f" error={error.__class__.__name__}"
    report_event_suppressed(
        message, title=f"Provider configuration {outcome}",
        category="admin_settings", level=6 if outcome == "published" else 5,
        key=(f"fleet-provider-{outcome}:{topic or 'none'}:{actor.pk}:"
             f"{revision or old_revision or 'none'}"))


def _pending_restart(published_revision):
    """A restart is pending only when something is actually published."""
    return bool(published_revision) and published_revision != _static(
        config_override.REVISION_KEY, "")


def _token_document(topic, bucket, key, allowed, s3):
    """Read the published document the edit token is bound to.

    ``expected_revision`` binds (edit_revision, published_revision), so an SMS
    save has to perform the same read state() performed even though it will
    never write the document.  Where state() may degrade to remote_error and a
    None revision, a writer must not: an unreadable document fails the save
    closed rather than letting a stale token compare equal to a missing one.
    """
    if not (bucket and key and allowed):
        return None
    if topic == "geoip":
        return _published(s3, bucket, key, allowed)
    try:
        return _published(_s3_client(), bucket, key, allowed)
    except Exception:
        raise merrors.ValueException(
            "Provider configuration changed; reload before publishing") from None


def apply(actor, topic, payload):
    actor = _superuser(actor)
    require_topic(topic)
    allowed = _allowed_keys()
    bucket, key = _location()
    kms_key = _kms_key()
    # GeoIP owns the fleet document and cannot be saved without the publishing
    # plane.  SMS owns one database row and must stay usable on an installation
    # that never publishes fleet configuration at all.
    if topic == "geoip" and (
            not bucket or not key or frozenset(FLEET_KEYS) - allowed or
            not kms_key or not _restart_enabled()):
        raise merrors.ValueException(
            "Fleet publishing requires its exact object, all provider delegations, "
            "a KMS key, and config-sync restart")
    section, expected_revision = _normalize_payload(payload, topic)
    results, _ = _test_provider_credentials(topic, section)
    failed = [name for name, result in results.items() if not result["success"]]
    if failed:
        raise merrors.ValueException(
            "Provider verification failed for " + ", ".join(failed))
    s3 = _s3_client() if topic == "geoip" else None
    current = None
    old_revision = revision = published_revision = version_id = None
    unchanged = False
    try:
        # A locked, stable installation row serializes this rare control-plane
        # operation across application nodes. Keeping the DB writes inside the
        # same transaction rolls them back when the conditional S3 write fails.
        with transaction.atomic():
            from mojo.apps.account.models import User
            User.objects.select_for_update().order_by("pk").first()
            actor = _superuser(actor, lock=True)
            current = _token_document(topic, bucket, key, allowed, s3)
            published_revision = current["document"]["revision"] if current else None
            old_revision = _configuration_token(
                _configuration_revision(lock=True), published_revision)
            if expected_revision != old_revision:
                raise merrors.ValueException(
                    "Provider configuration changed; reload before publishing")
            static_values = (section["static_values"] if topic == "geoip" else None)
            static_changed = bool(topic == "geoip" and (
                not current or current["document"]["settings"] != static_values))
            if static_changed and current and not current.get("etag"):
                raise merrors.ValueException(
                    "Published fleet configuration has no concurrency token")
            if topic == "geoip":
                _save_geoip_secret(section)
            else:
                _save_sms_config(section)
            revision = uuid.uuid4().hex
            _write_configuration_revision(revision)
            if not static_changed:
                unchanged = True
                version_id = current.get("version_id") if current else None
                revision = _configuration_token(revision, published_revision)
            else:
                body = config_override.encode_document(
                    static_values, revision, timezone.now().isoformat(), allowed)
                put_args = dict(
                    Bucket=bucket, Key=key, Body=body,
                    ContentType="application/json",
                    ServerSideEncryption="aws:kms", SSEKMSKeyId=kms_key,
                    Metadata={"sha256": config_override.sha256(body),
                              "revision": revision},
                )
                if current:
                    put_args["IfMatch"] = current["etag"]
                else:
                    put_args["IfNoneMatch"] = "*"
                response = s3.put_object(**put_args)
                version_id = response.get("VersionId")
                published_revision = revision
            # The candidate this call verified is now the stored one, so its
            # result describes the installation rather than a draft.
            _write_verify_state(topic, results[topic])
    except Exception as error:
        _audit(actor, "failed", revision, old_revision=old_revision, error=error,
               topic=topic)
        raise
    if unchanged:
        _audit(actor, "unchanged", revision, old_revision=old_revision,
               version_id=version_id, topic=topic)
        return {"published": False, "unchanged": True, "topic": topic,
                "revision": revision,
                "published_revision": published_revision,
                "version_id": version_id,
                "pending_restart": _pending_restart(published_revision),
                "results": results}
    _audit(actor, "published", revision, old_revision=old_revision,
           version_id=version_id, topic=topic)
    return {"published": True, "unchanged": False, "topic": topic,
            "revision": revision,
            "published_revision": published_revision,
            "version_id": version_id, "pending_restart": True,
            "results": results}
