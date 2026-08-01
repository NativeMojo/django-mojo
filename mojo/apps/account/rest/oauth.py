"""
OAuth REST endpoints.

Flow:
  GET  /api/auth/oauth/<provider>/begin    -> returns authorization URL
  GET|POST /api/auth/oauth/<provider>/callback -> provider redirects here, bounces to frontend
  POST /api/auth/oauth/<provider>/complete -> exchange code, auto-link user, issue JWT

All providers redirect to a backend callback endpoint. The authorization code
never appears in the frontend URL bar. The callback bounces the browser to the
frontend with code + state as query params, where the JS calls /complete.

Supported providers: google, apple (more can be added to services/oauth/__init__.py)
"""
from urllib.parse import urlencode, quote, urlsplit, urlunsplit, parse_qsl

from django.core.exceptions import DisallowedRedirect
from django.http import HttpResponseRedirect

from mojo import decorators as md
from mojo import errors as merrors
from mojo.apps.account.models import User
from mojo.apps.account.models.oauth import OAuthConnection
from mojo.apps.account.rest.user import jwt_login
from mojo.apps.account.services import extensions as account_extensions
from mojo.apps.account.services import auth_config
from mojo.apps.account.services import redirect_allowlist
from mojo.apps.account.services.oauth import get_provider
from mojo.helpers import logit
from mojo.helpers.response import JsonResponse
from mojo.helpers.settings import settings

def _get_origin(request):
    """Derive the origin (scheme + host) from the request."""
    origin = request.META.get("HTTP_ORIGIN", "").rstrip("/")
    if not origin:
        host = request.META.get("HTTP_HOST", "")
        scheme = "https" if request.is_secure() else "http"
        origin = f"{scheme}://{host}"
    return origin


def _get_redirect_uri(request, provider_name):
    """Use configured redirect URI or build one from the request origin."""
    oauth_redirect_uri = settings.get("OAUTH_REDIRECT_URI", "")
    if oauth_redirect_uri:
        return oauth_redirect_uri
    origin = _get_origin(request)
    return f"{origin}/auth/oauth/{provider_name}/complete"


def _validate_redirect_uri(request, redirect_uri):
    """
    Validate redirect_uri against the allowlist, matched as a URL.

    TWO sources, matched SEPARATELY (not concatenated into one list):
      - the ALLOWED_REDIRECT_URLS setting — deployment configuration;
      - `group.metadata["allowed_redirect_urls"]` for the group this request
        resolved to, read with `get_metadata_value`, which walks the parent
        chain so a child group inherits an ancestor's entries.

    They are matched under DISTINCT sources (`ALLOWED_REDIRECT_URLS` vs
    `group:<pk>`) so an unusable entry is attributed to the right party: a broken
    deployment entry files an operator incident, while a tenant's broken
    `metadata["allowed_redirect_urls"]` files a lower-severity, budgeted,
    fail-closed TENANT incident. The two lists carry very different trust — the
    tenant one is `manage_group`-writable and `request.group` is anonymously
    selectable via `?group=` / `?group_uuid=` on this public endpoint — so
    collapsing them into one source (as the old `allowed.extend(...)` did) would
    let a tenant's junk drive the operator's incident category. A match in EITHER
    source admits the URL. See `redirect_allowlist.matches_allowlist`.

    The per-group source is tenant-writable and caller-selectable, and that is a
    deliberate, accepted decision — not an oversight. Plain `metadata` is
    writable by any holder of `manage_group` on that group, and `request.group`
    is chosen by the caller via `?group=<id>` / `?group_uuid=<uuid>` on this
    public endpoint. It is kept because django-mojo is a REST platform third
    parties integrate with: a white-label tenant has to be able to self-serve
    the origin its own login page lands on, without a deploy of the platform.
    What bounds the residual risk is the matcher below — an entry authorizes the
    exact host it names and nothing else, so a tenant can bless an origin it
    already controls and cannot reach any other. Domain verification, moving the
    key under `metadata["protected"]`, and scoping the read to group members
    were each considered and declined (the last is impossible anyway on a public
    endpoint whose whole purpose is a signed-out white-label login page).

    Matching is `redirect_allowlist.matches_allowlist` — the same parsed-URL
    matcher the handoff destination allowlist uses, and it applies to BOTH
    sources. It replaced a bare `redirect_uri.startswith(entry)`, which
    terminated on nothing: an entry of `https://app.example.com` admitted
    `https://app.example.com.evil.tld/`, a host the attacker registers, and this
    endpoint is public, so the resulting token-theft chain needed no credential
    to start. Wildcard (`*.`) entries are NOT honored here — see the module
    docstring in the service.

    `kind="list"` is load-bearing, not cosmetic. This key resolves through
    `settings.get`, which reads a DB-backed `Setting` row BEFORE the file
    setting, and `Setting.value` is a `TextField` — so a deployment configuring
    the key that way holds a STRING. The old `list(...)` shattered it into
    single characters, leaving `startswith('h')` true for every `http(s)://` URL
    on earth; the coercion turns both the JSON-array and bare-string forms into
    the list they spell.

    The `list(...)` copy is load-bearing too. `settings.get(kind="list")` hands
    back the live settings list object when the value is already a list, so
    extending it in place would append the group's entries to the deployment's
    global setting — permanently, and growing on every request.

    Raises ValueException (400) if the URI is not on the allowlist or if no
    allowlist is configured at all.
    """
    deployment = list(settings.get("ALLOWED_REDIRECT_URLS", [], kind="list") or [])
    # getattr + truthiness, never hasattr: request.group may be an objict-shaped
    # stand-in, and objict answers hasattr True for every name.
    group = getattr(request, "group", None)
    group_entries = []
    if group:
        value = group.get_metadata_value("allowed_redirect_urls")
        if value:
            # `list(value)` intentionally preserves the pre-existing shape: a
            # bare-string value char-explodes (each char is an unusable entry the
            # matcher drops) and a non-iterable raises — UNCHANGED here, fixed
            # separately in #1103.
            group_entries = list(value)

    if not deployment and not group_entries:
        raise merrors.ValueException(
            "redirect_uri is not permitted: no ALLOWED_REDIRECT_URLS configured"
        )

    if redirect_allowlist.matches_allowlist(
            redirect_uri, deployment, source="ALLOWED_REDIRECT_URLS", request=request):
        return
    # `group.pk` is reached only inside this guard, so `group` is truthy here.
    if group_entries and redirect_allowlist.matches_allowlist(
            redirect_uri, group_entries, source=f"group:{group.pk}",
            request=request, tenant=True):
        return

    # A Redis-suppressed, budgeted, host-keyed incident replaces the per-request
    # log line: /begin is public and unauthenticated, so an unbounded per-request
    # line (or a raw event) is free amplification an anonymous caller drives.
    redirect_allowlist.report_refused_redirect_uri(redirect_uri, request=request)
    raise merrors.ValueException("redirect_uri is not on the allowlist")


def _refuse_gated_destination(request, url):
    """Refuse an OAuth landing URL that sits on a GATED destination host.

    Two gated surfaces, two answers. The handoff leg DELIVERS a group-scoped
    token to a gated destination; this leg REFUSES, because it cannot safely
    deliver one. `on_oauth_complete` ends at `jwt_login` and hands a full
    access+refresh pair to whichever origin posted to it, and routing it
    through a scoped delivery instead is not available: `_find_or_create_user`
    can create a brand-new account, add a GroupMember from the client-supplied
    `state.group_uuid`, fire USER_REGISTERED_HANDLER and persist provider
    tokens BEFORE a membership pre-flight could fail — a 403 after four side
    effects — and auto-joining the visitor to avoid that would be a membership
    grant driven by a URL.

    So under `enforce` a gated site that runs its OWN OAuth callback page loses
    OAuth login and must move that flow to the hosted auth pages, where OAuth
    already works and already routes through the gated handoff. `monitor` files
    an incident and proceeds, so a deployment learns which destinations that
    hits BEFORE it binds.

    Same-host is exempt: the hosted auth pages' OAuth buttons set the landing
    URL to the page's own URL, and those pages are served white-label on the
    tenant's own host — so with a zone-wide gating entry this would otherwise
    refuse a tenant's own Google button. A JWT landing in the auth origin's own
    storage is exactly the outcome the refusal reasons is safe.

    The refusal message is the string `/begin` ALREADY emits for an unlisted
    URI, so gated-versus-unlisted is not an oracle.
    """
    from mojo.apps.account.services import handoff_group
    mode = handoff_group.get_mode()
    if mode == handoff_group.MODE_OFF:
        return
    enforcing = mode == handoff_group.MODE_ENFORCE

    if handoff_group.is_own_host(request, url):
        ok, group = handoff_group.resolve_group(url, request=request)
        if ok and group is not None:
            reason = (
                f"AUTH_HANDOFF_GROUP_TOKEN_HOSTS gates {url!r}, which is the "
                f"auth origin serving this very request. An auth-page origin "
                f"must never be gated — the OAuth flow that lands there is the "
                f"hosted one, and the visitor's tokens already live in this "
                f"origin's storage. Remove the entry.")
            logit.error("oauth", reason)
            handoff_group.report_misconfigured(
                reason, "own_host", destination=url, request=request)
        return

    ok, group = handoff_group.resolve_group(url, request=request)
    if ok and group is None:
        return
    if ok:
        handoff_group.report_oauth(url, group, request=request, enforced=enforcing)
    else:
        handoff_group.report_refusal(url, request=request, enforced=enforcing)
    if enforcing:
        raise merrors.ValueException("redirect_uri is not on the allowlist")


def _resolve_state_group(state_data):
    """Look up an active Group from state_data['group_uuid'].

    Returns the Group if active, or None if absent / unknown / inactive.
    Logs a warning for unknown/inactive — the OAuth callback already passed,
    failing the whole login attempt to enforce group membership would lose it.
    """
    group_uuid = (state_data or {}).get("group_uuid")
    if not group_uuid:
        return None
    from mojo.apps.account.models.group import Group
    group = Group.objects.filter(uuid=group_uuid).first()
    if group is None:
        logit.warn("oauth", f"OAuth state references unknown group_uuid={group_uuid}")
        return None
    # Effective activeness (DM-048): a deactivated ancestor darkens the group too.
    if not group.is_effectively_active():
        logit.warn("oauth", f"OAuth state references inactive group_uuid={group_uuid}")
        return None
    return group


def _lookup_known_user(provider_name, profile):
    """Lookup-only resolution of the local user an OAuth profile maps to.

    Mirrors steps 1-2 of _find_or_create_user WITHOUT any side effect — no
    connection/user creation, no email-verified flip. Used by the
    post-credential geofence check (DM-043) so an existing user's
    bypass_geofence is honored while an unknown identity is enforced
    anonymously BEFORE any account provisioning happens.
    """
    conn = OAuthConnection.objects.filter(
        provider=provider_name, provider_uid=profile["uid"]
    ).select_related("user").first()
    if conn:
        return conn.user
    return User.lookup(email=profile["email"])


def _find_or_create_user(provider_name, profile, state_data=None, request=None):
    """
    Auto-link logic:
    1. Existing OAuthConnection  -> return (user, conn, False)
    2. Matching email on User    -> create OAuthConnection, return (user, conn, False)
    3. Neither                   -> create User + OAuthConnection, return (user, conn, True)

    Path 3 raises PermissionDeniedException if OAUTH_ALLOW_REGISTRATION is False.

    When path 3 runs and state_data carries an active group_uuid, the new user
    is also added to that group as a GroupMember and USER_REGISTERED_HANDLER
    is fired with source="oauth".

    state_data and request are optional (default None) so existing direct
    callers (notably tests that exercise the auto-link logic in isolation)
    continue to work without needing to construct a fake state/request.
    """
    uid = profile["uid"]
    email = profile["email"]
    display_name = profile.get("display_name")

    # 1. Existing connection
    conn = OAuthConnection.objects.filter(
        provider=provider_name, provider_uid=uid
    ).select_related("user").first()
    if conn:
        return conn.user, conn, False

    # 2. Existing user by email — OAuth has confirmed ownership of this address
    user = User.lookup(email=email)
    if user and not user.is_email_verified:
        user.is_email_verified = True
        user.save(update_fields=["is_email_verified", "modified"])
        logit.info("oauth", f"Marked email verified for {user.username} via {provider_name} OAuth")

    # 3. Create new user
    if not user:
        if not settings.get("OAUTH_ALLOW_REGISTRATION", True):
            raise merrors.PermissionDeniedException("Account registration via OAuth is not permitted")
        # Per-group registration method gate — a group can disable signup via
        # a given OAuth provider in its auth config. Only applies to the
        # toggleable providers (google/apple/github) and when a group is
        # resolved.
        if provider_name in auth_config.REGISTRATION_METHODS:
            _reg_group = _resolve_state_group(state_data)
            if _reg_group is not None:
                _reg_cfg = auth_config.resolve_auth_config(group=_reg_group)
                if provider_name not in (_reg_cfg.registration.methods or []):
                    raise merrors.PermissionDeniedException(
                        "Account registration via this provider is not permitted")
        user = User(email=email)
        user.username = user.generate_username_from_email()
        if display_name:
            user.display_name = display_name
        user.is_email_verified = True
        user.set_unusable_password()
        user.save()
        logit.info("oauth", f"Created new user {user.username} via {provider_name}")
        conn = OAuthConnection.objects.create(
            user=user,
            provider=provider_name,
            provider_uid=uid,
            email=email,
        )

        # GroupMember from state.group_uuid (if present + active)
        resolved_group = _resolve_state_group(state_data)
        if resolved_group is not None:
            from mojo.apps.account.models.member import GroupMember
            GroupMember.objects.get_or_create(user=user, group=resolved_group)

        # USER_REGISTERED_HANDLER — runtime errors propagate (no atomic here;
        # the user + conn are already committed, so the consumer is expected
        # to handle its own partial-state cleanup if it raises).
        account_extensions.fire_user_registered(
            user=user, request=request, group=resolved_group,
            source="oauth", extra={"provider": provider_name})

        return user, conn, True

    conn = OAuthConnection.objects.create(
        user=user,
        provider=provider_name,
        provider_uid=uid,
        email=email,
    )
    return user, conn, False


# The two schemes the callback bounce always permits. A custom deep-link scheme
# is added on top, per request, only when it was vetted at mint time.
_BOUNCE_SCHEMES = ("http", "https")


def _vetted_bounce_scheme(frontend_uri, allowlisted):
    """Return the custom scheme the callback bounce may widen to, or "".

    A scheme is trustworthy only from a provenance vetted here, at mint time,
    where the allowlist decision and `request.group` are in scope. Two qualify:

      * `allowlisted` — the caller's `redirect_uri` cleared
        `_validate_redirect_uri` against ALLOWED_REDIRECT_URLS (plus the group
        source), so the allowlist vetted that exact URL, scheme included;
      * a byte-equal match to this deployment's own `OAUTH_REDIRECT_URI` — an
        operator-configured value, trusted like an allowlist entry (it may
        itself be a deep link).

    Everything else is untrusted — above all the else-branch of
    `on_oauth_begin`, which builds `frontend_uri` from the UNVALIDATED
    `HTTP_ORIGIN` header. A scheme sourced there never widens the bounce.

    An http(s) scheme also returns "": those never need widening (the stock
    HttpResponseRedirect already admits them), so an http(s) flow stamps no
    `frontend_scheme` and its state stays byte-identical to before this change.
    A non-empty return is therefore always exactly one vetted custom scheme.
    """
    if not (allowlisted or frontend_uri == settings.get("OAUTH_REDIRECT_URI", "")):
        return ""
    scheme = redirect_allowlist.matchable_scheme(frontend_uri)
    return "" if scheme in _BOUNCE_SCHEMES else scheme


# -----------------------------------------------------------------
# Begin
# -----------------------------------------------------------------

@md.GET("auth/oauth/<str:provider>/begin")
@md.public_endpoint()
@md.requires_geofence(scope="auth")
def on_oauth_begin(request, provider):
    """Return the provider's authorization URL.

    Optional query parameter:
      redirect_uri — frontend URL the browser should land on after the OAuth
                     callback completes. Matched as a URL (scheme, host, port
                     and a segment-bounded path prefix) against the allowlist:
                     the ALLOWED_REDIRECT_URLS setting, plus — when this request
                     resolved a group — that group's
                     metadata["allowed_redirect_urls"], inherited up the parent
                     chain. A shared string prefix is not enough (see
                     _validate_redirect_uri).
                     Defaults to _get_redirect_uri().

    All providers redirect to the backend callback endpoint
    (/api/auth/oauth/<provider>/callback). The frontend_uri is stored in
    state so the callback knows where to bounce the browser.
    """
    try:
        svc = get_provider(provider)
    except ValueError:
        raise merrors.ValueException(f"Unknown provider: {provider}")

    # UX-only per-group method gate — applies to any provider that is a
    # toggleable login method (google/apple/github); providers outside
    # LOGIN_METHODS are never gated here.
    if provider in auth_config.LOGIN_METHODS:
        auth_config.assert_login_method(
            provider, auth_config.resolve_group_from_request(request))

    # frontend_uri = where the browser lands after the callback bounce
    custom_frontend_uri = request.DATA.get("redirect_uri", "")
    if custom_frontend_uri:
        _validate_redirect_uri(request, custom_frontend_uri)
        frontend_uri = custom_frontend_uri
    else:
        frontend_uri = _get_redirect_uri(request, provider)
    # `allowlisted` = the caller's redirect_uri cleared _validate_redirect_uri.
    # It is the load-bearing signal for _vetted_bounce_scheme below: only an
    # allowlisted (or OAUTH_REDIRECT_URI-matching) frontend_uri may widen the
    # callback's redirect scheme, never the origin-derived else branch.
    allowlisted = bool(custom_frontend_uri)
    # Covers BOTH branches deliberately. The `else` one is the load-bearing
    # case: it derives the landing URL from the UNVALIDATED HTTP_ORIGIN header
    # and never calls _validate_redirect_uri, so a caller with no redirect_uri
    # param picks the destination with a request header.
    _refuse_gated_destination(request, frontend_uri)
    # A custom-scheme landing (mobile deep link) needs the callback to widen its
    # Django redirect scheme allowlist to exactly that scheme. Derive it HERE, at
    # mint time, where the allowlist verdict is in scope, and stamp it into state
    # for the callback to re-check against the final URL. "" for an http(s) or
    # untrusted frontend_uri, and stamped below only when non-empty.
    frontend_scheme = _vetted_bounce_scheme(frontend_uri, allowlisted)

    # callback_uri = where the provider redirects (always our backend)
    origin = _get_origin(request)
    callback_uri = f"{origin}/api/auth/oauth/{provider}/callback"

    state_extra = {
        "redirect_uri": callback_uri,
        "frontend_uri": frontend_uri,
    }
    # Stamp the vetted deep-link scheme only when there is one, so the http(s)
    # state shape is byte-identical to before this change.
    if frontend_scheme:
        state_extra["frontend_scheme"] = frontend_scheme
    # Preserve group context through the OAuth round-trip for white-label
    # branding. Only accept `group_uuid` — the framework dispatcher reserves
    # `?group=` for integer IDs and any UUID passed there is rejected upstream
    # before this view runs (so silently falling back to it would just mask
    # client bugs).
    group_uuid = request.DATA.get("group_uuid", "")
    if group_uuid:
        state_extra["group_uuid"] = group_uuid

    state = svc.create_state(extra=state_extra)
    auth_url = svc.get_auth_url(state=state, redirect_uri=callback_uri)

    return JsonResponse({
        "status": True,
        "data": {
            "auth_url": auth_url,
            "state": state,
        },
    })


# -----------------------------------------------------------------
# Provider callback — receives redirect from provider, bounces to frontend
# -----------------------------------------------------------------

class _SchemeAwareRedirect(HttpResponseRedirect):
    """An HttpResponseRedirect whose allowed URL schemes are per-instance.

    Django's HttpResponseRedirectBase reads `self.allowed_schemes` (class
    default `["http", "https", "ftp"]`) AFTER its own `super().__init__`, so
    setting the instance attribute BEFORE calling super here shadows the class
    attribute cleanly — for this instance only. The class attribute is left
    untouched, so only redirects built through this class are widened; every
    other HttpResponseRedirect in the process keeps Django's stock default.
    """

    def __init__(self, redirect_to, *args, allowed_schemes=None, **kwargs):
        self.allowed_schemes = list(allowed_schemes)
        super().__init__(redirect_to, *args, **kwargs)


def _bounce_to_frontend(location, state_data):
    """302 the browser to `location`, widening to at most one vetted custom scheme.

    `location` is the FINAL merged URL — code/state already appended. The bounce
    always permits http(s); it additionally permits the custom scheme recorded in
    state at mint time (`frontend_scheme`) IFF re-parsing this exact final URL
    yields the same scheme. Re-parsing `location` (not the stored frontend_uri)
    re-applies every `_split` refusal — dot segment, backslash, opaque/bare shape
    — to the precise bytes about to go in the Location header, and derives the
    scheme already lowercased.

    Anything Django still refuses — a pre-#1102 state carrying no
    `frontend_scheme`, a scheme that no longer matches the final URL, an
    over-long or malformed target — floors to a 400 rather than the 500 a raw
    DisallowedRedirect would raise out of the generic error handler.
    """
    schemes = list(_BOUNCE_SCHEMES)
    vetted = (state_data or {}).get("frontend_scheme") or ""
    if vetted and vetted == redirect_allowlist.matchable_scheme(location):
        schemes.append(vetted)
    try:
        return _SchemeAwareRedirect(location, allowed_schemes=schemes)
    except DisallowedRedirect:
        raise merrors.ValueException(
            "Cannot return to the redirect_uri in this OAuth state")


@md.GET("auth/oauth/<str:provider>/callback")
@md.POST("auth/oauth/<str:provider>/callback")
@md.public_endpoint()
def on_oauth_callback(request, provider):
    """
    Receives the redirect from the OAuth provider after user authorization.

    Google: GET with code + state as query params.
    Apple:  POST (form_post) with code + state in body.

    Peeks at state to find the frontend_uri, then bounces the browser there
    with code + state as query params. The frontend JS then calls /complete.

    Deliberately NOT gated (see `_refuse_gated_destination`), and stated here
    so it does not read as an oversight: this only 302s the browser with the
    provider's authorization code, which is useless without our client secret.
    The only actor who can spend it is our own /complete, which refuses. A
    check here would replace a navigation with a raw JSON error for no
    security gain.
    """
    # Google sends GET query params, Apple sends POST form data
    code = request.GET.get("code") or request.POST.get("code", "")
    state = request.GET.get("state") or request.POST.get("state", "")

    if not code or not state:
        raise merrors.ValueException("Missing code or state in OAuth callback")

    try:
        svc = get_provider(provider)
    except ValueError:
        raise merrors.ValueException(f"Unknown provider: {provider}")

    # Peek at state (without consuming) to find the frontend URI to bounce to.
    # The state is consumed later by on_oauth_complete.
    state_data = svc.peek_state(state)
    if not state_data:
        raise merrors.PermissionDeniedException("Invalid or expired OAuth state", 401, 401)

    frontend_uri = state_data.get("frontend_uri", "")
    if not frontend_uri:
        raise merrors.ValueException("No frontend_uri in OAuth state")

    redirect_params = {"code": code, "state": state}
    # Preserve group context so branding survives the OAuth round-trip.
    # Use `group_uuid` (not `group`) because the framework dispatcher reserves
    # `?group=` for integer IDs and rejects UUID values with 400, which would
    # break the next request from the frontend.
    group_uuid = state_data.get("group_uuid", "")
    if group_uuid:
        redirect_params["group_uuid"] = group_uuid

    # frontend_uri may already carry a query (e.g. the app's ?redirect=), so
    # merge rather than naively concatenating a second "?". Preserving that
    # query is what lets the post-login redirect target survive the OAuth
    # round-trip. Drop any caller-supplied copies of the keys we set here: the
    # allowlist match ignores the query entirely (it compares scheme, host,
    # port and path), so an attacker can smuggle e.g. ?code=EVIL into an
    # otherwise allowed URL; since URLSearchParams.get() returns the first
    # match, a duplicate placed before the real value would shadow it and
    # sabotage the victim's login. Stripping them makes the server-set values
    # the only ones present. Still load-bearing after the matcher was
    # tightened — do not remove it.
    parts = urlsplit(frontend_uri)
    preserved = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                 if k not in redirect_params]
    query = urlencode(preserved + list(redirect_params.items()), quote_via=quote)
    return _bounce_to_frontend(
        urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment)),
        state_data)


# -----------------------------------------------------------------
# Complete
# -----------------------------------------------------------------

@md.POST("auth/oauth/<str:provider>/complete")
@md.POST("oauth/<str:provider>/complete")
@md.requires_params("code", "state")
@md.public_endpoint()
@md.requires_geofence(scope="auth", after_auth=True)
def on_oauth_complete(request, provider):
    """Exchange authorization code for tokens, resolve user, issue JWT."""
    try:
        svc = get_provider(provider)
    except ValueError:
        raise merrors.ValueException(f"Unknown provider: {provider}")

    state = request.DATA.get("state")
    state_data = svc.consume_state(state)
    if state_data is None:
        raise merrors.PermissionDeniedException("Invalid or expired OAuth state", 401, 401)

    # The AUTHORITATIVE gating check, and it runs BEFORE any provider call.
    # It survives a mode flip inside the state's TTL, covers a legacy state
    # with no frontend_uri, and re-checks the caller's own origin — which is
    # where the response body physically goes.
    _refuse_gated_destination(request, state_data.get("frontend_uri"))
    _refuse_gated_destination(request, _get_origin(request))

    code = request.DATA.get("code")
    # redirect_uri is bound to the state — use it for the token exchange.
    # Falls back to the default if a legacy state has no stored URI.
    redirect_uri = state_data.get("redirect_uri") or _get_redirect_uri(request, provider)

    try:
        tokens = svc.exchange_code(code=code, redirect_uri=redirect_uri)
        profile = svc.get_profile(tokens)
    except ValueError as exc:
        raise merrors.PermissionDeniedException(str(exc), 401, 401)

    if not profile.get("uid"):
        raise merrors.PermissionDeniedException("Could not retrieve user identity from provider")

    # Post-credential geofence (DM-043): the provider exchange has proven the
    # identity — enforce BEFORE _find_or_create_user so a blocked-geo caller
    # can never provision an account, join a group, fire the registration
    # webhook, or persist provider tokens. Existing users resolve lookup-only
    # (bypass_geofence honored); an unknown identity enforces anonymously —
    # the same posture as the old pre-view block. jwt_login re-checks below
    # (cached no-op / bypass short-circuit).
    from mojo.apps.account.services.geofence import enforcement
    blocked = enforcement.enforce(
        request, scope="auth", user=_lookup_known_user(provider, profile))
    if blocked is not None:
        return blocked

    user, conn, created = _find_or_create_user(provider, profile, state_data, request)

    if not user.is_active:
        raise merrors.PermissionDeniedException("Account is disabled")

    # Store fresh tokens on the connection
    conn.set_secret("access_token", tokens.get("access_token"))
    if tokens.get("refresh_token"):
        conn.set_secret("refresh_token", tokens.get("refresh_token"))
    conn.save()

    logit.info("oauth", f"User {user.username} logged in via {provider}")
    extra = {"is_new_user": True} if created else None
    return jwt_login(request, user, source="oauth", extra=extra, is_new_user=created)


# -----------------------------------------------------------------
# OAuth Connection Management
# -----------------------------------------------------------------

@md.URL("account/oauth_connection")
@md.URL("account/oauth_connection/<int:pk>")
@md.denies_key_backed_session(methods=("POST", "PUT", "PATCH", "DELETE"))
@md.requires_auth()
def on_oauth_connection(request, pk=None):
    """Standard CRUD for OAuth connections (GET list, GET detail, POST update).
    DELETE is handled by the custom endpoint below with lockout protection."""
    if request.method == "DELETE" and pk is not None:
        return on_oauth_connection_delete(request, pk)
    return OAuthConnection.on_rest_request(request, pk)


def on_oauth_connection_delete(request, pk):
    """Unlink an OAuth connection with lockout protection."""
    # Admin bypass — manage_users can delete any connection
    if request.user.has_permission("manage_users"):
        conn = OAuthConnection.objects.filter(pk=pk).first()
        if not conn:
            raise merrors.PermissionDeniedException("Not found", 404, 404)
        conn.delete()
        return JsonResponse({"status": True})

    # Owner path — must be own connection
    conn = OAuthConnection.objects.filter(pk=pk, user=request.user).first()
    if not conn:
        raise merrors.PermissionDeniedException("Not found", 404, 404)

    # Lockout guard: if no usable password and this is the last active connection, block
    try:
        has_password = request.user.has_usable_password()
        active_count = OAuthConnection.objects.filter(user=request.user, is_active=True).count()
        if not has_password and active_count <= 1:
            raise merrors.ValueException("Cannot unlink your only login method. Set a password first.")
    except merrors.ValueException:
        raise
    except Exception:
        # Fail-closed: if guard check itself errors, deny the delete
        request.user.report_incident("OAuth unlink guard check failed", "oauth:unlink_guard_error")
        raise merrors.PermissionDeniedException("Unable to process request")

    conn.delete()
    return JsonResponse({"status": True})
