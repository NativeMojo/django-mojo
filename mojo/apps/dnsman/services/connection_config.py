"""Protected Fleet topic: candidate credentials never become a saved draft."""
from contextlib import contextmanager

from django.db import connection, transaction
from django.utils import timezone

from mojo import errors as me
from mojo.deploy import config_override
from mojo.helpers.settings import settings
from mojo.apps.dnsman import fleet_settings as schema
from mojo.apps.dnsman.services import acme_hub_client

SETUP_DESTINATION = {"route": "setup", "section": "aws", "capacity_route": "capacity",
                     "actor": "deployment_operator", "adoption_item": 8903}
ERRORS = {
    "profile_catalog_unavailable": 503, "profile_catalog_changed": 409,
    "profile_not_approved": 400, "hub_identity_mismatch": 400,
    "hub_identity_migration_required": 409, "connection_disable_ineligible": 409,
    "fleet_schema_unavailable": 503, "fleet_delegation_unavailable": 503,
    "publisher_unavailable": 503, "node_enrollment_unavailable": 503,
    "fleet_revision_changed": 409, "hub_unconfigured": 503,
    "hub_unavailable": 503, "hub_unauthorized": 403, "hub_unsupported": 503,
    "invalid_request": 400, "publication_uncertain": 503, "configuration_busy": 409,
}


class ConfigurationError(me.ValueException):
    def __init__(self, code):
        self.error_code = code
        status = ERRORS.get(code, 503)
        super().__init__(code, code=status, status=status)


def _admit_identity():
    # A nested caller may already own Domain/issuer fences. Never wait for an
    # identity owner that can itself be waiting on those fences. PostgreSQL
    # retains successful admission through the outer transaction, reentrantly.
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_xact_lock(%s, %s)", [0x444e534d, 8986])
        if not cursor.fetchone()[0]:
            raise ConfigurationError("configuration_busy")


@contextmanager
def identity_lock():
    """Admit profile publication/connection creation without waiting on fences."""
    with transaction.atomic():
        _admit_identity()
        yield


def _has_connections():
    from mojo.apps.dnsman.models import AcmeDelegation
    # Retired rows and tombstones remain identity-bearing.
    return AcmeDelegation.objects.filter(connection_mode="shared").exists()


def _static_values():
    return {key: settings.get_static(key, "") for key in schema.KEYS}


def _identity(profile):
    return {"consumer_project_uuid": profile["consumer_project_uuid"],
            "consumer_label": profile["consumer_label"]} if profile else None


def _probe(values, *, probe=None, proof=False):
    chosen = schema.profile(values.get(schema.KEYS[0]))
    if not chosen:
        raise ConfigurationError("profile_not_approved")
    if (values.get(schema.KEYS[1]) != chosen["origin"]
            or not schema.valid_key(values.get(schema.KEYS[2]))):
        raise ConfigurationError("hub_identity_mismatch")
    try:
        caps = (probe or acme_hub_client.capabilities_for)(
            chosen["origin"], values[schema.KEYS[2]], proof=proof)
        # capabilities_for validates the complete authenticated v2 response.
        # Feature activation may be false; the credential is still configurable.
        if caps.get("version") != 2:
            raise ConfigurationError("hub_unsupported")
        observed = schema.project_uuid(caps.get("consumer_project_uuid"))
        if observed != chosen["consumer_project_uuid"]:
            raise ConfigurationError("hub_identity_mismatch")
        return {"state": "verified", "observed_project_uuid": observed,
                "checked_at": timezone.now().isoformat(), "error_code": None,
                "sharing_ready": caps.get("shared_validation") is True and caps.get("can_consume") is True}
    except ConfigurationError:
        raise
    except acme_hub_client.AcmeHubClientError as error:
        code = "hub_unauthorized" if error.http_status in (401, 403) else (
            "hub_unsupported" if error.kind == "shared_validation_unsupported" else "hub_unavailable")
        raise ConfigurationError(code) from None
    except (ValueError, TypeError):
        raise ConfigurationError("hub_identity_mismatch") from None
    except Exception:
        raise ConfigurationError("hub_unavailable") from None


def loaded_identity(*, values=None, probe=None, proof=False):
    """Safe, fresh evidence about this process's actually loaded static tuple."""
    values = _static_values() if values is None else values
    profile_id = values.get(schema.KEYS[0])
    if not isinstance(profile_id, str) or not schema.PROFILE_RE.fullmatch(profile_id):
        profile_id = None
    result = dict(state="unavailable", profile_id=profile_id,
                  observed_project_uuid=None, checked_at=timezone.now().isoformat(),
                  error_code=None, sharing_ready=False)
    if not any(values.values()):
        return dict(result, state="disabled")
    try:
        result.update(_probe(values, probe=probe, proof=proof))
    except ConfigurationError as error:
        result["error_code"] = error.error_code
    return result


def validate_candidate(old, candidate, *, probe=None, has_connections=None):
    if set(candidate) != set(schema.KEYS):
        raise ConfigurationError("hub_identity_migration_required")
    connected = (has_connections or _has_connections)()
    if all(candidate[key] == "" for key in schema.KEYS):
        if connected:
            raise ConfigurationError("connection_disable_ineligible")
        return {"state": "disabled", "observed_project_uuid": None, "sharing_ready": False}
    new_profile = schema.profile(candidate[schema.KEYS[0]])
    if not new_profile:
        raise ConfigurationError("profile_not_approved")
    # Managed old identity comes from the installed catalog; legacy origin must
    # match the selected origin and the candidate must authenticate the UUID.
    old_effective = old or _static_values()
    old_profile = schema.profile(old_effective.get(schema.KEYS[0]))
    old_origin = old_effective.get(schema.KEYS[1])
    try:
        old_origin = schema.normalize_origin(old_origin) if old_origin else None
    except ValueError:
        old_origin = None
    if connected and (old_origin != new_profile["origin"] or
                      old_profile and old_profile["consumer_project_uuid"] != new_profile["consumer_project_uuid"]):
        raise ConfigurationError("hub_identity_migration_required")
    observation = _probe(candidate, probe=probe)
    # If an unprofiled legacy connection exists, authenticate its loaded key as
    # well. A new key's UUID cannot prove a prior unprofiled identity.
    if connected and not old_profile:
        previous = dict(old_effective, **{schema.KEYS[0]: new_profile["id"]})
        _probe(previous, probe=probe)
    return observation


def publication_policy(old, candidate):
    # Called only when presence/values of this owned bundle actually change.
    # Lock survives through the common conditional S3 write's transaction.
    _admit_identity()
    validate_candidate(old, candidate)


def require_current_identity(consumer, *, current_loader=None, static_values=None):
    """Run inside identity_lock immediately before persisting shared intent."""
    if current_loader is None:
        from mojo.apps.account.services import fleet_config, provider_setup
        loaded = _static_values() if static_values is None else static_values
        bucket, key = provider_setup._location()
        if not bucket or not key:
            if not loaded.get(schema.KEYS[0]):
                return
            raise ConfigurationError("publisher_unavailable")
        s3, bucket, key, allowed = fleet_config._context()
        current = fleet_config._read(s3, bucket, key, allowed)
        values = current["document"]["settings"] if current else {}
    else:
        values = current_loader()
    values = {key: values[key] for key in schema.KEYS if key in values}
    loaded = _static_values() if static_values is None else static_values
    # Legacy installations without a managed publication keep their existing
    # authenticated flow; never make a configured Fleet read optional.
    if not values and not loaded.get(schema.KEYS[0]):
        return
    if set(values) != set(schema.KEYS) or values != loaded:
        raise ConfigurationError("hub_identity_migration_required")
    chosen = schema.profile(values[schema.KEYS[0]])
    try:
        matches = chosen and schema.project_uuid(consumer) == chosen["consumer_project_uuid"]
    except ValueError:
        matches = False
    if not matches or values[schema.KEYS[1]] != chosen["origin"]:
        raise ConfigurationError("hub_identity_migration_required")


def enrollment_reasons():
    from mojo.apps.account.services import provider_setup as provider, fleet_apply
    reasons = []
    try:
        allowed = provider._allowed_keys()
        config_override.load_schema_modules(settings.get_static("CONFIG_SYNC_SCHEMA_MODULES", []))
    except Exception:
        return ["fleet_schema_unavailable"]
    if not schema.profiles():
        reasons.append("profile_catalog_unavailable")
    if any(config_override.get_definition(key) is None for key in schema.KEYS):
        reasons.append("fleet_schema_unavailable")
    sync_allowed = config_override.normalize_allowed(settings.get_static("CONFIG_SYNC_OVERRIDE_ALLOWED_KEYS", []))
    if set(schema.KEYS) - allowed or set(schema.KEYS) - sync_allowed:
        reasons.append("fleet_delegation_unavailable")
    # The same deployment-owned profile module set must exist in both planes.
    admin_modules = settings.get_static("ADMIN_FLEET_CONFIG_SCHEMA_MODULES", [])
    sync_modules = settings.get_static("CONFIG_SYNC_SCHEMA_MODULES", [])
    def modules(value):
        return set(value.split(",") if isinstance(value, str) else value or [])
    if not modules(admin_modules) or modules(admin_modules) != modules(sync_modules):
        reasons.append("fleet_schema_unavailable")
    bucket, key = provider._location()
    if not bucket or not key or not provider._kms_key() or not provider._restart_enabled():
        reasons.append("publisher_unavailable")
    try:
        fleet_apply.expected_nodes()
    except Exception:
        reasons.append("node_enrollment_unavailable")
    return list(dict.fromkeys(reasons))


def unavailable(code):
    return {"schema_version": 1, "available": False, "unavailable_reasons": [code],
            "catalog_revision": None, "profiles": [], "selected_profile_id": None,
            "expected_identity": None, "loaded_identity": None,
            "credential": {"configured": False, "source": "unknown", "overridden": False},
            "revision": None, "loaded_revision": None, "pending_restart": False,
            "fleet": {"status": "unavailable", "nodes": [], "healthy_everywhere": False},
            "setup_destination": dict(SETUP_DESTINATION)}


def state(actor, *, probe=None):
    from mojo.apps.account.services import fleet_config, fleet_apply, provider_setup as provider
    provider._superuser(actor)
    result = unavailable("publisher_unavailable")
    reasons = enrollment_reasons()
    result.update(catalog_revision=schema.catalog_revision(), profiles=schema.profiles())
    values = {}
    try:
        s3, bucket, key, allowed = fleet_config._context()
        current = fleet_config._read(s3, bucket, key, allowed)
        values = current["document"]["settings"] if current else {}
        result["revision"] = current["document"]["revision"] if current else None
        if s3.get_bucket_versioning(Bucket=bucket).get("Status") != "Enabled":
            reasons.append("publisher_unavailable")
    except Exception:
        reasons.append("publisher_unavailable")
    loaded = _static_values()
    effective = {key: values.get(key, loaded[key]) for key in schema.KEYS}
    chosen = schema.profile(effective[schema.KEYS[0]])
    result.update(selected_profile_id=chosen["id"] if chosen else None,
                  expected_identity=_identity(chosen), loaded_identity=loaded_identity(probe=probe),
                  credential={"configured": bool(effective[schema.KEYS[2]]),
                              "source": "published" if schema.KEYS[2] in values else "static" if loaded[schema.KEYS[2]] else "none",
                              "overridden": schema.KEYS[2] in values},
                  loaded_revision=settings.get_static(config_override.REVISION_KEY, None),
                  pending_restart=provider._pending_restart(result["revision"]))
    result["fleet"] = fleet_apply.observe(
        result["revision"], topic_required=any(key in values for key in schema.KEYS)
        and any(values.get(key) for key in schema.KEYS),
        topic_identity={"profile_id": chosen["id"], "consumer_project_uuid": chosen["consumer_project_uuid"]} if chosen else None)
    if result["revision"] and result["fleet"].get("status") == "unconfigured":
        reasons.append("node_enrollment_unavailable")
    result["unavailable_reasons"] = list(dict.fromkeys(reasons))
    result["available"] = not reasons
    return result


def _request(payload):
    fields = {"action", "expected_revision", "expected_catalog_revision", "profile_id", "key_action"}
    if (not isinstance(payload, dict) or not fields <= set(payload)
            or set(payload) - fields - {"replacement_key"}
            or payload["action"] not in ("preview", "publish")
            or payload["key_action"] not in ("keep", "replace", "clear")
            or "replacement_key" in payload and payload["key_action"] != "replace"
            or not isinstance(payload["profile_id"], str)
            or "replacement_key" in payload and not isinstance(payload["replacement_key"], str)):
        raise ConfigurationError("invalid_request")
    expected = payload["expected_revision"]
    if expected is not None and (not isinstance(expected, str) or not config_override.REVISION_RE.fullmatch(expected)):
        raise ConfigurationError("invalid_request")
    catalog = payload["expected_catalog_revision"]
    if not isinstance(catalog, str) or len(catalog) != 64 or not all(c in "0123456789abcdef" for c in catalog):
        raise ConfigurationError("invalid_request")
    if catalog != schema.catalog_revision():
        raise ConfigurationError("profile_catalog_changed")


def candidate(payload, values, *, loaded=None):
    loaded = _static_values() if loaded is None else loaded
    if payload["key_action"] == "clear":
        return {key: "" for key in schema.KEYS}
    chosen = schema.profile(payload["profile_id"])
    if not chosen:
        raise ConfigurationError("profile_not_approved")
    stored = values.get(schema.KEYS[2], loaded[schema.KEYS[2]])
    replacement = payload.get("replacement_key", "")
    credential = replacement if payload["key_action"] == "replace" and replacement else stored
    if schema.KEYS[2] not in values and not replacement:
        try:
            if schema.normalize_origin(loaded[schema.KEYS[1]]) != chosen["origin"]:
                raise ValueError()
        except ValueError:
            raise ConfigurationError("hub_identity_mismatch") from None
    return dict(zip(schema.KEYS, (chosen["id"], chosen["origin"], credential)))


def mutate(actor, payload, *, probe=None):
    from mojo.apps.account.services import fleet_config, provider_setup as provider
    actor = provider._superuser(actor)
    _request(payload)
    reasons = enrollment_reasons()
    if reasons:
        raise ConfigurationError(reasons[0])
    try:
        s3, bucket, key, allowed = fleet_config._context()
    except Exception:
        raise ConfigurationError("publisher_unavailable") from None
    try:
        with transaction.atomic():
            from mojo.apps.account.models import User
            User.objects.select_for_update().order_by("pk").first()
            actor = provider._superuser(actor, lock=True)
            with identity_lock():
                # Re-evaluate installed catalog after installation/actor locks.
                _request(payload)
                try:
                    current = fleet_config._read(s3, bucket, key, allowed)
                except Exception:
                    raise ConfigurationError("publisher_unavailable") from None
                revision = current["document"]["revision"] if current else None
                if payload["expected_revision"] != revision:
                    raise ConfigurationError("fleet_revision_changed")
                old = current["document"]["settings"] if current else {}
                topic = candidate(payload, old)
                observation = validate_candidate(
                    {k: old[k] for k in schema.KEYS if k in old}, topic, probe=probe)
                if payload["action"] == "preview":
                    return {"can_publish": True, "revision": revision,
                            "catalog_revision": schema.catalog_revision(),
                            "profile_id": topic[schema.KEYS[0]] or None,
                            "expected_identity": _identity(schema.profile(topic[schema.KEYS[0]])),
                            "identity": observation, "credential_changed": topic[schema.KEYS[2]] != old.get(schema.KEYS[2], settings.get_static(schema.KEYS[2], "")),
                            "consequences": ["connection_disabled" if not topic[schema.KEYS[0]] else "restart_required",
                                             "apply_and_verify_every_expected_node", "old_key_retirement_is_separate"]}
                # Fleet's common conditional writer rechecks the full policy.
                # Explicit disable must write empty sensitive overrides; generic
                # Fleet intentionally treats a blank secret as 'keep'.
                values = dict(old, **topic)
                import uuid
                new_revision = uuid.uuid4().hex
                response = provider._write_document(s3, bucket, key, provider._kms_key(),
                                                     allowed, current, values, new_revision)
    except me.MojoException:
        raise
    except Exception:
        raise ConfigurationError("publication_uncertain") from None
    fleet_config._audit(actor, "published", new_revision, schema.KEYS)
    return {"published": True, "revision": new_revision, "version_id": response.get("VersionId"),
            "pending_restart": True, "applied": False}
