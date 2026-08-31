"""Owner-only Assistant enablement, credential storage, and verification.

The protected controls in this owner plane are the ones ``mojo.helpers.llm``,
the incident LLM handlers and the MCP door already read, so nothing here
re-implements credential resolution:

    LLM_ADMIN_ENABLED          feature flag
    LLM_ADMIN_API_KEY          the Assistant's own credential (optional)
    LLM_ADMIN_MODEL            legacy picker pin; guarded route owns runtime model
    LLM_ADMIN_VERIFY_STATE     how the STORED Assistant credential last verified
    LLM_HANDLER_API_KEY        the PLATFORM credential for safety-policy
                               routes that explicitly select ``handler``
    LLM_HANDLER_VERIFY_STATE   how the STORED platform credential last verified
    ASSISTANT_MCP_ENABLED      remote agent access (MCP) switch — descriptor
                               owned by the assistant app, written only here
    LLM_EMERGENCY_STOP        database half of the monotonic emergency stop
    LLM_AUTONOMOUS_INCIDENT_TRIAGE_ENABLED / _ACTIVATED_AT
                               catch-all switch and no-history watermark

The sibling ``llm_safety.activate_policy`` owner action writes the protected
``LLM_SAFETY_POLICY_EXPECTED_HASH`` agreement row.

Both credentials are encrypted secret ``Setting`` rows. ``SettingsHelper.get``
resolves database rows ahead of ``django.conf``, so an Admin-stored credential
is live the moment it commits and outranks the deployment file. Resolution
order is retained only for legacy picker/display helpers; guarded requests use
their exact policy-route credential with no fallback. All these controls are
catalog-protected (``admin_settings.is_catalog_protected``), so the generic
``/api/settings`` surface and every other ``Setting`` writer refuse them; this
service is the only writer, and it goes through the ``_protected_writer`` save
path so ``push_to_cache`` runs on every write. A queryset ``.update()`` would
pass an ORM read and leave the Redis value ``Setting.resolve`` consults first
stale — a disable that silently did not take effect.

Remote agent access adds three read-only surfaces on top of that switch: the
connect address, an explicit discovery self-check, and the list of grants a
client signed in with. Everything from ``services.oauth_server`` is imported
INSIDE the function that uses it — ``rest/admin_portal.py`` imports this module
at URL-load time, and ``oauth_server`` pulls in models.
"""

import json
import re
from urllib.parse import urlsplit

from django.db import transaction
from django.utils import timezone

from mojo import errors as merrors
from mojo.helpers import llm, logit
from mojo.helpers.redis import get_connection
from mojo.helpers.safe_fetch import safe_fetch
from mojo.helpers.settings import settings
from mojo.apps.account.services import system_settings
from mojo.apps.account.services.provider_setup import key_hint


SCHEMA_VERSION = 2

ENABLED_KEY = "LLM_ADMIN_ENABLED"
API_KEY = "LLM_ADMIN_API_KEY"
MODEL_KEY = "LLM_ADMIN_MODEL"
VERIFY_STATE_KEY = "LLM_ADMIN_VERIFY_STATE"
HANDLER_KEY = "LLM_HANDLER_API_KEY"
HANDLER_VERIFY_STATE_KEY = "LLM_HANDLER_VERIFY_STATE"
FALLBACK_KEY = HANDLER_KEY
EMERGENCY_STOP_KEY = "LLM_EMERGENCY_STOP"
AUTONOMOUS_TRIAGE_KEY = "LLM_AUTONOMOUS_INCIDENT_TRIAGE_ENABLED"
AUTONOMOUS_TRIAGE_WATERMARK_KEY = "LLM_AUTONOMOUS_INCIDENT_TRIAGE_ACTIVATED_AT"

# --- remote agent access (MCP) ---------------------------------------------
MCP_ENABLED_KEY = "ASSISTANT_MCP_ENABLED"
MCP_PATH_KEY = "ASSISTANT_MCP_PATH"
MCP_DEFAULT_PATH = "api/assistant/mcp"
DISCOVERY_CACHE_KEY = "assistant:mcp:discovery"
DISCOVERY_TTL = 60          # seconds a network verdict is served before re-probing
DISCOVERY_TIMEOUT = 5
DISCOVERY_MAX_BYTES = 65536
MAX_GRANT_ROWS = 200
UNCHECKED = {"ok": None, "code": "", "detail": "", "checked_at": ""}

# Fixed vocabulary, exactly like VERIFY_MESSAGES: no response body and no
# exception repr ever reaches an operator. ``{error}`` is one of safe_fetch's
# own fixed strings, which may name BASE_URL's hostname — operator-configured,
# not secret.
DISCOVERY_MESSAGES = {
    "ok": "The discovery document is reachable at the public address.",
    "switched_off": "Remote agent access is switched off, so nothing is published.",
    "no_address": "No public address is configured. Set the Public API address "
                  "in System Setup first.",
    "redirected": "The public address redirected the discovery request; nginx "
                  "must serve /.well-known/ from the application directly.",
    "status": "The public address answered HTTP {status} instead of the "
              "discovery document — nginx is not forwarding /.well-known/ to "
              "the application.",
    "wrong_document": "The public address answered something other than the "
                      "discovery document — nginx is not forwarding "
                      "/.well-known/ to the application.",
    # No interpolation: safe_fetch's connect/resolve failures name the host
    # they could not reach, and a redirect hop that fails to resolve reports
    # the HOP's hostname. A fixed sentence keeps every detail free of one.
    "fetch": "The public address could not be fetched from this server.",
}

# The two credentials an owner can store, check and clear. Each has its own
# verification record so the page can say how each STORED key last checked.
TARGETS = {
    "assistant": (API_KEY, VERIFY_STATE_KEY),
    "handler": (HANDLER_KEY, HANDLER_VERIFY_STATE_KEY),
}

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
    """Where the exact admin credential comes from, and its last four characters.

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
    return {"configured": False, "hint": "", "source": "none"}


def _handler_key_state():
    """Where the platform credential comes from: this Admin, the deployment
    file, or nowhere. Same disclosure rule as ``_key_state``."""
    stored = _stored_value(HANDLER_KEY)
    if stored:
        return {"configured": True, "hint": key_hint(stored), "source": "admin"}
    deployed = settings.get_static(HANDLER_KEY, None)
    if deployed:
        return {"configured": True, "hint": key_hint(deployed),
                "source": "deployment"}
    return {"configured": False, "hint": "", "source": "none"}


def _model_state(refresh=False, route=None):
    pinned = _stored_value(MODEL_KEY)
    if isinstance(pinned, str):
        pinned = pinned.strip()
    source = "admin" if pinned else ""
    if not pinned:
        deployed = settings.get_static(MODEL_KEY, None)
        if isinstance(deployed, str) and deployed.strip():
            pinned, source = deployed.strip(), "deployment"
    route = route or {}
    return {
        "selected": pinned or "",
        "effective": route.get("model") or "",
        "source": source or "automatic",
        "choices": llm.model_choices(refresh=refresh),
    }


def read_verify_state(key=VERIFY_STATE_KEY):
    """The persisted record of how one STORED credential last verified."""
    value = _stored_value(key)
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
        from mojo.apps.account.services import llm_safety
        return llm_safety.route_state("assistant")["ready"]
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Remote agent access (MCP)
# ---------------------------------------------------------------------------

def mcp_path():
    """The absolute request path the MCP door is routed and registered at.

    Delegated to ``mcp_auth.configured_path()`` — the ONE helper the route, the
    OAuth registration and the challenge all call — rather than re-derived
    here. Re-deriving it was subtly wrong under ``MOJO_APPEND_SLASH``: the
    helper appends the trailing slash, so the resource actually registered and
    stored on every grant is ``/api/assistant/mcp/`` while this returned the
    unslashed form. The Admin's ``resource__endswith`` filter then matched
    nothing, and every tool-door connection went unlisted and unswept by
    Disconnect all.

    The lazy import and the ``is_installed`` guard follow the rest of this
    module: ``rest/admin_portal.py`` imports it at URL-load time, and the
    assistant app is optional. Without it, the fallback is the same expression
    ``configured_path`` uses for a deployment that has no assistant installed —
    nothing can be registered there anyway.
    """
    from django.apps import apps

    if apps.is_installed("mojo.apps.assistant"):
        from mojo.apps.assistant.mcp import auth as mcp_auth

        return mcp_auth.configured_path()
    return "/" + str(settings.get_static(
        MCP_PATH_KEY, MCP_DEFAULT_PATH)).strip("/")


def api_root():
    """The REST API root — the PREFIX resource an `api` grant is bound to.

    Read from ``mojo.helpers.request`` rather than re-derived, so this surface
    and the registration in the assistant app's ``ready()`` cannot disagree.
    """
    from mojo.helpers.request import API_ROOT

    return API_ROOT


def grant_paths():
    """Both resource paths remote agent access owns, for the Admin surface.

    The switch turns on one feature with two doors; the Connected-agents list,
    its count and Disconnect-all all span exactly these two, and still nothing
    else the installation may protect with the same authorization server.
    """
    return [mcp_path(), api_root()]


def _access_kind(scopes):
    """What a grant's scopes mean in this surface's vocabulary.

    Deliberately computed here rather than in ``oauth_server.list_grants``:
    "Tools" / "Full API" is the Assistant setup page's language, and the
    generic Admin API keeps returning the raw ``scopes``.
    """
    scopes = scopes if isinstance(scopes, list) else []
    tools = "mcp" in scopes
    full = "api" in scopes
    if tools and full:
        return "both"
    if full:
        return "api"
    return "tools"


def mcp_enabled():
    """Live read, never ``get_static``: the switch takes effect immediately."""
    return bool(settings.get(MCP_ENABLED_KEY, False, kind="bool"))


def mcp_ready():
    """The capability bit: could a remote client actually connect right now?

    A switch that is on with no public address advertises a door nobody can
    find, so the chip says "on" only when all three hold.
    """
    from django.apps import apps
    from mojo.apps.account.services import oauth_server

    return bool(apps.is_installed("mojo.apps.assistant")
                and mcp_enabled()
                and oauth_server.public_origin())


def _discovery_record(ok, code, detail, resource=""):
    """One verdict, bounded, with the resource URL it was actually about."""
    return {
        "ok": ok,
        "code": str(code or "")[:32],
        "detail": str(detail or "")[:300],
        "checked_at": timezone.now().isoformat()[:40],
        "resource": str(resource or "")[:600],
    }


def _public(record):
    """The wire shape — ``resource`` is bookkeeping, not something to render."""
    return {
        "ok": record.get("ok"),
        "code": record.get("code", ""),
        "detail": record.get("detail", ""),
        "checked_at": record.get("checked_at", ""),
    }


def discovery_cached(expected=None):
    """The cached NETWORK verdict, or UNCHECKED. Never reaches the network.

    ``expected`` is the resource URL the caller is about to show beside it. A
    record probed for a different one — a BASE_URL changed in System Setup, a
    different MCP path — is discarded rather than shown against the new
    address. Redis being down is simply "not checked yet".
    """
    try:
        raw = get_connection().get(DISCOVERY_CACHE_KEY)
    except Exception:
        return dict(UNCHECKED)
    if not raw:
        return dict(UNCHECKED)
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "ignore")
    try:
        record = json.loads(raw)
    except (TypeError, ValueError):
        return dict(UNCHECKED)
    if not isinstance(record, dict):
        return dict(UNCHECKED)
    if expected is not None and str(record.get("resource") or "")[:600] != expected:
        return dict(UNCHECKED)
    ok = record.get("ok")
    return {
        "ok": ok if isinstance(ok, bool) else None,
        "code": str(record.get("code") or "")[:32],
        "detail": str(record.get("detail") or "")[:300],
        "checked_at": str(record.get("checked_at") or "")[:40],
    }


def _discovery_cache_delete():
    try:
        get_connection().delete(DISCOVERY_CACHE_KEY)
    except Exception:
        pass


def _judge_discovery(result, error, expected):
    """Reduce one ``safe_fetch`` answer to a verdict in the fixed vocabulary."""
    if error is not None:
        # At max_redirects=0 the helper still parses and host-checks the one
        # hop, so a 3xx surfaces as "Redirect target is …", "Redirect to
        # unsupported scheme …" or "Too many redirects (max 0)" depending on
        # where the hop fails. All of them mean "redirected, never served".
        if error.startswith(("Too many redirects", "Redirect ")):
            return _discovery_record(
                False, "unreachable", DISCOVERY_MESSAGES["redirected"], expected)
        # Everything else — a refused connection, a timeout, an unresolvable
        # host, an unresolvable redirect HOP — is one fixed sentence.
        return _discovery_record(
            False, "unreachable", DISCOVERY_MESSAGES["fetch"], expected)
    if result.status_code != 200:
        return _discovery_record(
            False, "unreachable",
            DISCOVERY_MESSAGES["status"].format(status=result.status_code),
            expected)
    # A 200 is not enough: a front door serving the SPA's index.html for
    # unknown paths answers 200 with HTML. Only the real document passes.
    try:
        document = json.loads(result.content)
    except (TypeError, ValueError):
        document = None
    if not isinstance(document, dict) or document.get("resource") != expected:
        return _discovery_record(
            False, "unreachable", DISCOVERY_MESSAGES["wrong_document"], expected)
    return _discovery_record(True, "ok", DISCOVERY_MESSAGES["ok"], expected)


def check_discovery(*, origin=None, transport=None, resolver=None):
    """Probe this installation's OWN public address for the PRM document.

    ``origin``, ``transport`` and ``resolver`` are test seams; the REST layer
    passes none of them, so the probe can only ever target ``public_origin()``.
    Local verdicts (switched off, no address) are returned immediately and are
    never cached — only a network answer is, and that cache IS the rate limit
    on the outbound request.
    """
    from mojo.apps.account.services import oauth_server
    from mojo.apps.account.services.oauth_server import resources

    if not mcp_enabled():
        return _public(_discovery_record(
            False, "disabled", DISCOVERY_MESSAGES["switched_off"]))
    origin = oauth_server.public_origin() if origin is None else origin
    if not origin:
        return _public(_discovery_record(
            False, "disabled", DISCOVERY_MESSAGES["no_address"]))

    path = mcp_path()
    expected = oauth_server.canonical_url(origin, path)
    cached = discovery_cached(expected)
    if cached["ok"] is not None:
        return cached

    # allow_hosts covers the INITIAL url only (never a redirect hop), and
    # validate_base_url already refuses private literals and localhost — so the
    # exemption only ever matters for a public name that resolves privately
    # from inside the deployment, which is exactly this self-probe.
    result, error = safe_fetch(
        resources.prm_url(origin, path),
        timeout=DISCOVERY_TIMEOUT, max_bytes=DISCOVERY_MAX_BYTES,
        max_redirects=0, headers={"Accept": "application/json"},
        allow_hosts=[urlsplit(origin).hostname], schemes=("https",),
        resolver=resolver, transport=transport)
    verdict = _judge_discovery(result, error, expected)
    try:
        get_connection().setex(
            DISCOVERY_CACHE_KEY, DISCOVERY_TTL, json.dumps(verdict))
    except Exception:
        # The throttle is lost only while Redis is, behind an owner-only
        # control. The verdict itself is still the truth.
        pass
    return _public(verdict)


def mcp_state(check=False):
    """Everything the Remote agent access section renders.

    Drawing the page costs no outbound request: ``check`` is the owner's
    explicit control, and a plain read serves the cached network verdict (or
    nothing at all while the switch is off, so a flip made from the file plane
    or another node never shows a stale answer).
    """
    from mojo.apps.account.services import oauth_server
    from mojo.apps.account.services.oauth_server import resources

    origin = oauth_server.public_origin()
    path = mcp_path()
    expected = oauth_server.canonical_url(origin, path) if origin else ""
    enabled = mcp_enabled()
    if check:
        discovery = check_discovery()
    elif enabled:
        discovery = discovery_cached(expected)
    else:
        discovery = dict(UNCHECKED)
    # Scoped BY PATH to the two resources remote agent access owns — the MCP
    # door and the API root — and bounded in SQL: this surface owns remote agent
    # access, not every OAuth resource the installation may protect, and a page
    # load must not load a grant table it is going to slice. The count is a
    # separate COUNT(*) on the same predicate, so it stays honest past the slice.
    # Names and dates only — list_grants carries no token, jti or hash; `access`
    # is derived from the scopes it does carry.
    paths = grant_paths()
    grants = oauth_server.list_grants(resource_path=paths, limit=MAX_GRANT_ROWS)
    for row in grants:
        row["access"] = _access_kind(row.get("scopes"))
    return {
        "enabled": enabled,
        "path": path,
        "url": expected,
        "discovery_url": resources.prm_url(origin, path) if origin else "",
        "discovery": discovery,
        "grants": grants,
        "grant_count": oauth_server.count_grants(resource_path=paths),
    }


def state(refresh=False, check=False):
    """Everything the owner setup view renders. Never carries a credential."""
    from django.apps import apps
    from mojo.apps.account.services import llm_safety
    try:
        safety = llm_safety.aggregate_state(hours=24)
    except Exception:
        safety = {"hours": 24, "requests": [], "breakers": [],
                  "error": "safety_state_unavailable"}
    autonomous_enabled, autonomous_watermark = llm_safety.autonomous_triage_state()
    try:
        effective_stop = llm_safety.emergency_stopped()
    except Exception:
        effective_stop = True
    try:
        database_stop = llm_safety.emergency_stop_database()
    except Exception:
        database_stop = True
    try:
        static_stop = llm_safety.emergency_stop_static()
    except Exception:
        static_stop = True
    route = llm_safety.route_state("assistant")
    return {
        "schema_version": SCHEMA_VERSION,
        "enabled": bool(settings.get(ENABLED_KEY, False, kind="bool")),
        "key": _key_state(),
        "handler_key": _handler_key_state(),
        "model": _model_state(refresh=refresh, route=route),
        "verify": read_verify_state(VERIFY_STATE_KEY),
        "handler_verify": read_verify_state(HANDLER_VERIFY_STATE_KEY),
        "emergency_stop": effective_stop,
        "emergency_stop_static": static_stop,
        "emergency_stop_database": database_stop,
        "route": route,
        "autonomous_triage": autonomous_enabled,
        "autonomous_triage_activated_at": (
            autonomous_watermark.isoformat() if autonomous_watermark else None),
        "assistant_installed": apps.is_installed("mojo.apps.assistant"),
        "realtime_installed": apps.is_installed("mojo.apps.realtime"),
        "mcp": mcp_state(check=check),
        "safety": safety,
    }


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def _verify_candidate(actor, candidate=None, stored_target=None):
    """Test one key and reduce the answer to the fixed vocabulary."""
    if not candidate:
        return {"ok": False, "code": "not_configured",
                "message": VERIFY_MESSAGES["not_configured"]}
    try:
        if stored_target:
            llm_target = "admin" if stored_target == "assistant" else "handler"
            ok, message = llm.verify_api_key(llm_target)
        else:
            from mojo.apps.account.services import llm_safety
            ok = bool(llm_safety.verify_candidate(actor, candidate))
            message = None
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


def _write_verify_state(result, key=VERIFY_STATE_KEY):
    """Record how one STORED credential verified. Never a rejected candidate.

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
        rows = _global_rows(key, lock=True)
        row = rows[0] if rows else Setting(key=key, group=None, is_secret=False)
        row.is_secret = False
        row.mojo_secrets = None
        row.value = entry
        row.save(_protected_writer=key, _skip_cache=True)
        if len(rows) > 1:
            Setting.objects.filter(
                pk__in=[item.pk for item in rows[1:]]).delete()
        transaction.on_commit(row.push_to_cache)


def normalize_target(value):
    if value in (None, ""):
        return "assistant"
    if not isinstance(value, str) or value not in TARGETS:
        raise merrors.ValueException("target must be assistant or handler")
    return value


def verify(actor, api_key=None, target="assistant"):
    """Test a candidate key, or the stored one for ``target`` when none is
    supplied. Each stored probe is exact-target: ``assistant`` tests only the
    admin credential and ``handler`` tests only the platform credential."""
    system_settings.require_system_admin(actor)
    target = normalize_target(target)
    candidate = _normalize_api_key(api_key)
    tested_stored = not candidate
    if tested_stored:
        candidate = settings.get(
            API_KEY if target == "assistant" else HANDLER_KEY, None)
    result = _verify_candidate(
        actor, candidate, stored_target=target if tested_stored else None)
    if tested_stored and result["code"] != "not_configured":
        _write_verify_state(result, TARGETS[target][1])
    _audit(actor, [f"verify_{target}"], "verified" if result["ok"] else "unverified")
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


def _audit(actor, fields, outcome, budget=None, window=3600):
    """File one suppressed incident event for an owner edit.

    ``budget`` belongs on any caller that mints an UNBOUNDED number of distinct
    keys — the per-grant revoke key does, one per id — so per-key suppression
    cannot be turned into an unbounded event flood.
    """
    actor_id = getattr(actor, "pk", None)
    changed = ",".join(sorted(fields)) or "none"

    def write():
        from mojo.apps.incident import report_event_suppressed
        report_event_suppressed(
            f"Assistant setup actor={actor_id} fields={changed} "
            f"outcome={outcome}",
            title="Assistant setup changed", category="admin_settings", level=5,
            key=f"assistant-setup:{actor_id}:{changed}:{outcome}",
            window=window, budget=budget)

    transaction.on_commit(write)


def _log_switch(actor, enabled):
    """A durable, direction-naming line on the actor for every switch write.

    The incident event is suppressed once per ``(category, key)`` per hour, so
    it is a "something changed" signal rather than a history. This is not
    suppressed: on -> off -> on inside one hour reads as three lines naming
    three directions, the way ``oauth_server.revoke_grant`` logs each
    revocation on its own.
    """
    try:
        actor.log(
            f"Remote agent access (MCP) switched {'on' if enabled else 'off'}",
            "assistant:mcp_switch")
    except Exception:
        logit.exception(
            "assistant setup: could not write the mcp switch audit line")


def _credential_edit(actor, label, candidate, clear):
    """Validate one credential's replace/clear pair and pre-verify a candidate."""
    if not isinstance(clear, bool):
        raise merrors.ValueException(f"clear_{label} must be true or false")
    candidate = _normalize_api_key(candidate)
    if candidate and clear:
        raise merrors.ValueException(
            f"Clearing the {label.replace('_', ' ')} and supplying one are different edits")
    verified = None
    if candidate:
        verified = _verify_candidate(actor, candidate)
        if not verified["ok"]:
            # Nothing is stored. An installation must never run a credential
            # nobody proved.
            raise merrors.ValueException(
                f"The {label.replace('_', ' ')} was not accepted. {verified['message']}")
    return candidate, verified


def save(actor, *, enabled, model, api_key=None, clear_api_key=False,
         handler_api_key=None, clear_handler_api_key=False, mcp_enabled=None,
         emergency_stop=None, autonomous_triage=None):
    """Apply one owner edit atomically, or refuse the whole thing.

    ``api_key`` is the admin credential; ``handler_api_key`` is the platform
    credential. Each policy route selects exactly one. A newly supplied credential is
    verified BEFORE the transaction opens: a provider round trip must not be
    made while the installation lock is held, and refusing early means a
    rejected key never reaches the database at all.

    ``mcp_enabled`` is the remote agent access switch and follows the API
    keys' "omit to keep" rule: ``None`` (absent, or a JSON ``null``) leaves the
    stored value alone, so a browser tab that outlived a deploy and does not
    send the field cannot switch remote access off on its next ordinary save.
    Any other non-boolean is refused rather than coerced.
    """
    from mojo.apps.account.services import llm_safety
    system_settings.require_system_admin(actor)
    if not isinstance(enabled, bool):
        raise merrors.ValueException("enabled must be true or false")
    if mcp_enabled is not None and not isinstance(mcp_enabled, bool):
        raise merrors.ValueException("mcp_enabled must be true or false")
    if emergency_stop is not None and not isinstance(emergency_stop, bool):
        raise merrors.ValueException("emergency_stop must be true or false")
    if autonomous_triage is not None and not isinstance(autonomous_triage, bool):
        raise merrors.ValueException("autonomous_triage must be true or false")
    model = normalize_model(model)
    candidate, verified = _credential_edit(actor, "api_key", api_key, clear_api_key)
    handler_candidate, handler_verified = _credential_edit(
        actor, "handler_api_key", handler_api_key, clear_handler_api_key)

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
        if clear_handler_api_key:
            _clear_value(HANDLER_KEY)
            _clear_value(HANDLER_VERIFY_STATE_KEY)
            changed.append("clear_handler_api_key")
        elif handler_candidate:
            _write_secret(HANDLER_KEY, handler_candidate)
            changed.append("handler_api_key")
        _write_value(ENABLED_KEY, bool(enabled))
        if emergency_stop is not None:
            _write_value(EMERGENCY_STOP_KEY, emergency_stop)
            changed.append(f"emergency_stop:{'on' if emergency_stop else 'off'}")
        if autonomous_triage is not None:
            was_enabled, _ = llm_safety.autonomous_triage_state()
            _write_value(AUTONOMOUS_TRIAGE_KEY, autonomous_triage)
            if autonomous_triage and not was_enabled:
                _write_value(
                    AUTONOMOUS_TRIAGE_WATERMARK_KEY,
                    timezone.now().isoformat())
            changed.append(
                f"autonomous_triage:{'on' if autonomous_triage else 'off'}")
        if mcp_enabled is not None:
            _write_value(MCP_ENABLED_KEY, mcp_enabled)
            # The DIRECTION is part of the key, not just the message: a bare
            # "mcp_enabled" would collapse on -> off -> on into one ambiguous
            # event under the hourly (category, key) suppression.
            changed.append(f"mcp_enabled:{'on' if mcp_enabled else 'off'}")
            _log_switch(actor, mcp_enabled)
            # "Flip on, check now" must never show yesterday's answer.
            transaction.on_commit(_discovery_cache_delete)
        if model:
            _write_value(MODEL_KEY, model)
        else:
            _clear_value(MODEL_KEY)
        _audit(actor, changed, "saved")

    # The key that was just stored IS the stored key, so its verification is
    # an observation of the running configuration.
    if verified is not None:
        _write_verify_state(verified, VERIFY_STATE_KEY)
    if handler_verified is not None:
        _write_verify_state(handler_verified, HANDLER_VERIFY_STATE_KEY)
    return state()


def revoke_grant(actor, grant_id):
    """Disconnect ONE remote agent. Returns 1 when a live grant was killed.

    An unknown or already-inactive id is a quiet ``0`` rather than a 404: this
    is owner-only, so there is no enumeration concern, and the page repaints
    from the fresh state either way.
    """
    from mojo.apps.account.services import oauth_server

    system_settings.require_system_admin(actor)
    # `True` is an int in Python, and a bool grant id is a caller bug, not a
    # row selector.
    if isinstance(grant_id, bool) or not isinstance(grant_id, int) or grant_id <= 0:
        raise merrors.ValueException("grant_id must be a positive integer")
    revoked = oauth_server.revoke_grant_by_id(grant_id, actor=actor)
    # Keyed per grant id so report_event_suppressed's hourly (category, key)
    # dedupe never swallows a second grant's revocation — and budgeted, because
    # a distinct key per id is exactly the unbounded-key case the budget exists
    # for.
    _audit(actor, [f"revoke_grant:{grant_id}"],
           "revoked" if revoked else "not_found", budget=50)
    return 1 if revoked else 0


def revoke_all_grants(actor):
    """Disconnect every remote agent, for every user, at BOTH doors.

    Scoped to this feature's two resource paths — the MCP door and the API
    root — so a tool-door grant and a full-API grant are both swept by the one
    control the switch implies. An installation may protect other resources
    with the same authorization server, and the Assistant's setup view is still
    not where those get swept.
    """
    from mojo.apps.account.services import oauth_server

    system_settings.require_system_admin(actor)
    count = oauth_server.revoke_all_grants(
        actor=actor, resource_path=grant_paths())
    _audit(actor, ["revoke_all_grants"], f"revoked:{count}", budget=50)
    return count
