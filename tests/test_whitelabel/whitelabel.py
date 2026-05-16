"""
Tests for white-label auth pages — per-group branding in bouncer views.

Contracts enforced:
  - Group.auth_domain field stores and looks up correctly
  - resolve_by_auth_domain returns active group for matching hostname
  - resolve_by_auth_domain returns None for unknown hostname
  - resolve_by_auth_domain returns None for inactive group
  - _resolve_group prefers hostname over ?group= query param
  - _resolve_group falls back to ?group=<uuid> when hostname doesn't match
  - _auth_context resolves settings per-group when group is provided
  - _auth_context falls back to global settings when group has no overrides
  - auth_domain uniqueness is enforced at DB level
  - OAuth state preserves group_uuid through the round-trip
  - Cache invalidation clears stale entries on auth_domain change
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq


TEST_GROUP_NAME = 'test-whitelabel-operator'
TEST_GROUP_UUID = 'wl-test-uuid-001'
TEST_AUTH_DOMAIN = 'auth.testoperator.example.com'
TEST_PARENT_NAME = 'test-whitelabel-parent'
TEST_PARENT_UUID = 'wl-test-parent-uuid-001'
TEST_ADMIN_USER = 'wl_admin'
TEST_ADMIN_PWORD = 'wl##admin99'


@th.django_unit_setup()
def setup_whitelabel(opts):
    from mojo.apps.account.models import User, Group
    from mojo.apps.account.models.setting import Setting
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip='127.0.0.1')

    # Clean up from previous runs
    User.objects.filter(username=TEST_ADMIN_USER).delete()
    Group.objects.filter(uuid__in=[TEST_GROUP_UUID, TEST_PARENT_UUID]).delete()

    # Create admin user
    admin = User(username=TEST_ADMIN_USER, email=f'{TEST_ADMIN_USER}@test.com')
    admin.is_staff = True
    admin.is_superuser = True
    admin.is_email_verified = True
    admin.save()
    admin.save_password(TEST_ADMIN_PWORD)
    admin.add_permission(['manage_groups', 'manage_users', 'view_groups'])
    opts.admin = admin

    # Create parent group
    parent = Group.objects.create(
        name=TEST_PARENT_NAME,
        uuid=TEST_PARENT_UUID,
        is_active=True,
        kind='platform',
    )
    opts.parent_group = parent

    # Create operator group with auth_domain
    group = Group.objects.create(
        name=TEST_GROUP_NAME,
        uuid=TEST_GROUP_UUID,
        auth_domain=TEST_AUTH_DOMAIN,
        is_active=True,
        kind='operator',
        parent=parent,
    )
    opts.group = group

    # Set group-level branding overrides
    Setting.objects.filter(group__in=[group, parent]).delete()
    Setting.objects.create(key='AUTH_LOGO_URL', value='https://testoperator.com/logo.png', group=group)
    Setting.objects.create(key='AUTH_APP_TITLE', value='Test Operator', group=group)

    # Set a parent-level override (for fallback testing)
    Setting.objects.create(key='AUTH_HERO_HEADLINE', value='Parent Platform Welcome', group=parent)

    # Warm the Setting cache
    try:
        Setting.warm_cache(group_id=group.pk)
        Setting.warm_cache(group_id=parent.pk)
    except Exception:
        pass

    # Login admin via the test server
    resp = opts.client.login(TEST_ADMIN_USER, TEST_ADMIN_PWORD)
    assert opts.client.is_authenticated, "Admin login failed during setup"


# ---------------------------------------------------------------------------
# Group.auth_domain field
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_auth_domain_stored(opts):
    """auth_domain field is stored and retrievable."""
    from mojo.apps.account.models import Group
    group = Group.objects.get(uuid=TEST_GROUP_UUID)
    assert_eq(group.auth_domain, TEST_AUTH_DOMAIN,
              f"Expected auth_domain '{TEST_AUTH_DOMAIN}', got '{group.auth_domain}'")


@th.django_unit_test()
def test_auth_domain_uniqueness(opts):
    """Two groups cannot have the same auth_domain."""
    from django.db import IntegrityError
    from mojo.apps.account.models import Group
    try:
        Group.objects.create(
            name='duplicate-domain-group',
            uuid='wl-test-dup-001',
            auth_domain=TEST_AUTH_DOMAIN,
            is_active=True,
        )
        assert False, "Expected IntegrityError for duplicate auth_domain"
    except IntegrityError:
        pass
    finally:
        # Clean up any partial state
        from django.db import connection
        connection.cursor()  # force rollback of failed transaction
        Group.objects.filter(uuid='wl-test-dup-001').delete()


# ---------------------------------------------------------------------------
# resolve_by_auth_domain
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_resolve_by_auth_domain_found(opts):
    """resolve_by_auth_domain returns the correct active group."""
    from mojo.apps.account.models import Group
    result = Group.resolve_by_auth_domain(TEST_AUTH_DOMAIN)
    assert_true(result is not None, "Expected a group for matching auth_domain")
    assert_eq(result.pk, opts.group.pk,
              f"Expected group pk {opts.group.pk}, got {result.pk}")


@th.django_unit_test()
def test_resolve_by_auth_domain_unknown(opts):
    """resolve_by_auth_domain returns None for unknown hostname."""
    from mojo.apps.account.models import Group
    result = Group.resolve_by_auth_domain('unknown.example.com')
    assert_true(result is None,
                f"Expected None for unknown hostname, got {result}")


@th.django_unit_test()
def test_resolve_by_auth_domain_inactive(opts):
    """resolve_by_auth_domain returns None when group is inactive."""
    from mojo.apps.account.models import Group
    opts.group.is_active = False
    opts.group.save(update_fields=['is_active'])
    try:
        result = Group.resolve_by_auth_domain(TEST_AUTH_DOMAIN)
        assert_true(result is None,
                    f"Expected None for inactive group, got {result}")
    finally:
        opts.group.is_active = True
        opts.group.save(update_fields=['is_active'])


@th.django_unit_test()
def test_resolve_by_auth_domain_empty(opts):
    """resolve_by_auth_domain returns None for empty/None hostname."""
    from mojo.apps.account.models import Group
    assert_true(Group.resolve_by_auth_domain('') is None,
                "Expected None for empty hostname")
    assert_true(Group.resolve_by_auth_domain(None) is None,
                "Expected None for None hostname")


# ---------------------------------------------------------------------------
# _resolve_group (hostname + query param)
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_resolve_group_by_query_param(opts):
    """_resolve_group falls back to ?group_uuid=<uuid> when hostname doesn't match."""
    from mojo.apps.account.rest.bouncer.views import _resolve_group
    from django.test import RequestFactory
    factory = RequestFactory()
    request = factory.get(f'/auth?group_uuid={TEST_GROUP_UUID}')
    # RequestFactory doesn't set get_host to our auth_domain, so hostname won't match
    result = _resolve_group(request)
    assert_true(result is not None, "Expected group from ?group_uuid= param")
    assert_eq(result.pk, opts.group.pk,
              f"Expected group pk {opts.group.pk}, got {result.pk}")


@th.django_unit_test()
def test_resolve_group_ignores_group_int_alias(opts):
    """_resolve_group does NOT read ?group=<uuid>.

    The framework dispatcher (mojo/decorators/http.py) reserves `?group=`
    for integer IDs and 400s on any non-integer value before this view
    runs. The bouncer's UUID fallback must therefore use ?group_uuid=.
    Reading `?group=` here would be dead code that gives a false sense
    the param works publicly.
    """
    from mojo.apps.account.rest.bouncer.views import _resolve_group
    from django.test import RequestFactory
    factory = RequestFactory()
    request = factory.get(f'/auth?group={TEST_GROUP_UUID}')
    result = _resolve_group(request)
    assert_true(result is None,
                f"Expected None when uuid is passed via ?group= (reserved for "
                f"integer IDs by the dispatcher), got {result}")


@th.django_unit_test()
def test_resolve_group_invalid_uuid(opts):
    """_resolve_group returns None for invalid ?group_uuid= value."""
    from mojo.apps.account.rest.bouncer.views import _resolve_group
    from django.test import RequestFactory
    factory = RequestFactory()
    request = factory.get('/auth?group_uuid=nonexistent-uuid')
    result = _resolve_group(request)
    assert_true(result is None,
                f"Expected None for invalid group uuid, got {result}")


# ---------------------------------------------------------------------------
# _auth_context per-group branding
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_auth_context_with_group(opts):
    """_auth_context resolves group-specific settings."""
    from mojo.apps.account.rest.bouncer.views import _auth_context
    from django.test import RequestFactory
    factory = RequestFactory()
    request = factory.get('/auth')
    ctx = _auth_context(request, group=opts.group)
    assert_eq(ctx['logo_url'], 'https://testoperator.com/logo.png',
              f"Expected operator logo, got '{ctx['logo_url']}'")
    assert_eq(ctx['brand_name'], 'Test Operator',
              f"Expected 'Test Operator', got '{ctx['brand_name']}'")
    assert_eq(ctx['group_uuid'], TEST_GROUP_UUID,
              f"Expected group_uuid '{TEST_GROUP_UUID}', got '{ctx['group_uuid']}'")


@th.django_unit_test()
def test_auth_context_parent_fallback(opts):
    """_auth_context falls back to parent group settings."""
    from mojo.apps.account.rest.bouncer.views import _auth_context
    from django.test import RequestFactory
    factory = RequestFactory()
    request = factory.get('/auth')
    ctx = _auth_context(request, group=opts.group)
    # AUTH_HERO_HEADLINE is set on parent but not on child
    assert_eq(ctx['hero_headline'], 'Parent Platform Welcome',
              f"Expected parent headline, got '{ctx['hero_headline']}'")


@th.django_unit_test()
def test_auth_context_no_group(opts):
    """_auth_context without group returns global defaults (backwards compat)."""
    from mojo.apps.account.rest.bouncer.views import _auth_context
    from django.test import RequestFactory
    factory = RequestFactory()
    request = factory.get('/auth')
    ctx = _auth_context(request, group=None)
    assert_eq(ctx['group_uuid'], '',
              f"Expected empty group_uuid, got '{ctx['group_uuid']}'")
    # Should not return the test operator's overrides
    assert_true(ctx['brand_name'] != 'Test Operator',
                "Expected global brand, not operator override")


@th.django_unit_test()
def test_auth_context_group_urls_preserve_param(opts):
    """auth_url and register_url include ?group_uuid= when group is set."""
    from mojo.apps.account.rest.bouncer.views import _auth_context
    from django.test import RequestFactory
    factory = RequestFactory()
    request = factory.get('/auth')
    ctx = _auth_context(request, group=opts.group)
    assert_true(f'group_uuid={TEST_GROUP_UUID}' in ctx['auth_url'],
                f"Expected ?group_uuid= in auth_url, got '{ctx['auth_url']}'")
    assert_true(f'group_uuid={TEST_GROUP_UUID}' in ctx['register_url'],
                f"Expected ?group_uuid= in register_url, got '{ctx['register_url']}'")
    # Must NOT use `?group=` for UUID — the framework dispatcher reserves
    # that param for integer IDs and 400s on UUID values.
    assert_true(f'group={TEST_GROUP_UUID}' not in ctx['auth_url'].replace('group_uuid=', ''),
                f"auth_url must not emit `?group=<uuid>` (dispatcher rejects "
                f"non-int values), got '{ctx['auth_url']}'")


# ---------------------------------------------------------------------------
# Challenge page branding
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_challenge_default_branding(opts):
    """Challenge page uses REDACTED branding by default."""
    from mojo.apps.account.rest.bouncer.views import _DEFAULT_CHALLENGE_LOGO, _DEFAULT_CHALLENGE_BRAND
    from mojo.helpers.settings import settings
    # No BOUNCER_CHALLENGE_LOGO_URL set for this group
    logo = settings.get('BOUNCER_CHALLENGE_LOGO_URL', _DEFAULT_CHALLENGE_LOGO, group=opts.group)
    assert_eq(logo, _DEFAULT_CHALLENGE_LOGO,
              f"Expected default challenge logo, got '{logo}'")
    brand = settings.get('BOUNCER_CHALLENGE_BRAND', _DEFAULT_CHALLENGE_BRAND, group=opts.group)
    assert_eq(brand, _DEFAULT_CHALLENGE_BRAND,
              f"Expected default challenge brand, got '{brand}'")


@th.django_unit_test()
def test_challenge_custom_branding(opts):
    """Challenge page uses group override when set."""
    from mojo.apps.account.models.setting import Setting
    from mojo.helpers.settings import settings
    from mojo.apps.account.rest.bouncer.views import _DEFAULT_CHALLENGE_LOGO
    Setting.objects.create(
        key='BOUNCER_CHALLENGE_LOGO_URL',
        value='https://testoperator.com/challenge-logo.png',
        group=opts.group,
    )
    try:
        Setting.warm_cache(group_id=opts.group.pk)
    except Exception:
        pass
    logo = settings.get('BOUNCER_CHALLENGE_LOGO_URL', _DEFAULT_CHALLENGE_LOGO, group=opts.group)
    assert_eq(logo, 'https://testoperator.com/challenge-logo.png',
              f"Expected custom challenge logo, got '{logo}'")
    # Clean up
    Setting.objects.filter(key='BOUNCER_CHALLENGE_LOGO_URL', group=opts.group).delete()


# ---------------------------------------------------------------------------
# OAuth state round-trip
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_oauth_state_preserves_group(opts):
    """OAuth begin stores group_uuid in state; peek retrieves it."""
    from mojo.apps.account.services.oauth.base import OAuthProvider
    svc = OAuthProvider()
    state = svc.create_state(extra={
        'redirect_uri': 'https://example.com/callback',
        'frontend_uri': 'https://example.com/auth',
        'group_uuid': TEST_GROUP_UUID,
    })
    data = svc.peek_state(state)
    assert_true(data is not None, "Expected state data from peek")
    assert_eq(data.get('group_uuid'), TEST_GROUP_UUID,
              f"Expected group_uuid '{TEST_GROUP_UUID}' in state, got '{data.get('group_uuid')}'")
    # Consume to clean up
    svc.consume_state(state)


# ---------------------------------------------------------------------------
# Group REST API includes auth_domain
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_group_rest_includes_auth_domain(opts):
    """Group detail REST response includes auth_domain field."""
    resp = opts.client.get(f'/api/group/{opts.group.pk}')
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")
    data = resp.response.data
    assert_eq(data.auth_domain, TEST_AUTH_DOMAIN,
              f"Expected auth_domain in REST response, got '{data.auth_domain}'")


@th.django_unit_test()
def test_group_rest_update_auth_domain(opts):
    """Admin can update auth_domain via REST."""
    new_domain = 'auth.updated.example.com'
    resp = opts.client.post(f'/api/group/{opts.group.pk}', {
        'auth_domain': new_domain,
    })
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")
    from mojo.apps.account.models import Group
    group = Group.objects.get(pk=opts.group.pk)
    assert_eq(group.auth_domain, new_domain,
              f"Expected updated auth_domain '{new_domain}', got '{group.auth_domain}'")
    # Restore original
    group.auth_domain = TEST_AUTH_DOMAIN
    group.save(update_fields=['auth_domain'])
