"""Owner-only Assistant enablement, credential storage, and verification.

The four keys this service owns are the ones ``mojo.helpers.llm`` already
reads, so nothing here re-implements resolution:

    LLM_ADMIN_ENABLED       feature flag
    LLM_ADMIN_API_KEY       encrypted credential (secret Setting row)
    LLM_ADMIN_MODEL         explicit model pin, absent means "automatic"
    LLM_ADMIN_VERIFY_STATE  how the STORED credential last verified

``SettingsHelper.get`` resolves database rows ahead of ``django.conf``, so an
Admin-stored credential is live the moment it commits and outranks both the
deployment file and the ``LLM_HANDLER_API_KEY`` fallback. All four are
catalog-protected (``admin_settings.is_catalog_protected``), so the generic
``/api/settings`` surface and every other ``Setting`` writer refuse them; this
service is the only writer, and it goes through the ``_protected_writer`` save
path so ``push_to_cache`` runs on every write. A queryset ``.update()`` would
pass an ORM read and leave the Redis value ``Setting.resolve`` consults first
stale — a disable that silently did not take effect.
"""

import json
import re

from django.db import transaction
from django.utils import timezone

from mojo import errors as merrors
from mojo.helpers import llm
from mojo.helpers.settings import settings
from mojo.apps.account.services import system_settings
from mojo.apps.account.services.provider_setup import key_hint


SCHEMA_VERSION = 1

ENABLED_KEY = "LLM_ADMIN_ENABLED"
API_KEY = "LLM_ADMIN_API_KEY"
MODEL_KEY = "LLM_ADMIN_MODEL"
VERIFY_STATE_KEY = "LLM_ADMIN_VERIFY_STATE"
FALLBACK_KEY = "LLM_HANDLER_API_KEY"

MAX_API_KEY_LENGTH = 4096
# Structural only. The fetched catalogue is network-dependent, so validating a
# pin against it would refuse a valid re-save whenever the API is unreachable.
MODEL_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]{0,80}$")

# Every message this service can record or return. Fixed vocabulary: no
# exception repr, no provider response body, and no fragment of a credential
# ever reaches an operator-visible string.
VERIFY_MESSAGES = {
    "verified": "Anthropic accepted this key.",
    "invalid_key": "Anthropic rejected this key.",
    "unreachable": "Anthropic could not be reached to check this key.",
    "not_configured": "No API key is configured to check.",
}


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def _global_rows(key, *, lock=False):
    """Global rows for one key, oldest first.

    PostgreSQL still permits a duplicate global row. Reads take the oldest one
    rather than failing, matching ``provider_setup._read_verify_state``; writes
    take the lock and repair to a single row.
    """
    from mojo.apps.account.models import Setting
    rows = Setting.objects.filter(key=key, group=None).order_by("pk")
    if lock:
        rows = rows.select_for_update()
    return list(rows)


def _stored_value(key):
    rows = _global_rows(key)
    if not rows:
        return None
    try:
        return rows[0].get_value()
    except Exception:
        return None


def _key_state():
    """Where the effective credential comes from, and its last four characters.

    The raw value never leaves this function; only ``configured``, a four
    character hint, and a provenance word do.
    """
    stored = _stored_value(API_KEY)
    if stored:
        return {"configured": True, "hint": key_hint(stored), "source": "admin"}
    deployed = settings.get_static(API_KEY, None)
    if deployed:
        return {"configured": True, "hint": key_hint(deployed),
                "source": "deployment"}
    fallback = settings.get(FALLBACK_KEY, None)
    if fallback:
        return {"configured": True, "hint": key_hint(fallback),
                "source": "fallback"}
    return {"configured": False, "hint": "", "source": "none"}


def _model_state(refresh=False):
    pinned = _stored_value(MODEL_KEY)
    if isinstance(pinned, str):
        pinned = pinned.strip()
    source = "admin" if pinned else ""
    if not pinned:
        deployed = settings.get_static(MODEL_KEY, None)
        if isinstance(deployed, str) and deployed.strip():
            pinned, source = deployed.strip(), "deployment"
    return {
        "selected": pinned or "",
        "effective": llm.get_model("general"),
        "source": source or "automatic",
        "choices": llm.model_choices(refresh=refresh),
    }


def read_verify_state():
    """The persisted record of how the STORED credential last verified."""
    value = _stored_value(VERIFY_STATE_KEY)
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "ignore")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {"ok": None, "code": "", "message": "", "at": ""}
    if not isinstance(value, dict):
        return {"ok": None, "code": "", "message": "", "at": ""}
    code = str(value.get("code") or "")[:64]
    return {
        "ok": value.get("ok") is True if value.get("ok") is not None else None,
        "code": code,
        "message": VERIFY_MESSAGES.get(code, ""),
        "at": str(value.get("at") or "")[:40],
    }


def is_ready():
    """The one cheap boolean the Admin bootstrap needs."""
    if not settings.get(ENABLED_KEY, False, kind="bool"):
        return False
    try:
        return bool(llm.get_api_key())
    except Exception:
        return False


def state(refresh=False):
    """Everything the owner setup view renders. Never carries a credential."""
    from django.apps import apps
    return {
        "schema_version": SCHEMA_VERSION,
        "enabled": bool(settings.get(ENABLED_KEY, False, kind="bool")),
        "key": _key_state(),
        "model": _model_state(refresh=refresh),
        "verify": read_verify_state(),
        "assistant_installed": apps.is_installed("mojo.apps.assistant"),
        "realtime_installed": apps.is_installed("mojo.apps.realtime"),
    }


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def _verify_candidate(candidate):
    """Test one key and reduce the answer to the fixed vocabulary."""
    if not candidate:
        return {"ok": False, "code": "not_configured",
                "message": VERIFY_MESSAGES["not_configured"]}
    try:
        ok, message = llm.verify_api_key(candidate)
    except Exception:
        # A broken client library is an unreachable provider as far as the
        # operator is concerned, and its repr is not theirs to read.
        return {"ok": False, "code": "unreachable",
                "message": VERIFY_MESSAGES["unreachable"]}
    if ok:
        return {"ok": True, "code": "verified",
                "message": VERIFY_MESSAGES["verified"]}
    code = "invalid_key" if str(message or "").startswith("API key is invalid") \
        else "unreachable"
    return {"ok": False, "code": code, "message": VERIFY_MESSAGES[code]}


def _write_verify_state(result):
    """Record how the STORED credential verified. Never a rejected candidate.

    A draft that was never saved is not the configuration this installation is
    running, so recording it would make the setup page describe something that
    does not exist — the ``tested_stored`` rule from ``provider_setup.test``.
    """
    from mojo.apps.account.models import Setting
    entry = json.dumps({
        "ok": bool(result["ok"]),
        "code": result["code"],
        "at": timezone.now().isoformat(),
    })
    with transaction.atomic():
        rows = _global_rows(VERIFY_STATE_KEY, lock=True)
        row = rows[0] if rows else Setting(
            key=VERIFY_STATE_KEY, group=None, is_secret=False)
        row.is_secret = False
        row.mojo_secrets = None
        row.value = entry
        row.save(_protected_writer=VERIFY_STATE_KEY, _skip_cache=True)
        if len(rows) > 1:
            Setting.objects.filter(
                pk__in=[item.pk for item in rows[1:]]).delete()
        transaction.on_commit(row.push_to_cache)


def verify(actor, api_key=None):
    """Test a candidate key, or the effective one when none is supplied."""
    system_settings.require_system_admin(actor)
    candidate = _normalize_api_key(api_key)
    tested_stored = not candidate
    if tested_stored:
        candidate = llm.get_api_key()
    result = _verify_candidate(candidate)
    if tested_stored and result["code"] != "not_configured":
        _write_verify_state(result)
    _audit(actor, ["verify"], "verified" if result["ok"] else "unverified")
    return result


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def _normalize_api_key(value):
    if value in (None, ""):
        return ""
    if not isinstance(value, str) or len(value) > MAX_API_KEY_LENGTH:
        raise merrors.ValueException("The API key is invalid")
    value = value.strip()
    if any(char.isspace() for char in value):
        raise merrors.ValueException("The API key is invalid")
    return value


def normalize_model(value):
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise merrors.ValueException("The model must be a model identifier")
    value = value.strip()
    if not value:
        return ""
    if not MODEL_RE.fullmatch(value):
        raise merrors.ValueException("The model must be a model identifier")
    return value


def _cache_delete(key):
    from mojo.apps.account.models import Setting
    redis = Setting._redis()
    if redis:
        redis.hdel(Setting._redis_key(), key)


def _write_value(key, value):
    """Store one non-secret assistant key through its dedicated writer."""
    from mojo.apps.account.models import Setting
    rows = _global_rows(key, lock=True)
    row = rows[0] if rows else Setting(key=key, group=None, is_secret=False)
    row.is_secret = False
    row.mojo_secrets = None
    row.set_value(value)
    row.save(_protected_writer=key, _skip_cache=True)
    if len(rows) > 1:
        Setting.objects.filter(pk__in=[item.pk for item in rows[1:]]).delete()
    transaction.on_commit(row.push_to_cache)


def _clear_value(key):
    from mojo.apps.account.models import Setting
    rows = _global_rows(key, lock=True)
    if rows:
        Setting.objects.filter(pk__in=[row.pk for row in rows]).delete()
    transaction.on_commit(lambda: _cache_delete(key))


def _write_secret(key, value):
    from mojo.apps.account.models import Setting
    rows = _global_rows(key, lock=True)
    row = rows[0] if rows else Setting(key=key, group=None, is_secret=True)
    row.is_secret = True
    row.set_value(value)
    row.save(_protected_writer=key, _skip_cache=True)
    if len(rows) > 1:
        Setting.objects.filter(pk__in=[item.pk for item in rows[1:]]).delete()
    transaction.on_commit(row.push_to_cache)


def _audit(actor, fields, outcome):
    actor_id = getattr(actor, "pk", None)
    changed = ",".join(sorted(fields)) or "none"

    def write():
        from mojo.apps.incident import report_event_suppressed
        report_event_suppressed(
            f"Assistant setup actor={actor_id} fields={changed} "
            f"outcome={outcome}",
            title="Assistant setup changed", category="admin_settings", level=5,
            key=f"assistant-setup:{actor_id}:{changed}:{outcome}")

    transaction.on_commit(write)


def save(actor, *, enabled, model, api_key=None, clear_api_key=False):
    """Apply one owner edit atomically, or refuse the whole thing.

    A newly supplied credential is verified BEFORE the transaction opens: a
    provider round trip must not be made while the installation lock is held,
    and refusing early means a rejected key never reaches the database at all.
    """
    system_settings.require_system_admin(actor)
    if not isinstance(enabled, bool):
        raise merrors.ValueException("enabled must be true or false")
    if not isinstance(clear_api_key, bool):
        raise merrors.ValueException("clear_api_key must be true or false")
    candidate = _normalize_api_key(api_key)
    model = normalize_model(model)
    if candidate and clear_api_key:
        raise merrors.ValueException(
            "Clearing the API key and supplying one are different edits")

    verified = None
    if candidate:
        verified = _verify_candidate(candidate)
        if not verified["ok"]:
            # Nothing is stored. An installation must never run a credential
            # nobody proved.
            raise merrors.ValueException(
                f"The API key was not accepted. {verified['message']}")

    from mojo.apps.account.models import User
    changed = ["enabled", "model"]
    with transaction.atomic():
        # One installation-wide lock: two owners can only ever race on one
        # credential, and the loser reads the truth back immediately because
        # both actions return the fresh state().
        User.objects.select_for_update().order_by("pk").first()
        system_settings.require_system_admin(actor)
        if clear_api_key:
            _clear_value(API_KEY)
            _clear_value(VERIFY_STATE_KEY)
            changed.append("clear_api_key")
        elif candidate:
            _write_secret(API_KEY, candidate)
            changed.append("api_key")
        _write_value(ENABLED_KEY, bool(enabled))
        if model:
            _write_value(MODEL_KEY, model)
        else:
            _clear_value(MODEL_KEY)
        _audit(actor, changed, "saved")

    if verified is not None:
        # The key that was just stored IS the stored key, so its verification
        # is an observation of the running configuration.
        _write_verify_state(verified)
    return state()
