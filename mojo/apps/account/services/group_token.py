"""
Group-scoped tokens — a bearer that authenticates as a real user but whose
authority is capped at ONE group.

    Authorization: grouptoken gt1.<b64payload>.<sig>

Why this exists: the only bearer that authenticates as a person today is a
platform JWT, which grants everything that account can reach in every group it
belongs to. Handing one to JavaScript on a page whose content a tenant controls
means the tenant receives a platform credential for every visitor who signs in.
A group token authenticates as the visitor and resolves authorization ONLY
through that user's membership in the signed group — no other group, no
descendants, no platform-global grants.

Deliberately NOT a JWT. A JWT signed with `user.get_auth_key()` would be
accepted verbatim by `User.validate_jwt` under `Authorization: bearer`, and by
`on_refresh_token` as a `refresh_token` — trading the scoped token for a full
unscoped pair. `jwt.decode` cannot parse the `gt1.` format at all, so
"a group token cannot be upgraded into a JWT" holds by construction.

Stateless: no table, no row per token. Revocation has two levers:

  * per-group — bump `Group.get_group_token_epoch()` (the `revoke_group_tokens`
    POST_SAVE_ACTION, or `group.bump_group_token_epoch()`), which invalidates
    every outstanding token for that group;
  * per-user — rotate the user's `auth_key` (what `POST /api/auth/sessions/revoke`
    already does), which invalidates that user's tokens everywhere.

Minting is SERVICE-LEVEL ONLY. No REST endpoint mints one and no login/handoff
flow selects one; an in-process caller on a platform origin calls `mint()`
directly.

Registration is opt-in per deployment (see D6 in the docs) — both entries are
required, and both settings replace their defaults wholesale:

    AUTH_BEARER_HANDLERS = {
        "grouptoken": "mojo.apps.account.services.group_token.validate_token"}
    AUTH_BEARER_NAME_MAP = {
        "bearer": "user", "apikey": "user", "grouptoken": "user"}
"""
from mojo import errors as merrors
from mojo.helpers import crypto, dates, logit
from mojo.helpers.settings import settings


# Version tag. It is signed AS PART OF the HMAC'd string ("gt1." + payload),
# not merely prefixed to the wire format: mojo/apps/account/utils/tokens.py
# signs a bare b64 payload with the SAME per-user key, so without this domain
# separation a payload that decodes under both schemes could carry a signature
# across families.
TOKEN_VERSION = "gt1"

# Clock-skew tolerance for `iat`. A module constant, not a setting — one number
# nobody needs to tune (KISS).
_MAX_FUTURE_SKEW = 60

# ONE failure string for every refusal except expiry. Distinct messages would
# turn this into an account/group-state oracle for anyone holding a token (the
# same reasoning as User.validate_jwt and ApiKey.validate_token).
INVALID_TOKEN = "Invalid group token"
EXPIRED_TOKEN = "Group token expired"


def get_ttl():
    """Return the group-token lifetime in seconds.

    Public because the handoff delivery path reports it to the destination app
    as `expires_in` (mirrors `auth_handoff.get_ttl()`).
    """
    return settings.get_static("GROUP_TOKEN_TTL", 3600, kind="int")


class GroupScopedToken:
    """The identity object a validated group token puts on the request.

    NOT a model — never saved, no pk. It duck-types exactly the surface the
    framework's guards read off `request.api_key`, so the single
    `restricted_identity()` predicate covers both credential kinds with no
    call-site special-casing.

    The two members that carry the whole confinement:

      * `is_group_allowed` — STRICT equality with the signed group. Unlike an
        ApiKey, descendants are NOT included: a token minted for a gated site
        must not reach that site's sub-tenants.
      * `has_permission` — always False. The token never satisfies a grant on
        its own; authority comes exclusively from the user's GroupMember row in
        the signed group, resolved with check_user=False so the member's
        untenanted platform-wide dict is never consulted.
    """

    def __init__(self, user, group):
        self.user = user
        self.group = group
        self.group_id = group.pk
        # Reads as an ApiKey that ASSUMES a member: request.user is a real User
        # whose GLOBAL permission dict must never be consulted. Every guard that
        # keys on is_override_user_session() needs this True.
        self.override_user = True
        self.pk = None
        self.id = None
        self.is_active = True
        self.limits = {}

    def __str__(self):
        return f"grouptoken:{self.user_id}@{self.group_id}"

    @property
    def user_id(self):
        return self.user.pk

    @property
    def is_superuser(self):
        return False

    def is_group_allowed(self, group):
        """The signed group and nothing else — no descendants (see class doc)."""
        if group is None or not group.is_effectively_active():
            return False
        return group.pk == self.group_id

    def has_permission(self, perms):
        """A group token never satisfies a grant by itself."""
        return False

    def get_groups(self, is_active=True, include_children=False):
        from mojo.apps.account.models import Group
        if not self.group.is_effectively_active():
            return Group.objects.none()
        return Group.objects.filter(pk=self.group_id)

    def get_groups_with_permission(self, perms, is_active=True):
        """The signed group iff the user holds `perms` AS A MEMBER of it.

        check_user=False mirrors the detail path exactly — a global grant must
        not widen a group token's list results.
        """
        from mojo.apps.account.models import Group
        if not self.group.is_effectively_active():
            return Group.objects.none()
        if not self.group.user_has_permission(self.user, perms, False):
            return Group.objects.none()
        return Group.objects.filter(pk=self.group_id)


def can_mint(user, group):
    """Would `mint(user, group)` be permitted? READ-ONLY — no writes, no token.

    The guard list lives here, and `mint` is its only other caller, so a
    pre-flight can never drift from the real bar. Read-only matters: `mint`
    signs with `user.get_auth_key()`, which lazily generates and SAVES an
    auth_key, so "call mint and discard the token" is not side-effect free.

    Guards mirror ApiKey.validate_acting_user:
      * never a superuser — a bearer token in a browser must not be a route to
        platform-wide authority;
      * the user is active and the group is EFFECTIVELY active (it and every
        ancestor);
      * DIRECT active membership (check_parents=False) — delegation must not
        exceed the delegator, so a member of the parent gets no token for a
        child group.
    """
    if user is None or group is None:
        return False
    if getattr(user, "is_superuser", False):
        return False
    if not getattr(user, "is_active", False):
        return False
    if not group.is_effectively_active():
        return False
    if group.get_member_for_user(user, check_parents=False) is None:
        return False
    return True


def mint(user, group, issued_at=None):
    """Issue a group token for `user` confined to `group`. Raises on refusal.

    The refusal bar is `can_mint()` — see it for the guards and why they are
    those. ONE bare PermissionDeniedException for every refusal: a caller that
    could tell "not a member" from "group is dark" holds an account/group-state
    oracle.

    `issued_at` (unix seconds) is for tests and for callers that need a
    deterministic clock; leave it None in production.
    """
    if not can_mint(user, group):
        raise merrors.PermissionDeniedException()

    if issued_at is None:
        issued_at = int(dates.utcnow().timestamp())
    payload = {
        "u": int(user.pk),
        "g": int(group.pk),
        "e": int(group.get_group_token_epoch()),
        "iat": int(issued_at),
    }
    body = crypto.b64_encode(payload)
    signed = f"{TOKEN_VERSION}.{body}"
    return f"{signed}.{crypto.sign(signed, user.get_auth_key())}"


def _payload_int(payload, key):
    value = payload.get(key)
    # bool is a subclass of int in Python — reject it explicitly.
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return value


def validate_token(token, request=None):
    """AUTH_BEARER_HANDLERS handler. Returns (User, None) or (None, error).

    The middleware calls this bare (mojo/middleware/auth.py), so EVERY failure
    mode has to come back as a tuple: `b64_decode` raises binascii/Unicode/JSON
    errors on garbage, and `hmac.compare_digest` raises TypeError on a
    non-ASCII signature segment, which a latin-1-decoded HTTP_AUTHORIZATION
    header can carry. Malformed input must 401, never 500.
    """
    try:
        return _validate_token(token, request)
    except Exception:
        logit.exception("group token validation failed")
        return None, INVALID_TOKEN


def _validate_token(token, request):
    from mojo.apps.account.models import Group, User

    # WebSocket auth is refused: mojo/apps/realtime/auth.py calls handlers with
    # request=None, and this credential has no realtime story yet. Fail closed
    # and early rather than raising on the request writes below.
    if request is None:
        return None, INVALID_TOKEN

    if not token or not isinstance(token, str):
        return None, INVALID_TOKEN
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != TOKEN_VERSION:
        return None, INVALID_TOKEN

    payload = crypto.b64_decode(parts[1])
    if not isinstance(payload, dict):
        return None, INVALID_TOKEN
    user_id = _payload_int(payload, "u")
    group_id = _payload_int(payload, "g")
    epoch = _payload_int(payload, "e")
    issued_at = _payload_int(payload, "iat")
    if None in (user_id, group_id, epoch, issued_at):
        return None, INVALID_TOKEN

    user = User.objects.filter(pk=user_id, is_active=True).first()
    if user is None:
        return None, INVALID_TOKEN

    # Signature over "gt1.<payload>" — see TOKEN_VERSION. Per-user key, so a
    # token forged against user A's key cannot be replayed as user B, and
    # rotating auth_key revokes every token that user holds.
    if not crypto.verify(f"{TOKEN_VERSION}.{parts[1]}", parts[2], user.get_auth_key()):
        return None, INVALID_TOKEN

    now = int(dates.utcnow().timestamp())
    if now - issued_at > get_ttl():
        # The ONE distinct message: a client that can tell "expired" from
        # "rejected" can re-mint instead of prompting a full re-auth, and
        # expiry discloses nothing about account or group state.
        return None, EXPIRED_TOKEN
    if issued_at - now > _MAX_FUTURE_SKEW:
        return None, INVALID_TOKEN

    # Re-assert the superuser bar at AUTH time, not only at mint time: a user
    # can be promoted after a token was issued, and User.has_permission returns
    # True for everything once is_superuser is set (mirrors ApiKey.validate_token).
    if user.is_superuser:
        logit.error(
            f"group token for user {user.pk} refused: user is a superuser")
        return None, INVALID_TOKEN

    group = Group.objects.filter(pk=group_id).first()
    if group is None or not group.is_effectively_active():
        return None, INVALID_TOKEN

    if epoch != group.get_group_token_epoch():
        return None, INVALID_TOKEN

    # Direct active membership only — same bar as mint(), re-checked every
    # request so removing the GroupMember row revokes immediately.
    if group.get_member_for_user(user, check_parents=False) is None:
        return None, INVALID_TOKEN

    request.group = group
    request.group_token = GroupScopedToken(user, group)
    # Attribution identity, same contract as ApiKey.validate_token.
    request.acting_user = user
    return user, None
