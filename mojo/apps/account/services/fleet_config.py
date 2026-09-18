"""Structured, versioned fleet settings on the existing exact S3 object.

Authentication freshness is enforced by the REST boundary. Every service also
requires a live superuser, checked again under the installation write lock.
"""

import uuid

from django.db import transaction

from mojo import errors as merrors
from mojo.deploy import config_override
from . import provider_setup as provider


HISTORY_LIMIT = 50


def _context():
    allowed = provider._allowed_keys()
    bucket, key = provider._location()
    if not bucket or not key:
        raise merrors.ValueException("Fleet configuration location is not configured")
    return provider._s3_client(), bucket, key, allowed


def _read(s3, bucket, key, allowed, version_id=None):
    try:
        return provider._published(s3, bucket, key, allowed, version_id=version_id)
    except Exception:
        # AWS/custom validators may include data in exception text. Never return it.
        raise merrors.ValueException("Fleet configuration could not be read and verified") from None


def _audit(actor, action, revision, keys):
    from mojo.apps.incident import report_event_suppressed
    report_event_suppressed(
        f"Fleet configuration {action} user={actor.pk} revision={revision or 'none'} "
        f"keys={','.join(sorted(keys))}",
        title=f"Fleet configuration {action}", category="admin_settings", level=6,
        key=f"fleet-config:{action}:{actor.pk}:{revision or 'none'}")


def state(actor):
    provider._superuser(actor)
    s3, bucket, key, allowed = _context()
    current = _read(s3, bucket, key, allowed)
    values = current["document"]["settings"] if current else {}
    entries = []
    for definition in config_override.definitions():
        name = definition["key"]
        if name not in allowed:
            continue
        entry = dict(definition)
        entry["overridden"] = name in values
        if entry["sensitive"]:
            entry.pop("default", None)
            entry["configured"] = bool(values.get(name, provider._static(name)))
        else:
            entry["current"] = values.get(name, provider._static(name, entry.get("default")))
        entries.append(entry)
    revision = current["document"]["revision"] if current else None
    return {"schema_version": 1, "revision": revision,
            "version_id": current.get("version_id") if current else None,
            "entries": entries, "published": current is not None,
            "loaded_revision": provider._static(config_override.REVISION_KEY, None),
            "pending_restart": provider._pending_restart(revision),
            "publish_configured": bool(allowed and provider._kms_key())}


def _payload(payload, restore=False):
    fields = {"expected_revision", "version_id" if restore else "changes"}
    if not isinstance(payload, dict) or set(payload) != fields:
        raise merrors.ValueException("Fleet configuration request has unsupported or missing fields")
    revision = payload["expected_revision"]
    if revision is not None and (not isinstance(revision, str) or
                                not config_override.REVISION_RE.fullmatch(revision)):
        raise merrors.ValueException("Fleet configuration revision is invalid")
    if restore:
        version = payload["version_id"]
        if not isinstance(version, str) or not version or len(version) > 1024 or version == "null":
            raise merrors.ValueException("Fleet configuration version is invalid")
    elif not isinstance(payload["changes"], dict) or not payload["changes"] or len(payload["changes"]) > config_override.MAX_SETTINGS:
        raise merrors.ValueException("Fleet configuration changes must be a bounded nonempty object")
    return revision


def _changes(values, changes, allowed):
    result = dict(values)
    for key, change in changes.items():
        definition = config_override.get_definition(key)
        if key not in allowed or definition is None:
            raise merrors.ValueException("Fleet configuration setting is not delegated")
        if not isinstance(change, dict):
            raise merrors.ValueException("Fleet configuration change must be an object")
        action = change.get("action")
        if action == "clear" and set(change) == {"action"}:
            result.pop(key, None)
        elif action == "set" and set(change) == {"action", "value"}:
            if definition["sensitive"] and change["value"] == "":
                continue
            result[key] = change["value"]
        else:
            raise merrors.ValueException("Fleet configuration change has an invalid action or fields")
    try:
        return config_override.validate_settings(result, allowed)
    except Exception:
        raise merrors.ValueException("Fleet configuration contains an invalid setting value") from None


def _save(actor, payload, restoring=False):
    actor = provider._superuser(actor)
    expected = _payload(payload, restore=restoring)
    s3, bucket, key, allowed = _context()
    revision = None
    keys = []
    try:
        with transaction.atomic():
            from mojo.apps.account.models import User
            User.objects.select_for_update().order_by("pk").first()
            actor = provider._superuser(actor, lock=True)
            current = _read(s3, bucket, key, allowed)
            old_revision = current["document"]["revision"] if current else None
            if expected != old_revision:
                raise merrors.ValueException("Fleet configuration changed; reload before publishing")
            old_values = current["document"]["settings"] if current else {}
            if restoring:
                historical = _read(s3, bucket, key, allowed, payload["version_id"])
                if historical is None:
                    raise merrors.ValueException("Fleet configuration version does not exist")
                values = historical["document"]["settings"]
                keys = sorted(set(old_values) | set(values))
            else:
                values = _changes(old_values, payload["changes"], allowed)
                keys = sorted(payload["changes"])
            revision = uuid.uuid4().hex
            response = provider._write_document(
                s3, bucket, key, provider._kms_key(), allowed, current, values, revision)
    except (merrors.ValueException, merrors.PermissionDeniedException):
        _audit(actor, "rejected", None, keys)
        raise
    except Exception:
        _audit(actor, "failed", None, keys)
        raise merrors.ValueException("Fleet publication failed; reload to check the published revision") from None
    _audit(actor, "restored" if restoring else "published", revision, keys)
    return {"published": True, "revision": revision, "version_id": response.get("VersionId"),
            "pending_restart": True, "applied": False}


def publish(actor, payload):
    return _save(actor, payload)


def restore(actor, payload):
    return _save(actor, payload, restoring=True)


def history(actor):
    provider._superuser(actor)
    s3, bucket, key, _ = _context()
    try:
        response = s3.list_object_versions(Bucket=bucket, Prefix=key, MaxKeys=HISTORY_LIMIT)
    except Exception:
        raise merrors.ValueException("Fleet configuration history could not be read") from None
    # Prefix is the only S3 listing filter, so exclude all sibling object keys.
    versions = []
    for item in response.get("Versions", [])[:HISTORY_LIMIT]:
        if item.get("Key") != key:
            continue
        modified = item.get("LastModified")
        versions.append({"version_id": item.get("VersionId"),
                         "current": item.get("IsLatest") is True,
                         "published_at": modified.isoformat() if modified else None})
    return {"versions": versions, "truncated": response.get("IsTruncated") is True}
