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
from testit import helpers as th
from testit.helpers import assert_true, assert_eq


def assert_false(value, msg):
    """Local mirror of assert_true — most of this module is "must be refused",
    and `assert_true(not x, ...)` reads backwards for every one of them.
    testit.helpers has no assert_false today (same helper as handoff.py)."""
    assert not value, msg


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
