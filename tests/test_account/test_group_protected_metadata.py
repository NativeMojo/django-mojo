"""ITEM-030 regression — JSONField `__replace` must not bypass PROTECTED_JSON_PERMS.

Bug: on_rest_update_jsonfield ran the protected-JSON guard
(_can_edit_protected_json + the meta:protected_changed audit) only on the merge
branch. A top-level {"__replace": true} took the replace branch, which setattr'd
the whole JSONField unchecked — so a group-confined ApiKey holding only
manage_group (a SAVE_PERMS grant, not a PROTECTED_JSON_PERMS one) could rewrite
Group.metadata.protected.* wholesale, silently. The outer non-dict branch
(posting a list/string over a metadata dict) had the same hole, and
_can_edit_protected_json read user.is_superuser directly, which an ApiKey
doesn't define (AttributeError → 500 instead of a clean 403 on the merge path).

Fix: the guard + audit run for merge, replace (__replace / JSON_REPLACE_FIELDS),
and non-dict overwrites — in both directions (incoming carries "protected", or
an existing "protected" subtree would be clobbered) — and the superuser check
uses getattr so ApiKey callers get 403, not 500.

Style mirrors tests/test_account/test_group_save_perms.py.
"""
import uuid as _uuid
from testit import helpers as th

PROTECTED = {"payments": {"allowed_origins": ["https://real.example"]}}
EVIL = {"payments": {"allowed_origins": ["https://evil.example"]}}


def _key_auth(opts, token):
    opts.client.logout()
    opts.client.bearer = "apikey"
    opts.client.access_token = token
    opts.client.is_authenticated = True


@th.django_unit_setup()
def setup_group_protected_metadata(opts):
    """Two groups: one carrying a metadata.protected subtree, one without.
    Two group-confined ApiKeys on the protected group: manage_group only
    (SAVE_PERMS but not PROTECTED_JSON_PERMS) and manage_group + admin_verify
    (privileged). A third manage_group-only key on the unprotected group."""
    from mojo.apps.account.models import Group, ApiKey

    # delete-before-create — tests run against a long-lived DB
    ApiKey.objects.filter(name__startswith="grpprot_key_").delete()
    Group.objects.filter(name__startswith="grpprot_grp_").delete()

    tag = _uuid.uuid4().hex[:8]
    grp = Group.objects.create(
        name=f"grpprot_grp_{tag}",
        kind="organization",
        is_active=True,
        metadata={"motto": "original", "protected": PROTECTED},
    )
    opts.grp_id = grp.pk

    plain_grp = Group.objects.create(
        name=f"grpprot_grp_plain_{tag}",
        kind="organization",
        is_active=True,
        metadata={"motto": "original"},
    )
    opts.plain_grp_id = plain_grp.pk

    _, opts.limited_token = ApiKey.create_for_group(
        group=grp, name=f"grpprot_key_limited_{tag}",
        permissions={"manage_group": True})
    _, opts.priv_token = ApiKey.create_for_group(
        group=grp, name=f"grpprot_key_priv_{tag}",
        permissions={"manage_group": True, "admin_verify": True})
    _, opts.plain_grp_token = ApiKey.create_for_group(
        group=plain_grp, name=f"grpprot_key_plain_{tag}",
        permissions={"manage_group": True})


@th.django_unit_test("ITEM-030: __replace carrying protected is 403 for manage_group-only key")
def test_replace_with_protected_denied(opts):
    """The core regression (WMX-API-127 repro). Pre-fix this returned 200 and
    rewrote metadata.protected wholesale."""
    from mojo.apps.account.models import Group
    _key_auth(opts, opts.limited_token)
    try:
        resp = opts.client.post(
            f"/api/group/{opts.grp_id}",
            {"metadata": {"__replace": True, "protected": EVIL}})
        assert resp.status_code in (401, 403), \
            f"__replace with protected must be denied, got {resp.status_code}: {opts.client.last_response.body}"
        grp = Group.objects.get(pk=opts.grp_id)
        assert grp.metadata.get("protected") == PROTECTED, \
            f"SECURITY: protected metadata was rewritten: {grp.metadata.get('protected')!r}"
        assert grp.metadata.get("motto") == "original", \
            f"metadata was replaced despite the 403: {grp.metadata!r}"
    finally:
        opts.client.logout()


@th.django_unit_test("ITEM-030: __replace WITHOUT protected still 403s when it would clobber an existing protected subtree")
def test_replace_clobbering_protected_denied(opts):
    """Direction two: the incoming dict carries no protected key, but a wholesale
    replace would silently delete the existing one."""
    from mojo.apps.account.models import Group
    _key_auth(opts, opts.limited_token)
    try:
        resp = opts.client.post(
            f"/api/group/{opts.grp_id}",
            {"metadata": {"__replace": True, "motto": "rewritten"}})
        assert resp.status_code in (401, 403), \
            f"__replace clobbering protected must be denied, got {resp.status_code}: {opts.client.last_response.body}"
        grp = Group.objects.get(pk=opts.grp_id)
        assert grp.metadata.get("protected") == PROTECTED, \
            f"SECURITY: protected metadata was clobbered: {grp.metadata!r}"
    finally:
        opts.client.logout()


@th.django_unit_test("ITEM-030: merge touching protected is 403 for manage_group-only key (not 500)")
def test_merge_with_protected_denied(opts):
    """The merge branch was always guarded, but an ApiKey caller hit
    user.is_superuser (undefined on ApiKey) and got a 500. Must be a clean 403."""
    from mojo.apps.account.models import Group
    _key_auth(opts, opts.limited_token)
    try:
        resp = opts.client.post(
            f"/api/group/{opts.grp_id}",
            {"metadata": {"protected": EVIL}})
        assert resp.status_code in (401, 403), \
            f"merge with protected must be a clean 403, got {resp.status_code}: {opts.client.last_response.body}"
        grp = Group.objects.get(pk=opts.grp_id)
        assert grp.metadata.get("protected") == PROTECTED, \
            f"SECURITY: protected metadata was merged over: {grp.metadata.get('protected')!r}"
    finally:
        opts.client.logout()


@th.django_unit_test("ITEM-030: non-dict overwrite of a protected-bearing JSONField is 403")
def test_nondict_overwrite_denied(opts):
    """The outer else branch: posting a list over a metadata dict clobbers the
    protected subtree with no check."""
    from mojo.apps.account.models import Group
    _key_auth(opts, opts.limited_token)
    try:
        resp = opts.client.post(f"/api/group/{opts.grp_id}", {"metadata": []})
        assert resp.status_code in (401, 403), \
            f"non-dict overwrite of protected metadata must be denied, got {resp.status_code}: {opts.client.last_response.body}"
        grp = Group.objects.get(pk=opts.grp_id)
        assert isinstance(grp.metadata, dict) and grp.metadata.get("protected") == PROTECTED, \
            f"SECURITY: protected metadata was wiped by a non-dict overwrite: {grp.metadata!r}"
    finally:
        opts.client.logout()


@th.django_unit_test("ITEM-030: __replace WITHOUT protected content anywhere still works (200)")
def test_replace_without_protected_allowed(opts):
    """No over-blocking: when neither the incoming dict nor the existing value
    has a protected key, a manage_group key may still replace wholesale."""
    from mojo.apps.account.models import Group
    _key_auth(opts, opts.plain_grp_token)
    try:
        resp = opts.client.post(
            f"/api/group/{opts.plain_grp_id}",
            {"metadata": {"__replace": True, "motto": "rewritten"}})
        assert resp.status_code == 200, \
            f"replace of unprotected metadata must succeed, got {resp.status_code}: {opts.client.last_response.body}"
        grp = Group.objects.get(pk=opts.plain_grp_id)
        assert grp.metadata == {"motto": "rewritten"}, \
            f"replace did not persist wholesale; metadata is {grp.metadata!r}"
    finally:
        opts.client.logout()


@th.django_unit_test("ITEM-030: key holding admin_verify CAN __replace protected (200) and it is audited")
def test_privileged_replace_allowed_and_audited(opts):
    """PROTECTED_JSON_PERMS holder replaces wholesale, and the unconditional
    meta:protected_changed audit fires on the replace path too."""
    from mojo.apps.account.models import Group
    from mojo.apps.logit.models import Log
    new_protected = {"payments": {"allowed_origins": ["https://partner.example"]}}
    _key_auth(opts, opts.priv_token)
    try:
        resp = opts.client.post(
            f"/api/group/{opts.grp_id}",
            {"metadata": {"__replace": True, "motto": "replaced", "protected": new_protected}})
        assert resp.status_code == 200, \
            f"admin_verify key must replace protected metadata, got {resp.status_code}: {opts.client.last_response.body}"
        grp = Group.objects.get(pk=opts.grp_id)
        assert grp.metadata.get("protected") == new_protected, \
            f"privileged replace did not persist; metadata is {grp.metadata!r}"
        assert grp.metadata.get("motto") == "replaced", \
            f"privileged replace was not wholesale; metadata is {grp.metadata!r}"
        audits = Log.objects.filter(
            kind="meta:protected_changed",
            model_name="account.Group",
            model_id=opts.grp_id)
        assert audits.exists(), \
            "no meta:protected_changed audit log was written for the replace"
    finally:
        opts.client.logout()
