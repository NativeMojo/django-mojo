import ipaddress

from objict import objict, nobjict
from .request_parser import RequestDataParser
from mojo.helpers.settings import settings
from mojo.helpers.crypto.sign import verify_signature, get_signature_header

DUID_HEADER = settings.get_static('DUID_HEADER', 'X-Mojo-UID').replace('-', '_').upper()
DUID_HEADER = f"HTTP_{DUID_HEADER}"

REQUEST_PARSER = RequestDataParser()

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


def is_key_backed_session(request):
    # True when this request authenticated with an ApiKey — REGARDLESS of whose
    # identity that key acts as. An ApiKey with override_user=True puts a real
    # User in request.user, so the type of request.user stops being a reliable
    # "is a human driving this" signal; request.api_key is the one that keeps
    # telling the truth. The credential is a bearer token sitting in a config
    # file, not a person at a keyboard.
    #
    # Use this — never is_request_user() — for any check that means "this is a
    # machine" (machine-identity gates, credential-mutation blocks). Use
    # is_request_user() only where the question is genuinely "is request.user a
    # User model instance" (attribution).
    return getattr(request, "api_key", None) is not None


def is_override_user_session(request):
    # True only for an ApiKey that ASSUMES a member (ApiKey.override_user), i.e.
    # the case where request.user is a real User whose GLOBAL permission dict
    # would otherwise be consulted.
    #
    # The distinction from is_key_backed_session matters: for an unlinked or
    # reference-mode key, request.user IS the ApiKey, so `request.user.
    # has_permission(...)` reads the KEY's own dict — which is correct and
    # already bounded to the key's group. Only in override mode does that read
    # resolve to a member's untenanted platform-wide grants.
    api_key = getattr(request, "api_key", None)
    return api_key is not None and bool(getattr(api_key, "override_user", False))


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
