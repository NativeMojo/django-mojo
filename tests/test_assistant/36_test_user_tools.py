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
    """query_rate_limits reaches Redis and returns only bounded active entries.

    Redis SCAN deliberately makes no ordering or membership guarantee.  The
    full suite shares this database, so exact fixture membership belongs in the
    deterministic adapter tests below rather than this live integration seam.
    """
    from mojo.apps.assistant import get_registry
    from mojo.apps.assistant.services.tools.users import (
        MAX_RESULTS,
        _tool_query_rate_limits,
    )
    from mojo.helpers.redis import get_connection

    r = get_connection()
    fixed_keys = ["rl:testit_users_tool_fair:ip:127.0.0.1"]
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
        assert_true(isinstance(result["truncated"], bool),
                    "Truncation state must be explicit")
        assert_true(bool(result["rate_limits"]),
                    "The live Redis seam should return at least one active key")
        assert_true(all(
            entry["key"].startswith(("rl:", "srl:"))
            and entry["ttl_seconds"] > 0
            and isinstance(entry["count"], int)
            for entry in result["rate_limits"]
        ), "Every returned entry must be an active bounded rate-limit record")
        assert_eq(get_registry()["query_rate_limits"]["permission"], "view_admin",
                  "Rate-limit inspection must remain behind view_admin")
    finally:
        r.delete(*cleanup_keys)


@th.django_unit_test()
def test_query_rate_limits_bounds_inspection_and_scan_work(opts):
    """Ineligible keys and empty SCAN pages must not create unbounded work."""
    from mojo.apps.assistant.services.tools.users import (
        MAX_RESULTS,
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

    class FamilyPressureRedis(FiniteRedis):
        def scan(self, cursor, match, count):
            if match == "rl:*":
                return 0, [f"rl:fixed:{index}" for index in range(count)]
            return 0, ["srl:sliding:active"]

    pressured_result = _collect_rate_limits(FamilyPressureRedis())
    pressured_keys = {
        entry["key"] for entry in pressured_result["rate_limits"]
    }
    assert_true("srl:sliding:active" in pressured_keys,
                "Round-robin lanes must preserve sliding-window visibility "
                "under fixed-window cap pressure")
    assert_true(len(pressured_keys) <= MAX_RESULTS,
                "Family fairness must not exceed the global result cap")


@th.django_unit_test()
def test_query_rate_limits_scans_cluster_primaries_as_bounded_lanes(opts):
    """Cluster scans must target and exhaust each primary independently."""
    from mojo.apps.assistant.services.tools.users import (
        MAX_RATE_LIMIT_SCAN_CALLS,
        _collect_rate_limits,
    )

    class Primary:
        def __init__(self, name):
            self.name = name

    class FiniteClusterRedis:
        def __init__(self):
            self.primaries = [Primary("primary-a"), Primary("primary-b")]
            self.calls = []

        def get_primaries(self):
            return self.primaries

        def scan(self, cursor, match, count, target_nodes=None):
            if target_nodes not in self.primaries:
                raise AssertionError("Cluster SCAN must target exactly one primary")
            self.calls.append((target_nodes.name, match, cursor))
            if target_nodes.name == "primary-a" and match == "srl:*":
                if cursor == 0:
                    return {target_nodes.name: 7}, ["srl:primary-a:active"]
                return {target_nodes.name: 0}, []
            key = f"{match[:-1]}{target_nodes.name}:active"
            return {target_nodes.name: 0}, [key]

        def ttl(self, key):
            return 60

        def type(self, key):
            return "zset" if key.startswith("srl:") else "string"

        def zcard(self, key):
            return 2

        def get(self, key):
            return "3"

    finite = FiniteClusterRedis()
    finite_result = _collect_rate_limits(finite)
    assert_eq(finite_result["count"], 4,
              "Both key families from both primaries should be represented")
    assert_true(not finite_result["truncated"],
                "Every primary cursor was exhausted without buffered keys")
    assert_eq(len(finite.calls), 5,
              "Each lane should stop issuing SCAN after its own cursor reaches zero")
    assert_eq(
        [cursor for node, pattern, cursor in finite.calls
         if node == "primary-a" and pattern == "srl:*"],
        [0, 7],
        "The next cluster SCAN must use the targeted primary's cursor map value",
    )
    assert_eq({node for node, pattern, cursor in finite.calls},
              {"primary-a", "primary-b"},
              "Every primary must receive explicitly targeted scans")

    class EndlessClusterRedis:
        def __init__(self):
            self.primaries = [Primary("primary-a"), Primary("primary-b")]
            self.calls = []

        def get_primaries(self):
            return self.primaries

        def scan(self, cursor, match, count, target_nodes=None):
            if target_nodes not in self.primaries:
                raise AssertionError("Cluster SCAN must target exactly one primary")
            self.calls.append((target_nodes.name, match))
            return {target_nodes.name: 1}, []

    endless = EndlessClusterRedis()
    endless_result = _collect_rate_limits(endless)
    assert_eq(len(endless.calls), MAX_RATE_LIMIT_SCAN_CALLS,
              "Node-targeted cluster commands must share the global SCAN budget")
    assert_true(endless_result["truncated"],
                "Unfinished per-primary cursors at the command cap are truncated")
    lane_counts = {}
    for lane in endless.calls:
        lane_counts[lane] = lane_counts.get(lane, 0) + 1
    assert_eq(set(lane_counts), {
        ("primary-a", "rl:*"),
        ("primary-a", "srl:*"),
        ("primary-b", "rl:*"),
        ("primary-b", "srl:*"),
    }, "The bounded scan loop must visit every family/primary lane")
    assert_true(max(lane_counts.values()) - min(lane_counts.values()) <= 1,
                "The global cluster SCAN budget must be allocated fairly")
