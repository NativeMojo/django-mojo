"""
Tests for the auth config — resolution precedence, validation, the Group
save-time guard, and the public GET /api/auth/config endpoint.

Contracts enforced:
  - resolve_auth_config: defaults <- AUTH_CONFIG <- group <- parent chain
  - deep-merge: dicts merge key-by-key, lists replace wholesale
  - validate_auth_config rejects bad method tokens / enums / custom_css
  - Group.on_rest_pre_save rejects an invalid metadata.auth_config at write time
  - GET /api/auth/config returns the resolved public config
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq


AC_GROUP_NAME = 'test-auth-config-group'
AC_GROUP_UUID = 'ac01234567890abcdef01234567890ab'
AC_PARENT_NAME = 'test-auth-config-parent'
AC_PARENT_UUID = 'ac99234567890abcdef01234567890ab'
AC_ADMIN_USER = 'ac_admin'
AC_ADMIN_PWORD = 'ac##admin99'

ALL_LOGIN_METHODS = ["password", "sms", "passkey", "magic", "google", "apple"]


@th.django_unit_setup()
def setup_auth_config(opts):
    from mojo.apps.account.models import User, Group
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip='127.0.0.1')

    # Setup cleans up before creating — tests run on long-lived databases.
    User.objects.filter(username=AC_ADMIN_USER).delete()
    Group.objects.filter(uuid__in=[AC_GROUP_UUID, AC_PARENT_UUID]).delete()

    admin = User(username=AC_ADMIN_USER, email=f'{AC_ADMIN_USER}@test.com')
    admin.is_staff = True
    admin.is_superuser = True
    admin.is_email_verified = True
    admin.save()
    admin.save_password(AC_ADMIN_PWORD)
    admin.add_permission(['manage_groups', 'view_groups'])
    opts.admin = admin

    parent = Group.objects.create(
        name=AC_PARENT_NAME, uuid=AC_PARENT_UUID, is_active=True, kind='platform')
    group = Group.objects.create(
        name=AC_GROUP_NAME, uuid=AC_GROUP_UUID, is_active=True,
        kind='operator', parent=parent)
    opts.parent = parent
    opts.group = group

    opts.client.login(AC_ADMIN_USER, AC_ADMIN_PWORD)
    assert opts.client.is_authenticated, "admin login failed during setup"


def _reset_metadata(*groups):
    for g in groups:
        g.metadata = {}
        g.save(update_fields=["metadata"])


# ---------------------------------------------------------------------------
# resolve_auth_config — precedence and merge semantics
# ---------------------------------------------------------------------------

@th.django_unit_test("resolve_auth_config returns code defaults when nothing is set")
def test_resolve_defaults(opts):
    from mojo.apps.account.services import auth_config as ac
    cfg = ac.resolve_auth_config(group=None)
    assert_eq(cfg.theme.app_title, "DJANGO MOJO",
              f"default app_title must be 'DJANGO MOJO', got {cfg.theme.app_title!r}")
    assert_eq(cfg.registration.passkey_prompt, "off",
              f"default passkey_prompt must be 'off', got {cfg.registration.passkey_prompt!r}")
    assert cfg.registration.enabled is True, \
        "registration must be enabled by default"
    assert_eq(sorted(cfg.login.methods), sorted(ALL_LOGIN_METHODS),
              f"default login.methods must be all six methods, got {cfg.login.methods}")


@th.django_unit_test("resolve_auth_config: group metadata.auth_config overrides defaults")
def test_resolve_group_override(opts):
    from mojo.apps.account.services import auth_config as ac
    opts.group.metadata = {"auth_config": {
        "theme": {"app_title": "Acme"},
        "login": {"methods": ["passkey", "sms"]},
    }}
    opts.group.save(update_fields=["metadata"])
    try:
        cfg = ac.resolve_auth_config(group=opts.group)
        assert_eq(cfg.theme.app_title, "Acme",
                  f"group app_title override must win, got {cfg.theme.app_title!r}")
        assert_eq(list(cfg.login.methods), ["passkey", "sms"],
                  f"login.methods list must replace wholesale, got {cfg.login.methods}")
        # dict-merge: theme keys not overridden keep their defaults
        assert_eq(cfg.theme.layout, "card",
                  f"un-overridden theme keys keep defaults (dict merge), got {cfg.theme.layout!r}")
    finally:
        _reset_metadata(opts.group)


@th.django_unit_test("resolve_auth_config: parent auth config is inherited by the child")
def test_resolve_parent_chain(opts):
    from mojo.apps.account.services import auth_config as ac
    opts.parent.metadata = {"auth_config": {"theme": {
        "app_title": "Parent Brand", "hero_headline": "From Parent"}}}
    opts.parent.save(update_fields=["metadata"])
    opts.group.metadata = {"auth_config": {"theme": {"app_title": "Child Brand"}}}
    opts.group.save(update_fields=["metadata"])
    try:
        cfg = ac.resolve_auth_config(group=opts.group)
        assert_eq(cfg.theme.app_title, "Child Brand",
                  f"child theme must override the parent, got {cfg.theme.app_title!r}")
        assert_eq(cfg.theme.hero_headline, "From Parent",
                  f"child must inherit the parent's hero_headline, got {cfg.theme.hero_headline!r}")
    finally:
        _reset_metadata(opts.parent, opts.group)


# ---------------------------------------------------------------------------
# validate_auth_config
# ---------------------------------------------------------------------------

@th.django_unit_test("validate_auth_config rejects an unknown login method token")
def test_validate_bad_method(opts):
    from mojo.apps.account.services import auth_config as ac
    from mojo import errors as merrors
    try:
        ac.validate_auth_config({"login": {"methods": ["password", "telepathy"]}})
        assert False, "validator must reject an unknown login method"
    except merrors.ValueException as e:
        assert "telepathy" in str(e), \
            f"error must name the offending method, got: {e}"


@th.django_unit_test("validate_auth_config rejects an empty login.methods")
def test_validate_empty_methods(opts):
    from mojo.apps.account.services import auth_config as ac
    from mojo import errors as merrors
    try:
        ac.validate_auth_config({"login": {"methods": []}})
        assert False, "validator must reject an empty login.methods (locks everyone out)"
    except merrors.ValueException:
        pass


@th.django_unit_test("validate_auth_config rejects a bad passkey_prompt enum value")
def test_validate_bad_prompt(opts):
    from mojo.apps.account.services import auth_config as ac
    from mojo import errors as merrors
    try:
        ac.validate_auth_config({"registration": {"passkey_prompt": "maybe"}})
        assert False, "validator must reject an unknown passkey_prompt value"
    except merrors.ValueException:
        pass


@th.django_unit_test("validate_auth_config rejects custom_css containing '<' (XSS breakout)")
def test_validate_css_angle_bracket(opts):
    from mojo.apps.account.services import auth_config as ac
    from mojo import errors as merrors
    try:
        ac.validate_auth_config({"theme": {
            "custom_css": "x{}</style><script>alert(1)</script>"}})
        assert False, "validator must reject custom_css that can break out of <style>"
    except merrors.ValueException:
        pass


@th.django_unit_test("validate_auth_config rejects custom_css with an external URL")
def test_validate_css_external_url(opts):
    from mojo.apps.account.services import auth_config as ac
    from mojo import errors as merrors
    try:
        ac.validate_auth_config({"theme": {
            "custom_css": "body{background:url('http://evil.example.com/leak')}"}})
        assert False, "validator must reject custom_css that loads external resources"
    except merrors.ValueException:
        pass


@th.django_unit_test("validate_auth_config accepts a fully valid config")
def test_validate_ok(opts):
    from mojo.apps.account.services import auth_config as ac
    # Must not raise.
    ac.validate_auth_config({
        "theme": {"custom_css": "body{color:#222}", "layout": "fullscreen"},
        "login": {"methods": ["passkey", "sms"]},
        "registration": {
            "passkey_prompt": "required",
            "methods": ["password"],
            "fields": [
                {"name": "email", "required": True},
                {"name": "password", "required": True},
            ],
        },
    })


# ---------------------------------------------------------------------------
# Group save-time guard (on_rest_pre_save)
# ---------------------------------------------------------------------------

@th.django_unit_test("Group REST rejects an invalid metadata.auth_config on save")
def test_group_rest_rejects_bad_config(opts):
    resp = opts.client.post(f'/api/group/{opts.group.pk}', {
        "metadata": {"auth_config": {"login": {"methods": ["not-a-method"]}}},
    })
    assert resp.status_code in (400, 422), \
        f"saving an invalid auth config must be rejected, got {resp.status_code}: " \
        f"{opts.client.last_response.body}"


@th.django_unit_test("Group REST accepts a valid metadata.auth_config on save")
def test_group_rest_accepts_good_config(opts):
    resp = opts.client.post(f'/api/group/{opts.group.pk}', {
        "metadata": {"auth_config": {"login": {"methods": ["passkey", "sms"]}}},
    })
    assert resp.status_code == 200, \
        f"a valid auth config must save, got {resp.status_code}: " \
        f"{opts.client.last_response.body}"
    _reset_metadata(opts.group)


# ---------------------------------------------------------------------------
# GET /api/auth/config
# ---------------------------------------------------------------------------

@th.django_unit_test("GET /api/auth/config returns the resolved public config for a group")
def test_public_config_endpoint(opts):
    opts.group.metadata = {"auth_config": {
        "theme": {"app_title": "Acme App"},
        "login": {"methods": ["passkey"]},
    }}
    opts.group.save(update_fields=["metadata"])
    try:
        resp = opts.client.get(f'/api/auth/config?group_uuid={AC_GROUP_UUID}')
        assert_eq(resp.status_code, 200,
                  f"config endpoint must return 200, got {resp.status_code}")
        data = resp.response.data
        assert_eq(data.theme.app_title, "Acme App",
                  f"theme must reflect the group config, got {data.theme.app_title!r}")
        assert_eq(list(data.login.methods), ["passkey"],
                  f"login.methods must reflect the group config, got {data.login.methods}")
    finally:
        _reset_metadata(opts.group)


@th.django_unit_test("GET /api/auth/config returns deployment defaults when no group is given")
def test_public_config_endpoint_default(opts):
    resp = opts.client.get('/api/auth/config')
    assert_eq(resp.status_code, 200,
              f"config endpoint must return 200 with no group, got {resp.status_code}")
    data = resp.response.data
    assert_eq(sorted(data.login.methods), sorted(ALL_LOGIN_METHODS),
              f"default config must offer all login methods, got {data.login.methods}")
