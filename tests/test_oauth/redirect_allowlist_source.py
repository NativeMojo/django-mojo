"""Regression: the OAuth landing allowlist has exactly ONE source.

`_validate_redirect_uri` used to combine `settings.ALLOWED_REDIRECT_URLS` with
`group.get_metadata_value("allowed_redirect_urls")`. Plain `metadata` is
writable by any holder of `manage_group` on that group, and `request.group` is
selectable by ANY anonymous caller through `?group=<id>` / `?group_uuid=<uuid>`
in the dispatcher — so a tenant-writable key was a platform-wide control over
where an OAuth login is allowed to land.

Every request here is made ANONYMOUSLY (this module never logs in) because that
is the actual attack shape: the caller, not the server, picks the group.

`ALLOWED_REDIRECT_URLS` is pinned to `["https://example.com/"]` in the test
project settings, so no `th.server_settings` reload is needed anywhere in this
module (parallel-safe). The attacker origin below deliberately does NOT
prefix-collide with that pinned entry — the allowlist match is still a bare
`startswith`, so a host like `https://example.com.attacker.tld/` would pass the
GLOBAL check and turn these tests green for the wrong reason.

Assertions target the 400 and the absence of `state`, never the contents of
`auth_url`: `auth_url` always points at the provider, and the landing URL only
surfaces later (it is stashed in the Redis state as `frontend_uri`), so
string-matching `auth_url` would pass both before and after the fix.
"""
from urllib.parse import quote

from testit import helpers as th

PROVIDER = "google"
GROUP_PREFIX = "oauthredir_"

# Not a prefix-relative of the pinned "https://example.com/" entry.
ATTACKER_PREFIX = "https://attacker.example/"
ATTACKER_URI = "https://attacker.example/collect"
# Sits under the pinned global entry.
ALLOWED_URI = "https://example.com/login"

REFUSAL = "redirect_uri is not on the allowlist"


@th.django_unit_setup()
def setup_redirect_allowlist(opts):
    from mojo.apps.account.models.group import Group

    # Long-lived DB: delete this module's own rows before creating them. The
    # prefix keeps the sweep clear of every other package's groups.
    Group.objects.filter(name__startswith=GROUP_PREFIX).delete()

    tenant = Group.objects.create(name=f"{GROUP_PREFIX}tenant", kind="organization")
    tenant.metadata = {"allowed_redirect_urls": [ATTACKER_PREFIX]}
    tenant.save()

    # The key lives on the PARENT here, so the child exercises the parent-chain
    # walk in get_metadata_value rather than a direct read.
    parent = Group.objects.create(name=f"{GROUP_PREFIX}parent", kind="organization")
    parent.metadata = {"allowed_redirect_urls": [ATTACKER_PREFIX]}
    parent.save()
    child = Group.objects.create(name=f"{GROUP_PREFIX}child", kind="organization",
                                 parent=parent)

    # Group.objects.create() leaves uuid=None (it is lazily assigned), and the
    # dispatcher's group_uuid branch would silently no-op against a null uuid.
    opts.tenant_uuid = tenant.get_uuid()
    opts.tenant_id = tenant.pk
    opts.child_uuid = child.get_uuid()


def _begin(opts, redirect_uri, group_param=None):
    """Anonymous GET /begin with a redirect_uri and optional group context."""
    url = (f"/api/auth/oauth/{PROVIDER}/begin"
           f"?redirect_uri={quote(redirect_uri, safe='')}")
    if group_param:
        url = f"{url}&{group_param}"
    return opts.client.get(url)


def _assert_refused(resp, context):
    body = resp.response
    assert resp.status_code == 400, (
        f"{context}: the attacker origin must be refused with 400, got "
        f"{resp.status_code}: {body}")
    assert body.get("error") == REFUSAL, (
        f"{context}: expected the existing refusal message {REFUSAL!r} (shared "
        f"verbatim with the gated-destination refusal so the two are not "
        f"distinguishable), got {body.get('error')!r}")
    data = body.get("data") or {}
    assert not data.get("auth_url"), (
        f"{context}: a refused begin must not return an auth_url, got "
        f"{data.get('auth_url')!r}")
    assert not data.get("state"), (
        f"{context}: a refused begin must not mint OAuth state — the state is "
        f"what carries frontend_uri to the callback bounce. Got "
        f"{data.get('state')!r}")


@th.django_unit_test("oauth: group metadata cannot widen the redirect allowlist")
def test_group_metadata_cannot_widen_oauth_allowlist(opts):
    """The named regression: an anonymous caller names a group whose plain
    metadata lists an attacker origin, and must NOT get a usable begin."""
    resp = _begin(opts, ATTACKER_URI, f"group_uuid={opts.tenant_uuid}")
    _assert_refused(resp, "group_uuid names a group whose metadata lists the origin")


@th.django_unit_test("oauth: an ANCESTOR's metadata cannot widen the allowlist")
def test_parent_group_metadata_cannot_widen_oauth_allowlist(opts):
    """get_metadata_value walks the parent chain, so removing only the direct
    read would leave the inherited one live."""
    resp = _begin(opts, ATTACKER_URI, f"group_uuid={opts.child_uuid}")
    _assert_refused(resp, "group_uuid names a child whose PARENT lists the origin")


@th.django_unit_test("oauth: ?group=<id> cannot widen the redirect allowlist")
def test_group_id_param_cannot_widen_oauth_allowlist(opts):
    """`?group=<int>` is a separate dispatcher branch from `?group_uuid=`."""
    resp = _begin(opts, ATTACKER_URI, f"group={opts.tenant_id}")
    _assert_refused(resp, "group=<id> names a group whose metadata lists the origin")


@th.django_unit_test("oauth: the redirect allowlist does not vary with group context")
def test_oauth_allowlist_does_not_depend_on_group_context(opts):
    """The effective allowlist is a pure function of deployment config.

    Both legs matter: the attacker origin is refused identically with and
    without group context (nothing a caller supplies widens the list), and the
    pinned global entry is admitted identically with and without it (a
    legitimate white-label flow that passes group context is not collateral
    damage).
    """
    bare = _begin(opts, ATTACKER_URI)
    _assert_refused(bare, "no group context at all")
    with_group = _begin(opts, ATTACKER_URI, f"group_uuid={opts.tenant_uuid}")
    _assert_refused(with_group, "group context supplied")
    assert bare.response.get("error") == with_group.response.get("error"), (
        f"group context must not change the refusal at all, got "
        f"{bare.response.get('error')!r} vs {with_group.response.get('error')!r}")

    for label, param in (("without group context", None),
                         ("with group context", f"group_uuid={opts.tenant_uuid}")):
        ok = _begin(opts, ALLOWED_URI, param)
        assert ok.status_code == 200, (
            f"a redirect_uri under the pinned global ALLOWED_REDIRECT_URLS entry "
            f"must still be admitted {label}, got {ok.status_code}: {ok.response}")
        assert ok.response.data.state, (
            f"an admitted begin must mint OAuth state {label}: {ok.response}")
