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
    from both rate-limit families without letting one family consume the cap."""
    from mojo.apps.assistant import get_registry
    from mojo.apps.assistant.services.tools.users import (
        MAX_RESULTS,
        _tool_query_rate_limits,
    )
    from mojo.helpers.redis import get_connection

    r = get_connection()
    fixed_keys = [
        f"rl:testit_users_tool_fair:ip:127.0.0.1:{index}"
        for index in range(MAX_RESULTS)
    ]
    sliding_key = "srl:testit_users_tool:account:0"
    no_ttl_key = "rl:testit_users_tool_fair:no_ttl"
    cleanup_keys = fixed_keys + [sliding_key, no_ttl_key]
    r.delete(*cleanup_keys)
    with r.pipeline() as pipe:
        for fixed_key in fixed_keys:
            pipe.set(fixed_key, 3, ex=60)
        pipe.zadd(sliding_key, {"1": 1.0, "2": 2.0})
        pipe.expire(sliding_key, 60)
        pipe.set(no_ttl_key, 9)
        pipe.execute()
    try:
        result = _tool_query_rate_limits({}, opts.users_admin)
        assert_true("error" not in result,
                    f"Tool returned an error: {result.get('error')}")
        assert_eq(result["count"], len(result["rate_limits"]),
                  "Reported count should match the returned entries")
        assert_true(result["count"] <= MAX_RESULTS,
                    "Rate-limit results must stay within the global cap")
        assert_true(result["truncated"],
                    "A full capped result must report that more keys may exist")
        by_key = {e["key"]: e for e in result["rate_limits"]}
        returned_fixed = [key for key in fixed_keys if key in by_key]
        assert_true(returned_fixed,
                    "The capped result should include fixed-window entries")
        fixed_entry = by_key[returned_fixed[0]]
        assert_eq(fixed_entry["count"], 3,
                  "Fixed-window count should reflect the stored value")
        assert_true(fixed_entry["ttl_seconds"] > 0,
                    "Fixed-window entry should carry a positive TTL")
        assert_true(sliding_key in by_key,
                    "Sliding-window entries must survive fixed-window cap pressure")
        assert_eq(by_key[sliding_key]["count"], 2,
                  "Sliding-window count should be the zset cardinality")
        assert_true(by_key[sliding_key]["ttl_seconds"] > 0,
                    "Sliding-window entry should carry a positive TTL")
        assert_true(no_ttl_key not in by_key,
                    "Entries without a positive TTL must remain filtered out")
        assert_eq(get_registry()["query_rate_limits"]["permission"], "view_admin",
                  "Rate-limit inspection must remain behind view_admin")
    finally:
        r.delete(*cleanup_keys)


@th.django_unit_test()
def test_query_rate_limits_bounds_inspection_and_scan_work(opts):
    """Ineligible keys and empty SCAN pages must not create unbounded work."""
    from mojo.apps.assistant.services.tools.users import (
        MAX_RATE_LIMIT_KEYS_INSPECTED,
        MAX_RATE_LIMIT_SCAN_CALLS,
        _collect_rate_limits,
    )

    class IneligibleFloodRedis:
        def __init__(self):
            self.scan_calls = 0
            self.ttl_calls = 0

        def scan(self, cursor, match, count):
            self.scan_calls += 1
            keys = [
                f"{match[:-1]}ineligible:{self.scan_calls}:{index}"
                for index in range(count)
            ]
            return 1, keys

        def ttl(self, key):
            self.ttl_calls += 1
            return -1

    flood = IneligibleFloodRedis()
    flooded_result = _collect_rate_limits(flood)
    assert_eq(flood.ttl_calls, MAX_RATE_LIMIT_KEYS_INSPECTED,
              "Ineligible keys must stop at the hard inspection budget")
    assert_true(flood.scan_calls <= MAX_RATE_LIMIT_SCAN_CALLS,
                "Inspection must also remain inside the global SCAN-call budget")
    assert_eq(flooded_result["count"], 0,
              "No-TTL keys must not become active rate-limit results")
    assert_true(flooded_result["truncated"],
                "Hitting the inspection budget must report truncation")

    class EmptyScanRedis:
        def __init__(self):
            self.scan_calls = 0

        def scan(self, cursor, match, count):
            self.scan_calls += 1
            return 1, []

    empty = EmptyScanRedis()
    empty_result = _collect_rate_limits(empty)
    assert_eq(empty.scan_calls, MAX_RATE_LIMIT_SCAN_CALLS,
              "Empty cursor pages must stop at the hard SCAN-call budget")
    assert_true(empty_result["truncated"],
                "An unfinished cursor at the SCAN budget must report truncation")

    class FiniteRedis:
        def scan(self, cursor, match, count):
            return 0, [f"{match[:-1]}finite"]

        def ttl(self, key):
            return 60

        def type(self, key):
            return "zset" if key.startswith("srl:") else "string"

        def zcard(self, key):
            return 2

        def get(self, key):
            return "3"

    finite_result = _collect_rate_limits(FiniteRedis())
    assert_eq(finite_result["count"], 2,
              "A fully scanned finite keyspace should return both families")
    assert_true(not finite_result["truncated"],
                "Exhausted cursors with no buffered keys must be complete")
