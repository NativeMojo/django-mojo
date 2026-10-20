"""
RFC 8628 device authorization — sign-in for a client that has no browser.

    1. The client POSTs `device_authorization` and is handed a long
       `device_code` (kept) and a short `user_code` (shown to a person).
    2. The person opens the verification page on any device, signs in, enters
       or confirms the user code, reads who is asking for what and from where,
       chooses how long the connection may last, and approves or denies.
    3. The client polls the token endpoint with the device code and receives an
       ordinary grant: the same confinement, refresh rotation, replay detection
       and revocation as a browser consent, with `flow="device_code"`.

The flow's inherent risk is remote-consent phishing: someone starts a request
and sends the link to a victim, and nothing binds the credential to the place
it is delivered. What stands against it here, all in one module:

  * the page shows facts about the REQUESTING machine — its IP, location, user
    agent and the request's age — and warns when that network is not the
    approver's own;
  * a pre-filled code is never approved without the person pressing Approve on
    a screen that repeats it;
  * approval needs an interactive session (the REST layer adds a fresh login);
  * the user code is single use, short lived and stored only as an HMAC, and
    failed lookups are capped per person and per IP;
  * it is off by default and `mcp`-only unless the deployment widens it.

Every function that reads configuration takes `cfg=` and `registry=`, so tests
drive it in-process without touching the shared settings or resource registry.
"""
import datetime
import hashlib
import hmac
import ipaddress
import secrets

from django.conf import settings as django_settings
from django.db import IntegrityError, transaction
from objict import objict

from mojo import errors as merrors
from mojo.helpers import dates, logit
from mojo.helpers.redis import get_connection

from . import clients, consent, resources, tokens
from .resources import API_SCOPE, DEFAULT_SCOPE, DEVICE_GRANT, SERVER_PATH

# RFC 8628 §6.1: twenty consonants — no vowels, so no words — case-insensitive.
USER_CODE_CHARSET = "BCDFGHJKLMNPQRSTVWXZ"
USER_CODE_LENGTH = 8
# The lifetimes offered on the page, before the deployment's cap trims them.
LIFETIME_OPTIONS = (30, 60, 90)
SLOW_DOWN_STEP = 5
MAX_INTERVAL = 60
# Failed lookups: about 5 per person and 20 per IP in a 15-minute window, which
# keeps a guesser to a vanishing fraction of the 20^8 code space.
FAILURE_WINDOW = 900
USER_FAILURE_LIMIT = 5
IP_FAILURE_LIMIT = 20
DEVICE_TEMPLATE = "account/oauth_device.html"
INVALID_CODE = "That code isn't valid or has expired. Check it and try again."
SESSION_REQUIRED = "An interactive session is required to approve a device"


class DeviceError(Exception):
    """An RFC error code, a description safe to return, and an HTTP status."""

    def __init__(self, code, description="", status=400):
        self.code = code
        self.description = description or code
        self.status = status
        super().__init__(self.description)


def config():
    """The flow's deployment settings, read fresh. Tests pass their own."""
    return objict(
        enabled=bool(resources.device_flow_enabled()),
        code_ttl=resources.device_code_ttl(),
        interval=resources.device_interval(),
        allow_api=bool(resources.device_allow_api()),
        max_ttl_days=resources.device_max_ttl_days())


def is_available(origin, registry=None, cfg=None):
    cfg = cfg or config()
    return bool(cfg.enabled) and resources.is_ready(origin, registry)


def lifetime_options(cfg=None):
    """The grant lifetimes, in days, the approving person may choose from.

    Never more than the deployment's cap. A cap that is not itself one of the
    standard options is offered as the longest choice, so an operator who sets
    45 gets 30 or 45 rather than silently only 30.
    """
    cap = (cfg or config()).max_ttl_days
    options = [days for days in LIFETIME_OPTIONS if days <= cap]
    if cap not in options:
        options.append(cap)
    return options


# --- codes ----------------------------------------------------------------

def generate_user_code():
    chars = "".join(secrets.choice(USER_CODE_CHARSET) for _ in range(USER_CODE_LENGTH))
    return format_user_code(chars)


def normalize_user_code(raw):
    """The canonical eight characters, or None.

    Case, spaces and dashes are ignored, because people retype what they see.
    Anything outside the charset is refused rather than mapped: a lookalike
    letter is a typo, and guessing what was meant would widen the code space a
    guesser gets for free.
    """
    if not isinstance(raw, str) or len(raw) > 32:
        return None
    cleaned = "".join(ch for ch in raw.upper() if ch not in " -\t")
    if len(cleaned) != USER_CODE_LENGTH:
        return None
    if any(ch not in USER_CODE_CHARSET for ch in cleaned):
        return None
    return cleaned


def format_user_code(normalized):
    return f"{normalized[:4]}-{normalized[4:]}"


def _user_code_hash(normalized):
    # Keyed, unlike the device code's plain sha256: ~34 bits of user code would
    # fall to an offline search the moment the table leaked.
    key = f"oauth-device-user-code:{django_settings.SECRET_KEY}".encode()
    return hmac.new(key, normalized.encode(), hashlib.sha256).hexdigest()


def _sha256_hex(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def _request_ip(request):
    return getattr(request, "ip", None) or request.META.get("REMOTE_ADDR") or ""


def _request_ua(request):
    raw = getattr(request, "user_agent", None) or request.META.get("HTTP_USER_AGENT", "")
    return clients.clean_text(str(raw or ""), limit=512)


# --- 1. the client starts a request ---------------------------------------

def start(request, origin, registry=None, cfg=None):
    """POST device_authorization. Returns the RFC 8628 §3.2 response dict."""
    from mojo.apps.account.models.oauth_device_code import OAuthDeviceCode

    cfg = cfg or config()
    if not is_available(origin, registry, cfg):
        raise DeviceError("not_found", "not found", 404)

    data = consent._request_data(request)
    try:
        client = clients.resolve_client(consent._single(data, "client_id"))
    except clients.ClientError as err:
        raise DeviceError(err.code, err.description, 401)
    if DEVICE_GRANT not in clients.client_grant_types(client):
        raise DeviceError(
            "unauthorized_client", "this client is not registered for the device grant")

    try:
        params = consent.validate_scope_resource(data, origin, registry)
    except consent.RedirectableError as err:
        raise DeviceError(err.code, err.description)
    if API_SCOPE in params["granted"] and not cfg.allow_api:
        raise DeviceError("invalid_scope", "scope api is not offered to devices")

    now = dates.utcnow()
    _purge(now)
    raw_device = secrets.token_urlsafe(32)
    user_code = None
    for _attempt in range(5):
        candidate = generate_user_code()
        try:
            with transaction.atomic():
                OAuthDeviceCode.objects.create(
                    client=client,
                    device_code_hash=_sha256_hex(raw_device),
                    user_code_hash=_user_code_hash(normalize_user_code(candidate)),
                    scope=params["scope"],
                    resource=params["resource"],
                    interval=cfg.interval,
                    expires=now + datetime.timedelta(seconds=cfg.code_ttl),
                    requester_ip=_request_ip(request)[:45] or None,
                    requester_ua=_request_ua(request))
            user_code = candidate
            break
        except IntegrityError:
            # A user code still on file. Draw again; five collisions in a row
            # in a 20^8 space means something else is wrong.
            continue
    if user_code is None:
        raise DeviceError("server_error", "could not allocate a user code", 503)

    verification_uri = f"{origin}{SERVER_PATH}/device"
    return {
        "device_code": raw_device,
        "user_code": user_code,
        "verification_uri": verification_uri,
        "verification_uri_complete": f"{verification_uri}?user_code={user_code}",
        "expires_in": cfg.code_ttl,
        "interval": cfg.interval,
    }


def _purge(now):
    """Opportunistic — device codes are worthless a day after they expire."""
    from mojo.apps.account.models.oauth_device_code import OAuthDeviceCode

    try:
        OAuthDeviceCode.objects.filter(
            expires__lt=now - datetime.timedelta(days=1)).delete()
    except Exception:
        pass


# --- 2. a person looks the code up, then approves or denies ----------------

def _require_available(origin, registry, cfg):
    if not is_available(origin, registry, cfg):
        raise merrors.PermissionDeniedException(
            "Device sign-in is not available", 404, 404)


def _require_session(request):
    """The approving session's auth_time, or refuse — exactly as consent does."""
    if getattr(request, "bearer", None) != "bearer":
        raise merrors.PermissionDeniedException(SESSION_REQUIRED)
    auth_time = consent._session_auth_time(request)
    if auth_time is None:
        raise merrors.PermissionDeniedException(SESSION_REQUIRED)
    return auth_time


def _failure_keys(request):
    return (f"oauth:device:fail:user:{request.user.pk}",
            f"oauth:device:fail:ip:{_request_ip(request) or 'unknown'}")


def _check_failures(request):
    """Refuse every lookup — right or wrong — once a counter is spent."""
    user_key, ip_key = _failure_keys(request)
    try:
        user_count, ip_count = get_connection().mget(user_key, ip_key)
    except Exception:
        logit.exception("oauth: could not read the device lookup counters")
        return
    if int(user_count or 0) >= USER_FAILURE_LIMIT or int(ip_count or 0) >= IP_FAILURE_LIMIT:
        raise merrors.RateLimitException(
            FAILURE_WINDOW, "Too many incorrect codes. Try again later.")


def _record_failure(request):
    """Count one failed lookup; report the crossing of either limit once."""
    user_key, ip_key = _failure_keys(request)
    try:
        pipe = get_connection().pipeline()
        pipe.incr(user_key)
        pipe.expire(user_key, FAILURE_WINDOW)
        pipe.incr(ip_key)
        pipe.expire(ip_key, FAILURE_WINDOW)
        user_count, _ok, ip_count, _ok2 = pipe.execute()
    except Exception:
        logit.exception("oauth: could not count a failed device lookup")
        return
    if user_count == USER_FAILURE_LIMIT or ip_count == IP_FAILURE_LIMIT:
        try:
            request.user.report_incident(
                f"OAuth device user code guessed wrong {user_count} times by "
                f"this account ({ip_count} from {_request_ip(request)})",
                "oauth:device_code_bruteforce", level=6, request=request)
        except Exception:
            logit.exception("oauth: could not report the device brute-force incident")


def _find_pending(request, raw_code):
    """The pending, unexpired request a user code names — or a counted refusal."""
    from mojo.apps.account.models.oauth_device_code import OAuthDeviceCode

    _check_failures(request)
    normalized = normalize_user_code(raw_code)
    row = None
    if normalized:
        row = OAuthDeviceCode.objects.filter(
            user_code_hash=_user_code_hash(normalized), status="pending",
            expires__gt=dates.utcnow()).select_related("client").first()
    if row is None or not row.client.is_active:
        _record_failure(request)
        raise merrors.ValueException(INVALID_CODE)
    return row, normalized


def _default_geo(ip):
    from mojo.apps.account.models import GeoLocatedIP

    geo = GeoLocatedIP.geolocate(ip)
    return ", ".join(part for part in (geo.city, geo.region, geo.country_code) if part)


def _geo_label(ip, geo_lookup=None):
    """A place name for the requester's IP, or "" — never an error on the page."""
    if not ip:
        return ""
    try:
        return clients.clean_text(str((geo_lookup or _default_geo)(ip) or ""))
    except Exception:
        logit.exception("oauth: could not geolocate a device requester")
        return ""


def _network(ip):
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return None
    prefix = 24 if address.version == 4 else 64
    return ipaddress.ip_network(f"{address}/{prefix}", strict=False)


def _different_network(requester_ip, approver_ip):
    """True unless both addresses are known and share a /24 (IPv4) or /64 (IPv6)."""
    a = _network(requester_ip or "")
    b = _network(approver_ip or "")
    return a is None or b is None or a != b


def _consent_facts(request, row, normalized, cfg):
    granted = [token for token in (row.scope or "").split() if token]
    facts = consent.consent_context(row.client, granted, row.resource)
    who = request.user.email or request.user.username
    facts["access_lines"] = [line.replace("{email}", who) for line in facts["access_lines"]]
    now = dates.utcnow()
    options = lifetime_options(cfg)
    facts.update({
        "user_code": format_user_code(normalized),
        "requester": {
            "ip": row.requester_ip or "",
            "location": row.requester_geo or "",
            "user_agent": row.requester_ua or "",
            "age_seconds": max(0, int((now - row.created).total_seconds())),
        },
        "network_mismatch": _different_network(row.requester_ip, _request_ip(request)),
        # The tool door also requires one of these at request time; saying so
        # now saves the person approving a connection that cannot work.
        "missing_permission": (
            DEFAULT_SCOPE in granted
            and not request.user.has_permission(["view_admin", "assistant"])),
        "lifetime_options": options,
        "lifetime_default": options[0],
        "expires_in": max(0, int((row.expires - now).total_seconds())),
    })
    return facts


def lookup(request, origin, registry=None, cfg=None, geo_lookup=None):
    """POST device/lookup. Returns what the consent screen shows."""
    from mojo.apps.account.models.oauth_device_code import OAuthDeviceCode

    cfg = cfg or config()
    _require_available(origin, registry, cfg)
    _require_session(request)
    row, normalized = _find_pending(request, request.DATA.get("user_code"))
    if not row.requester_geo and row.requester_ip:
        row.requester_geo = _geo_label(row.requester_ip, geo_lookup)
        if row.requester_geo:
            OAuthDeviceCode.objects.filter(pk=row.pk).update(
                requester_geo=row.requester_geo)
    return _consent_facts(request, row, normalized, cfg)


def _chosen_lifetime(raw, cfg):
    options = lifetime_options(cfg)
    if raw in (None, ""):
        return options[0]
    try:
        days = int(raw)
    except (TypeError, ValueError):
        days = None
    if days not in options:
        raise merrors.ValueException(
            f"Choose a connection lifetime of {', '.join(str(d) for d in options)} days")
    return days


def _audit(user, row, verb):
    name = row.client.client_name or row.client.client_id
    try:
        user.log(
            f"OAuth device sign-in {verb} for {name}, requested from "
            f"{row.requester_ip or 'an unknown address'}",
            f"oauth:device_{verb}")
    except Exception:
        logit.exception(f"oauth: could not write the device_{verb} audit line")


def approve(request, origin, registry=None, cfg=None):
    """POST device/approve. The REST layer has already demanded a fresh login."""
    from mojo.apps.account.models.oauth_device_code import OAuthDeviceCode

    cfg = cfg or config()
    _require_available(origin, registry, cfg)
    auth_time = _require_session(request)
    row, _normalized = _find_pending(request, request.DATA.get("user_code"))
    days = _chosen_lifetime(request.DATA.get("ttl_days"), cfg)
    now = dates.utcnow()
    moved = OAuthDeviceCode.objects.filter(
        pk=row.pk, status="pending", expires__gt=now).update(
            status="approved", user=request.user, auth_time=auth_time,
            approved_at=now, grant_ttl_days=days)
    if moved != 1:
        raise merrors.ValueException(INVALID_CODE)
    _audit(request.user, row, "approved")
    return {"status": "approved", "ttl_days": days}


def deny(request, origin, registry=None, cfg=None):
    """POST device/deny. The client's next poll answers access_denied."""
    from mojo.apps.account.models.oauth_device_code import OAuthDeviceCode

    cfg = cfg or config()
    _require_available(origin, registry, cfg)
    _require_session(request)
    row, _normalized = _find_pending(request, request.DATA.get("user_code"))
    moved = OAuthDeviceCode.objects.filter(pk=row.pk, status="pending").update(
        status="denied", user=request.user)
    if moved != 1:
        raise merrors.ValueException(INVALID_CODE)
    _audit(request.user, row, "denied")
    return {"status": "denied"}


# --- 3. the client polls the token endpoint -------------------------------

def _replay(row):
    """A consumed device code presented again: kill what it produced, report it."""
    if row.grant is not None:
        tokens.revoke_grant(row.grant, reason="device_code_replay")
    if row.user is None:
        return
    try:
        row.user.report_incident(
            f"OAuth device code replayed for client {row.client_id}",
            "oauth:device_code_replay", level=6)
    except Exception:
        logit.exception("oauth: could not report the device code replay incident")


def poll(request, origin, registry=None, cfg=None):
    """The token endpoint's device_code branch. Returns the token response.

    Raises DeviceError with the RFC 8628 §3.5 code. The order matters: replay
    is judged before the client, so a stolen code cannot be laundered through
    another client_id; the client is judged before the poll bookkeeping, so a
    stranger cannot slow the real client down.
    """
    from mojo.apps.account.models.oauth_device_code import OAuthDeviceCode

    cfg = cfg or config()
    if not cfg.enabled:
        raise DeviceError("unsupported_grant_type", "the device grant is not enabled")
    raw = request.DATA.get("device_code")
    client_id = request.DATA.get("client_id")
    if not isinstance(raw, str) or not raw or not client_id:
        raise DeviceError("invalid_request", "device_code and client_id are required")
    try:
        client = clients.resolve_client(client_id)
    except clients.ClientError as err:
        raise DeviceError(err.code, err.description, 401)

    row = OAuthDeviceCode.objects.filter(
        device_code_hash=_sha256_hex(raw)).select_related("user", "grant").first()
    if row is None:
        raise DeviceError("invalid_grant", "invalid device code")
    if row.status == "consumed":
        _replay(row)
        raise DeviceError("invalid_grant", "invalid device code")
    if row.client_id != client.pk:
        raise DeviceError("invalid_grant", "invalid device code")

    now = dates.utcnow()
    if row.expires <= now:
        raise DeviceError("expired_token", "the device code has expired")
    if row.last_polled is not None and \
            (now - row.last_polled).total_seconds() < row.interval:
        interval = min(MAX_INTERVAL, row.interval + SLOW_DOWN_STEP)
        OAuthDeviceCode.objects.filter(pk=row.pk).update(
            interval=interval, last_polled=now)
        raise DeviceError("slow_down", f"poll at most every {interval} seconds")
    OAuthDeviceCode.objects.filter(pk=row.pk).update(last_polled=now)

    if row.status == "pending":
        raise DeviceError("authorization_pending", "waiting for approval")
    if row.status == "denied":
        raise DeviceError("access_denied", "the request was denied")

    # Approved. Single use is settled by rowcount: a concurrent poll that loses
    # gets invalid_grant rather than a second grant.
    burned = OAuthDeviceCode.objects.filter(
        pk=row.pk, status="approved").update(status="consumed")
    if burned != 1:
        raise DeviceError("invalid_grant", "invalid device code")
    user = row.user
    if user is None or not user.is_active:
        raise DeviceError("invalid_grant", "invalid device code")
    entry = resources.resolve(resources.resource_path(row.resource), registry)
    if entry is None or not resources.is_enabled(entry, registry):
        raise DeviceError("invalid_grant", "invalid device code")

    days = min(row.grant_ttl_days or lifetime_options(cfg)[0],
               resources.DEVICE_MAX_TTL_CEILING)
    scopes = [token for token in (row.scope or "").split() if token]
    grant = tokens.create_grant(
        user, client, scopes, row.resource, row.auth_time,
        flow="device_code", ttl_days=days)
    OAuthDeviceCode.objects.filter(pk=row.pk).update(grant=grant)
    try:
        return tokens.issue_tokens(grant)
    except tokens.RotationLost:
        raise DeviceError("invalid_grant", "invalid device code")


# --- the verification page --------------------------------------------------

def handle_page(request, origin, registry=None, cfg=None):
    """GET the verification page. Always returns an HttpResponse.

    The page is a shell: everything it shows about a request arrives from
    `lookup` after the person signs in, so nothing here depends on the code
    being valid. A `user_code` in the URL only pre-fills the box.
    """
    cfg = cfg or config()
    if not is_available(origin, registry, cfg):
        return consent._not_available(request, template=DEVICE_TEMPLATE)

    normalized = normalize_user_code(consent._single(consent._request_data(request), "user_code"))
    page_return = f"{SERVER_PATH}/device"
    if normalized:
        page_return = f"{page_return}?user_code={format_user_code(normalized)}"
    return consent._render(request, consent._page_context(request, {
        "page_title": "Connect a device",
        "prefill_code": format_user_code(normalized) if normalized else "",
        "lookup_url": f"{SERVER_PATH}/device/lookup",
        "approve_url": f"{SERVER_PATH}/device/approve",
        "deny_url": f"{SERVER_PATH}/device/deny",
        "page_return": page_return,
    }), template=DEVICE_TEMPLATE)
