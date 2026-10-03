"""API-key `last_used` is a throttled touch, not a write per request (#6565).

Both key kinds used to UPDATE `last_used` on every authenticated request. The
stamp is now rewritten only when it is unset or older than
API_KEY_TOUCH_SECONDS (default 300), so a burst of requests on one key costs
one UPDATE. Exercised in process through the real auth entry points
(User.validate_jwt for a UserAPIKey JWT, ApiKey.validate_token for a group
key), counting the UPDATE statements each issues against its own table.
"""
import datetime
import time
import uuid

from testit import helpers as th


def _updates_to(captured, table):
    prefix = f'UPDATE "{table}"'
    return [q["sql"] for q in captured if q["sql"].startswith(prefix)]


def _request():
    from objict import objict
    return objict(ip="127.0.0.1", META={}, group=None)


@th.django_unit_test("UserAPIKey: two quick requests stamp last_used with ONE update")
def test_user_api_key_touch_is_throttled(opts):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext
    from mojo.apps.account.models import User, UserAPIKey
    from mojo.apps.account.models import user_api_key as key_module

    email = f"keytouch_{uuid.uuid4().hex[:10]}@touch.test"
    User.objects.filter(username=email).delete()
    user = User.objects.create_user(username=email, email=email, password="Touch##6565")
    table = UserAPIKey._meta.db_table
    try:
        package = UserAPIKey.create_for_user(user, expire_days=1, label="touch test")
        assert UserAPIKey.objects.get(pk=package.id).last_used is None, (
            "a new key must start with last_used unset")

        with CaptureQueriesContext(connection) as ctx:
            first, err = User.validate_jwt(package.token, _request())
            assert err is None and first is not None and first.pk == user.pk, (
                f"the key must authenticate its user, got user={first} err={err!r}")
            stamped = UserAPIKey.objects.get(pk=package.id).last_used
            time.sleep(1)
            second, err = User.validate_jwt(package.token, _request())
            assert err is None and second is not None, (
                f"the key must authenticate on the second request, got err={err!r}")
        updates = _updates_to(ctx.captured_queries, table)
        assert len(updates) == 1, (
            f"two requests 1s apart must issue exactly one {table} UPDATE, "
            f"got {len(updates)}: {updates}")
        assert stamped is not None, "the first request must stamp last_used"
        after = UserAPIKey.objects.get(pk=package.id).last_used
        assert after == stamped, (
            f"the second request inside the window must leave last_used alone, "
            f"was {stamped} now {after}")

        # A stamp older than the window is rewritten on the next request.
        stale = stamped - datetime.timedelta(seconds=key_module.API_KEY_TOUCH_SECONDS + 60)
        UserAPIKey.objects.filter(pk=package.id).update(last_used=stale)
        with CaptureQueriesContext(connection) as ctx:
            third, err = User.validate_jwt(package.token, _request())
            assert err is None and third is not None, (
                f"the key must authenticate after the window, got err={err!r}")
        updates = _updates_to(ctx.captured_queries, table)
        assert len(updates) == 1, (
            f"a stale stamp must be rewritten with one UPDATE, got {len(updates)}: {updates}")
        refreshed = UserAPIKey.objects.get(pk=package.id).last_used
        assert refreshed > stamped, (
            f"a stale stamp must move forward, was {stale} now {refreshed}")
    finally:
        UserAPIKey.objects.filter(user=user).delete()
        user.delete()


@th.django_unit_test("ApiKey: two quick requests stamp last_used with ONE update")
def test_group_api_key_touch_is_throttled(opts):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext
    from mojo.apps.account.models import ApiKey
    from mojo.apps.account.models import api_key as key_module
    from mojo.apps.account.models.group import Group

    name = f"keytouch-{uuid.uuid4().hex[:10]}"
    Group.objects.filter(name=name).delete()
    group = Group(name=name, kind="default")
    group.save()
    table = ApiKey._meta.db_table
    try:
        api_key, token = ApiKey.create_for_group(group, "touch test")
        assert api_key.last_used is None, "a new key must start with last_used unset"

        with CaptureQueriesContext(connection) as ctx:
            first, err = ApiKey.validate_token(token, _request())
            assert err is None and first is not None, (
                f"the key must authenticate, got err={err!r}")
            stamped = ApiKey.objects.get(pk=api_key.pk).last_used
            time.sleep(1)
            second, err = ApiKey.validate_token(token, _request())
            assert err is None and second is not None, (
                f"the key must authenticate on the second request, got err={err!r}")
        updates = _updates_to(ctx.captured_queries, table)
        assert len(updates) == 1, (
            f"two requests 1s apart must issue exactly one {table} UPDATE, "
            f"got {len(updates)}: {updates}")
        assert stamped is not None, "the first request must stamp last_used"
        after = ApiKey.objects.get(pk=api_key.pk).last_used
        assert after == stamped, (
            f"the second request inside the window must leave last_used alone, "
            f"was {stamped} now {after}")

        stale = stamped - datetime.timedelta(seconds=key_module.API_KEY_TOUCH_SECONDS + 60)
        ApiKey.objects.filter(pk=api_key.pk).update(last_used=stale)
        with CaptureQueriesContext(connection) as ctx:
            third, err = ApiKey.validate_token(token, _request())
            assert err is None and third is not None, (
                f"the key must authenticate after the window, got err={err!r}")
        updates = _updates_to(ctx.captured_queries, table)
        assert len(updates) == 1, (
            f"a stale stamp must be rewritten with one UPDATE, got {len(updates)}: {updates}")
        refreshed = ApiKey.objects.get(pk=api_key.pk).last_used
        assert refreshed > stamped, (
            f"a stale stamp must move forward, was {stale} now {refreshed}")
    finally:
        ApiKey.objects.filter(group=group).delete()
        group.delete()
