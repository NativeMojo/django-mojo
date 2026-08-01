from urllib.parse import urlparse

from django.http import HttpResponse
from mojo.helpers.settings import settings

DUID_HEADER = settings.get_static('DUID_HEADER', 'X-Mojo-UID')

_BOUNCER_PREFIXES = (
    '/api/account/bouncer/',
    '/account/static/mojo-',
)

# The public, unauthenticated bouncer API. Deliberately NARROWER than
# _BOUNCER_PREFIXES, because BOUNCER_ALLOW_ANY_ORIGIN must be strictly narrower
# than an operator-curated allowlist:
#   - device / signal / signature are permission-gated admin endpoints that
#     return device fingerprints, IPs, muids and geo (rest/bouncer_admin.py).
#   - verify_pass is the sole carrier of X-Bouncer-Muid (a stable device id) and
#     exists for nginx auth_request — server-to-server, which ignores CORS. No
#     browser client calls it, so excluding it costs the feature nothing.
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

    Never settings.get(): that consults the DB/Redis-backed Setting store, which
    is writable over REST and group-scopable. A database row must not be able to
    flip a CORS bypass. kind='bool' is the framework's fail-closed coercion — an
    unrecognized string degrades to the declared default with a warning instead
    of failing open the way a bare bool() would.
    """
    try:
        return bool(settings.get_static('BOUNCER_ALLOW_ANY_ORIGIN', False, kind='bool'))
    except Exception:
        return False


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

    The allowlist is tested FIRST and unconditionally, so a deployment that has
    not set BOUNCER_ALLOW_ANY_ORIGIN behaves exactly as it did before the flag
    existed, and an allowlist entry still wins when the flag is on.

    When the flag is on and `allow_any` is True — public bouncer API paths only —
    any well-formed http(s) origin is echoed with Access-Control-Allow-Credentials.
    That gives up origin attribution on those endpoints: every website a visitor
    loads can drive them inside that visitor's browser. It exists for deployments
    whose caller domains are not knowable at deploy time and which validate the
    calling domain themselves.
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
        # cannot send cookies with Allow-Origin: *), but only if the request
        # Origin is on the BOUNCER_ALLOWED_ORIGINS allowlist — or, when
        # BOUNCER_ALLOW_ANY_ORIGIN is enabled, if the path is one of the public
        # bouncer API endpoints. Non-allowlisted cross-origin requests still get
        # the wildcard fallback below, which blocks credentialed flows at the
        # browser but keeps non-credentialed API use working.
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
