"""Regression: a dot-segment path defeats the redirect allowlist's path boundary.

`redirect_allowlist._split()` returns `urlsplit(url).path` UNNORMALIZED, and
`_entry_matches` compares it with a segment-bounded `startswith`. So a candidate
of `https://app.example.com/oauth/callback/../../../anything` matched an entry of
`https://app.example.com/oauth/callback`: the string is under the prefix, but the
browser resolves the `..` BEFORE it issues the request and lands three segments
up — off the prefix entirely, delivering the OAuth `code`+`state` somewhere the
allowlist never blessed. `%2e%2e` and the other `%2e` spellings smuggle the same
segment past a naive string check.

Refusal, not normalization
--------------------------
The fix REFUSES any `.`/`..` segment (in any `%2e` spelling) rather than
canonicalizing it. Normalization is a second parser bolted next to the browser's,
and the two disagree at the edges (`%2f`, tab/CR/LF, double-encoding) — every
disagreement is a bypass. Refusing is a fixed point: a URL that cannot carry a
traversal segment cannot escape the prefix no matter who resolves it. The rule is
shared by BOTH allowlists (they share `_split`) and by custom-scheme deep links.

The deliberate NON-catches are part of the contract and are locked below:
`%2f`-encoded slashes (`..%2f..%2fx` is ONE opaque segment to WHATWG) and
double-encoded `%252e` (a browser turns it into the literal text `%2e`, never a
dot) are still admitted — a browser does not resolve either, so neither escapes.

Test shape
----------
Every test except the last calls `_validate_redirect_uri` in-process and
overrides `django.conf.settings` directly (`_entries` below), mirroring
`redirect_uri.py`: the testit ban on `override_settings`, and `th.server_settings`
reloading the *server* process an in-process read never consults. The endpoint
test runs against the test project's pinned `ALLOWED_REDIRECT_URLS`
(`["https://example.com/"]`) so it needs neither a `Setting` row nor a reload.

Assertions target BEHAVIOR only — admitted vs refused, and the endpoint's status
code. Never a log line or an incident: the diagnostics around this refusal are a
separate item (#1098 converts them next), so pinning a log string here would
couple this regression to work that is about to move.
"""
import contextlib
from urllib.parse import quote

from testit import helpers as th

PROVIDER = "google"

SETTING_KEY = "ALLOWED_REDIRECT_URLS"

# A path-bearing entry: the boundary the traversal tries to climb out of.
ENTRY = "https://app.example.com/oauth/callback"
ENTRIES = [ENTRY]

# The message /begin and the allowlist share verbatim for a refused URI.
REFUSAL = "redirect_uri is not on the allowlist"




def _refusal(redirect_uri, entries=ENTRIES):
    """Return the refusal reason for `redirect_uri`, or None when admitted."""
    from mojo import errors as merrors
    from mojo.apps.account.rest import oauth

    installed = list(entries) if isinstance(entries, (list, tuple)) else entries
    try:
        # `request=None` = no group context, so `entries` IS the whole
        # allowlist (the per-group source contributes nothing). The raw value
        # is injected through the deployment_entries seam, which applies the
        # same kind="list" coercion settings.get would — no process-global
        # django.conf mutation (maestro item #1839).
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
def setup_redirect_dot_segments(opts):
    """Drop any global ALLOWED_REDIRECT_URLS row before this module runs.

    `settings.get` is DB-first, so a row stranded by a previous interrupted run
    would shadow every in-process override below. This module creates no rows;
    the delete is purely defensive (delete-before-create on a long-lived DB).
    """
    from mojo.apps.account.models.setting import Setting

    Setting.remove(SETTING_KEY)


# ---------------------------------------------------------------------------
# The named regression
# ---------------------------------------------------------------------------

@th.django_unit_test("oauth: a dot-segment candidate cannot escape the prefix")
def test_dot_segment_candidate_cannot_escape_the_prefix(opts):
    """THE regression. The string sits under the entry prefix, but the browser
    resolves the `..` up and out before the request, so it must be refused."""
    _assert_refused(ENTRY + "/../../../anything",
                    "the traversal from the workspec climbs out of the prefix")
    _assert_admitted(ENTRY, "positive control — the exact allowlisted path")
    _assert_admitted(ENTRY + "/inner",
                     "positive control — a real path under the prefix")


@th.django_unit_test("oauth: percent-encoded double-dot spellings are refused")
def test_dot_segment_percent_encoded_forms_are_refused(opts):
    """Every `%2e` spelling of `..`, plus the tab-interrupted form.

    urlsplit strips ASCII tab/LF/CR before the path is read, so `.\\t.` arrives
    as `..` and is caught — proving the guard sees the same bytes the browser
    will, not the raw request string.
    """
    for form in ("%2e%2e", "%2E%2E", ".%2e", "%2e.", "%2E.", "%2e%2E"):
        _assert_refused(ENTRY + f"/{form}/x",
                        f"the {form!r} spelling of a double-dot segment")
    _assert_refused(ENTRY + "/.\t./x",
                    "a tab-interrupted `..` — urlsplit strips the tab, so the "
                    "path resolves to `..` exactly as a browser would read it")


@th.django_unit_test("oauth: single-dot and trailing dot segments are refused")
def test_single_dot_segment_and_trailing_dots_are_refused(opts):
    """A lone `.` is a dot segment too (it re-anchors nothing but proves the
    check is not `..`-only), and a trailing `..`/`.` with no following segment
    still resolves in the browser."""
    _assert_refused(ENTRY + "/./x", "an interior single-dot segment")
    _assert_refused(ENTRY + "/%2e/x", "the `%2e` spelling of a single-dot segment")
    _assert_refused(ENTRY + "/..", "a trailing double-dot segment")
    _assert_refused(ENTRY + "/.", "a trailing single-dot segment")


@th.django_unit_test("oauth: a dot-segment ENTRY is unusable and admits nothing")
def test_dot_segment_in_an_entry_is_unusable(opts):
    """The refusal is symmetric — an entry spelled with a dot segment is dropped
    as unusable, exactly like a truncated `"h"` entry, and can bless nothing.

    Asserted by CONSEQUENCE (what the junk entry does or does not admit), never
    by inspecting a log line.
    """
    junk = ENTRY + "/../.."
    _assert_refused("https://app.example.com/anything",
                    "a dot-segment entry must not admit an arbitrary path",
                    entries=[junk])
    _assert_refused(junk,
                    "a dot-segment entry must not even admit its own literal "
                    "string as a candidate",
                    entries=[junk])
    _assert_admitted(ENTRY + "/inner",
                     "a clean entry alongside the junk one still works",
                     entries=[junk, ENTRY])


@th.django_unit_test("oauth: dot-segment refusal covers the handoff allowlist")
def test_dot_segment_refusal_covers_the_handoff_allowlist(opts):
    """The rule is in the shared `_split`, so the wildcard-capable handoff
    allowlist (`allow_wildcard=True`) inherits it — matched directly, no oauth
    validator in the path."""
    from mojo.apps.account.services import redirect_allowlist

    entries = ["https://*.example.com/app"]

    def matches(url):
        return redirect_allowlist.matches_allowlist(
            url, entries, source="AUTH_HANDOFF_ALLOWED_URLS", allow_wildcard=True)

    assert not matches("https://a.example.com/app/../../x"), (
        "a `..` traversal under a wildcard handoff entry must be refused")
    assert not matches("https://a.example.com/app/%2e%2e/x"), (
        "the `%2e%2e` spelling under a wildcard handoff entry must be refused")
    assert matches("https://a.example.com/app/x"), (
        "a real path under the wildcard handoff prefix must still match")


@th.django_unit_test("oauth: dot-segment refusal covers custom-scheme deep links")
def test_dot_segment_refusal_covers_custom_schemes(opts):
    """Custom schemes route through the same `_split`, so a traversal in a deep
    link is refused too, and a dot-segment deep-link entry is unusable."""
    entries = ["myapp://callback", "com.example.app:/oauth"]

    _assert_refused("myapp://callback/../..",
                    "a `..` traversal on a deep link", entries=entries)
    _assert_refused("myapp://callback/%2e%2e/x",
                    "the `%2e%2e` spelling on a deep link", entries=entries)
    _assert_refused("com.example.app:/oauth/../x",
                    "a `..` traversal on an empty-authority deep link",
                    entries=entries)

    _assert_admitted("myapp://callback/oauth",
                     "a clean path under the deep-link entry", entries=entries)
    _assert_admitted("com.example.app:///oauth",
                     "the three-slash empty-authority form of the entry",
                     entries=entries)

    _assert_refused("myapp://callback/oauth",
                    "a dot-segment deep-link entry is unusable and admits nothing",
                    entries=["myapp://callback/.."])


@th.django_unit_test("oauth: dots that are not whole segments still match")
def test_paths_with_dots_that_are_not_segments_still_match(opts):
    """The false-positive lock. Dots inside a label, a `%2e`-prefixed name, a
    `%2f`-encoded slash and a double-encoded `%252e` are NOT traversal segments,
    and the refusal must not touch them."""
    entries = ["https://app.example.com/"]
    for url, why in (
            ("https://app.example.com/v1.2/callback", "a dotted version label"),
            ("https://app.example.com/a..b/x", "`..` inside a single label"),
            ("https://app.example.com/callback.html", "a file extension"),
            ("https://app.example.com/%2eprofile",
             "`%2e` as the first char of a label, not a whole segment"),
            ("https://app.example.com/cb/..%2f..%2fx",
             "`%2f` is not a segment delimiter — one opaque segment, not `../..`"),
            ("https://app.example.com/cb/%252e%252e/x",
             "double-encoded `%252e` is literal text `%2e` to a browser, not a dot")):
        _assert_admitted(url, why, entries=entries)


# ---------------------------------------------------------------------------
# End to end. Runs LAST. Two requests only — /begin throttles anonymous callers.
# ---------------------------------------------------------------------------

@th.django_unit_test("oauth: /begin refuses a dot-segment redirect_uri end to end")
def test_oauth_begin_refuses_a_dot_segment_uri_endpoint(opts):
    """The full public endpoint against the pinned `ALLOWED_REDIRECT_URLS`.

    Both URLs name the pinned host `example.com`, so the ONLY difference is the
    dot segment: the refused one proves the guard fires on the real endpoint, the
    control proves the 400 is the traversal and not a broken read. Assertions
    target the 400 and the ABSENCE of state, never `auth_url` contents (which
    always point at the provider).
    """
    refused = "https://example.com/a/../../x"
    resp = opts.client.get(
        f"/api/auth/oauth/{PROVIDER}/begin"
        f"?redirect_uri={quote(refused, safe='')}")
    body = resp.response
    assert resp.status_code == 400, (
        f"/begin must refuse the dot-segment redirect_uri {refused!r}, "
        f"got {resp.status_code}: {body}")
    assert body.get("error") == REFUSAL, (
        f"the refusal must stay the shared message {REFUSAL!r}, "
        f"got {body.get('error')!r}")
    data = body.get("data") or {}
    assert not data.get("auth_url"), (
        f"a refused begin must return no auth_url, got {data.get('auth_url')!r}")
    assert not data.get("state"), (
        f"a refused begin must mint no OAuth state, got {data.get('state')!r}")

    control = "https://example.com/callback"
    ok = opts.client.get(
        f"/api/auth/oauth/{PROVIDER}/begin"
        f"?redirect_uri={quote(control, safe='')}")
    assert ok.status_code == 200, (
        f"the same host WITHOUT a dot segment must still begin normally — this "
        f"proves the 400 above is the traversal, not a broken allowlist read. "
        f"Got {ok.status_code}: {ok.response}")
    assert ok.response.data.auth_url, (
        f"an admitted begin must return an auth_url: {ok.response}")
    assert ok.response.data.state, (
        f"an admitted begin must mint OAuth state: {ok.response}")
