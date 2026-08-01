"""The OAuth landing allowlist has TWO sources, and the matcher guards both.

`_validate_redirect_uri` combines `settings.ALLOWED_REDIRECT_URLS` with
`group.get_metadata_value("allowed_redirect_urls")` for whichever group the
request resolved to. That per-group source is tenant self-service: a white-label
tenant registers the origin its own login page lands on without a deploy of the
platform. It is deliberately kept even though plain `metadata` is writable by a
holder of `manage_group` and the group is chosen by the (possibly anonymous)
caller through `?group=<id>` / `?group_uuid=<uuid>` — an accepted decision, on
the record in `_validate_redirect_uri`'s docstring.

What bounds it is the matcher, and that is what this module pins. Every entry —
global or per-group — goes through `redirect_allowlist.matches_allowlist`, so an
entry authorizes the EXACT host it names and nothing that merely begins with it.
A tenant blesses an origin it already controls; it cannot reach a neighbour's.

Every request here is made ANONYMOUSLY (this module never logs in), because that
is the real shape of the flow: a signed-out visitor on a white-label login page
is exactly who calls `/begin`.

`ALLOWED_REDIRECT_URLS` is pinned to `["https://example.com/"]` in the test
project settings, so no `th.server_settings` reload is needed anywhere in this
module (parallel-safe). Every per-group origin below is deliberately unrelated
to that pinned entry — nothing about them can satisfy the GLOBAL check, so an
admitted `/begin` here can only mean the per-group source produced it, and a
refused one can only mean the matcher refused it.

Assertions target the 400 / 200 and the presence or absence of `state`, never
the contents of `auth_url`: `auth_url` always points at the provider, and the
landing URL only surfaces later (it is stashed in the Redis state as
`frontend_uri`), so string-matching `auth_url` would prove nothing either way.
"""
from urllib.parse import quote

from testit import helpers as th

PROVIDER = "google"
GROUP_PREFIX = "oauthredir_"

# Per-group entries. None of these is a prefix-relative of the pinned global
# "https://example.com/" entry, so the global list can never admit them.
TENANT_ENTRY = "https://tenant-a.example/app"
TENANT_URI = "https://tenant-a.example/app/callback"
# The attacker-registered host that merely BEGINS with the tenant's. Only a
# parsed-URL match can tell it apart from the entry.
TENANT_LOOKALIKE = "https://tenant-a.example.evil.tld/app/callback"
# A sibling path sharing a string prefix with the entry's path.
TENANT_SIBLING_PATH = "https://tenant-a.example/application"

PARENT_ENTRY = "https://tenant-b.example/"
PARENT_URI = "https://tenant-b.example/landing"

# A group whose metadata value is a bare STRING rather than a list.
STRING_URI = "https://tenant-c.example/landing"
STRING_UNRELATED = "https://totally.evil.tld/steal"

# A URL the PINNED deployment list (`["https://example.com/"]`) already admits,
# used to prove a non-list group value drops to "no group entry" and the request
# still validates against the deployment list — rather than 500-ing.
GLOBAL_URI = "https://example.com/landing"

# A group whose metadata value is an OBJECT whose KEY is a URL. The old
# `list(value)` yielded the dict KEYS, so the key acted as an entry; the
# coercion drops a dict as an unusable source (the dict-narrowing).
DICT_ENTRY = "https://tenant-d.example/"

# String FORMS a text-backed `Setting` row would also accept under `kind="list"`:
# a JSON-array string and a comma-separated string.
JSON_ARRAY_VALUE = '["https://tenant-e.example/app"]'
JSON_ARRAY_UNDER = "https://tenant-e.example/app/x"   # a path under the entry
CSV_VALUE = "https://tenant-f.example/one,https://tenant-f.example/two"
CSV_ONE = "https://tenant-f.example/one"
CSV_TWO = "https://tenant-f.example/two"

REFUSAL = "redirect_uri is not on the allowlist"


@th.django_unit_setup()
def setup_redirect_allowlist_group_source(opts):
    from mojo.apps.account.models.group import Group

    # Long-lived DB: delete this module's own rows before creating them. The
    # prefix keeps the sweep clear of every other package's groups.
    Group.objects.filter(name__startswith=GROUP_PREFIX).delete()

    tenant = Group.objects.create(name=f"{GROUP_PREFIX}tenant", kind="organization")
    tenant.metadata = {"allowed_redirect_urls": [TENANT_ENTRY]}
    tenant.save()

    # The key lives on the PARENT here, so the child exercises the parent-chain
    # walk in get_metadata_value rather than a direct read.
    parent = Group.objects.create(name=f"{GROUP_PREFIX}parent", kind="organization")
    parent.metadata = {"allowed_redirect_urls": [PARENT_ENTRY]}
    parent.save()
    child = Group.objects.create(name=f"{GROUP_PREFIX}child", kind="organization",
                                 parent=parent)

    # Metadata is a JSONField, so a tenant can write a bare string where a list
    # belongs. Nothing coerces it on the way in.
    stringy = Group.objects.create(name=f"{GROUP_PREFIX}stringy", kind="organization")
    stringy.metadata = {"allowed_redirect_urls": STRING_URI}
    stringy.save()

    # Non-list values. A JSONField round-trips each JSON type exactly, so these
    # reach get_metadata_value() as a Python int / bool / dict — the shapes that
    # used to char-shatter (str) or raise TypeError (int/bool) or leak dict keys.
    inty = Group.objects.create(name=f"{GROUP_PREFIX}inty", kind="organization")
    inty.metadata = {"allowed_redirect_urls": 5}
    inty.save()

    booly = Group.objects.create(name=f"{GROUP_PREFIX}booly", kind="organization")
    booly.metadata = {"allowed_redirect_urls": True}
    booly.save()

    dicty = Group.objects.create(name=f"{GROUP_PREFIX}dicty", kind="organization")
    dicty.metadata = {"allowed_redirect_urls": {DICT_ENTRY: True}}
    dicty.save()

    # String FORMS the deployment setting would also accept under kind="list".
    jsonstr = Group.objects.create(name=f"{GROUP_PREFIX}jsonstr", kind="organization")
    jsonstr.metadata = {"allowed_redirect_urls": JSON_ARRAY_VALUE}
    jsonstr.save()

    csv = Group.objects.create(name=f"{GROUP_PREFIX}csv", kind="organization")
    csv.metadata = {"allowed_redirect_urls": CSV_VALUE}
    csv.save()

    # Group.objects.create() leaves uuid=None (it is lazily assigned), and the
    # dispatcher's group_uuid branch would silently no-op against a null uuid.
    opts.tenant_uuid = tenant.get_uuid()
    opts.tenant_id = tenant.pk
    opts.child_uuid = child.get_uuid()
    opts.stringy_uuid = stringy.get_uuid()
    opts.inty_uuid = inty.get_uuid()
    opts.inty_pk = inty.pk
    opts.booly_uuid = booly.get_uuid()
    opts.booly_pk = booly.pk
    opts.dicty_uuid = dicty.get_uuid()
    opts.dicty_pk = dicty.pk
    opts.jsonstr_uuid = jsonstr.get_uuid()
    opts.jsonstr_pk = jsonstr.pk
    opts.csv_uuid = csv.get_uuid()
    opts.csv_pk = csv.pk


def _begin(opts, redirect_uri, group_param=None):
    """Anonymous GET /begin with a redirect_uri and optional group context."""
    url = (f"/api/auth/oauth/{PROVIDER}/begin"
           f"?redirect_uri={quote(redirect_uri, safe='')}")
    if group_param:
        url = f"{url}&{group_param}"
    return opts.client.get(url)


def _assert_admitted(resp, context):
    body = resp.response
    assert resp.status_code == 200, (
        f"{context}: must be admitted with 200, got {resp.status_code}: {body}")
    data = body.get("data") or {}
    assert data.get("auth_url"), (
        f"{context}: an admitted begin must return a provider auth_url, got "
        f"{data.get('auth_url')!r}")
    assert data.get("state"), (
        f"{context}: an admitted begin must mint OAuth state — the state is "
        f"what carries frontend_uri to the callback bounce. Got "
        f"{data.get('state')!r}")


def _assert_refused(resp, context):
    body = resp.response
    assert resp.status_code == 400, (
        f"{context}: must be refused with 400, got {resp.status_code}: {body}")
    assert body.get("error") == REFUSAL, (
        f"{context}: expected the existing refusal message {REFUSAL!r} (shared "
        f"verbatim with the gated-destination refusal so the two are not "
        f"distinguishable), got {body.get('error')!r}")
    data = body.get("data") or {}
    assert not data.get("auth_url"), (
        f"{context}: a refused begin must not return an auth_url, got "
        f"{data.get('auth_url')!r}")
    assert not data.get("state"), (
        f"{context}: a refused begin must not mint OAuth state. Got "
        f"{data.get('state')!r}")


@th.django_unit_test("oauth: a group's own metadata entry admits its own origin")
def test_group_metadata_admits_its_own_origin(opts):
    """Tenant self-service, through both group-selection parameters.

    The control leg is the same URL with NO group context: it is refused,
    because the pinned global list does not cover this origin. That is what
    proves the 200s below came from the per-group source and not from a
    coincidental global match.
    """
    bare = _begin(opts, TENANT_URI)
    _assert_refused(bare, "the tenant origin with no group context at all")

    _assert_admitted(_begin(opts, TENANT_URI, f"group_uuid={opts.tenant_uuid}"),
                     "the tenant's own origin selected by ?group_uuid=")
    # `?group=<int>` is a separate dispatcher branch from `?group_uuid=`.
    _assert_admitted(_begin(opts, TENANT_URI, f"group={opts.tenant_id}"),
                     "the tenant's own origin selected by ?group=<id>")


@th.django_unit_test("oauth: a group entry does not admit a host that merely starts with it")
def test_group_metadata_does_not_admit_a_lookalike_host(opts):
    """The per-group source goes through the parsed matcher, not a prefix test.

    `https://tenant-a.example.evil.tld/` is a host the attacker registers, and
    it merely BEGINS with the tenant's entry. Under the `startswith` check this
    replaced it would have been admitted — with the tenant's own group named in
    the query, by an anonymous caller. The path leg proves the same thing on the
    other axis: an entry of `/app` does not stretch to `/application`.
    """
    _assert_refused(
        _begin(opts, TENANT_LOOKALIKE, f"group_uuid={opts.tenant_uuid}"),
        "a suffix-extended lookalike of the tenant's own host")
    _assert_refused(
        _begin(opts, TENANT_LOOKALIKE, f"group={opts.tenant_id}"),
        "the same lookalike host selected by ?group=<id>")
    _assert_refused(
        _begin(opts, TENANT_SIBLING_PATH, f"group_uuid={opts.tenant_uuid}"),
        "a sibling path sharing a string prefix with the entry's path")


@th.django_unit_test("oauth: an ANCESTOR's metadata entry is inherited")
def test_parent_group_metadata_is_inherited(opts):
    """`get_metadata_value` walks the parent chain, so a child group inherits
    the entries its ancestors registered — that is how a tenant with a group
    tree configures its landing origin once, at the top."""
    bare = _begin(opts, PARENT_URI)
    _assert_refused(bare, "the parent's origin with no group context at all")

    _assert_admitted(_begin(opts, PARENT_URI, f"group_uuid={opts.child_uuid}"),
                     "a child group whose PARENT registered the origin")


@th.django_unit_test("oauth: a STRING-valued group metadata entry admits nothing unrelated")
def test_string_valued_group_metadata_admits_nothing_unrelated(opts):
    """The char-explosion class is dead on the per-group path too.

    `metadata` is a JSONField, so a tenant can store a bare string where a list
    belongs, and `extend()` then shatters it into single characters. Under the
    `startswith` check this replaced, an entry of `"h"` admitted every
    `http(s)://` URL on earth — from a tenant-writable key, on a public
    endpoint. The parsed matcher refuses every one of those characters as an
    unusable entry instead, so an unrelated host stays refused.

    Only the security property is asserted. Whether the string ALSO fails to
    admit the host it spells is a usability wart, not a contract — a future
    change that coerces the value into a one-entry list must not have to edit
    this test.
    """
    _assert_refused(
        _begin(opts, STRING_UNRELATED, f"group_uuid={opts.stringy_uuid}"),
        "an unrelated host against a STRING-valued group metadata entry")
    _assert_refused(
        _begin(opts, "https://app.example.com/callback",
               f"group_uuid={opts.stringy_uuid}"),
        "a second unrelated host — no single character may act as an entry")


@th.django_unit_test("oauth: a STRING-valued group entry admits the URL it spells")
def test_string_valued_group_metadata_admits_the_url_it_spells(opts):
    """The coercion turns a bare string into the single entry it spells.

    Before #1103 a bare-string metadata value was `list()`-exploded into single
    characters, every one an unusable entry — so the tenant's OWN landing origin
    was refused (a silently-dead allowlist). `coerce_entries` now reads it with
    the same `kind="list"` rule the deployment setting gets: a bare string is the
    single entry it spells, so the tenant's own origin is admitted. This FAILS at
    HEAD (the characters all fail `_split`, so the URL is refused 400).
    """
    _assert_admitted(
        _begin(opts, STRING_URI, f"group_uuid={opts.stringy_uuid}"),
        "a STRING-valued group entry admits the exact URL it spells")
    # Fail-closed half: it admits the URL it spells and nothing else.
    _assert_refused(
        _begin(opts, STRING_UNRELATED, f"group_uuid={opts.stringy_uuid}"),
        "an unrelated host against the same STRING-valued group entry")


@th.django_unit_test("oauth: a non-list group metadata value cannot 500 the public begin")
def test_non_list_group_metadata_cannot_break_begin(opts):
    """A truthy non-iterable metadata value must never crash the public /begin.

    Before #1103 `group_entries = list(value)` on a truthy non-iterable (`5`,
    `True`) raised `TypeError: 'int'/'bool' object is not iterable`, which the
    generic error handler turned into a 500 — on the PUBLIC, anonymously
    selectable `/begin`, drivable by a single anonymous request. `coerce_entries`
    drops such a value as an unusable source, so the group contributes no entry
    and the request validates against the deployment list as if no group were
    named. Both legs 500 at HEAD.
    """
    for uuid, label in ((opts.inty_uuid, "an int"), (opts.booly_uuid, "a bool")):
        refusal = _begin(opts, STRING_UNRELATED, f"group_uuid={uuid}")
        assert refusal.status_code != 500, (
            f"{label}-valued group metadata must not 500 /begin — a non-list "
            f"value used to reach `list(value)` and raise TypeError; got "
            f"{refusal.status_code}: {refusal.response}")
        _assert_refused(
            refusal,
            f"an unrelated URL with {label}-valued group metadata (no entry)")
        normal = _begin(opts, GLOBAL_URI, f"group_uuid={uuid}")
        assert normal.status_code != 500, (
            f"{label}-valued group metadata must not 500 the normal-flow leg "
            f"either — same TypeError origin; got {normal.status_code}: "
            f"{normal.response}")
        _assert_admitted(
            normal,
            f"a deployment-list URL with {label}-valued group metadata beside it")


@th.django_unit_test("oauth: a dict-valued group entry contributes no entries (keys are not entries)")
def test_dict_valued_group_metadata_contributes_no_entries(opts):
    """The dict narrowing: a dict value's KEYS no longer act as entries.

    Before #1103 `group_entries = list(value)` on a dict yielded its KEYS, so a
    tenant that wrote `{"https://tenant-d.example/": true}` accidentally
    allowlisted that host — `list({...})` handed the key straight to the matcher,
    which admitted a URL under it. `coerce_entries` drops a dict as an unusable
    source, so the same URL is now refused. This FAILS at HEAD (200 — the key
    acts as an entry). Write a JSON array, not an object.
    """
    _assert_refused(
        _begin(opts, DICT_ENTRY + "callback", f"group_uuid={opts.dicty_uuid}"),
        "a URL under a dict metadata value's KEY (keys are not entries)")


@th.django_unit_test("oauth: string forms of the group value are coerced like the setting")
def test_group_metadata_string_forms_are_coerced_like_the_setting(opts):
    """A JSON-array string and a comma-separated string coerce like the setting.

    A tenant may store the value as a JSON-array string (`'["https://…"]'`) or a
    comma-separated string, exactly as a text-backed `Setting` row holds
    `ALLOWED_REDIRECT_URLS`. `coerce_entries` reads both into the list they spell,
    so their origins are admitted while an unrelated host stays refused. Both
    forms char-shatter and are refused at HEAD.
    """
    # JSON-array string → the array it spells; the entry admits a path under it.
    _assert_admitted(
        _begin(opts, JSON_ARRAY_UNDER, f"group_uuid={opts.jsonstr_uuid}"),
        "a path under the json-array group entry")
    _assert_refused(
        _begin(opts, STRING_UNRELATED, f"group_uuid={opts.jsonstr_uuid}"),
        "an unrelated host against the json-array group")
    # Comma-separated string → both entries it spells.
    _assert_admitted(
        _begin(opts, CSV_ONE, f"group_uuid={opts.csv_uuid}"),
        "the first entry of the comma-separated group value")
    _assert_admitted(
        _begin(opts, CSV_TWO, f"group_uuid={opts.csv_uuid}"),
        "the second entry of the comma-separated group value")
    _assert_refused(
        _begin(opts, STRING_UNRELATED, f"group_uuid={opts.csv_uuid}"),
        "an unrelated host against the comma-separated group")


@th.django_unit_test("oauth: coerce_entries matches the settings kind='list' coercion")
def test_coerce_entries_matches_the_settings_list_coercion(opts):
    """`coerce_entries` must not drift from `settings.get(kind="list")`.

    The per-group source needs the SAME coercion the deployment list gets, so a
    tenant's value behaves identically whether it lands in group metadata or in a
    text-backed `Setting` row. This pins each shape's output AND asserts it equals
    the settings helper's own `_convert_value(value, "list", [])` — deliberately
    reaching a private method, because the whole point is that the two
    implementations must stay identical. This is a pure in-process check (no
    server), so it exercises `coerce_entries` directly.
    """
    from mojo.apps.account.services import redirect_allowlist
    from mojo.helpers.settings import settings

    cases = [
        (["https://a.example/", "https://b.example/"],
         ["https://a.example/", "https://b.example/"], "a real list passes through"),
        (["https://a.example/", 5, None],
         ["https://a.example/", 5, None], "a list with junk members is returned whole"),
        ([], [], "an empty list is empty"),
        ('["https://a.example/", "https://b.example/"]',
         ["https://a.example/", "https://b.example/"], "a JSON-array string parses"),
        ("https://a.example/,https://b.example/",
         ["https://a.example/", "https://b.example/"], "a comma string splits"),
        ("https://a.example/",
         ["https://a.example/"], "a bare string is the one entry it spells"),
        ("[bad]", [], "bracket-wrapped broken JSON is an unusable source"),
        ("", [], "an empty string is empty"),
        (None, [], "None is empty"),
        ({}, [], "an empty dict is empty"),
        (0, [], "zero is empty"),
        (False, [], "False is empty"),
        (5, [], "a truthy int is unusable"),
        (True, [], "a truthy bool is unusable"),
        (1.5, [], "a float is unusable"),
        ({"https://a.example/": True}, [], "a dict's keys are not entries"),
        (("https://a.example/",), [], "a tuple is unusable"),
    ]
    for value, expected, why in cases:
        got = redirect_allowlist.coerce_entries(value, source="test")
        assert got == expected, (
            f"coerce_entries({value!r}) should be {expected!r} ({why}), got {got!r}")
        # Drift pin: reaches a private helper on purpose — coerce_entries and the
        # settings kind="list" coercion must never diverge.
        reference = settings._convert_value(value, "list", [], name="ALLOWED_REDIRECT_URLS")
        assert got == reference, (
            f"coerce_entries({value!r})={got!r} drifted from settings "
            f"_convert_value(...)={reference!r} — the two must stay identical")
