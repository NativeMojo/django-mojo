"""
Gated auth-handoff destinations — a destination host the DEPLOYMENT has
declared gated receives a GroupScopedToken package instead of a platform JWT.

The property under test, stated honestly: a gated destination host never
receives a platform JWT through a mojo-hosted auth flow. Two surfaces answer
that differently and both are covered here — the handoff/exchange leg DELIVERS
a scoped token, and the OAuth completion leg REFUSES.

Sections:
  A  resolution + the deny-rule matcher (in-process, no server reloads)
  B  endpoints, gating enforce + allowlist enforced   (reload block 1)
  C  endpoints, gating monitor                        (reload block 2)
  D  endpoints, gating enforce WITHOUT allowlist      (reload block 3)
  E  endpoints, shipped defaults — the upgrade-safety test (no reload)
  F  mode-flip window and corrupt `gid`
  G  the negative-delivery class: a JWT for a gated host is a bug
  H  the OAuth leg
  I  the client contract (source assertions — no JS runtime in the suite)

Everything defaults OFF: AUTH_HANDOFF_GROUP_TOKEN_MODE is "off" unless a
deployment sets it, and section E pins that a default deployment is unchanged.
"""
import contextlib

from testit import helpers as th
from testit.helpers import assert_true, assert_eq


def assert_false(value, msg):
    """Local mirror of assert_true — most of this module is "must be refused",
    and `assert_true(not x, ...)` reads backwards for every one of them.
    testit.helpers has no assert_false today (same helper as handoff.py)."""
    assert not value, msg


MEMBER = "hgt_member"
OUTSIDER = "hgt_outsider"
SUPERUSER = "hgt_super"
TEMP = "hgt_temp"
PWORD = "hgt##mojo99"

GATED_HOST = "gated.hgt.example.net"
UNGATED_HOST = "plain.hgt.example.net"

# ONE gating entry — a bare host — covers every one of these. Each pair is a
# bypass that the destination allowlist's own matcher would have let through as
# an UNGATED destination, minting a plain JWT whose code lands right back on
# the gated origin.
BLOCK1_GATED_DESTS = [
    (f"https://{GATED_HOST}/", "the exact host"),
    (f"http://{GATED_HOST}/", "an http:// destination under a scheme-less entry"),
    (f"https://{GATED_HOST}:8443/", "a non-default port"),
    (f"https://{GATED_HOST}./", "a trailing dot"),
    (f"https://deep.sub.{GATED_HOST}/", "two extra labels of subdomain depth"),
    (f"HTTPS://{GATED_HOST.upper()}/", "an uppercase scheme and host"),
    (f"https://{GATED_HOST}/deep/path?q=1", "a path and query under the host"),
]

UNGATED_DEST = f"https://{UNGATED_HOST}/"

# Module-level state the resolver fixtures read. They are addressed by the name
# testit loads this module under (tests/ is on sys.path), so load_function()
# resolves the SAME module object — the precedent is handoff.py's resolvers.
_STATE = {}














def _data(resp):
    """The `data` dict of a response body, or {} when there isn't one."""
    body = resp.response
    if not isinstance(body, dict):
        return {}
    data = body.get("data")
    return data if isinstance(data, dict) else {}


def _mint(opts, destination, **kwargs):
    """Mint a handoff code for `destination`; returns the response."""
    return opts.client.post("/api/auth/handoff", {"redirect_uri": destination}, **kwargs)


def _clear_limits():
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")
    for key in ("auth_handoff", "auth_exchange", "login", "refresh_token"):
        clear_rate_limits(ip="127.0.0.1", key=key)


@th.django_unit_setup()
def setup_gating(opts):
    from mojo.apps.account.models import User, Group

    _clear_limits()

    # Long-lived DB: delete before creating. A `hgt_` prefix keeps this module
    # clear of tests/test_auth/group_token.py, whose setup deletes every `gt_`
    # user and group and which can run in a sibling thread.
    User.objects.filter(username__startswith="hgt_").delete()
    Group.objects.filter(name__startswith="hgt_").delete()

    group_a = Group.objects.create(name="hgt_tenant_a", kind="organization")
    child_a = Group.objects.create(name="hgt_child_a", kind="organization",
                                   parent=group_a)
    inactive = Group.objects.create(name="hgt_inactive", kind="organization",
                                    is_active=False)
    dark_parent = Group.objects.create(name="hgt_dark_parent", kind="organization",
                                       is_active=False)
    dark_child = Group.objects.create(name="hgt_dark_child", kind="organization",
                                      parent=dark_parent)

    def _user(username, **kwargs):
        user = User(username=username, email=f"{username}@example.com",
                    display_name=username, **kwargs)
        user.save()
        user.is_email_verified = True
        user.is_active = True
        user.save_password(PWORD)
        user.save()
        return user

    member = _user(MEMBER)
    outsider = _user(OUTSIDER)
    superu = _user(SUPERUSER, is_superuser=True)
    temp = _user(TEMP)

    group_a.add_member(member)
    group_a.add_member(superu)
    group_a.add_member(temp)

    opts.group_a_id = group_a.pk
    opts.group_a_uuid = group_a.get_uuid()
    opts.child_a_id = child_a.pk
    opts.child_a_uuid = child_a.get_uuid()
    opts.inactive_uuid = inactive.get_uuid()
    opts.dark_child_uuid = dark_child.get_uuid()
    opts.member_id = member.pk
    opts.outsider_id = outsider.pk
    opts.super_id = superu.pk
    opts.temp_id = temp.pk
    _STATE["group_a_id"] = group_a.pk


# ---------------------------------------------------------------------------
# A. Resolution and matching — in-process, no server reloads.
# The _gating-driven cluster moved to tests/test_auth_extended_serial/
# handoff_group_token.py (maestro item #1839): _gating mutates
# django.conf.settings process-wide.
#
# The C2 bypass class lives here. Every "must gate" row below is a destination
# that `redirect_allowlist`'s own matcher would treat as a DIFFERENT origin
# from the entry — correct for an allow rule, a live bypass for a deny rule.
# ---------------------------------------------------------------------------





















@th.django_unit_test("gating: can_deliver is the mint bar, and nothing wider")
def test_can_deliver_is_the_mint_bar(opts):
    from mojo.apps.account.models import User, Group
    from mojo.apps.account.services import handoff_group as hg

    group_a = Group.objects.get(pk=opts.group_a_id)
    child_a = Group.objects.get(pk=opts.child_a_id)

    assert_true(hg.can_deliver(User.objects.get(pk=opts.member_id), group_a),
                "a direct active member must be deliverable")
    assert_false(hg.can_deliver(User.objects.get(pk=opts.outsider_id), group_a),
                 "a non-member must not be deliverable")
    assert_false(hg.can_deliver(User.objects.get(pk=opts.super_id), group_a),
                 "a superuser must NEVER hold a group token, membership or not "
                 "— a browser bearer must not be a route to platform authority")
    assert_false(hg.can_deliver(User.objects.get(pk=opts.member_id), child_a),
                 "membership in the PARENT must not deliver a token for a child "
                 "group — delegation must not exceed the delegator")
    assert_false(hg.can_deliver(None, group_a), "a missing user must not deliver")
    assert_false(hg.can_deliver(User.objects.get(pk=opts.member_id), None),
                 "a missing group must not deliver")


@th.django_unit_test("gating: an auth-page origin in the gating map is detectable (C9)")
def test_own_host_detection(opts):
    from objict import objict
    from mojo.apps.account.services import handoff_group as hg

    request = objict(META={"HTTP_HOST": f"{GATED_HOST}:8443"})
    assert_true(hg.is_own_host(request, f"https://{GATED_HOST}/app"),
                "a gated destination on the very host serving this request is a "
                "configuration error — the page short-circuits a same-origin "
                "redirect to direct navigation and the JWT is already there")
    assert_false(hg.is_own_host(request, f"https://other.{GATED_HOST}/app"),
                 "a different host must not be mistaken for the auth origin")
    assert_false(hg.is_own_host(objict(META={}), f"https://{GATED_HOST}/"),
                 "no HTTP_HOST means no same-origin claim can be made")

    # The same-origin comparison must NOT inherit the entry-shape test. An
    # auth origin on an IP literal or a single label can never appear in the
    # gating map, but it is still unmistakably itself — applying the shape
    # test here would refuse every OAuth begin on such a deployment.
    local = objict(META={"HTTP_HOST": "127.0.0.1:5555"})
    assert_true(hg.is_own_host(local, "http://127.0.0.1:5555/auth/oauth/google/complete"),
                "an IP-literal auth origin must still recognise itself")
    assert_false(hg.is_own_host(local, f"https://{GATED_HOST}/app"),
                 "an IP-literal auth origin must not claim a foreign host")
    assert_false(hg.is_own_host(local, "https://127.0.0.1\\@evil.tld/"),
                 "the backslash parser differential must hold for the "
                 "same-origin comparison too, or a crafted URL claims to be "
                 "the auth origin while the browser navigates elsewhere")


# ---------------------------------------------------------------------------
# E. Endpoints — the shipped defaults. No reload, no fixture, on purpose.
#
# The endpoint gating blocks (enforce, monitor, missing-prerequisite, and the
# mode-flip/corrupt-gid test) need th.server_settings() reloads, so they moved
# to tests/test_auth_extended_serial/handoff_group_token.py (maestro #2791).
# ---------------------------------------------------------------------------

@th.unit_test("shipped default: a would-be-gated host still gets a full JWT pair")
def test_defaults_are_untouched(opts):
    """THE upgrade-safety test.

    It runs against the test server's default state deliberately: no
    server_settings, nothing configured. A deployment that upgrades without
    setting AUTH_HANDOFF_GROUP_TOKEN_MODE must behave byte-for-byte as it does
    today, including for the very host the other tests gate.
    """
    _clear_limits()
    assert_true(opts.client.login(MEMBER, PWORD), "member login should succeed")
    resp = _mint(opts, f"https://{GATED_HOST}/")
    assert_eq(resp.status_code, 200,
              f"the shipped default must still mint for any destination, got "
              f"{resp.status_code}: {resp.response}")
    code = _data(resp).get("code")
    assert_true(bool(code), f"no code minted: {resp.response}")
    opts.client.logout()

    resp = opts.client.post("/api/auth/exchange", {"code": code})
    assert_eq(resp.status_code, 200,
              f"the shipped default must still exchange, got {resp.status_code}: "
              f"{resp.response}")
    data = _data(resp)
    assert_true(bool(data.get("refresh_token")),
                f"with gating off, EVERY destination receives the full JWT pair "
                f"it receives today. Keys: {sorted(data.keys())}")
    assert_false(str(data.get("access_token") or "").startswith("gt1."),
                 f"nothing may be gated while the mode is off, got "
                 f"{data.get('access_token')!r}")
    assert_false("token_type" in data,
                 f"the JWT response shape must not change: {sorted(data.keys())}")


# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# I. Client contract
#
# The sinks are client-side JS and there is no JS runtime in the suite, so the
# source is what is checkable and what actually regresses (same precedent as
# the ?back= / ?redirect= scheme-guard tests in tests/test_auth/handoff.py).
# Every assertion below is a rule that, if broken, upgrades a group-scoped
# session back into a platform JWT in the browser.
# ---------------------------------------------------------------------------

def _read(*parts):
    from pathlib import Path
    import mojo
    path = Path(mojo.__file__).resolve().parent.joinpath(*parts)
    assert_true(path.exists(), f"expected {path} to exist")
    return path.read_text(encoding="utf-8")


def _block(src, start, end, what):
    """Return the source between `start` and the next `end` after it."""
    head = src.split(start, 1)
    assert_eq(len(head), 2, f"{what}: could not find {start!r} in the source")
    body = head[1].split(end, 1)
    assert_eq(len(body), 2, f"{what}: could not find the end marker {end!r}")
    return body[0]


def _js():
    return _read("apps", "account", "static", "account", "mojo-auth.js")


def _tpl():
    return _read("apps", "account", "templates", "account", "auth_base.html")


@th.django_unit_test("mojo-auth.js: saveTokens CLEARS a refresh token the response omits")
def test_js_savetokens_clears_refresh_on_absence(opts):
    src = _js()
    body = _block(src, "function saveTokens(", "\n    }", "saveTokens")

    assert_true("localStorage.setItem(KEYS.refresh" in body,
                "saveTokens must still store a refresh token when one is sent")
    assert_true("localStorage.removeItem(KEYS.refresh" in body,
                "saveTokens must CLEAR the stored refresh token when the response "
                "carries none — otherwise a group-scoped session inherits the "
                "previous JWT session's refresh token, and any later "
                "refreshToken() trades it for a full platform pair")


@th.django_unit_test("mojo-auth.js: saveTokens records the token kind and its expiry")
def test_js_savetokens_records_kind_and_expiry(opts):
    src = _js()
    keys = _block(src, "var KEYS = {", "};", "KEYS")
    assert_true("'token_type'" in keys,
                f"KEYS must declare token_type, got: {keys!r}")
    assert_true("'token_expires_at'" in keys,
                f"KEYS must declare token_expires_at, got: {keys!r}")

    body = _block(src, "function saveTokens(", "\n    }", "saveTokens")
    assert_true("localStorage.setItem(KEYS.type" in body,
                "saveTokens must record the token kind so a destination app "
                "reading localStorage knows which Authorization scheme to send")
    assert_true("KEYS.expires" in body,
                "saveTokens must record an absolute expiry — a gt1. payload "
                "carries no exp claim, so expires_in is the only lifetime signal")


@th.django_unit_test("mojo-auth.js: getTokenType exists and the token string decides")
def test_js_token_type_is_derived_from_the_token(opts):
    src = _js()
    assert_true("getTokenType: function ()" in src,
                "mojo-auth.js must expose getTokenType() — an app has to be able "
                "to tell a scoped session from a platform one")
    assert_true("function _tokenType(" in src,
                "the kind test must live in ONE helper; two copies drift")
    helper = _block(src, "function _tokenType(", "\n    }", "_tokenType")
    assert_true("GROUP_TOKEN_PREFIX" in helper,
                "the kind must be decided by the gt1. prefix ON THE TOKEN, not "
                "by a stored marker a previous session could have left behind")


@th.django_unit_test("mojo-auth.js: getAuthHeader emits the scheme the token needs")
def test_js_auth_header_switches_scheme(opts):
    src = _js()
    assert_false("return t ? 'Bearer ' + t : null;" in src,
                 "getAuthHeader must no longer send Bearer unconditionally — a "
                 "group token under the Bearer scheme is simply a 401")
    body = _block(src, "getAuthHeader: function () {", "\n        },", "getAuthHeader")
    assert_true("'grouptoken '" in body,
                f"getAuthHeader must be able to emit the grouptoken scheme, got: {body!r}")
    assert_true("'Bearer '" in body,
                f"getAuthHeader must still emit Bearer for a JWT, got: {body!r}")
    assert_true("_tokenType(t)" in body,
                "the scheme must be derived from the CURRENT token on every "
                "call, never cached — a session can change kind mid-page")


@th.django_unit_test("mojo-auth.js: isTokenExpired is kind-aware and fails closed")
def test_js_is_token_expired_is_kind_aware(opts):
    src = _js()
    body = _block(src, "isTokenExpired: function () {", "\n        },", "isTokenExpired")
    assert_true("getTokenType()" in body,
                "isTokenExpired must branch on the token kind — decoding a gt1. "
                "token as a JWT finds no exp claim and reports a perfectly "
                "valid token as expired")
    assert_true("KEYS.expires" in body,
                "the group-token branch must read the stored token_expires_at")
    assert_true("return true;" in body,
                "a missing or unparsable expiry must be treated as EXPIRED "
                "(fail closed), not as valid")


@th.django_unit_test("mojo-auth.js: a group session can neither refresh nor mint a handoff code")
def test_js_group_session_has_no_upgrade_path(opts):
    src = _js()

    refresh = _block(src, "refreshToken: function () {", "\n        },", "refreshToken")
    assert_true("getTokenType() === 'grouptoken'" in refresh,
                "refreshToken must refuse under a group-scoped session — that "
                "path is exactly how a confined token would become a platform pair")
    assert_true(refresh.index("getTokenType() === 'grouptoken'")
                < refresh.index("localStorage.getItem(KEYS.refresh)"),
                "the refusal must come BEFORE the stored refresh token is read, "
                "so a token stranded by an earlier session is never spent")

    handoff = _block(src, "requestHandoffCode: function (destination) {",
                     "\n        },", "requestHandoffCode")
    assert_true("getTokenType() === 'grouptoken'" in handoff,
                "requestHandoffCode must refuse under a group-scoped session — "
                "the server refuses it too, and a handoff code buys a JWT pair")


@th.django_unit_test("mojo-auth.js: logout clears every token key")
def test_js_logout_clears_all_four_keys(opts):
    src = _js()
    body = _block(src, "logout: function () {", "\n        },", "logout")
    for key in ("KEYS.access", "KEYS.refresh", "KEYS.type", "KEYS.expires"):
        assert_true(f"removeItem({key})" in body,
                    f"logout must clear {key} — a stale kind marker or expiry "
                    f"left behind outlives the session it described. Got: {body!r}")


@th.django_unit_test("auth_base.html: the session check never refreshes a group session")
def test_template_session_check_is_type_guarded(opts):
    src = _tpl()
    check = _block(src, "if (!SKIP_SESSION_CHECK", "// Magic link token",
                   "session check")
    assert_true('MojoAuth.getTokenType() === "grouptoken"' in check,
                "the session check must recognise a group-scoped session")
    assert_true(check.index('getTokenType() === "grouptoken"')
                < check.index("MojoAuth.refreshToken()"),
                "the guard must precede the refresh call: the .catch on that "
                "call runs MojoAuth.logout(), which would throw away a valid "
                "group token")


@th.django_unit_test("auth_base.html: the handoff .catch surfaces the server's message")
def test_template_catch_surfaces_server_message(opts):
    src = _tpl()
    body = _block(src, "redirect: function ()", "onAuthSuccess:", "_mat.redirect")
    catch = _block(body, ".catch(function", "});", "_mat.redirect .catch")
    assert_true("MojoAuth.getError(" in catch,
                "the handoff .catch must surface the server's own message — a "
                "gated destination answers with a membership refusal a human "
                "can act on, and flattening it to 'That destination isn't "
                "allowed' hides it")
    assert_true("That destination isn't allowed" in catch,
                "the existing copy must remain as the fallback")


@th.django_unit_test("auth_base.html: a group session does NOT get direct cross-origin navigation")
def test_template_direct_nav_is_not_group_aware(opts):
    """A negative assertion, and the reason it exists is a rejected design.

    Letting a group-scoped session take the direct-navigation branch would send
    the visitor cross-origin with NO code — the same crafted ?redirect= link
    the .catch exists to refuse, just without the server round-trip. Under a
    group session requestHandoffCode() rejects, the .catch fires, the error is
    shown and the page stays put. That is already correct; a new branch here
    would be the bug.
    """
    src = _tpl()
    body = _block(src, "redirect: function ()", "onAuthSuccess:", "_mat.redirect")
    condition = _block(body, "if (!isCrossOrigin", ")", "direct-navigation condition")
    assert_false("grouptoken" in condition,
                 f"the direct-navigation condition must NOT special-case a "
                 f"group session, got: {condition!r}")
    assert_false("getTokenType" in condition,
                 f"the direct-navigation condition must not consult the token "
                 f"kind at all, got: {condition!r}")
