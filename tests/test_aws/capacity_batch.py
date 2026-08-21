"""Capacity batch plan/apply: ordering, fingerprints, refusals, the runner.

Everything here works against report-shaped envelopes rather than mocked AWS
clients: plan-time validation is DEFINED as report-level checking (execution
re-derives the real guards), so the envelope IS the contract under test. The
REST tests reuse the unwrap idiom from tests/test_aws/capacity.py.
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

BATCH_WALK = "batch-test-walk"
BATCH_STOP = "batch-test-stop"
BATCH_CLAIM = "batch-test-claim"


# ── fixtures ────────────────────────────────────────────────────────────────

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
    with mock.patch.object(capacity, "report", return_value=envelope), \
            mock.patch.object(capacity, "cache", store):
        return capacity.plan_batch(_actor(), steps)


def _refused(steps, envelope, code, index=None):
    from mojo.apps.aws.services import capacity

    store = mock.Mock()
    with mock.patch.object(capacity, "report", return_value=envelope), \
            mock.patch.object(capacity, "cache", store):
        with th.assert_raises(capacity.CapacityError) as caught:
            capacity.plan_batch(_actor(), steps)
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


def _batch_record(batch_id, steps):
    return {"schema_version": 1, "id": batch_id, "plan_id": "plan-x",
            "actor": 7, "state": "running", "started": "2026-08-20T00:00:00",
            "updated": "2026-08-20T00:00:00", "current_index": 0,
            "message": "requested", "error_code": None,
            "steps": steps, "ttl": 600}


def _batch_step(index, action, resource, params=None):
    return {"index": index, "action": action, "resource": resource,
            "params": dict(params or {}), "description": f"step {index}",
            "kind": "add", "state": "pending", "operation": None,
            "phase": None, "message": None, "error_code": None}


def _clear_keys():
    from django.core.cache import cache as django_cache
    from mojo.apps.aws.services import capacity

    for action in capacity.ACTIONS:
        for resource in ("fleet", NODE_A, NODE_B, NODE_C, NODE_D, CLUSTER,
                         READER_A, READER_B, CACHE_GROUP):
            capacity._release(capacity._claim_key(action, resource))
    for batch_id in (BATCH_WALK, BATCH_STOP, BATCH_CLAIM):
        try:
            django_cache.delete(capacity._batch_key(batch_id))
        except Exception:
            pass
    capacity.invalidate()


@th.django_unit_setup()
def setup_capacity_batch(opts):
    # Claims and fixed-id batch records live in a shared cache with long TTLs;
    # delete what this module will create so a previous run cannot bleed in.
    # Plan ids and plan locks are uuid4-keyed, so they cannot collide.
    _clear_keys()


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
    with mock.patch.object(capacity, "report",
                           return_value=envelope) as reader, \
            mock.patch.object(capacity, "cache", mock.Mock()):
        capacity.plan_batch(_actor(), [{"action": "add_node"}])
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
    with mock.patch.object(capacity, "report", return_value=envelope):
        stored = capacity.plan_batch(_actor(), [
            {"action": "set_cache_replicas", "resource": CACHE_GROUP,
             "count": 2, "apply_immediately": True}])
    degraded_fresh = _envelope(
        nodes=envelope["nodes"]["instances"], caches=[_cache_row()],
        warnings=[{"code": "caches",
                   "iam_action": "elasticache:DescribeReplicationGroups"}])
    with mock.patch.object(capacity, "report",
                           return_value=degraded_fresh) as reader:
        with th.assert_raises(capacity.CapacityError) as caught:
            capacity.apply_batch(_actor(), stored["id"])
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
    with mock.patch.object(capacity, "report", return_value=envelope):
        stored = capacity.plan_batch(_actor(), [{"action": "add_node"}])
    drifted = _full_envelope()
    drifted["nodes"]["instances"].append(_node(NODE_D, "mojo-api-d"))
    with mock.patch.object(capacity, "report", return_value=drifted), \
            mock.patch("mojo.apps.jobs.publish") as published:
        with th.assert_raises(capacity.CapacityError) as caught:
            capacity.apply_batch(_actor(), stored["id"])
    assert caught.exception.error_code == "plan_stale" \
        and caught.exception.status == 409, \
        f"a drifted fleet answered {caught.exception.error_code}"
    assert published.call_count == 0, "a stale plan still published a job"


@th.django_unit_test("a plan id is single-use; the second apply names the running batch")
def test_apply_batch_is_single_use(opts):
    from mojo.apps.aws.services import capacity

    envelope = _full_envelope()
    with mock.patch.object(capacity, "report", return_value=envelope):
        stored = capacity.plan_batch(_actor(), [{"action": "add_node"}])
        with mock.patch("mojo.apps.jobs.publish") as published:
            first = capacity.apply_batch(_actor(), stored["id"])
            assert published.call_count == 1, "the first apply did not publish"
            with th.assert_raises(capacity.CapacityError) as caught:
                capacity.apply_batch(_actor(), stored["id"])
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
    with mock.patch.object(capacity, "report", return_value=envelope):
        stored = capacity.plan_batch(_actor(), [
            {"action": "add_node"},
            {"action": "add_reader", "resource": CLUSTER}])
        with mock.patch("mojo.apps.jobs.publish",
                        side_effect=RuntimeError("no runners")):
            with th.assert_raises(capacity.CapacityError) as caught:
                capacity.apply_batch(_actor(), stored["id"])
    assert caught.exception.error_code == "batch_dispatch_failed" \
        and caught.exception.status == 503, \
        f"a dead dispatch answered {caught.exception.error_code}"
    from django.core.cache import cache as django_cache
    failed_id = django_cache.get(capacity._plan_lock_key(stored["id"]))
    failed = capacity._read_batch(failed_id)
    assert failed and failed["state"] == "failed" \
        and failed["error_code"] == "batch_dispatch_failed", \
        f"the failed batch record does not say why: {failed}"

    with mock.patch.object(capacity, "report", return_value=envelope):
        stored = capacity.plan_batch(_actor(), [
            {"action": "add_node"},
            {"action": "add_reader", "resource": CLUSTER}])
        with mock.patch("mojo.apps.jobs.publish") as published:
            batch = capacity.apply_batch(_actor(), stored["id"])
    kwargs = published.call_args.kwargs
    assert kwargs["func"] == "mojo.apps.aws.asyncjobs.capacity_batch", \
        f"the wrong job was published: {kwargs['func']}"
    assert kwargs["max_retries"] == 0, \
        "a redeliverable batch job would re-run mutations"
    floor = sum(capacity._deadline_for(step["action"])
                for step in batch["steps"])
    assert kwargs["max_exec_seconds"] >= floor, \
        (f"max_exec_seconds {kwargs['max_exec_seconds']} undercuts the "
         f"summed step deadlines {floor}")


# ── run_batch ───────────────────────────────────────────────────────────────

@th.django_unit_test("the runner walks every step through the unchanged apply, auditing each")
def test_run_batch_walks_steps_and_audits(opts):
    from mojo.apps.aws.rest.capacity import AUDIT_ACTIONS
    from mojo.apps.aws.services import capacity

    record = _batch_record(BATCH_WALK, [
        _batch_step(0, "add_node", ""),
        _batch_step(1, "set_cache_replicas", CACHE_GROUP,
                    {"count": 2, "apply_immediately": True}),
        _batch_step(2, "drain_node", NODE_B),
    ])
    capacity._write_batch(record)
    operations = iter(["op-1", "op-2", "op-3"])
    applied = mock.Mock(
        side_effect=lambda actor, action, resource="", **params:
        {"id": next(operations)})
    with mock.patch.object(capacity, "apply", applied), \
            mock.patch.object(capacity, "_read_operation",
                              side_effect=lambda op: {
                                  "state": "done", "phase": "complete",
                                  "message": f"{op} finished"}), \
            mock.patch.object(capacity, "_sleep"), \
            mock.patch("mojo.apps.account.services.admin_platform"
                       ".audit_after_commit") as audit:
        result = capacity.run_batch(BATCH_WALK)

    assert result == "done", f"a clean walk ended {result!r}"
    final = capacity._read_batch(BATCH_WALK)
    assert [step["state"] for step in final["steps"]] == ["done"] * 3, \
        f"not every step finished: {[s['state'] for s in final['steps']]}"
    assert final["state"] == "done", f"the batch did not finish: {final['state']}"
    assert final["steps"][1]["operation"] == "op-2", \
        f"the child operation id was not recorded: {final['steps'][1]}"

    assert applied.call_args_list[1].kwargs == \
        {"count": 2, "apply_immediately": True}, \
        f"step params were not passed through: {applied.call_args_list[1]}"
    assert applied.call_args_list[0].args[0].pk == 7, \
        "the runner did not act as the batch's recorded actor"

    assert audit.call_count == 3, \
        f"3 steps wrote {audit.call_count} audit rows"
    audited = [(call.args[1], call.args[2]) for call in audit.call_args_list]
    assert audited == [
        (AUDIT_ACTIONS["add_node"], "fleet:op-1"),
        (AUDIT_ACTIONS["set_cache_replicas"], f"{CACHE_GROUP}:op-2"),
        (AUDIT_ACTIONS["drain_node"], f"{NODE_B}:op-3"),
    ], f"the audit trail is wrong: {audited}"


@th.django_unit_test("a failed step stops the batch; later steps are not attempted")
def test_run_batch_stops_at_first_failure(opts):
    from mojo.apps.aws.services import capacity

    record = _batch_record(BATCH_STOP, [
        _batch_step(0, "add_reader", CLUSTER),
        _batch_step(1, "remove_reader", READER_A),
        _batch_step(2, "drain_node", NODE_B),
    ])
    capacity._write_batch(record)
    operations = iter(["op-1", "op-2"])
    applied = mock.Mock(
        side_effect=lambda actor, action, resource="", **params:
        {"id": next(operations)})

    def read_operation(operation_id):
        if operation_id == "op-2":
            return {"state": "failed", "error_code": "provider_denied",
                    "message": "AWS refused the delete."}
        return {"state": "done", "phase": "complete", "message": "ok"}

    with mock.patch.object(capacity, "apply", applied), \
            mock.patch.object(capacity, "_read_operation",
                              side_effect=read_operation), \
            mock.patch.object(capacity, "_sleep"), \
            mock.patch("mojo.apps.account.services.admin_platform"
                       ".audit_after_commit"):
        result = capacity.run_batch(BATCH_STOP)

    assert result == "failed", f"a failed step ended the batch {result!r}"
    final = capacity._read_batch(BATCH_STOP)
    states = [step["state"] for step in final["steps"]]
    assert states == ["done", "failed", "not_attempted"], \
        f"the stop semantics are wrong: {states}"
    assert final["steps"][1]["error_code"] == "provider_denied", \
        f"the failed step lost the child's code: {final['steps'][1]}"
    assert final["state"] == "failed", "the batch did not fail"
    assert "Step 2 of 3 failed" in final["message"] \
        and "1 step(s) were not attempted" in final["message"], \
        f"the batch message does not say where things stand: {final['message']}"
    assert applied.call_count == 2, \
        f"apply ran {applied.call_count} times; step 3 must never start"


@th.django_unit_test("a claim conflict mid-batch stops it and leaves the claim untouched")
def test_run_batch_claim_conflict_stops(opts):
    from django.core.cache import cache as django_cache
    from mojo.apps.aws.services import capacity

    capacity._claim(capacity.ACTION_REMOVE_READER, READER_A, 99)
    record = _batch_record(BATCH_CLAIM, [
        _batch_step(0, "add_reader", CLUSTER),
        _batch_step(1, "remove_reader", READER_A),
        _batch_step(2, "drain_node", NODE_B),
    ])
    capacity._write_batch(record)

    def fake_apply(actor, action, resource="", **params):
        if action == capacity.ACTION_ADD_READER:
            return {"id": "op-1"}
        # The REAL claim path, against the REAL held claim.
        capacity._claim(action, resource, getattr(actor, "pk", None))
        raise AssertionError("the held claim was not honored")

    try:
        with mock.patch.object(capacity, "apply", side_effect=fake_apply), \
                mock.patch.object(capacity, "_read_operation",
                                  return_value={"state": "done",
                                                "phase": "complete",
                                                "message": "ok"}), \
                mock.patch.object(capacity, "_sleep"), \
                mock.patch("mojo.apps.account.services.admin_platform"
                           ".audit_after_commit"):
            result = capacity.run_batch(BATCH_CLAIM)

        assert result == "failed", f"a claim conflict ended the batch {result!r}"
        final = capacity._read_batch(BATCH_CLAIM)
        assert final["steps"][1]["error_code"] == "capacity_in_progress", \
            f"the conflict code was lost: {final['steps'][1]}"
        assert [step["state"] for step in final["steps"]] == \
            ["done", "failed", "not_attempted"], \
            f"the stop semantics are wrong: {[s['state'] for s in final['steps']]}"
        holder = django_cache.get(
            capacity._claim_key(capacity.ACTION_REMOVE_READER, READER_A))
        assert holder and holder.get("actor") == 99, \
            f"the OTHER operation's claim was disturbed: {holder}"
    finally:
        capacity._release(
            capacity._claim_key(capacity.ACTION_REMOVE_READER, READER_A))


# ── REST ────────────────────────────────────────────────────────────────────

def _view(name):
    import inspect
    from mojo.apps.aws.rest import capacity as views
    return inspect.unwrap(getattr(views, name))


def _request(user, **data):
    from objict import objict
    return SimpleNamespace(user=user, DATA=objict(**data), META={})


def _user(pk=1, superuser=False, perms=()):
    granted = set(perms)
    user = mock.Mock(is_superuser=superuser, pk=pk, username=f"user-{pk}")
    user.has_permission.side_effect = lambda wanted: bool(granted & set(wanted))
    return user


@th.django_unit_test("both batch endpoints carry the FULL apply gate — plan leaks intent")
def test_batch_rest_gates_match_apply(opts):
    from mojo import errors as me
    from mojo.apps.aws.rest import capacity as views
    from mojo.apps.aws.services import capacity
    from mojo.helpers import infrastructure

    for name in ("on_capacity_plan", "on_capacity_plan_apply"):
        func = getattr(views, name)
        assert getattr(func, "_mojo_denies_key_backed_session", False), \
            f"{name} accepts key-backed sessions"
        assert getattr(func, "_mojo_requires_fresh_auth", False), \
            f"{name} does not require fresh auth"
        assert getattr(func, "_mojo_fresh_auth_seconds", None) == 600, \
            f"{name} does not pin its fresh-auth window"

    plan_view = _view("on_capacity_plan")
    apply_view = _view("on_capacity_plan_apply")
    plan_body = {"steps": [{"action": "add_node"}]}
    apply_body = {"plan_id": "plan-1"}

    with mock.patch.object(capacity, "plan_batch") as planned, \
            mock.patch.object(capacity, "apply_batch") as applied:
        for perms in (["manage_aws"], ["manage_aws", "manage_platform"]):
            with th.assert_raises(me.PermissionDeniedException):
                plan_view(_request(_user(perms=perms), **plan_body))
            with th.assert_raises(me.PermissionDeniedException):
                apply_view(_request(_user(perms=perms), **apply_body))
        assert planned.call_count == 0 and applied.call_count == 0, \
            "a non-superuser reached the batch service"

        # External mode answers about the INSTALLATION, before the service and
        # before the superuser question.
        with mock.patch.object(infrastructure, "is_external",
                               return_value=True):
            for view, body in ((plan_view, plan_body),
                               (apply_view, apply_body)):
                response = view(_request(_user(superuser=True), **body))
                assert response.status_code == 403, \
                    f"external mode answered {response.status_code}"
        assert planned.call_count == 0 and applied.call_count == 0, \
            "external mode still reached the batch service"

        # The plan endpoint writes NO audit row; plan/apply writes exactly one.
        planned.return_value = {"id": "plan-9", "steps": []}
        applied.return_value = {"id": "batch-9", "steps": [{}, {}]}
        with mock.patch("mojo.apps.account.services.admin_platform"
                        ".audit_after_commit") as audit:
            plan_view(_request(_user(superuser=True), **plan_body))
            assert audit.call_count == 0, \
                f"a plan (a read of intent) wrote {audit.call_count} audit rows"
            apply_view(_request(_user(superuser=True), **apply_body))
        assert audit.call_count == 1, \
            f"one batch apply wrote {audit.call_count} audit rows"
        _, action, target = audit.call_args[0]
        assert action == "aws_capacity_batch", f"the audit action is {action!r}"
        assert target == "batch-9:2 steps", \
            f"the audit target does not identify the batch: {target!r}"

        # Malformed step lists never reach the service.
        for bad in (None, [], "steps", [{"action": "add_node"}, "x"]):
            with th.assert_raises(me.ValueException):
                plan_view(_request(_user(superuser=True), steps=bad))
        assert planned.call_count == 1, \
            "a malformed step list still reached the service"


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
