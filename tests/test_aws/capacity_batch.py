"""Capacity batch plan/apply: ordering, fingerprints, and refusals.

Everything here works against report-shaped envelopes rather than mocked AWS
clients: plan-time validation is DEFINED as report-level checking (execution
re-derives the real guards), so the envelope IS the contract under test.
Envelopes reach the service through the plan_batch/apply_batch injection
seams (report_fn=/store=), never by patching shared module attributes. The
batch-runner walks and the in-process REST-gate tests, which do patch module
attributes, live in tests/test_aws_extended_serial/capacity_batch.py
(maestro #2558).
"""

from types import SimpleNamespace
from unittest import mock

from testit import helpers as th


NODE_A = "i-0a1b2c3d4e5f60011"
NODE_B = "i-0a1b2c3d4e5f60022"
NODE_C = "i-0a1b2c3d4e5f60033"
NODE_D = "i-0a1b2c3d4e5f60044"
CLUSTER = "mojo-batch-aurora"
READER_A = "mojo-batch-aurora-2"
READER_B = "mojo-batch-aurora-3"
CACHE_GROUP = "mojo-batch-redis"


# ── fixtures ────────────────────────────────────────────────────────────────

def _capacity_publish(call):
    """Scope for th.capture_publishes: only this module's capacity jobs.

    An unscoped patch of mojo.apps.jobs.publish is process-global and swallows
    every parallel module's publishes (maestro item #1839); the scoped capture
    records capacity jobs and forwards everything else to the real publish.
    """
    return str(call.get("func", "")).startswith("mojo.apps.aws.asyncjobs.capacity")

def _actor(pk=1):
    return SimpleNamespace(pk=pk, username="batch-actor", is_superuser=True)


def _node(identifier, name, healthy=True, itype="m6i.large", state=None):
    return {"id": identifier, "name": name,
            "state": state or ("healthy" if healthy else "unhealthy"),
            "instance_state": "running", "instance_type": itype,
            "zone": "us-east-1a", "healthy": healthy, "self": False,
            "primary": False, "added_by_capacity": False, "groups": []}


def _aurora(identifier=CLUSTER, writer_class="db.r6g.xlarge", readers=None,
            reader_classes=None, status="available"):
    readers = list(readers if readers is not None else [READER_A])
    classes = dict(reader_classes if reader_classes is not None
                   else {reader: "db.t4g.medium" for reader in readers})
    return {"identifier": identifier, "kind": "aurora",
            "engine": "aurora-postgresql", "status": status,
            "writer": f"{identifier}-1", "readers": readers,
            "writer_instance_class": writer_class,
            "reader_instance_classes": classes,
            "reader_endpoint": None, "endpoint": None}


def _cache_row(identifier=CACHE_GROUP, replicas=1, node_type="cache.t4g.medium",
               failover=True, status="available", blocked=None):
    members = [{"id": f"{identifier}-{index + 1:03d}"}
               for index in range(replicas + 1)]
    return {"identifier": identifier, "status": status,
            "replica_count": replicas, "cluster_enabled": False,
            "automatic_failover_on": failover, "multi_az_on": failover,
            "node_type": node_type,
            "resize_impact": ("rolling" if (failover and replicas >= 1)
                              else "downtime"),
            "members": members,
            "min_replicas": 1 if failover else 0,
            "blocked_reason": blocked}


def _envelope(nodes=None, databases=None, caches=None, mode="managed",
              self_id=None, warnings=None):
    from mojo.apps.aws.services import capacity

    envelope = {
        "schema_version": 1, "region": "us-east-1", "mode": mode,
        "generated_at": "2026-08-20T00:00:00+00:00", "node_id_pinned": False,
        "nodes": {"balancers": [], "groups": [],
                  "instances": list(nodes or []),
                  "self": self_id,
                  "self_check": "matched" if self_id else "unavailable"},
        "databases": list(databases or []),
        "caches": list(caches or []),
        "egress": {"fleet_available": True, "addresses_available": True,
                   "policy_available": True, "enabled": False, "attached": [],
                   "pending_nodes": [], "reserved": []},
        "warnings": list(warnings or []),
    }
    envelope["actions"] = capacity._offers(envelope)
    return envelope


def _full_envelope():
    return _envelope(
        nodes=[_node(NODE_A, "mojo-api-a"), _node(NODE_B, "mojo-api-b"),
               _node(NODE_C, "mojo-api-c")],
        databases=[_aurora(readers=[READER_A, READER_B])],
        caches=[_cache_row()],
        self_id=NODE_A)


def _plan(steps, envelope, store=None):
    """plan_batch against a fixed envelope, with the plan store mocked out."""
    from mojo.apps.aws.services import capacity

    store = store if store is not None else mock.Mock()
    return capacity.plan_batch(_actor(), steps,
                               report_fn=lambda refresh=False: envelope,
                               store=store)


def _refused(steps, envelope, code, index=None):
    from mojo.apps.aws.services import capacity

    store = mock.Mock()
    with th.assert_raises(capacity.CapacityError) as caught:
        capacity.plan_batch(_actor(), steps,
                            report_fn=lambda refresh=False: envelope,
                            store=store)
    assert caught.exception.error_code == code, \
        (f"the wrong refusal for {steps!r}: {caught.exception.error_code!r} "
         f"(wanted {code!r}): {caught.exception.message}")
    assert store.set.call_count == 0, \
        f"a refused plan was still written to the cache ({code})"
    if index is not None:
        assert caught.exception.data.get("step") == index, \
            (f"the refusal did not name step {index}: "
             f"{caught.exception.data}")
    return caught.exception


# ── ordering and fingerprint ────────────────────────────────────────────────

@th.django_unit_test("a mixed submission ranks add → resize → remove, terminate behind its drain")
def test_order_steps_adds_resizes_removes(opts):
    from mojo.apps.aws.services import capacity

    steps = [
        {"action": "terminate_node", "resource": NODE_B, "kind": "remove"},
        {"action": "remove_reader", "resource": READER_A, "kind": "remove"},
        {"action": "set_cache_replicas", "resource": CACHE_GROUP, "kind": "remove"},
        {"action": "resize_database", "resource": READER_B, "kind": "change"},
        {"action": "drain_node", "resource": NODE_B, "kind": "remove"},
        {"action": "resize_cache", "resource": CACHE_GROUP, "kind": "change"},
        {"action": "set_cache_replicas", "resource": "other-group", "kind": "add"},
        {"action": "add_reader", "resource": CLUSTER, "kind": "add"},
        {"action": "add_node", "resource": "", "kind": "add"},
    ]
    ordered = [(step["action"], step["resource"])
               for step in capacity._order_steps(steps)]
    assert ordered == [
        ("add_node", ""),
        ("add_reader", CLUSTER),
        ("set_cache_replicas", "other-group"),
        ("resize_cache", CACHE_GROUP),
        ("resize_database", READER_B),
        ("set_cache_replicas", CACHE_GROUP),
        ("remove_reader", READER_A),
        ("drain_node", NODE_B),
        ("terminate_node", NODE_B),
    ], f"the batch order is wrong: {ordered}"

    # A terminate with no drain in the batch keeps its own rank; a paired one
    # sits immediately behind its drain, whatever the submission order said.
    loose = [(step["action"], step["resource"]) for step in
             capacity._order_steps([
                 {"action": "terminate_node", "resource": NODE_C, "kind": "remove"},
                 {"action": "drain_node", "resource": NODE_A, "kind": "remove"},
                 {"action": "terminate_node", "resource": NODE_A, "kind": "remove"},
                 {"action": "add_node", "resource": "", "kind": "add"},
             ])]
    assert loose == [("add_node", ""), ("drain_node", NODE_A),
                     ("terminate_node", NODE_A), ("terminate_node", NODE_C)], \
        f"the unpaired terminate broke the pinning: {loose}"


@th.django_unit_test("the fleet fingerprint is structural: statuses flap, structure hashes")
def test_fleet_fingerprint_is_structural(opts):
    from mojo.apps.aws.services import capacity

    base = _full_envelope()
    first = capacity._fleet_fingerprint(base)

    # Key order and list order do not matter; the projection is canonical.
    shuffled = _envelope(
        caches=[_cache_row()],
        databases=[_aurora(readers=[READER_B, READER_A])],
        nodes=[_node(NODE_C, "mojo-api-c"), _node(NODE_B, "mojo-api-b"),
               _node(NODE_A, "mojo-api-a")],
        self_id=NODE_A)
    assert capacity._fleet_fingerprint(shuffled) == first, \
        "reordering rows changed the fingerprint"

    # Transient statuses are deliberately excluded: a backup flap must not
    # 409 a valid plan at apply time.
    flapping = _full_envelope()
    flapping["databases"][0]["status"] = "backing-up"
    flapping["caches"][0]["status"] = "snapshotting"
    flapping["nodes"]["instances"][0]["state"] = "unused"
    assert capacity._fleet_fingerprint(flapping) == first, \
        "a status-only change altered the fingerprint"

    drifted = _full_envelope()
    drifted["caches"][0]["replica_count"] = 2
    assert capacity._fleet_fingerprint(drifted) != first, \
        "a replica-count change did not alter the fingerprint"

    smaller = _full_envelope()
    smaller["nodes"]["instances"].pop()
    assert capacity._fleet_fingerprint(smaller) != first, \
        "a missing node did not alter the fingerprint"

    rewired = _full_envelope()
    rewired["databases"][0]["readers"] = [READER_A]
    assert capacity._fleet_fingerprint(rewired) != first, \
        "a changed reader list did not alter the fingerprint"

    # A standalone writer's class is structural too (not just Aurora's).
    standalone = _envelope(databases=[{
        "identifier": "solo", "kind": "standalone", "status": "available",
        "writer": "solo", "readers": [], "instance_class": "db.m6g.large",
        "reader_instance_classes": {}}])
    resized = _envelope(databases=[{
        "identifier": "solo", "kind": "standalone", "status": "available",
        "writer": "solo", "readers": [], "instance_class": "db.r6g.xlarge",
        "reader_instance_classes": {}}])
    assert capacity._fleet_fingerprint(standalone) != \
        capacity._fleet_fingerprint(resized), \
        "a standalone class change did not alter the fingerprint"


# ── plan-time validation ────────────────────────────────────────────────────

@th.django_unit_test("the plan refuses at plan time exactly what apply would refuse")
def test_plan_refuses_what_apply_would(opts):
    envelope = _full_envelope()

    _refused([{"action": "explode"}], envelope, "invalid_request", index=0)
    exc = _refused([{"action": "enable_stable_ips"}], envelope,
                   "invalid_request", index=0)
    assert "alone" in exc.message, \
        f"the stable-ips refusal does not say to run it alone: {exc.message}"

    _refused([{"action": "drain_node", "resource": "i-0nope"}], envelope,
             "resource_not_found", index=0)
    _refused([{"action": "remove_reader", "resource": "not-a-reader"}],
             envelope, "not_a_reader", index=0)
    _refused([{"action": "set_cache_replicas", "resource": "no-such-group",
               "count": 2, "apply_immediately": True}], envelope,
             "resource_not_found", index=0)

    # NODE_A is the node serving the request.
    _refused([{"action": "drain_node", "resource": NODE_A}], envelope,
             "cannot_remove_self", index=0)

    _refused([{"action": "drain_node", "resource": NODE_B},
              {"action": "drain_node", "resource": NODE_B}], envelope,
             "invalid_request", index=1)

    # The failover floor, the missing window, and a no-op count.
    _refused([{"action": "set_cache_replicas", "resource": CACHE_GROUP,
               "count": 0, "apply_immediately": True}], envelope,
             "automatic_failover_requires_replica", index=0)
    _refused([{"action": "set_cache_replicas", "resource": CACHE_GROUP,
               "count": 2}], envelope, "invalid_request", index=0)
    _refused([{"action": "set_cache_replicas", "resource": CACHE_GROUP,
               "count": 1, "apply_immediately": True}], envelope,
             "no_change", index=0)

    # A resize to the size the resource already runs, and a raw type string.
    _refused([{"action": "resize_cache", "resource": CACHE_GROUP,
               "size": "medium", "apply_immediately": True}], envelope,
             "no_change", index=0)
    _refused([{"action": "resize_database", "resource": READER_A,
               "size": "db.r6g.large", "apply_immediately": True}], envelope,
             "invalid_request", index=0)
    _refused([{"action": "resize_database", "resource": READER_A,
               "size": "medium"}], envelope, "invalid_request", index=0)

    # Terminate needs an in-batch drain or an already-drained shape.
    _refused([{"action": "terminate_node", "resource": NODE_B}], envelope,
             "not_drained", index=0)
    drained = _full_envelope()
    for row in drained["nodes"]["instances"]:
        if row["id"] == NODE_C:
            row["healthy"] = False
            row["state"] = "unused"
    plan = _plan([{"action": "terminate_node", "resource": NODE_C}], drained)
    assert plan["steps"][0]["action"] == "terminate_node", \
        "a drained-but-registered node could not be batch-terminated"

    # Healthy-drain budget: only drains of HEALTHY nodes consume it. B and C
    # healthy, D unhealthy, no self: draining D and B passes (D consumes no
    # budget), adding C crosses the line.
    fleet = _envelope(nodes=[_node(NODE_B, "b"), _node(NODE_C, "c"),
                             _node(NODE_D, "d", healthy=False)])
    survivable = _plan([{"action": "drain_node", "resource": NODE_D},
                        {"action": "drain_node", "resource": NODE_B}], fleet)
    assert len(survivable["steps"]) == 2, \
        "an unhealthy node's drain consumed the healthy budget"
    _refused([{"action": "drain_node", "resource": NODE_D},
              {"action": "drain_node", "resource": NODE_B},
              {"action": "drain_node", "resource": NODE_C}], fleet,
             "last_healthy_target", index=2)

    # Resizing what this same batch removes, in either submission order.
    _refused([{"action": "remove_reader", "resource": READER_A},
              {"action": "resize_database", "resource": READER_A,
               "size": "medium", "apply_immediately": True}], envelope,
             "conflicting_steps", index=1)
    _refused([{"action": "resize_database", "resource": READER_A,
               "size": "medium", "apply_immediately": True},
              {"action": "remove_reader", "resource": READER_A}], envelope,
             "conflicting_steps", index=1)


@th.django_unit_test("plans read the CACHED report; a degraded envelope refuses, never guesses")
def test_plan_cached_report_and_degraded_refusal(opts):
    from mojo.apps.aws.services import capacity

    envelope = _full_envelope()
    reader = mock.Mock(return_value=envelope)
    capacity.plan_batch(_actor(), [{"action": "add_node"}],
                        report_fn=reader, store=mock.Mock())
    assert reader.call_args == mock.call(), \
        (f"plan_batch did not take the plain cached report: "
         f"{reader.call_args}")

    # A warning naming a section a step touches is a 503, not unknown_resource.
    degraded = _envelope(
        nodes=[_node(NODE_A, "a"), _node(NODE_B, "b")],
        caches=[_cache_row()],
        warnings=[{"code": "caches",
                   "iam_action": "elasticache:DescribeReplicationGroups"}])
    _refused([{"action": "set_cache_replicas", "resource": CACHE_GROUP,
               "count": 2, "apply_immediately": True}], degraded,
             "report_degraded")

    # A warning on a section NO step touches does not block the plan.
    unrelated = _envelope(
        nodes=[_node(NODE_A, "a"), _node(NODE_B, "b")],
        warnings=[{"code": "databases",
                   "iam_action": "rds:DescribeDBClusters"}])
    plan = _plan([{"action": "add_node"}], unrelated)
    assert plan["steps"][0]["action"] == "add_node", \
        "an unrelated degraded section blocked the plan"

    # The same refusal guards apply: never fingerprint an incomplete fleet.
    stored = capacity.plan_batch(_actor(), [
        {"action": "set_cache_replicas", "resource": CACHE_GROUP,
         "count": 2, "apply_immediately": True}],
        report_fn=lambda refresh=False: envelope)
    degraded_fresh = _envelope(
        nodes=envelope["nodes"]["instances"], caches=[_cache_row()],
        warnings=[{"code": "caches",
                   "iam_action": "elasticache:DescribeReplicationGroups"}])
    reader = mock.Mock(return_value=degraded_fresh)
    with th.assert_raises(capacity.CapacityError) as caught:
        capacity.apply_batch(_actor(), stored["id"], report_fn=reader)
    assert caught.exception.error_code == "report_degraded" \
        and caught.exception.status == 503, \
        f"a degraded apply-time sweep answered {caught.exception.error_code}"
    assert reader.call_args == mock.call(refresh=True), \
        f"apply_batch did not take a FRESH report: {reader.call_args}"


@th.django_unit_test("every step comes back worded and priced, unpriced types honestly null")
def test_plan_prices_and_words_every_step(opts):
    envelope = _envelope(
        nodes=[_node(NODE_A, "mojo-api-a"), _node(NODE_B, "mojo-api-b"),
               _node(NODE_C, "mojo-api-c")],
        databases=[_aurora(readers=[READER_A, READER_B])],
        # cache.t4g.small is deliberately NOT in COST_TABLE: the honest-null
        # path must fire, not a silent $0.
        caches=[_cache_row(node_type="cache.t4g.small")],
        self_id=NODE_A)
    plan = _plan([
        {"action": "resize_database", "resource": READER_A, "size": "medium",
         "apply_immediately": True},
        {"action": "drain_node", "resource": NODE_C},
        {"action": "terminate_node", "resource": NODE_C},
        {"action": "add_node"},
        {"action": "resize_cache", "resource": CACHE_GROUP, "size": "large",
         "apply_immediately": True},
        {"action": "add_reader", "resource": CLUSTER},
        {"action": "resize_database", "resource": READER_B, "size": "medium",
         "apply_immediately": True},
    ], envelope)

    steps = plan["steps"]
    assert [step["index"] for step in steps] == list(range(len(steps))), \
        f"the plan's step indexes are not sequential: {steps}"
    for step in steps:
        assert step["description"], f"an unworded step: {step}"

    by_key = {(step["action"], step["resource"]): step for step in steps}
    assert by_key[("add_node", "")]["description"] == "Add an app node" \
        and by_key[("add_node", "")]["monthly_delta_usd"] == 70.0, \
        f"add_node is misworded or mispriced: {by_key[('add_node', '')]}"
    assert by_key[("add_reader", CLUSTER)]["monthly_delta_usd"] == 350.0, \
        (f"add_reader did not price the writer's class: "
         f"{by_key[('add_reader', CLUSTER)]}")
    reader_step = by_key[("resize_database", READER_A)]
    assert reader_step["description"] == \
        f"Resize reader {READER_A} to db.r6g.large", \
        f"the reader resize is misworded: {reader_step['description']}"
    assert reader_step["monthly_delta_usd"] == 125.0, \
        f"the reader resize delta is wrong: {reader_step}"
    assert by_key[("terminate_node", NODE_C)]["monthly_delta_usd"] == -70.0, \
        f"terminate is not a negative delta: {by_key[('terminate_node', NODE_C)]}"
    assert by_key[("drain_node", NODE_C)]["monthly_delta_usd"] == 0.0, \
        f"a drain costs nothing: {by_key[('drain_node', NODE_C)]}"
    assert by_key[("terminate_node", NODE_C)]["description"] == \
        "Terminate mojo-api-c", \
        "terminate does not use the node's name"

    unpriced = by_key[("resize_cache", CACHE_GROUP)]
    assert unpriced["monthly_delta_usd"] is None, \
        f"an unpriced type was silently priced: {unpriced}"
    assert any("no listed price for cache.t4g.small" in warning
               for warning in unpriced["warnings"]), \
        f"the unpriced step carries no warning: {unpriced['warnings']}"
    assert plan["estimate_complete"] is False, \
        "an unpriced step still claimed a complete estimate"
    assert plan["total_monthly_delta_usd"] == 600.0, \
        (f"the total does not sum the priced steps: "
         f"{plan['total_monthly_delta_usd']}")


@th.django_unit_test("the plan store gets TTL 300, and the fingerprint never leaves the server")
def test_plan_store_ttl_and_shape(opts):
    from mojo.apps.aws.services import capacity

    store = mock.Mock()
    result = _plan([{"action": "add_node"}], _full_envelope(), store=store)
    assert store.set.call_count == 1, \
        f"the plan was stored {store.set.call_count} times, not once"
    key, record, ttl = store.set.call_args[0]
    assert ttl == capacity.PLAN_TTL == 300, f"the plan TTL is {ttl}"
    assert key == capacity._plan_key(result["id"]), \
        f"the plan was stored under the wrong key: {key}"
    assert "fingerprint" in record, "the stored record carries no fingerprint"
    assert "fingerprint" not in result, \
        "the fingerprint leaked into the API response"
    assert result["expires_in"] == 300, \
        f"the response does not state its expiry: {result.get('expires_in')}"


@th.django_unit_test("an add_node step carries its placement, and prices the source it names")
def test_add_node_step_carries_placement(opts):
    # Two adds in one batch, into DIFFERENT subnets: the duplicate-step check
    # already exempts add_node. The envelope is shape-only here, so whether
    # each subnet is really in its source's zone is the child apply()'s call.
    envelope = _envelope(
        nodes=[_node(NODE_A, "mojo-api-a"),
               _node(NODE_B, "mojo-sites-a", itype="t3.medium")])
    plan = _plan([
        {"action": "add_node", "source_instance": NODE_B,
         "subnet_id": "subnet-0bbb"},
        {"action": "add_node", "subnet_id": "subnet-0ccc"},
    ], envelope)

    steps = plan["steps"]
    assert len(steps) == 2, f"two adds into two subnets did not both survive: {steps}"
    assert steps[0]["params"] == {"source_instance": NODE_B,
                                  "subnet_id": "subnet-0bbb"}, \
        f"the step dropped its placement: {steps[0]['params']}"
    assert steps[1]["params"] == {"subnet_id": "subnet-0ccc"}, \
        f"an add naming only a subnet carried the wrong params: {steps[1]}"
    assert steps[0]["description"] == \
        "Add an app node cloned from mojo-sites-a in subnet-0bbb", \
        f"the step does not say where the node comes from or lands: " \
        f"{steps[0]['description']}"
    assert steps[1]["description"] == "Add an app node in subnet-0ccc", \
        f"a subnet-only add is misworded: {steps[1]['description']}"

    # The NAMED source is the node that gets cloned, so its type is the type
    # that gets billed — not healthy[0]'s.
    assert steps[0]["monthly_delta_usd"] == 30.0, \
        f"the named source's type was not priced: {steps[0]}"
    assert steps[1]["monthly_delta_usd"] == 70.0, \
        f"an unnamed source stopped pricing the first healthy node: {steps[1]}"
    assert plan["total_monthly_delta_usd"] == 100.0, \
        f"the batch total is wrong: {plan['total_monthly_delta_usd']}"


@th.django_unit_test("a batch add_node refuses a source the report does not serve")
def test_add_node_step_refuses_a_bad_placement(opts):
    from mojo.apps.aws.services import capacity

    envelope = _envelope(nodes=[_node(NODE_A, "mojo-api-a"),
                                _node(NODE_B, "mojo-api-b", healthy=False)])
    # The source IS checkable against the envelope — it must be a healthy row.
    with th.assert_raises(capacity.CapacityError) as caught:
        _plan([{"action": "add_node", "source_instance": NODE_B}], envelope)
    assert caught.exception.error_code == "source_not_serving" \
        and caught.exception.status == 409, \
        f"an unhealthy source was not refused: {caught.exception.error_code}"
    assert caught.exception.data.get("step") == 0, \
        f"the refusal does not name the offending step: {caught.exception.data}"
    _refused([{"action": "add_node", "source_instance": NODE_D}], envelope,
             "source_not_serving")
    # The subnet is shape-checked only: an empty subnet holds no node by
    # definition, so no envelope built from node rows could validate it. The
    # child apply() proves it against AWS before it takes a claim.
    _refused([{"action": "add_node", "subnet_id": "vpc-0aaa"}], envelope,
             "invalid_request")


# ── apply_batch ─────────────────────────────────────────────────────────────

@th.django_unit_test("an expired plan is a 404 and a drifted fleet is a 409 — never a silent re-plan")
def test_apply_batch_expiry_and_stale(opts):
    from mojo.apps.aws.services import capacity

    with th.assert_raises(capacity.CapacityError) as caught:
        capacity.apply_batch(_actor(), "plan-that-never-existed")
    assert caught.exception.error_code == "plan_not_found" \
        and caught.exception.status == 404, \
        f"an unknown plan answered {caught.exception.error_code}"
    assert "expire" in caught.exception.message, \
        f"the 404 does not explain plan expiry: {caught.exception.message}"

    envelope = _full_envelope()
    stored = capacity.plan_batch(_actor(), [{"action": "add_node"}],
                                 report_fn=lambda refresh=False: envelope)
    drifted = _full_envelope()
    drifted["nodes"]["instances"].append(_node(NODE_D, "mojo-api-d"))
    with th.capture_publishes(_capacity_publish) as published:
        with th.assert_raises(capacity.CapacityError) as caught:
            capacity.apply_batch(_actor(), stored["id"],
                                 report_fn=lambda refresh=False: drifted)
    assert caught.exception.error_code == "plan_stale" \
        and caught.exception.status == 409, \
        f"a drifted fleet answered {caught.exception.error_code}"
    assert len(published) == 0, "a stale plan still published a job"


@th.django_unit_test("a plan id is single-use; the second apply names the running batch")
def test_apply_batch_is_single_use(opts):
    from mojo.apps.aws.services import capacity

    envelope = _full_envelope()
    stored = capacity.plan_batch(_actor(), [{"action": "add_node"}],
                                 report_fn=lambda refresh=False: envelope)
    with th.capture_publishes(_capacity_publish) as published:
        first = capacity.apply_batch(_actor(), stored["id"],
                                     report_fn=lambda refresh=False: envelope)
        assert len(published) == 1, "the first apply did not publish"
        with th.assert_raises(capacity.CapacityError) as caught:
            capacity.apply_batch(_actor(), stored["id"],
                                 report_fn=lambda refresh=False: envelope)
    assert caught.exception.error_code == "plan_already_applied" \
        and caught.exception.status == 409, \
        f"a second apply answered {caught.exception.error_code}"
    assert caught.exception.data.get("batch") == first["id"], \
        (f"the 409 does not name the running batch: "
         f"{caught.exception.data} vs {first['id']}")


@th.django_unit_test("a publish failure fails the batch closed; success carries the job contract")
def test_apply_batch_dispatch_failure(opts):
    from mojo.apps.aws.services import capacity

    envelope = _full_envelope()
    stored = capacity.plan_batch(_actor(), [
        {"action": "add_node"},
        {"action": "add_reader", "resource": CLUSTER}],
        report_fn=lambda refresh=False: envelope)
    with th.capture_publishes(_capacity_publish,
                              side_effect=RuntimeError("no runners")):
        with th.assert_raises(capacity.CapacityError) as caught:
            capacity.apply_batch(_actor(), stored["id"],
                                 report_fn=lambda refresh=False: envelope)
    assert caught.exception.error_code == "batch_dispatch_failed" \
        and caught.exception.status == 503, \
        f"a dead dispatch answered {caught.exception.error_code}"
    from django.core.cache import cache as django_cache
    failed_id = django_cache.get(capacity._plan_lock_key(stored["id"]))
    failed = capacity._read_batch(failed_id)
    assert failed and failed["state"] == "failed" \
        and failed["error_code"] == "batch_dispatch_failed", \
        f"the failed batch record does not say why: {failed}"

    stored = capacity.plan_batch(_actor(), [
        {"action": "add_node"},
        {"action": "add_reader", "resource": CLUSTER}],
        report_fn=lambda refresh=False: envelope)
    with th.capture_publishes(_capacity_publish) as published:
        batch = capacity.apply_batch(_actor(), stored["id"],
                                     report_fn=lambda refresh=False: envelope)
    assert len(published) == 1, f"expected one capacity publish, got {len(published)}"
    kwargs = published[-1]
    assert kwargs["func"] == "mojo.apps.aws.asyncjobs.capacity_batch", \
        f"the wrong job was published: {kwargs['func']}"
    assert kwargs["max_retries"] == 0, \
        "a redeliverable batch job would re-run mutations"
    floor = sum(capacity._deadline_for(step["action"])
                for step in batch["steps"])
    assert kwargs["max_exec_seconds"] >= floor, \
        (f"max_exec_seconds {kwargs['max_exec_seconds']} undercuts the "
         f"summed step deadlines {floor}")


# ── REST ────────────────────────────────────────────────────────────────────

@th.django_unit_test("anonymous callers are refused; the status route answers batch asks honestly")
def test_batch_rest_anonymous_and_status(opts):
    from mojo.apps.account.models import User

    opts.client.logout()
    response = opts.client.post("/api/aws/capacity/plan",
                                {"steps": [{"action": "add_node"}]})
    assert response.status_code in (401, 403), \
        f"plan answered {response.status_code} to an anonymous caller"
    response = opts.client.post("/api/aws/capacity/plan/apply",
                                {"plan_id": "plan-1"})
    assert response.status_code in (401, 403), \
        f"plan/apply answered {response.status_code} to an anonymous caller"

    User.objects.filter(username="capacity-batch-root").delete()
    root = User.objects.create_user(
        email="capacity-batch-root@test.com", username="capacity-batch-root",
        password="example")
    root.is_active = True
    root.is_superuser = True
    root.save()
    opts.client.login("capacity-batch-root", "example")
    try:
        response = opts.client.get(
            "/api/aws/capacity/status?batch=batch-that-never-existed")
        assert response.status_code == 404, \
            f"an unknown batch answered {response.status_code}"
        body = response.json or {}
        assert body.get("error_code") == "batch_not_found", \
            f"the 404 came from routing, not the handler: {body}"

        response = opts.client.get("/api/aws/capacity/status")
        assert response.status_code == 400, \
            f"a status ask with neither id answered {response.status_code}"
        response = opts.client.get(
            "/api/aws/capacity/status?operation=op-1&batch=batch-1")
        assert response.status_code == 400, \
            f"an ambiguous status ask answered {response.status_code}"
    finally:
        opts.client.logout()
