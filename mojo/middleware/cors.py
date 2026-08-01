from urllib.parse import urlparse

from django.http import HttpResponse
from mojo.helpers.settings import settings

DUID_HEADER = settings.get_static('DUID_HEADER', 'X-Mojo-UID')

_BOUNCER_PREFIXES = (
    '/api/account/bouncer/',
    '/account/static/mojo-',
)

# The public, unauthenticated bouncer API — the paths the any-origin default
# covers. Deliberately NARROWER than _BOUNCER_PREFIXES:
#   - device / signal / signature are permission-gated admin endpoints that
#     return device fingerprints, IPs, muids and geo (rest/bouncer_admin.py).
#   - verify_pass is the sole carrier of X-Bouncer-Muid (a stable device id) and
#     exists for nginx auth_request — server-to-server, which ignores CORS. No
#     browser client calls it, so excluding it costs the feature nothing.
# Neither exclusion restricts a legitimate third-party caller of the public API.
_BOUNCER_PUBLIC_ENDPOINTS = frozenset(('assess', 'event', 'message'))

# Never echo a header value carrying these. Django raises BadHeaderError on
# CR/LF, which from inside middleware would surface as an unhandled 500 rather
# than a graceful deny.
_ORIGIN_BAD_CHARS = ('\r', '\n', '\t', ' ')


def _is_bouncer_path(path):
    for prefix in _BOUNCER_PREFIXES:
        if path.startswith(prefix):
            return True
    return False


def _is_bouncer_public_path(path):
    """True only for the public bouncer API endpoints.

    Derived from the prefix plus a segment set rather than hardcoded absolute
    paths: MOJO_APPEND_SLASH appends a trailing slash to every registered route
    (mojo/decorators/http.py), under which an exact-match tuple would silently
    never match and the bypass would become a no-op with no error and no log.
    """
    prefix = _BOUNCER_PREFIXES[0]
    if not path.startswith(prefix):
        return False
    return path[len(prefix):].strip('/') in _BOUNCER_PUBLIC_ENDPOINTS


def _allow_any_origin():
    """Read BOUNCER_ALLOW_ANY_ORIGIN from file-based settings only.

    Defaults to True. This is an open REST API platform: third-party callers are
    the product, so credentialed cross-origin access to the public bouncer API is
    on unless an operator opts OUT. Setting it False is the restriction; leaving
    it alone is not a grant of anything new.

    Never settings.get(): that consults the DB/Redis-backed Setting store, which
    is writable over REST and group-scopable. A database row must not be able to
    flip the origin policy either way. kind='bool' degrades an unrecognized
    string to the DECLARED default — now True — with a coercion warning.

    The except returns True for the same reason: an exception reading a setting
    must not silently re-impose a restriction the operator never asked for.
    """
    try:
        return bool(settings.get_static('BOUNCER_ALLOW_ANY_ORIGIN', True, kind='bool'))
    except Exception:
        return True


def _is_echoable_origin(origin):
    """True if an arbitrary request Origin is safe to echo back.

    Applies ONLY to the BOUNCER_ALLOW_ANY_ORIGIN echo branch. Allowlisted
    origins are matched by exact string and never reach here, so operator
    entries such as 'chrome-extension://...' or 'capacitor://localhost' keep
    working exactly as before.
    """
    # Sandboxed iframes, data: documents and file:// pages send `Origin: null`.
    # Echoing that with credentials grants an unattributable context the same
    # access as a named one.
    if origin == 'null':
        return False
    for char in _ORIGIN_BAD_CHARS:
        if char in origin:
            return False
    try:
        parts = urlparse(origin)
    except Exception:
        return False
    if parts.scheme not in ('http', 'https'):
        return False
    if not parts.netloc:
        return False
    if parts.path or parts.params or parts.query or parts.fragment:
        return False
    return True


def _credentialed_origin(request, allow_any=False):
    """Return the request Origin when it may be echoed with credentials, else ''.

    The allowlist is tested FIRST and unconditionally. That ordering is what makes
    BOUNCER_ALLOWED_ORIGINS the opt-out mechanism: it still wins by exact string,
    so entries that are not well-formed http(s) origins ('chrome-extension://…',
    'capacitor://localhost') keep working regardless of the flag.

    By default (`BOUNCER_ALLOW_ANY_ORIGIN` unset → True) any well-formed http(s)
    origin is echoed with Access-Control-Allow-Credentials on the public bouncer
    API paths — `allow_any` is True only for those. Set the flag False to restrict
    those endpoints to BOUNCER_ALLOWED_ORIGINS.

    Note what this header does and does not carry. mojo REST auth is
    Authorization-header based and every mojo cookie is SameSite=Lax, so the
    credentials this permits are not ambient session authority; the public
    endpoints are already reachable cross-origin through the '*' fallback below.
    """
    origin = request.META.get('HTTP_ORIGIN', '')
    if not origin:
        return ''
    allowed = settings.get_static('BOUNCER_ALLOWED_ORIGINS') or []
    if origin in allowed:
        return origin
    if allow_any and _allow_any_origin() and _is_echoable_origin(origin):
        return origin
    return ''


# middleware/cors.py
class CORSMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Handle preflight requests
        if request.method == 'OPTIONS':
            response = HttpResponse()
        else:
            response = self.get_response(request)

        # Bouncer paths: credentialed CORS with specific Origin (browsers
        # cannot send cookies with Allow-Origin: *). Granted when the request
        # Origin is on the BOUNCER_ALLOWED_ORIGINS allowlist, or — by default,
        # unless BOUNCER_ALLOW_ANY_ORIGIN is set False — for any well-formed
        # origin on the public bouncer API endpoints. Anything still not granted
        # falls through to the wildcard below, which blocks credentialed flows at
        # the browser but keeps non-credentialed API use working.
        #
        # The public-path test is PATH-based only, never method-based, so the
        # OPTIONS short-circuit above and the real request take the identical
        # decision. A preflight can never promise credentials the actual
        # response then withholds.
        if _is_bouncer_path(request.path):
            origin = _credentialed_origin(
                request, allow_any=_is_bouncer_public_path(request.path))
            if origin:
                response['Access-Control-Allow-Origin'] = origin
                response['Access-Control-Allow-Credentials'] = 'true'
                response['Vary'] = 'Origin'

        # Default wildcard origin for any response the bouncer block didn't set
        if 'Access-Control-Allow-Origin' not in response:
            response['Access-Control-Allow-Origin'] = '*'

        # Allow all methods to minimize preflight requests
        response['Access-Control-Allow-Methods'] = 'GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS'

        # Allow common headers to minimize preflight requests
        response['Access-Control-Allow-Headers'] = (
            'Accept, Accept-Encoding, Authorization, Content-Type, '
            'Origin, User-Agent, X-Requested-With, X-CSRFToken, '
            f'X-API-Key, {DUID_HEADER}, Cache-Control, Pragma'
        )

        # Long preflight cache (24 hours)
        response['Access-Control-Max-Age'] = '86400'

        # Expose headers that frontend might need
        response['Access-Control-Expose-Headers'] = (
            'Content-Disposition, X-Total-Count, X-Bouncer-Muid, X-Bouncer-Reason'
        )

        return response
