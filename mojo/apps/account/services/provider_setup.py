"""Superuser setup for fleet GeoIP and the system Mojo SMS provider."""

import uuid

from django.db import transaction
from django.utils import timezone

from mojo import errors as merrors
from mojo.deploy import config_override
from mojo.helpers.settings import settings


FLEET_KEYS = tuple(config_override.DEFAULTS)
FILENAME = "django.override.json"
SETUP_REVISION_KEY = "ADMIN_PROVIDER_SETUP_REVISION"


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
    secret_configured = Setting.objects.filter(
        key="GEOIP_API_KEY_MOJO", group=None, is_secret=True).exists()
    sms = {"configured": False, "remote_url": "", "api_key_configured": False}
    if apps.is_installed("mojo.apps.phonehub"):
        from mojo.apps.phonehub.models import PhoneConfig
        row = PhoneConfig.objects.filter(group=None, provider="mojo", is_active=True).first()
        if row:
            sms = {
                "configured": True,
                "remote_url": row.mojo_remote_url or "",
                "api_key_configured": bool(row.get_mojo_api_key()),
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
        },
        "sms": sms,
    }


def _normalize_payload(payload):
    if not isinstance(payload, dict) or set(payload) - {
            "geoip", "sms", "expected_revision"}:
        raise merrors.ValueException(
            "Provider setup accepts only geoip, sms, and expected_revision")
    geoip = payload.get("geoip") or {}
    sms = payload.get("sms") or {}
    if not isinstance(geoip, dict) or not isinstance(sms, dict):
        raise merrors.ValueException("Provider setup sections must be objects")
    static_values = {key: geoip.get(key, default)
                     for key, default in config_override.DEFAULTS.items()}
    try:
        static_values = config_override.validate_settings(static_values, _allowed_keys())
    except ValueError as error:
        raise merrors.ValueException(str(error)) from None
    api_key = geoip.get("GEOIP_API_KEY_MOJO")
    if api_key is not None and (not isinstance(api_key, str) or len(api_key) > 4096):
        raise merrors.ValueException("GeoIP API key is invalid")
    clear_geoip_key = geoip.get("clear_api_key") is True
    allowed_sms = {"remote_url", "api_key", "clear_api_key", "test_mode"}
    if set(sms) - allowed_sms:
        raise merrors.ValueException("SMS setup contains unsupported fields")
    remote_url = sms.get("remote_url", "")
    if not config_override.https_origin(remote_url):
        raise merrors.ValueException("SMS remote URL must be one HTTPS origin")
    sms_key = sms.get("api_key")
    if sms_key is not None and (not isinstance(sms_key, str) or len(sms_key) > 4096):
        raise merrors.ValueException("SMS API key is invalid")
    expected_revision = payload.get("expected_revision")
    if expected_revision is not None and (
            not isinstance(expected_revision, str) or
            not config_override.REVISION_RE.fullmatch(expected_revision)):
        raise merrors.ValueException("Provider setup revision is invalid")
    return static_values, api_key, clear_geoip_key, {
        "remote_url": remote_url.rstrip("/"),
        "api_key": sms_key,
        "clear_api_key": sms.get("clear_api_key") is True,
        "test_mode": sms.get("test_mode") is True,
    }, expected_revision


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


def _save_database(api_key, clear_geoip_key, sms):
    from mojo.apps.phonehub.models import PhoneConfig
    _write_secret("GEOIP_API_KEY_MOJO", api_key, clear_geoip_key)
    with transaction.atomic():
        system_rows = list(PhoneConfig.objects.select_for_update().filter(
            group=None).order_by("pk"))
        row = next((item for item in system_rows if item.is_active), None)
        if row is None:
            row = next((item for item in system_rows if item.provider == "mojo"), None)
        if row is None:
            row = PhoneConfig(
                group=None, provider="mojo", name="Mojo Remote SMS", is_active=True)
        else:
            PhoneConfig.objects.filter(
                group=None, is_active=True).exclude(pk=row.pk).update(is_active=False)
            row.provider = "mojo"
            row.name = "Mojo Remote SMS"
        row.mojo_remote_url = sms["remote_url"]
        row.test_mode = sms["test_mode"]
        row.is_active = True
        if sms["clear_api_key"]:
            row.set_mojo_api_key(None)
        elif sms["api_key"] not in (None, ""):
            row.set_mojo_api_key(sms["api_key"])
        row.save()


def _stored_geoip_key(submitted_url):
    from mojo.apps.account.models import Setting
    loaded_url = str(_static(
        "GEOIP_MOJO_PROVIDER_URL",
        config_override.DEFAULTS["GEOIP_MOJO_PROVIDER_URL"]) or "").rstrip("/")
    if submitted_url.rstrip("/") != loaded_url:
        return ""
    row = Setting.objects.filter(
        key="GEOIP_API_KEY_MOJO", group=None, is_secret=True).order_by("pk").first()
    return row.get_value() if row else ""


def _stored_sms_key(submitted_url):
    from mojo.apps.phonehub.models import PhoneConfig
    row = PhoneConfig.objects.filter(group=None, is_active=True).order_by("pk").first()
    if (not row or row.provider != "mojo" or
            (row.mojo_remote_url or "").rstrip("/") != submitted_url.rstrip("/")):
        return ""
    return row.get_mojo_api_key()


def _credential_result(url, api_key, required):
    from mojo.apps.phonehub.services import mojo_provider
    response = mojo_provider.verify_credentials(url, api_key)
    if not response.ok:
        code = response.code or "connection_failed"
        if code in ("http_401", "http_403"):
            message = "The remote rejected the API key"
        elif code == "http_404":
            message = "The remote does not expose API-key verification"
        elif code == "timeout":
            message = "The remote provider timed out"
        else:
            message = "The remote provider could not be verified"
        return {"success": False, "code": code, "message": message}
    permissions = response.permissions or {}
    if required and not any(permissions.get(name) is True for name in required):
        names = " or ".join(required)
        return {"success": False, "code": "insufficient_permission",
                "message": f"The API key requires {names}"}
    return {"success": True, "code": None, "message": "Connection verified"}


def _test_provider_credentials(api_key, clear_geoip_key, sms, static_values):
    geo_url = static_values["GEOIP_MOJO_PROVIDER_URL"]
    geo_key = "" if clear_geoip_key else (api_key or _stored_geoip_key(geo_url))
    sms_key = "" if sms["clear_api_key"] else (
        sms["api_key"] or _stored_sms_key(sms["remote_url"]))
    return {
        "geoip": ({"success": True, "code": "clear_requested",
                   "message": "GeoIP credential will be cleared"}
                  if clear_geoip_key else _credential_result(
                      geo_url, geo_key,
                      (("geoip_sync",) if static_values["GEOIP_MOJO_SYNC_ENABLED"]
                       else ()))),
        "sms": ({"success": True, "code": "clear_requested",
                 "message": "SMS credential will be cleared"}
                if sms["clear_api_key"] else _credential_result(
                    sms["remote_url"], sms_key, ("send_sms", "comms"))),
    }


def test(actor, payload):
    _superuser(actor)
    static_values, api_key, clear_geoip_key, sms, _ = _normalize_payload(payload)
    results = _test_provider_credentials(
        api_key, clear_geoip_key, sms, static_values)
    return {"tested": True, "results": results,
            "success": all(item["success"] for item in results.values())}


def _audit(actor, outcome, revision, old_revision=None, version_id=None, error=None):
    from mojo.apps.incident import report_event_suppressed
    keys = ",".join(FLEET_KEYS)
    message = (f"Admin fleet provider configuration {outcome} by user={actor.pk} "
               f"keys={keys} old_revision={old_revision or 'none'} "
               f"new_revision={revision or 'none'} version={version_id or 'none'}")
    if error:
        message += f" error={error.__class__.__name__}"
    report_event_suppressed(
        message, title=f"Fleet provider configuration {outcome}",
        category="admin_settings", level=6 if outcome == "published" else 5,
        key=f"fleet-provider-{outcome}:{actor.pk}:{revision or old_revision or 'none'}")


def apply(actor, payload):
    actor = _superuser(actor)
    allowed = _allowed_keys()
    bucket, key = _location()
    kms_key = _kms_key()
    if (not bucket or not key or frozenset(FLEET_KEYS) - allowed or not kms_key or
            not _restart_enabled()):
        raise merrors.ValueException(
            "Fleet publishing requires its exact object, all provider delegations, "
            "a KMS key, and config-sync restart")
    static_values, api_key, clear_geoip_key, sms, expected_revision = \
        _normalize_payload(payload)
    results = _test_provider_credentials(
        api_key, clear_geoip_key, sms, static_values)
    failed = [name for name, result in results.items() if not result["success"]]
    if failed:
        raise merrors.ValueException(
            "Provider verification failed for " + ", ".join(failed))
    s3 = _s3_client()
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
            current = _published(s3, bucket, key, allowed)
            published_revision = current["document"]["revision"] if current else None
            old_revision = _configuration_token(
                _configuration_revision(lock=True), published_revision)
            if expected_revision != old_revision:
                raise merrors.ValueException(
                    "Provider configuration changed; reload before publishing")
            static_changed = (
                not current or current["document"]["settings"] != static_values)
            if static_changed and current and not current.get("etag"):
                raise merrors.ValueException(
                    "Published fleet configuration has no concurrency token")
            _save_database(api_key, clear_geoip_key, sms)
            revision = uuid.uuid4().hex
            _write_configuration_revision(revision)
            if not static_changed:
                unchanged = True
                version_id = current.get("version_id")
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
    except Exception as error:
        _audit(actor, "failed", revision, old_revision=old_revision, error=error)
        raise
    if unchanged:
        _audit(actor, "unchanged", revision, old_revision=old_revision,
               version_id=version_id)
        return {"published": False, "unchanged": True, "revision": revision,
                "published_revision": published_revision,
                "version_id": version_id,
                "pending_restart": published_revision != _static(
                    config_override.REVISION_KEY, ""),
                "results": results}
    _audit(actor, "published", revision, old_revision=old_revision,
           version_id=version_id)
    return {"published": True, "unchanged": False, "revision": revision,
            "published_revision": published_revision,
            "version_id": version_id, "pending_restart": True,
            "results": results}
