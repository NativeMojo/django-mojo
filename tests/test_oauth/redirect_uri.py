"""Regression: the OAuth landing allowlist matches a URL, not a string prefix.

`_validate_redirect_uri` used to answer `redirect_uri.startswith(prefix)`. A
prefix test terminates nowhere — not on an authority boundary and not on a path
segment — so an entry of `https://app.example.com` admitted
`https://app.example.com.evil.tld/`, a host the attacker simply registers.

`/begin` is a `@md.public_endpoint()`, so the whole chain is pre-auth: the
attacker calls `/begin` server-side with their own landing URL, hands the victim
the returned provider `auth_url` (a legitimate provider URL), and our `/callback`
302s the victim's `code` + `state` to the attacker's page, which posts them to
`/complete` and receives the victim's full access + refresh pair.

There were TWO bypasses in those lines, and the second one is total:

  * the suffix bypass above; and
  * `ALLOWED_REDIRECT_URLS` is read through `settings.get`, which resolves a
    DB-backed `Setting` row BEFORE the file setting. `Setting.value` is a
    `TextField`, so such a row is a **string** — and the old
    `list(settings.get(...))` exploded it into single characters, leaving
    `startswith('h')` true for every `http(s)://` URL on earth. That is not a
    narrow bypass, it is no allowlist at all. `kind="list"` is what closes it,
    and `test_redirect_uri_string_valued_setting_is_not_a_bypass` plus the
    endpoint test below are its regressions.

Matching now goes through `redirect_allowlist.matches_allowlist` — the same
parsed-URL matcher the handoff destination allowlist uses. Scheme, host and port
are compared after parsing, the path prefix terminates on a segment boundary,
and query + fragment are ignored on BOTH sides.

Test shape
----------
Everything except the last test calls `_validate_redirect_uri` in-process and
overrides `django.conf.settings` directly (`_entries` below) — the testit ban on
`override_settings` and DM-015's note that `th.server_settings` reloads the
*server* process, which an in-process read never consults.

The endpoint test deliberately does NOT use `th.server_settings` either: the
package pins `ALLOWED_REDIRECT_URLS` precisely so this module needs no reload
(two reloads take an exclusive lock and kill the uvicorn worker under ~36
parallel packages). It installs a global `Setting` row instead, which
`settings.get` reads DB-first and live — and which is itself the string-valued
row the second bypass above lives in. The row's list is a strict SUPERSET of the
pinned entry, so the brief window it exists cannot narrow the allowlist for a
parallel package, and it is removed in a `finally`.
"""
import contextlib
from urllib.parse import quote

from testit import helpers as th

PROVIDER = "google"

SETTING_KEY = "ALLOWED_REDIRECT_URLS"

# The entry the workspec names. Deliberately WITHOUT a trailing slash: the test
# project pins "https://example.com/" *with* one, and a trailing slash makes the
# suffix shape unreachable through `startswith` (the next character after the
# host must be "/"), so the bug does not reproduce against stock settings.
ENTRY = "https://app.example.com"
ENTRIES = [ENTRY]
ALLOWED = "https://app.example.com/callback"
# The workspec's exact shape: an attacker-registered host that merely BEGINS
# with the allowlisted one.
SUFFIXED = "https://app.example.com.evil.tld/"

REFUSAL = "redirect_uri is not on the allowlist"
NO_ALLOWLIST = "redirect_uri is not permitted: no ALLOWED_REDIRECT_URLS configured"




def _refusal(redirect_uri, entries=ENTRIES):
    """Return the refusal reason for `redirect_uri`, or None when admitted.

    `entries` is installed verbatim — a list, a raw string, or None. Coercing it
    into a usable list is `settings.get(kind="list")`'s job, not the test's.
    """
    from mojo import errors as merrors
    from mojo.apps.account.rest import oauth

    installed = list(entries) if isinstance(entries, (list, tuple)) else entries
    try:
        # `request=None` = no group context, so the group-metadata source
        # contributes nothing and `entries` IS the whole allowlist. The
        # per-group source has its own module
        # (tests/test_oauth/redirect_allowlist_group_source.py).
        # The raw value is injected through the deployment_entries seam, which
        # applies the same kind="list" coercion settings.get would — no
        # process-global django.conf mutation (maestro item #1839).
        oauth._validate_redirect_uri(None, redirect_uri,
                                     deployment_entries=installed)
    except merrors.ValueException as exc:
        return str(exc.reason)
    return None


def _assert_refused(redirect_uri, why, entries=ENTRIES):
    reason = _refusal(redirect_uri, entries=entries)
    assert reason == REFUSAL, (
        f"{why}: {redirect_uri!r} must be refused with {REFUSAL!r} against "
        f"entries {entries!r}, got {reason!r}")


def _assert_admitted(redirect_uri, why, entries=ENTRIES):
    reason = _refusal(redirect_uri, entries=entries)
    assert reason is None, (
        f"{why}: {redirect_uri!r} must be admitted against entries "
        f"{entries!r}, got refusal {reason!r}")


@th.django_unit_setup()
def setup_redirect_uri(opts):
    """Drop any global ALLOWED_REDIRECT_URLS row before this module runs.

    `settings.get` is DB-first, so a row stranded by a previous interrupted run
    would shadow every in-process override below and turn the whole module into
    a silent false green. This is also the row the endpoint test creates, so the
    long-lived database is cleaned of this module's own data before it writes
    any.
    """
    from mojo.apps.account.models.setting import Setting

    Setting.remove(SETTING_KEY)


# ---------------------------------------------------------------------------
# The named regression
# ---------------------------------------------------------------------------

@th.tier("core")
@th.django_unit_test("oauth: an entry does not admit a host that merely starts with it")
def test_redirect_uri_refuses_suffixed_host(opts):
    """THE regression. `https://app.example.com.evil.tld/` is a different host
    than `https://app.example.com`, and only a URL match can tell."""
    _assert_refused(SUFFIXED, "the suffix-extended host from the workspec")
    _assert_admitted(ALLOWED, "positive control — the allowlisted host itself")


@th.django_unit_test("oauth: the confusable-host family is refused")
def test_redirect_uri_refuses_confusable_hosts(opts):
    """Every way a URL can claim to be `app.example.com` and navigate elsewhere.

    The backslash-before-`@` shape is the parser differential: Python's
    `urlsplit` reads the host as `app.example.com` (the backslash hides inside
    the userinfo, which `parts.hostname` has already discarded before any host
    charset test can see it), while a WHATWG browser terminates the authority at
    the backslash and navigates to `evil.tld`. Refusing any raw backslash is the
    only safe reading of that disagreement.
    """
    _assert_refused("https://app.example.com@evil.tld/",
                    "userinfo confusable — the real host is evil.tld")
    _assert_refused("https://evil.tld\\@app.example.com/",
                    "backslash before the @ — Python says app.example.com, a "
                    "browser says evil.tld")
    _assert_refused("https://app.example.com\\.evil.tld/",
                    "backslash inside the authority")
    _assert_refused("https://app.example.com%2Eevil.tld/",
                    "percent-encoded dot in the host")
    _assert_refused("https://app.example.com./",
                    "trailing dot — the root-anchored form is NOT normalized "
                    "away here, so it is refused (fail-closed)")
    _assert_refused("//app.example.com/callback",
                    "scheme-relative URL — no scheme to compare")
    _assert_refused("javascript:alert(1)//app.example.com",
                    "javascript: carries no host at all")
    _assert_refused("https://notapp.example.com/callback",
                    "a host merely containing the allowlisted one")


@th.django_unit_test("oauth: host case folds, but case cannot smuggle a suffix")
def test_redirect_uri_case_is_not_a_bypass(opts):
    """Hosts are case-insensitive per RFC 3986, so folding is correct — and it
    is a deliberate widening versus the old byte-comparing prefix test. It must
    not fold a *different* host into an allowed one."""
    _assert_admitted("https://APP.Example.com/callback",
                     "a case-varied spelling of the allowlisted host")
    _assert_refused("https://APP.EXAMPLE.COM.EVIL.TLD/",
                    "case variation must not launder the suffix host")


@th.django_unit_test("oauth: a path prefix terminates on a segment boundary")
def test_redirect_uri_path_terminates_on_a_boundary(opts):
    entries = ["https://app.example.com/app"]
    _assert_admitted("https://app.example.com/app",
                     "the exact allowlisted path", entries=entries)
    _assert_admitted("https://app.example.com/app/inner",
                     "a path under the allowlisted prefix", entries=entries)
    _assert_refused("https://app.example.com/application",
                    "a sibling path sharing a string prefix", entries=entries)
    _assert_refused("https://app.example.com/other",
                    "a path outside the prefix", entries=entries)


@th.django_unit_test("oauth: scheme and port must match exactly")
def test_redirect_uri_scheme_and_port_must_match(opts):
    _assert_refused("http://app.example.com/callback",
                    "an https entry must never admit an http destination — the "
                    "OAuth code and state would travel in cleartext")
    _assert_refused("https://app.example.com:8443/callback",
                    "a non-default port is a different origin")
    _assert_admitted("https://app.example.com:443/callback",
                     "the explicit default port is the same origin")


@th.django_unit_test("oauth: query and fragment are not part of the match")
def test_redirect_uri_query_is_not_part_of_the_match(opts):
    """The match compares scheme/host/port/path only.

    Two consequences, both deliberate. A legitimate landing URL keeps its own
    query (that is how the app's post-login `?redirect=` survives the OAuth
    round-trip), and an allowlisted URL can therefore carry ANY query — which is
    exactly why `on_oauth_callback` still strips caller-supplied copies of
    `code`/`state` out of `frontend_uri` before appending the real ones.
    """
    _assert_admitted("https://app.example.com/callback?redirect=%2Fworkspaces%2F%23%2F",
                     "a landing URL carrying its own query")
    _assert_admitted("https://app.example.com/callback?code=EVIL&state=EVIL",
                     "a smuggled code/state does not change the allowlist "
                     "answer — the callback's strip is what defuses it")
    _assert_refused("https://evil.tld/?to=https://app.example.com/callback",
                    "an allowlisted URL inside the query must not admit a "
                    "foreign host")


@th.tier("core")
@th.django_unit_test("oauth: a wildcard entry is inert for ALLOWED_REDIRECT_URLS")
def test_redirect_uri_wildcard_entry_is_inert(opts):
    """`*.` is supported for the HANDOFF allowlist and deliberately not here.

    Nothing can `startswith("*")`, so a wildcard entry was already dead config —
    activating it inside a change that tightens everything else would widen an
    allowlist the operator never re-consented to. It is skipped as unusable.
    """
    entries = ["https://*.example.com"]
    _assert_refused("https://app.example.com/callback",
                    "a wildcard entry must not admit a subdomain here",
                    entries=entries)
    _assert_refused("https://example.com/callback",
                    "a wildcard entry must not admit its base host here",
                    entries=entries)


@th.django_unit_test("oauth: unusable entries are skipped and can never match")
def test_redirect_uri_unusable_entries_are_skipped(opts):
    """A truncated or relative entry is dropped, not stretched.

    The old prefix test did the opposite: an entry of `"h"` admitted every
    `http(s)://` URL in existence.

    Custom-scheme entries are NOT junk — they became first-class in 7ba05075.
    Their supported shapes are covered by the DEEP_ENTRY tests below, and their
    fail-closed shapes (`myapp:callback`, `myapp:`, `myapp://`) by
    test_redirect_uri_custom_scheme_shapes_that_fail_closed.
    """
    junk = [None, "", "h", "/relative", "https://"]
    _assert_refused("https://totally.evil.tld/steal",
                    "an unusable entry must never admit anything",
                    entries=junk)
    _assert_refused(SUFFIXED,
                    "unusable entries alongside nothing usable admit nothing",
                    entries=junk)
    _assert_admitted(ALLOWED,
                     "a usable entry still works alongside unusable ones",
                     entries=junk + [ENTRY])


@th.django_unit_test("oauth: a STRING-valued setting is not a total bypass")
def test_redirect_uri_string_valued_setting_is_not_a_bypass(opts):
    """The second, total bypass.

    `ALLOWED_REDIRECT_URLS` resolves through `settings.get`, which consults a
    DB-backed `Setting` row first — and `Setting.value` is a `TextField`, so the
    value arrives as a **string**. The old code called `list()` on it, exploding
    it into single characters, after which `startswith('h')` admitted every
    `http(s)://` URL. Reading with `kind="list"` coerces both string shapes into
    a real list instead.
    """
    json_form = '["https://app.example.com"]'
    _assert_refused("https://totally.evil.tld/steal",
                    "a JSON-array STRING setting must not degrade into a "
                    "character list that admits every URL",
                    entries=json_form)
    _assert_refused(SUFFIXED,
                    "the suffix host is still refused with a string setting",
                    entries=json_form)
    _assert_admitted(ALLOWED,
                     "a JSON-array string is coerced into the list it spells",
                     entries=json_form)

    bare_form = "https://app.example.com"
    _assert_refused("https://totally.evil.tld/steal",
                    "a BARE string setting must not degrade into a character "
                    "list either",
                    entries=bare_form)
    _assert_admitted(ALLOWED,
                     "a bare string is coerced into a one-entry list",
                     entries=bare_form)


@th.tier("core")
@th.django_unit_test("oauth: no allowlist configured refuses with the unchanged message")
def test_redirect_uri_no_allowlist_configured(opts):
    """Contract lock — #1089 shipped migration advice quoting this string."""
    for empty, label in (([], "an empty list"),
                         ("", "an empty string setting"),
                         (None, "an explicit None")):
        reason = _refusal(ALLOWED, entries=empty)
        assert reason == NO_ALLOWLIST, (
            f"{label} must refuse with the unchanged no-allowlist message "
            f"{NO_ALLOWLIST!r}, got {reason!r}")


# ---------------------------------------------------------------------------
# Custom URL schemes — mobile deep links.
#
# A native app completes OAuth on its own deep link, so a custom-scheme entry
# has to be usable. It is NOT a web origin, so it is matched under its own
# narrower rules: exact case-folded scheme, exact case-folded authority compared
# byte-for-byte, and the same segment-bounded path prefix. No default ports (a
# custom scheme has none), no DNS-shaped hostname rules (an authority here is an
# app-registered label), no wildcards. The http(s) rules above are untouched by
# any of this, and the two families never cross.
# ---------------------------------------------------------------------------

DEEP_ENTRY = "myapp://callback"
DEEP_ENTRIES = [DEEP_ENTRY]
DEEP_ALLOWED = "myapp://callback/oauth"


@th.django_unit_test("oauth: a custom-scheme entry admits its own deep link")
def test_redirect_uri_custom_scheme_entry_is_usable(opts):
    """The named restore: `myapp://callback` is a working entry again.

    It went unusable when the matcher replaced the prefix test, which silently
    400'd every native-app OAuth flow. The authority is compared byte-for-byte
    after case-folding — which is also what makes a userinfo confusable inert
    here: `myapp://evil@callback` is simply a different authority, not a
    rewriting of `callback`.
    """
    _assert_admitted(DEEP_ENTRY, "the deep link the entry names",
                     entries=DEEP_ENTRIES)
    _assert_admitted(DEEP_ALLOWED, "a path under the entry",
                     entries=DEEP_ENTRIES)
    _assert_admitted("MyApp://CallBack/oauth",
                     "scheme and authority are case-insensitive",
                     entries=DEEP_ENTRIES)
    _assert_admitted("myapp://callback?code=x",
                     "the query is ignored for custom schemes too",
                     entries=DEEP_ENTRIES)

    _assert_refused("otherapp://callback",
                    "a different scheme — the OS routes it to a different app",
                    entries=DEEP_ENTRIES)
    _assert_refused("myapp://other",
                    "a different authority under the same scheme",
                    entries=DEEP_ENTRIES)
    _assert_refused("myapp://callback.evil",
                    "an authority that merely BEGINS with the entry's",
                    entries=DEEP_ENTRIES)
    _assert_refused("myapp://evil@callback",
                    "userinfo makes it a different authority, not the same one",
                    entries=DEEP_ENTRIES)
    # Isolates the guard: without it this parses to authority `callback` and
    # path `/oauth\x`, which sits under the entry and would MATCH.
    _assert_refused("myapp://callback/oauth\\x",
                    "the backslash guard applies to every scheme, not just web",
                    entries=DEEP_ENTRIES)


@th.django_unit_test("oauth: a custom-scheme path prefix terminates on a boundary")
def test_redirect_uri_custom_scheme_path_terminates_on_a_boundary(opts):
    entries = ["myapp://callback/oauth"]
    _assert_admitted("myapp://callback/oauth",
                     "the exact allowlisted deep link", entries=entries)
    _assert_admitted("myapp://callback/oauth/done",
                     "a path under the allowlisted prefix", entries=entries)
    _assert_refused("myapp://callback/oauthdone",
                    "a sibling path sharing a string prefix", entries=entries)
    _assert_refused("myapp://callback/other",
                    "a path outside the prefix", entries=entries)


@th.django_unit_test("oauth: custom-scheme and web entries never admit each other")
def test_redirect_uri_custom_scheme_and_web_never_cross(opts):
    """Schemes are compared before anything else, so the two families are
    disjoint in both directions. Restoring deep links cannot have widened the
    web path by so much as one URL."""
    _assert_refused("https://callback/oauth",
                    "an https URL against a custom-scheme entry",
                    entries=DEEP_ENTRIES)
    _assert_refused("http://callback/oauth",
                    "an http URL against a custom-scheme entry",
                    entries=DEEP_ENTRIES)
    _assert_refused("myapp://app.example.com/callback",
                    "a deep link against an https entry naming the same label",
                    entries=ENTRIES)
    _assert_admitted(ALLOWED,
                     "the https entry still admits its own URL with a "
                     "custom-scheme entry sitting alongside it",
                     entries=ENTRIES + DEEP_ENTRIES)


@th.django_unit_test("oauth: the empty-authority deep-link forms are one value")
def test_redirect_uri_custom_scheme_empty_authority(opts):
    """`com.example.app:/oauth` and `com.example.app:///oauth` both parse to an
    EMPTY authority with the path `/oauth`, so they are the same entry. An
    empty authority is a real, distinct value — it never equals `callback`."""
    for entry in ("com.example.app:/oauth", "com.example.app:///oauth"):
        entries = [entry]
        _assert_admitted("com.example.app:/oauth",
                         f"the one-slash form against {entry!r}", entries=entries)
        _assert_admitted("com.example.app:///oauth",
                         f"the three-slash form against {entry!r}", entries=entries)
        _assert_admitted("com.example.app:/oauth/done",
                         f"a path under {entry!r}", entries=entries)
        _assert_refused("com.example.app://callback/oauth",
                        f"a NON-empty authority against {entry!r} — an empty "
                        f"authority is a value, not a wildcard",
                        entries=entries)


@th.django_unit_test("oauth: ambiguous or dangerous custom-scheme shapes fail closed")
def test_redirect_uri_custom_scheme_shapes_that_fail_closed(opts):
    """What a custom scheme must NOT buy.

    The opaque form (`myapp:callback`, `mailto:a@b`) gives no way to tell an
    authority from a path, so it is refused on both sides rather than guessed
    at. A scheme with neither authority nor path would authorize an entire
    scheme. The script pseudo-schemes are a navigation sink and are refused
    outright, so a mistyped entry can never turn `frontend_uri` into stored XSS
    on the auth origin. `*.` is not a wildcard here — it is just an authority
    nothing equals.
    """
    for junk, why in (
            ("myapp:callback", "the opaque form — authority or path?"),
            ("mailto:someone@example.com", "mailto is the same opaque shape"),
            ("myapp:", "a bare scheme with nothing to match on"),
            ("myapp://", "a scheme with an empty authority AND empty path"),
            ("javascript:/alert(1)", "javascript: is a sink, never a destination"),
            ("data:/text/html", "data: is a sink, never a destination"),
            ("vbscript:/msgbox", "vbscript: is a sink, never a destination")):
        _assert_refused("https://totally.evil.tld/steal",
                        f"{why} — an unusable entry must admit nothing",
                        entries=[junk])
        _assert_refused(junk, f"{why} — and it must not admit itself either",
                        entries=[junk, ENTRY])

    _assert_refused("myapp://a.callback",
                    "`*.` is inert for a custom scheme — no wildcard expansion",
                    entries=["myapp://*.callback"])
    _assert_admitted("myapp://*.callback",
                     "it is only ever the literal authority it spells",
                     entries=["myapp://*.callback"])


# The end-to-end /begin test (the only DB writer) moved to
# tests/test_oauth_extended_serial/redirect_uri_endpoint.py (item #1839).

