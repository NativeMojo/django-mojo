"""
Cross-origin auth handoff token service.

A short-lived, single-use Redis token that lets an authenticated user on the
auth origin hand a JWT to a different-origin app, without putting the JWT in
the URL.

Token shape in Redis:
    key:   auth:handoff:<code>
    value: JSON { "uid": <user_id>, "ip": <issuing_ip>, "dest": <destination>,
                  "gid": <group_id or absent>,
                  "cc": <PKCE S256 challenge or absent> }
    TTL:   AUTH_HANDOFF_CODE_TTL seconds (default 60)

The destination is validated at ISSUANCE — see
`mojo.apps.account.services.redirect_allowlist`. What is stored here is an
audit record of where the code was minted for, never a second gate.

PKCE (RFC 7636) binds a code to the party that asked for it. A code minted with
a challenge (`cc`) exchanges only with the matching `code_verifier`, in every
mode. `AUTH_HANDOFF_REQUIRE_PKCE` decides only whether a code may be minted
WITHOUT one when it is going to an app on the device (a custom scheme, or a
loopback listener), where any other app can claim the same link.

What PKCE does NOT cover: a sign-in the hostile app starts itself. That app
supplies its own challenge and holds the verifier (RFC 8252 section 8.6). Only
the page asking the person first closes that, and that page is the consuming
project's.
"""
import ipaddress
import json
import re
import uuid
from urllib.parse import urlsplit

from mojo.helpers import logit
from mojo.helpers.redis import get_connection
from mojo.helpers.settings import settings

_KEY_PREFIX = "auth:handoff:"

PKCE_OFF = "off"
PKCE_NATIVE = "native"
_PKCE_MODES = (PKCE_OFF, PKCE_NATIVE)
_PKCE_RENOTIFY_SEC = 3600
# Distinct destinations one hour may report. The handoff endpoint is
# authenticated and rate-limited, but the destination is the caller's.
_PKCE_REPORT_BUDGET = 50


def get_ttl():
    """Return the configured handoff code TTL in seconds."""
    return settings.get("AUTH_HANDOFF_CODE_TTL", 60, kind="int")


def get_pkce_mode():
    """Return the normalized PKCE requirement: `off` or `native`.

    Read with `get_static`: a database row must not be able to switch a sign-in
    protection off. An unknown string is treated as `native`, because a typo in
    a security switch must not silently disable it.
    """
    raw = settings.get_static("AUTH_HANDOFF_REQUIRE_PKCE", PKCE_OFF)
    mode = str(raw or PKCE_OFF).strip().lower()
    if mode not in _PKCE_MODES:
        logit.error(
            "account.auth_handoff",
            f"AUTH_HANDOFF_REQUIRE_PKCE {raw!r} is not one of {_PKCE_MODES} — "
            f"treating it as {PKCE_NATIVE!r}")
        return PKCE_NATIVE
    return mode


_HOST_PLAIN_RE = re.compile(r"^[a-z0-9.\-]+$")
_HOST_NUMBER_RE = re.compile(r"^(?:[0-9]+|0x[0-9a-f]*)$")


def _parse_browser_ipv4(host):
    """The IPv4 address a browser turns `host` into, or None if it refuses it.

    A browser reads a host whose last label is a number as an IPv4 address and
    accepts short, integer, octal and hex spellings (WHATWG URL, "IPv4
    parser"): `127.1`, `2130706433`, `0x7f000001` and `0177.0.0.1` are all
    127.0.0.1. `ipaddress` rejects every one of them.
    """
    labels = host.split(".")
    if len(labels) > 4:
        return None
    numbers = []
    for label in labels:
        if not _HOST_NUMBER_RE.match(label):
            return None
        try:
            if label.startswith("0x"):
                numbers.append(int(label[2:] or "0", 16))
            elif len(label) > 1 and label.startswith("0"):
                numbers.append(int(label, 8))
            else:
                numbers.append(int(label, 10))
        except ValueError:
            return None
    if any(n > 255 for n in numbers[:-1]):
        return None
    last = numbers[-1]
    if last >= 256 ** (5 - len(numbers)):
        return None
    value = last
    for index, n in enumerate(numbers[:-1]):
        value += n * 256 ** (3 - index)
    return ipaddress.IPv4Address(value)


def _is_local_address(address):
    """Loopback, or the unspecified address, which reaches this machine too."""
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    return address.is_loopback or address.is_unspecified


def _is_loopback_host(host):
    """True when a browser would deliver `host` to this device, or when this
    function cannot tell: the unreadable case must not be the lenient one.

    A name that only RESOLVES to loopback (a public DNS record for 127.0.0.1)
    is not seen here; that needs the allowlist.
    """
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return True
    if host == "localhost" or host.endswith(".localhost"):
        return True
    if ":" in host:
        try:
            return _is_local_address(ipaddress.ip_address(host))
        except ValueError:
            return True
    # Percent-escapes and non-ASCII characters are folded by a browser before
    # it reads the host (`127.0.0.%31`, full-width digits). Not folded here.
    if not _HOST_PLAIN_RE.match(host):
        return True
    if _HOST_NUMBER_RE.match(host.rsplit(".", 1)[-1]):
        address = _parse_browser_ipv4(host)
        return True if address is None else _is_local_address(address)
    return False


def is_app_destination(destination):
    """True when a code for `destination` is delivered to an app on the device.

    False ONLY for http or https on a host that is not loopback. A custom
    scheme, `127.0.0.1` in any spelling a browser accepts, `[::1]`,
    `localhost`, an absent destination and one that does not parse are all
    True: each is somewhere another local program can also receive the code
    (RFC 8252 sections 7.1, 7.3 and 8.1), or somewhere this function cannot
    read, and the unreadable case must not be the lenient one.
    """
    from mojo.apps.account.services import redirect_allowlist
    if not destination or not isinstance(destination, str):
        return True
    # A browser reads a backslash as a slash in a web URL and urlsplit does
    # not, so the two can disagree on which part is the host.
    if "\\" in destination:
        return True
    try:
        parts = urlsplit(destination.strip())
        host = parts.hostname
    except ValueError:
        return True
    if (parts.scheme or "").lower() in ("http", "https") and _is_loopback_host(host):
        return True
    return redirect_allowlist.matchable_scheme(destination) not in ("http", "https")


def pkce_required(destination):
    """True when a code for `destination` may not be minted without a challenge."""
    return get_pkce_mode() == PKCE_NATIVE and is_app_destination(destination)


# "The request carried no such field", as distinct from a field sent as null.
NOT_SENT = object()


def check_exchange(data, code_verifier=NOT_SENT):
    """True when `code_verifier` is what the consumed record `data` calls for.

    Pass `NOT_SENT` when the request has no `code_verifier` field at all. A
    field that is present, whatever its value (null included), was sent.

    * no challenge stored, no verifier sent — a pre-PKCE client: True.
    * no challenge stored, a verifier sent — False. Accepting it would let an
      attacker mint a code for their OWN account and feed it to the real app,
      which believes it is completing the flow it started (RFC 9700 2.1.1).
    * a challenge stored — the verifier must be its S256 pre-image.

    Never raises: any non-string or malformed value is simply a failed check.
    """
    from mojo.apps.account.services.oauth_server import codes as oauth_codes
    challenge = data.get("cc") if isinstance(data, dict) else None
    if challenge is None:
        return code_verifier is NOT_SENT
    return oauth_codes.verify_pkce(challenge, code_verifier)


def _destination_host(destination):
    """The unit an operator would act on: the host of a web URL, the scheme of
    an app link (`myapp://auth` is `myapp`, whatever follows it)."""
    try:
        parts = urlsplit((destination or "").strip())
        scheme = (parts.scheme or "").lower()
        if scheme in ("http", "https"):
            return (parts.hostname or "").lower()
        return scheme
    except ValueError:
        return ""


def report_pkce_missing(destination, request=None, refused=False):
    """File a suppressed incident for a handoff to an app with no challenge.

    `refused=False` (mode `off`): the code was minted anyway. These incidents
    list who still signs in without a challenge, so the requirement can be
    turned on once the feed goes quiet. `refused=True` (mode `native`): no code
    was minted. One per destination per hour, budgeted; dropped when the
    suppression store is unreachable. Never raises.
    """
    from mojo.apps import incident
    host = _destination_host(destination) or "none"
    if refused:
        category, level = "auth:handoff_pkce_refused", 5
        title = f"Auth handoff refused, no PKCE challenge: {host}"
        body = (
            f"A handoff code for {destination!r:.200} was refused: "
            f"AUTH_HANDOFF_REQUIRE_PKCE is 'native' and the request carried no "
            f"code_challenge. The app that starts this sign-in must send one.")
    else:
        category, level = "auth:handoff_pkce_missing", 3
        title = f"Auth handoff to an app without a PKCE challenge: {host}"
        body = (
            f"A handoff code was minted for {destination!r:.200} with no "
            f"code_challenge. Any app on the device that claims the same link "
            f"can exchange a code it catches. Setting AUTH_HANDOFF_REQUIRE_PKCE "
            f"to 'native' would refuse this request.")
    # No `request=`: the reporter would store its query string, and a caller
    # may send the challenge there. Only these named facts are kept.
    incident.report_event_suppressed(
        body,
        key=host,
        title=title,
        category=category,
        level=level,
        scope="account",
        window=_PKCE_RENOTIFY_SEC,
        budget=_PKCE_REPORT_BUDGET,
        # The destination is the caller's: with Redis down, drop the event
        # rather than file one per request.
        fail_open=False,
        redirect_uri=str(destination or "")[:200],
        redirect_host=host,
        **request_facts(request))


def request_facts(request):
    """The facts of `request` a handoff incident keeps: who and from where.

    Deliberately NOT the query string or the body. Passing the request itself
    to the incident reporter stores `QUERY_STRING`, and both handoff endpoints
    accept their fields there: the code, the challenge and the verifier.
    """
    if request is None:
        return {}
    facts = {"source_ip": getattr(request, "ip", None)}
    try:
        facts["http_path"] = request.path
        facts["http_method"] = request.method
        facts["http_user_agent"] = request.META.get("HTTP_USER_AGENT", "")
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            facts["uid"] = user.id
    except Exception:
        pass
    return facts


def create_handoff_code(user, destination=None, ip=None, group_id=None, code_challenge=None):
    """
    Issue a short-lived handoff code for a fully authenticated user.

    Args:
        user:        User instance (must already have completed primary auth + any MFA).
        destination: The already-validated destination URL this code was minted
                     for. Recorded for audit only.
        ip:          Optional issuing IP for audit only — not enforced on consume.
        group_id:    Confine this code's delivery to one group — it exchanges
                     into a GroupScopedToken package instead of a JWT pair.
                     UNLIKE `destination` and `ip`, this IS enforced on consume.
        code_challenge: An already-validated PKCE S256 challenge. Enforced on
                     exchange by `check_exchange`.

    `gid` is the ONE encoding of the gating decision, and it is decided HERE,
    at issuance, from the server-validated destination — never re-derived at
    exchange. Re-resolving would let a resolver that breaks inside the code's
    TTL turn a gated code back into a platform JWT. A code minted before a mode
    flip is therefore honored under the decision that was taken when it was
    minted, in both directions, for at most AUTH_HANDOFF_CODE_TTL seconds. A
    code minted before this feature existed simply has no `gid` key.

    Returns:
        code string (32 hex chars).

    NEITHER `destination` NOR `ip` IS ENFORCED ON CONSUME, and deliberately so.
    `POST /api/auth/exchange` is called by the consuming app's own backend, which
    chooses its own source IP and its own headers; an attacker holding the code
    holds those too, so a consume-time comparison would reject honest callers
    behind a different egress while stopping nobody. The gate that matters runs
    before this function is ever reached — the caller must have checked the
    destination with `redirect_allowlist.is_allowed_destination()`. What is
    stored here answers "where was this code sent?" after the fact.
    """
    code = uuid.uuid4().hex
    payload = {"uid": user.id, "ip": ip or "", "dest": destination or ""}
    if group_id:
        payload["gid"] = int(group_id)
    if code_challenge:
        payload["cc"] = code_challenge
    data = json.dumps(payload)
    get_connection().setex(f"{_KEY_PREFIX}{code}", get_ttl(), data)
    return code


def consume_handoff_code(code):
    """
    Validate and consume (delete) a handoff code.

    Returns the stored data dict on success, None if invalid/expired.
    Single-use — atomic GETDEL guarantees only one concurrent caller wins.
    """
    if not code or not isinstance(code, str) or len(code) != 32 or not code.isalnum():
        return None
    raw = get_connection().getdel(f"{_KEY_PREFIX}{code}")
    if not raw:
        return None
    return json.loads(raw)
