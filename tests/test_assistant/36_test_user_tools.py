"""
Tests for the assistant users domain tools — query_rate_limits.
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TEST_EMAIL_USERS = 'assistant-users-admin@example.com'
TEST_PASSWORD = 'TestPass1!'


@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
def setup_user_tools(opts):
    from mojo.apps.account.models import User

    User.objects.filter(email=TEST_EMAIL_USERS).delete()
    opts.users_admin = User.objects.create_user(
        username=TEST_EMAIL_USERS, email=TEST_EMAIL_USERS, password=TEST_PASSWORD,
    )
    opts.users_admin.add_permission("view_admin")


@th.django_unit_test()
def test_query_rate_limits_reads_redis(opts):
    """query_rate_limits must reach Redis and report active rate-limit entries
    (rl:* fixed-window and srl:* sliding-window keys), not swallow an error."""
    from mojo.apps.assistant.services.tools.users import _tool_query_rate_limits
    from mojo.helpers.redis import get_connection

    r = get_connection()
    fixed_key = "rl:testit_users_tool:ip:127.0.0.1:0"
    sliding_key = "srl:testit_users_tool:account:0"
    r.delete(fixed_key, sliding_key)
    r.set(fixed_key, 3, ex=60)
    r.zadd(sliding_key, {"1": 1.0, "2": 2.0})
    r.expire(sliding_key, 60)
    try:
        result = _tool_query_rate_limits({}, opts.users_admin)
        assert_true("error" not in result,
                    f"Tool returned an error: {result.get('error')}")
        by_key = {e["key"]: e for e in result["rate_limits"]}
        assert_true(fixed_key in by_key,
                    f"Seeded fixed-window key {fixed_key} missing from results")
        assert_eq(by_key[fixed_key]["count"], 3,
                  "Fixed-window count should reflect the stored value")
        assert_true(by_key[fixed_key]["ttl_seconds"] > 0,
                    "Fixed-window entry should carry a positive TTL")
        assert_true(sliding_key in by_key,
                    f"Seeded sliding-window key {sliding_key} missing from results")
        assert_eq(by_key[sliding_key]["count"], 2,
                  "Sliding-window count should be the zset cardinality")
    finally:
        r.delete(fixed_key, sliding_key)
