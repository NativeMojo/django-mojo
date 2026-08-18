"""Superuser setup for fleet GeoIP and the system Mojo SMS provider."""

import uuid

from django.db import transaction
from django.utils import timezone

from mojo import errors as merrors
from mojo.deploy import config_override
from mojo.helpers.settings import settings


FLEET_KEYS = tuple(config_override.DEFAULTS)
FILENAME = "django.override.json"


def _static(name, default=None):
    return settings.get_static(name, default)


def _allowed_keys():
    configured = _static("ADMIN_FLEET_CONFIG_ALLOWED_KEYS", list(FLEET_KEYS))
    return config_override.normalize_allowed(configured) & frozenset(FLEET_KEYS)


def _location():
    bucket = _static("ADMIN_FLEET_CONFIG_BUCKET", _static("AWS_CONFIG_BUCKET", ""))
    prefix = _static("ADMIN_FLEET_CONFIG_PREFIX", _static("AWS_CONFIG_PREFIX", ""))
    filename = _static("ADMIN_FLEET_CONFIG_FILENAME", FILENAME)
    key = "/".join(part.strip("/") for part in (prefix, filename) if part)
    return str(bucket or ""), key


def _kms_key():
    return _static("ADMIN_FLEET_CONFIG_KMS_KEY_ID", _static("KMS_KEY_ID", ""))


def _s3_client():
    from mojo.helpers.aws.client import get_client
    return get_client("s3", region=_static("AWS_REGION", None))


def _superuser(actor):
    from mojo.apps.account.services.admin_settings import require_catalog_writer
    actor = require_catalog_writer(actor)
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
    payload = response["Body"].read(config_override.MAX_DOCUMENT_BYTES + 1)
    advertised = response.get("Metadata", {}).get("sha256")
    if not advertised or advertised != config_override.sha256(payload):
        raise merrors.ValueException("Published fleet configuration failed integrity verification")
    return config_override.decode_document(payload, allowed)


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
        desired_geoip.update(published["settings"])
    return {
        "available": bool(bucket and key and allowed and _kms_key()),
        "bucket_configured": bool(bucket),
        "object_key": key,
        "delegated_keys": sorted(allowed),
        "loaded_revision": loaded_revision or None,
        "published_revision": published.get("revision") if published else None,
        "pending_restart": bool(
            published and published.get("revision") != loaded_revision),
        "remote_error": remote_error,
        "geoip": {
            **desired_geoip,
            "GEOIP_API_KEY_MOJO_CONFIGURED": secret_configured,
        },
        "sms": sms,
    }


def _normalize_payload(payload):
    if not isinstance(payload, dict) or set(payload) - {"geoip", "sms"}:
        raise merrors.ValueException("Provider setup accepts only geoip and sms")
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
    return static_values, api_key, clear_geoip_key, {
        "remote_url": remote_url.rstrip("/"),
        "api_key": sms_key,
        "clear_api_key": sms.get("clear_api_key") is True,
        "test_mode": sms.get("test_mode") is True,
    }


def _write_secret(key, value, clear):
    from mojo.apps.account.models import Setting
    rows = list(Setting.objects.filter(key=key, group=None).order_by("pk"))
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
    row.save()
    if len(rows) > 1:
        Setting.objects.filter(pk__in=[item.pk for item in rows[1:]]).delete()


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


def apply(actor, payload):
    actor = _superuser(actor)
    allowed = _allowed_keys()
    bucket, key = _location()
    kms_key = _kms_key()
    if not bucket or not key or not allowed or not kms_key:
        raise merrors.ValueException(
            "Fleet publishing requires bucket, prefix, delegated keys, and a KMS key")
    static_values, api_key, clear_geoip_key, sms = _normalize_payload(payload)
    revision = uuid.uuid4().hex
    body = config_override.encode_document(
        static_values, revision, timezone.now().isoformat(), allowed)
    s3 = _s3_client()
    s3.put_object(
        Bucket=bucket, Key=key, Body=body,
        ContentType="application/json",
        ServerSideEncryption="aws:kms", SSEKMSKeyId=kms_key,
        Metadata={"sha256": config_override.sha256(body), "revision": revision},
    )
    _save_database(api_key, clear_geoip_key, sms)
    from mojo.apps.incident import report_event_suppressed
    report_event_suppressed(
        f"Admin fleet provider configuration published by user={actor.pk} revision={revision}",
        title="Fleet provider configuration published",
        category="admin_settings", level=6,
        key=f"fleet-provider-publish:{actor.pk}:{revision}")
    return {"published": True, "revision": revision, "pending_restart": True}
