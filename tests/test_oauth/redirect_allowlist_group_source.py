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

    # Group.objects.create() leaves uuid=None (it is lazily assigned), and the
    # dispatcher's group_uuid branch would silently no-op against a null uuid.
    opts.tenant_uuid = tenant.get_uuid()
    opts.tenant_id = tenant.pk
    opts.child_uuid = child.get_uuid()
    opts.stringy_uuid = stringy.get_uuid()


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
