"""Capacity batch runner walks and in-process REST gates.

Moved from tests/test_aws/capacity_batch.py because they patch shared module
attributes (capacity.apply/_read_operation/_sleep/plan_batch/apply_batch,
mojo.helpers.infrastructure.is_external, and
mojo.apps.account.services.admin_platform.audit_after_commit), which is
unsafe under the parallel default tier (maestro item #2558). They run in the
opt-in serial ``extended`` tier; the package __init__ declares TESTIT.

Identifiers deliberately differ from the default-tier module's values so the
two modules never touch each other's shared-cache keys.
"""

from types import SimpleNamespace
from unittest import mock

from testit import helpers as th


NODE_B = "i-0b1b2c3d4e5f60022"
CLUSTER = "mojo-batchx-aurora"
READER_A = "mojo-batchx-aurora-2"
CACHE_GROUP = "mojo-batchx-redis"

BATCH_WALK = "batchx-test-walk"
BATCH_STOP = "batchx-test-stop"
BATCH_CLAIM = "batchx-test-claim"


# ── fixtures ────────────────────────────────────────────────────────────────

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
        for resource in ("fleet", NODE_B, CLUSTER, READER_A, CACHE_GROUP):
            capacity._release(capacity._claim_key(action, resource))
    for batch_id in (BATCH_WALK, BATCH_STOP, BATCH_CLAIM):
        try:
            django_cache.delete(capacity._batch_key(batch_id))
        except Exception:
            pass
    capacity.invalidate()


@th.django_unit_setup()
def setup_capacity_batch_extended(opts):
    # Claims and fixed-id batch records live in a shared cache with long TTLs;
    # delete what this module will create so a previous run cannot bleed in.
    _clear_keys()


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
