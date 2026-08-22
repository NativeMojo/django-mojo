import ipaddress

from objict import objict, nobjict
from .request_parser import RequestDataParser
from mojo.helpers.settings import settings
from mojo.helpers.crypto.sign import verify_signature, get_signature_header

DUID_HEADER = settings.get_static('DUID_HEADER', 'X-Mojo-UID').replace('-', '_').upper()
DUID_HEADER = f"HTTP_{DUID_HEADER}"

REQUEST_PARSER = RequestDataParser()
API_ROOT = "/" + settings.get_static("MOJO_PREFIX", "api/").strip("/")
# The OAuth server's path root is read here separately, from the same setting
# the service reads: mojo/helpers must not import mojo.apps at import time.
OAUTH_ROOT = "/" + settings.get_static("OAUTH_SERVER_PATH", "api/account/oauth").strip("/")

def parse_request_data(request):
    """
    Consolidates all GET, POST, JSON body, and FILE data into one objict dict.
    Handles dotted keys and repeated fields.
    """
    return REQUEST_PARSER.parse(request)


# Additional helper function for debugging
def debug_request_data(request):
    """
    Debug version that shows step-by-step processing
    """
    print("=== DEBUG REQUEST PARSING ===")
    print(f"Method: {request.method}")
    print(f"Content-Type: {getattr(request, 'content_type', 'Not set')}")
    print(f"GET: {dict(request.GET)}")
    print(f"POST: {dict(request.POST)}")
    print(f"FILES: {list(request.FILES.keys())}")

    result = parse_request_data(request)
    print(f"Final result: {result}")
    return result


def get_referer(request):
    return request.META.get('HTTP_REFERER')


def restricted_identity(request):
    # The machine/confined identity for this request, if any: an ApiKey
    # (mojo/apps/account/models/api_key.py) or a GroupScopedToken
    # (mojo/apps/account/services/group_token.py). Both are bearer credentials
    # whose authority must stay inside one group tree regardless of whose
    # identity they carry, and both duck-type the same surface
    # (is_group_allowed / has_permission / get_groups /
    # get_groups_with_permission / override_user / is_active / limits), so the
    # guards can read one predicate instead of enumerating credential kinds.
    return getattr(request, "api_key", None) or getattr(request, "group_token", None)


def identity_allows_group(request, group):
    # Fail-closed tenant check for endpoints that authorize against an
    # ARBITRARY caller-named group (metrics account=group-<id>, the chat room
    # helpers, group/<pk>/member) rather than through model security's instance
    # re-bind. Returns True for an ordinary user session — those are gated by
    # their own membership/permission checks — and confines every restricted
    # identity to its own group.
    #
    # `group=None` denies a restricted identity: a row with no tenant belongs
    # to no tenant, and a confined credential has no business reaching it.
    ident = restricted_identity(request)
    if ident is None:
        return True
    return group is not None and ident.is_group_allowed(group)


def is_key_backed_session(request):
    # True when this request authenticated with a CONFINED bearer credential —
    # an ApiKey or a GroupScopedToken — REGARDLESS of whose identity it acts
    # as. Both can put a real User in request.user (ApiKey.override_user; a
    # group token always does), so the type of request.user stops being a
    # reliable "is a human driving this" signal; restricted_identity is the one
    # that keeps telling the truth. The credential is a bearer token sitting in
    # a config file or a browser's storage, not a person at a keyboard.
    #
    # Use this — never is_request_user() — for any check that means "this is a
    # machine or otherwise confined identity" (machine-identity gates,
    # credential-mutation blocks). Use is_request_user() only where the
    # question is genuinely "is request.user a User model instance"
    # (attribution).
    return restricted_identity(request) is not None


def sensitive_body_label(request):
    """Return a fixed log marker for credential-bearing API requests.

    Both request and response logging call this before honoring broad debug or
    file logging.  Keep the decision path-based: request logging runs before a
    view can annotate the request, and a secret endpoint must therefore be
    recognizable without inspecting its body.
    """
    path = str(getattr(request, "path", "") or "").rstrip("/")
    method = str(getattr(request, "method", "") or "").upper()
    if path.startswith(f"{API_ROOT}/auth/"):
        return "account_auth"
    if method == "POST" and path in (
            f"{API_ROOT}/login", f"{API_ROOT}/account/jwt/login",
            f"{API_ROOT}/refresh_token", f"{API_ROOT}/token/refresh",
            f"{API_ROOT}/account/jwt/refresh"):
        return "account_auth"
    if path.startswith(f"{API_ROOT}/group/apikey"):
        return "group_api_key"
    if path == f"{API_ROOT}/group/webhook_secret":
        return "group_webhook_secret"
    if path.startswith(f"{API_ROOT}/account/admin/user/password"):
        return "admin_password"
    if path == f"{API_ROOT}/account/admin/apikey/action":
        return "admin_api_key"
    if path == f"{API_ROOT}/account/admin/settings":
        return "admin_settings"
    # The Assistant setup writer carries an Anthropic API key in its body on
    # both `save` and `verify`. Without this entry LOGIT_DB_ALL / LOGIT_FILE_ALL
    # write it verbatim into the logit.Log table (readable at manage_logs /
    # view_logs / security / admin — well below the superuser tier that is
    # allowed to set the key) and into requests.log.
    if path == f"{API_ROOT}/account/admin/assistant":
        return "assistant_setup"
    # OAuth 2.1 credential carriers: `token` bodies hold a code_verifier or a
    # refresh token, `revoke` a live token, and `approve` the session bearer
    # plus the PKCE material. `register` and the discovery documents carry no
    # secret, so they are deliberately absent.
    if method == "POST" and path in (f"{OAUTH_ROOT}/token", f"{OAUTH_ROOT}/revoke"):
        return "oauth_token"
    if method == "POST" and path == f"{OAUTH_ROOT}/approve":
        return "oauth_approve"
    if path == f"{API_ROOT}/edge/webapp/link_key":
        return "webapp_deployment_key"
    if method == "POST" and path in (
            f"{API_ROOT}/edge/webapp/onboarding/choose",
            f"{API_ROOT}/edge/webapp/onboarding/workflow"):
        return "webapp_onboarding_secret"
    if path == f"{API_ROOT}/dnsman/credential/link":
        return "dns_provider_credential"
    if path in (
            f"{API_ROOT}/dnsman/registrar/quote",
            f"{API_ROOT}/dnsman/registrar/purchase"):
        return "registrar_confirmation"
    if path.startswith(f"{API_ROOT}/dnsman/certificate/material/"):
        return "certificate_material"
    if path.startswith(f"{API_ROOT}/edge/material/"):
        return "certificate_material"
    return None


def is_override_user_session(request):
    # True only for a confined credential that ASSUMES a member — an ApiKey
    # with override_user=True, or any GroupScopedToken (which always carries a
    # real user) — i.e. the case where request.user is a real User whose GLOBAL
    # permission dict would otherwise be consulted.
    #
    # The distinction from is_key_backed_session matters: for an unlinked or
    # reference-mode ApiKey, request.user IS the ApiKey, so `request.user.
    # has_permission(...)` reads the KEY's own dict — which is correct and
    # already bounded to the key's group. Only in override mode does that read
    # resolve to a member's untenanted platform-wide grants.
    ident = restricted_identity(request)
    return ident is not None and bool(getattr(ident, "override_user", False))


def is_request_user(request):
    # The framework's ONE predicate for "request.user is a real User instance".
    # User defines the `is_request_user` marker
    # (mojo/apps/account/models/user.py); machine identities must not. Absence
    # of the marker is the fail-closed direction: an unknown identity is
    # treated as a machine. Both the auth decorators (mojo/decorators/auth.py)
    # and model security (mojo/models/rest.py) key on this — keep them aligned
    # here, not on hand-rolled hasattr checks (DM-045).
    #
    # CAUTION: this returns True for an ApiKey with override_user=True, because
    # request.user really IS a User there. That is correct for attribution and
    # WRONG for any machine-identity or authorization gate — those must use
    # is_key_backed_session(request) above.
    user = getattr(request, "user", None)
    return user is not None and hasattr(user, "is_request_user")


def normalize_ip(value):
    # Return a clean IP string, or None for empty/garbage. Handles surrounding
    # whitespace, an IP:port suffix, bracketed IPv6 ([::1]), and IPv4-mapped IPv6
    # (::ffff:1.2.3.4 -> 1.2.3.4). Clean-or-None is safer for the
    # GenericIPAddressField consumers of request.ip than a raw header value.
    if not value:
        return None
    ip = value.strip()
    if ip.startswith('[') and ']' in ip:        # [::1]:443 -> ::1
        ip = ip[1:ip.index(']')]
    elif ip.count(':') == 1:                     # 1.2.3.4:5678 -> 1.2.3.4 (IPv4:port)
        ip = ip.split(':', 1)[0]
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped                  # ::ffff:1.2.3.4 -> 1.2.3.4
    return str(addr)


def get_remote_ip(request):
    # nginx (asgi.inc) sets X-Real-IP to the true client ($remote_addr) and overwrites
    # any client-supplied value; trust that. Never parse the client-controlled
    # X-Forwarded-For (its leftmost entry is attacker-supplied). Fall back to REMOTE_ADDR
    # only when X-Real-IP is absent.
    ip = normalize_ip(request.META.get('HTTP_X_REAL_IP'))
    if ip is None:
        ip = normalize_ip(request.META.get('REMOTE_ADDR'))
    return ip

def get_ip_sources(request):
    return objict({
        'x_forwarded_for': request.META.get('HTTP_X_FORWARDED_FOR'),
        'x_forwarded_proto': request.META.get('HTTP_X_FORWARDED_PROTO'),
        'x_forwarded_port': request.META.get('HTTP_X_FORWARDED_PORT'),
        'remote_addr': request.META.get('REMOTE_ADDR'),  # Will be ALB's IP
        'x_amzn_trace_id': request.META.get('HTTP_X_AMZN_TRACE_ID'),
    })

def get_device_id(request):
    # Look for 'buid' or 'duid' in GET parameters
    duid = request.META.get(DUID_HEADER, None)
    if duid:
        return duid

    for key in ['__buid__', 'duid', "buid"]:
        if key in request.GET:
            return request.GET[key]

    # Look for 'buid' or 'duid' in POST parameters
    for key in ['buid', 'duid']:
        if key in request.POST:
            return request.POST[key]

    return None

def get_user_agent(request):
    return request.META.get("HTTP_USER_AGENT", "")


def verify_signed_request(request, secret, header=None):
    """Verify an HMAC-SHA256 signature header on a Django request.

    Pulls raw `request.body` and the named header, then constant-time-compares
    against the expected HMAC of the body keyed on `secret`. Returns False
    (never raises) when:
        - secret is None / empty (Group has no webhook secret minted yet)
        - header is missing
        - signature does not match

    `header` defaults to the effective signature header name — X-Mojo-Signature
    unless the WEBHOOK_SIGNATURE_HEADER setting overrides it — so it stays in
    sync with the outbound send side. Pass an explicit `header` to override.

    Typical use after the view has resolved its own Group:

        if not verify_signed_request(request, group.get_webhook_secret()):
            raise merrors.PermissionDeniedException("invalid signature", 401, 401)
    """
    if not secret:
        return False
    if header is None:
        header = get_signature_header()
    meta_key = "HTTP_" + header.replace("-", "_").upper()
    sig = request.META.get(meta_key)
    if sig is None and hasattr(request, "headers"):
        sig = request.headers.get(header)
    if not sig:
        return False
    return verify_signature(request.body, sig, secret)


def parse_user_agent(text):
    """
    returns:
        {
          'user_agent': {
            'family': 'Mobile Safari',
            'major': '13',
            'minor': '5',
            'patch': None
          },
          'os': {
            'family': 'iOS',
            'major': '13',
            'minor': '5',
            'patch': None,
            'patch_minor': None
          },
          'device': {
            'family': 'iPhone',
            'brand': None,
            'model': None
          },
          'string': '...original UA string...'
        }
    """
    if not isinstance(text, str):
        text = get_user_agent(text)
    from ua_parser import user_agent_parser
    return objict.from_dict(user_agent_parser.Parse(text))
