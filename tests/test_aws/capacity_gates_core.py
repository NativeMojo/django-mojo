"""Default-tier representatives for the capacity security gates (item #2558).

The exhaustive versions of these contracts moved to
``tests/test_aws_extended_serial/capacity.py`` because they reach their
boundary by patching shared module attributes (``capacity._dispatch``,
``capacity.apply``, ``mojo.helpers.infrastructure``), which is not safe under
the parallel default tier. Four boundaries were too important to leave with no
default-tier coverage at all, so each one is asserted here WITHOUT a single
patch of production state:

* external-mode refusal — driven through the ``infrastructure_mode`` seam on
  ``capacity.apply`` and the pure ``capacity._offers(envelope)`` projection;
* the superuser AND manage_aws write gate — real users, real login, the live
  endpoint;
* the terminate fleet-membership proof — the service predicate itself, fed
  test-owned serving data and a locally built EC2 fake;
* the resize field gating — the live endpoint's 400s, which are all decided
  before anything reaches AWS.

Everything here is either a pure function over a dict this file built, a
locally constructed fake passed through an existing ``client=`` seam, or an
HTTP request from a user this file created. Nothing is mocked into place.
"""

from types import SimpleNamespace
from unittest import mock

from testit import helpers as th


GROUP_ARN = ("arn:aws:elasticloadbalancing:us-east-1:123456789012:"
             "targetgroup/mojo-gates/abcdef0123456789")
# Deliberately distinct from every other capacity module's ids: these never
# reach a claim key, but a shared identifier space invites one that does.
NODE_A = "i-0a1b2c3d4e5f6aa11"
NODE_B = "i-0a1b2c3d4e5f6aa22"
CACHE_GROUP = "mojo-gates-redis"
DATABASE = "mojo-gates-aurora-2"

ROOT_USERNAME = "capacity-gates-root"
OPERATOR_USERNAME = "capacity-gates-operator"
PASSWORD = "Ca##gates99xx"

IDENTITY = {"mojo:project": "mojo-gates", "mojo:env": "prod"}


# ── fixtures ────────────────────────────────────────────────────────────────

def _actor(pk=1):
    return SimpleNamespace(pk=pk, username="capacity-gates-actor",
                           is_superuser=True)


def _instance_row(instance_id, name, tags=None, state="running"):
    """One raw EC2 ``Instances`` row, as describe_instances returns it."""
    row = {
        "InstanceId": instance_id, "InstanceType": "m6i.large",
        "ImageId": "ami-0source", "SubnetId": "subnet-0aaa", "VpcId": "vpc-0aaa",
        "State": {"Name": state}, "Placement": {"AvailabilityZone": "us-east-1a"},
        "PrivateIpAddress": "10.0.1.11",
        "PrivateDnsName": "ip-10-0-1-11.ec2.internal",
        "SecurityGroups": [{"GroupId": "sg-0aaa"}],
        "Tags": [{"Key": "Name", "Value": name}],
    }
    for key, value in (tags or {}).items():
        row["Tags"].append({"Key": key, "Value": value})
    return row


def _routed_ec2_client(rows_by_id):
    """A LOCAL fake: describe_instances answered per instance-id filter.

    Passed through capacity's own ``ec2_client=`` seam, so no module attribute
    anywhere is replaced.
    """
    client = mock.Mock()

    def answer(Filters=None, **_kwargs):
        wanted = []
        for spec in Filters or []:
            if spec.get("Name") == "instance-id":
                wanted = spec.get("Values") or []
        rows = [rows_by_id[value] for value in wanted if value in rows_by_id]
        return {"Reservations": [{"Instances": rows}]}

    client.describe_instances.side_effect = answer
    return client


def _serving(registered=(NODE_A,)):
    """The serving map shape ``_fleet_serving`` returns, built by hand."""
    return {"groups": [{
        "arn": GROUP_ARN, "name": "mojo-gates", "port": 443,
        "targets": [{"id": node, "state": "healthy", "port": 443}
                    for node in registered]}]}


def _envelope(mode):
    """A report envelope healthy enough that only the MODE can block."""
    return {
        "mode": mode,
        "identity_available": True,
        "node_id_pinned": False,
        "nodes": {
            "serving_available": True,
            "inventory_available": True,
            "instances": [
                {"id": NODE_A, "healthy": True, "can_drain": True,
                 "instance_state": "running", "state": "healthy"},
                {"id": NODE_B, "healthy": True, "can_drain": True,
                 "instance_state": "running", "state": "healthy"},
            ],
        },
        "databases": [],
        "databases_available": True,
        "caches": [{"identifier": CACHE_GROUP, "replica_count": 1,
                    "blocked_reason": None}],
        "caches_available": True,
        "egress": {"fleet_available": True, "addresses_available": True,
                   "policy_available": True, "enabled": False,
                   "attached": [], "pending_nodes": []},
    }


def _make_user(username, superuser):
    from mojo.apps.account.models import User

    email = f"{username}@test.com"
    User.objects.filter(username=username).delete()
    User.objects.filter(email=email).delete()
    user = User.objects.create_user(
        email=email, username=username, password=PASSWORD)
    user.is_active = True
    user.is_email_verified = True
    user.requires_mfa = False
    user.is_superuser = superuser
    user.save()
    # manage_aws is the decorator half of the write gate. Both users hold it,
    # so what separates them in every test below is superuser and nothing else.
    user.add_permission(["manage_aws"])
    user.save()
    return user


@th.django_unit_setup()
def setup_capacity_gates(opts):
    # Long-lived database: delete what this module creates before creating it.
    opts.root = _make_user(ROOT_USERNAME, True)
    opts.operator = _make_user(OPERATOR_USERNAME, False)


# ── external mode ───────────────────────────────────────────────────────────

@th.django_unit_test("an externally-managed installation refuses EVERY capacity action")
def test_external_mode_refuses_every_action(opts):
    from mojo.apps.aws.services import capacity
    from mojo.helpers import infrastructure

    # `infrastructure_mode` is capacity.apply's declared test seam: the live
    # read stays untouched, so this proves the refusal without making any
    # other module's infrastructure read say "external" for the window.
    for action in list(capacity.ACTIONS) + ["not_an_action"]:
        with th.assert_raises(capacity.CapacityError) as caught:
            capacity.apply(_actor(), action, NODE_B, count=2,
                           apply_immediately=True,
                           infrastructure_mode=infrastructure.EXTERNAL)
        assert caught.exception.error_code == infrastructure.ERROR_CODE, \
            (f"{action} refused with {caught.exception.error_code!r} rather "
             f"than the infrastructure-mode code")
        assert caught.exception.status == 403, \
            f"{action} answered {caught.exception.status} in external mode"

    # The control, and the ordering proof in one: the SAME unknown action on a
    # managed installation is refused for its contents instead. So the loop
    # above really did refuse on the installation — before the request was even
    # parsed, which is why no future action can be added that skips the gate.
    with th.assert_raises(capacity.CapacityError) as caught:
        capacity.apply(_actor(), "not_an_action", NODE_B,
                       infrastructure_mode=infrastructure.MANAGED)
    assert caught.exception.error_code == "invalid_request", \
        (f"a managed installation refused an unknown action with "
         f"{caught.exception.error_code!r} — the external loop above proves "
         f"nothing if every call refuses the same way")


@th.django_unit_test("external mode offers no capacity control, whatever the fleet looks like")
def test_external_mode_offers_nothing(opts):
    from mojo.apps.aws.services import capacity
    from mojo.helpers import infrastructure

    # The panel renders `offers` verbatim, so a control the server would refuse
    # must not come back offered. `_offers` is pure over the envelope — this
    # calls it with two envelopes that differ ONLY in `mode`.
    managed = capacity._offers(_envelope(infrastructure.MANAGED))
    for name in (capacity.ACTION_ADD_NODE, capacity.ACTION_DRAIN_NODE,
                 capacity.ACTION_TERMINATE_NODE,
                 capacity.ACTION_SET_CACHE_REPLICAS,
                 capacity.ACTION_RESIZE_CACHE):
        assert managed[name] == {"offered": True, "blocked_reason": None}, \
            (f"{name} was not offered on a healthy managed fleet: "
             f"{managed[name]} — the external comparison below proves nothing")

    external = capacity._offers(_envelope(infrastructure.EXTERNAL))
    for name in capacity.ACTIONS:
        assert external[name] == {
            "offered": False,
            "blocked_reason": infrastructure.ERROR_CODE}, \
            f"{name} was still offered in external mode: {external[name]}"


@th.django_unit_test("an unreadable or unrecognized infrastructure mode fails closed")
def test_infrastructure_mode_fails_closed(opts):
    from mojo.helpers import infrastructure

    def reader_for(value):
        def read(_setting, _default=""):
            return value
        return read

    def broken(_setting, _default=""):
        raise RuntimeError("the settings backend is down")

    for value in ("", "managed", " MANAGED ", None):
        assert infrastructure.infrastructure_mode(
            reader=reader_for(value)) == infrastructure.MANAGED, \
            f"{value!r} did not read as a managed installation"
    for value in ("external", " External ", "extenral", "yes", 1, object()):
        assert infrastructure.infrastructure_mode(
            reader=reader_for(value)) == infrastructure.EXTERNAL, \
            (f"{value!r} did not fail closed to external — a typo in the "
             f"switch must never turn the refusal off")
    assert infrastructure.infrastructure_mode(reader=broken) == \
        infrastructure.EXTERNAL, \
        "a settings read that raised was treated as a licence to mutate"


# ── the write gate ──────────────────────────────────────────────────────────

@th.django_unit_test("capacity apply needs a literal superuser, not just manage_aws")
def test_apply_requires_a_superuser(opts):
    assert opts.client.login(OPERATOR_USERNAME, PASSWORD), \
        "the non-superuser operator could not log in"
    try:
        # An EMPTY body on purpose. The superuser check runs before the request
        # is parsed, so a 403 here (rather than the 400 a parsed empty body
        # earns) is what proves the gate is still in front of everything.
        response = opts.client.post("/api/aws/capacity/apply", {})
        assert response.status_code == 403, \
            (f"a manage_aws holder who is not a superuser got "
             f"{response.status_code}: {opts.client.last_response.body}")
        error = str((response.json or {}).get("error") or "").lower()
        assert "superuser" in error, \
            (f"the refusal did not come from the superuser gate: {error!r} — "
             f"a permission or freshness denial would pass a weaker assert")
    finally:
        opts.client.logout()

    # The same body from a superuser holding manage_aws gets PAST the gate and
    # is refused on its contents instead. Without this the test above would
    # still pass if the endpoint refused everyone.
    assert opts.client.login(ROOT_USERNAME, PASSWORD), \
        "the superuser could not log in"
    try:
        response = opts.client.post("/api/aws/capacity/apply", {})
        assert response.status_code == 400, \
            (f"a superuser holding manage_aws was stopped with "
             f"{response.status_code}: {opts.client.last_response.body}")
        error = str((response.json or {}).get("error") or "").lower()
        assert "action" in error, \
            f"the superuser's empty body was refused for the wrong reason: {error!r}"
    finally:
        opts.client.logout()


@th.django_unit_test("both batch endpoints carry the same superuser gate as apply")
def test_batch_endpoints_carry_the_apply_gate(opts):
    from mojo.apps.aws.rest import capacity as views

    # A plan writes nothing to AWS, but it reveals topology, intent and cost —
    # so it carries the write gate, not a read gate.
    for name in ("on_capacity_apply", "on_capacity_plan",
                 "on_capacity_plan_apply"):
        func = getattr(views, name)
        assert getattr(func, "_mojo_denies_key_backed_session", False), \
            f"{name} accepts key-backed sessions"
        assert getattr(func, "_mojo_requires_fresh_auth", False), \
            f"{name} does not require fresh auth"
        assert getattr(func, "_mojo_fresh_auth_seconds", None) == 600, \
            f"{name} does not pin its fresh-auth window"

    assert opts.client.login(OPERATOR_USERNAME, PASSWORD), \
        "the non-superuser operator could not log in"
    try:
        for path, body in (("/api/aws/capacity/plan",
                            {"steps": [{"action": "add_node"}]}),
                           ("/api/aws/capacity/plan/apply",
                            {"plan_id": "plan-1"})):
            response = opts.client.post(path, body)
            assert response.status_code == 403, \
                (f"{path} answered {response.status_code} to a manage_aws "
                 f"holder who is not a superuser: "
                 f"{opts.client.last_response.body}")
            error = str((response.json or {}).get("error") or "").lower()
            assert "superuser" in error, \
                f"{path} refused for a reason other than the superuser gate: {error!r}"
    finally:
        opts.client.logout()


# ── terminate: fleet membership ─────────────────────────────────────────────

@th.django_unit_test("terminate proves fleet membership from fresh EC2 facts, never from absence")
def test_terminate_membership_proof_fails_closed(opts):
    from mojo.apps.aws.services import capacity

    # `_prove_fleet_member` IS the guard a drained-out node meets: it is
    # reached exactly when the node is registered in no target group, and it
    # gates TerminateInstances. Nothing here is claimed or dispatched, so the
    # predicate can be asserted on its own with test-owned data.
    anchor = _instance_row(NODE_A, "mojo-gates-a",
                           {"managed-by": "django-mojo", **IDENTITY})

    def prove(rows, serving=None):
        return capacity._prove_fleet_member(
            NODE_B, serving if serving is not None else _serving(),
            ec2_client=_routed_ec2_client(rows))

    # Ownership tag PLUS an exact project/env match against a REGISTERED
    # member: proven, no refusal.
    matching = _instance_row(NODE_B, "mojo-gates-b",
                             {"managed-by": "django-mojo", **IDENTITY})
    prove({NODE_A: anchor, NODE_B: matching})

    # This feature's own clone stamp is proof on its own.
    cloned = _instance_row(NODE_B, "mojo-gates-b-clone",
                           {"mojo:created-by": "admin-capacity"})
    prove({NODE_A: _instance_row(NODE_A, "mojo-gates-a"), NODE_B: cloned})

    def refused(rows, serving=None):
        with th.assert_raises(capacity.CapacityError) as caught:
            prove(rows, serving=serving)
        return caught.exception

    # Same account, django-mojo tagged, DIFFERENT environment or project.
    for pairs in ({"managed-by": "django-mojo", "mojo:project": "mojo-gates",
                   "mojo:env": "staging"},
                  {"managed-by": "django-mojo", "mojo:project": "other-app",
                   "mojo:env": "prod"}):
        error = refused({NODE_A: anchor,
                         NODE_B: _instance_row(NODE_B, "mojo-gates-b", pairs)})
        assert error.error_code == "not_fleet_member" and error.status == 409, \
            (f"a cross-environment instance {pairs} was terminable: "
             f"{error.error_code}/{error.status}")

    # An anchor missing its identity tags proves nothing — never a wildcard.
    error = refused({NODE_A: _instance_row(NODE_A, "mojo-gates-a",
                                           {"managed-by": "django-mojo"}),
                     NODE_B: matching})
    assert error.error_code == "not_fleet_member", \
        f"an identity-less anchor vouched for a candidate: {error.error_code}"

    # No registered member at all: identity cannot be verified, so the
    # terminate is refused outright rather than allowed by default.
    error = refused({NODE_B: matching}, serving={"groups": []})
    assert error.error_code == "not_fleet_member", \
        f"an anchorless fleet still authorized a terminate: {error.error_code}"

    # Untagged, and unknown to EC2 entirely: both refusals, never a pass.
    error = refused({NODE_A: anchor, NODE_B: _instance_row(NODE_B, "stranger")})
    assert error.error_code == "not_fleet_member", \
        f"an untagged stranger was terminable: {error.error_code}"
    error = refused({NODE_A: anchor})
    assert error.error_code == "not_fleet_member", \
        f"an instance AWS never reported was terminable: {error.error_code}"


# ── resize: the fields the endpoint insists on ──────────────────────────────

@th.django_unit_test("a resize states its size and its window, or the endpoint refuses it")
def test_apply_resize_rest_fields(opts):
    # Every refusal below is decided in the view, before capacity.apply and
    # therefore before any AWS call — which is what makes this safe to run
    # against the live endpoint with no provider fakes at all.
    assert opts.client.login(ROOT_USERNAME, PASSWORD), \
        "the superuser could not log in"
    base = {"action": "resize_database", "resource": DATABASE,
            "confirm_resource": DATABASE}
    try:
        def refused(body, expect):
            response = opts.client.post("/api/aws/capacity/apply", body)
            assert response.status_code == 400, \
                (f"{body} answered {response.status_code}: "
                 f"{opts.client.last_response.body}")
            error = str((response.json or {}).get("error") or "").lower()
            assert expect in error, \
                f"{body} was refused for the wrong reason: {error!r}"

        refused(dict(base, apply_immediately=True), "size")
        refused(dict(base, size="medium"), "apply_immediately")
        # A string is not a boolean, and "false" would be truthy.
        refused(dict(base, size="medium", apply_immediately="true"),
                "apply_immediately")
        refused({"action": "resize_cache", "resource": CACHE_GROUP,
                 "confirm_resource": "wrong-group", "size": "large",
                 "apply_immediately": True}, "confirm_resource")
        # The control: a shape the view accepts reaches the ACTION check, so
        # the four refusals above are field decisions, not a blanket 400.
        refused(dict(base, action="resize_nothing"), "unknown capacity action")
    finally:
        opts.client.logout()


# ── the persistence gate: an unrecordable step never happens (item #2721) ────
#
# The BEHAVIORAL regressions — that a drain never deregisters and a terminate
# never terminates when the cache refuses the write — live in
# ``tests/test_aws_extended_serial/capacity.py``, because reaching them means
# spying on ``elbv2_helper``/``ec2_helper``, and every such patch is a counted
# cold site the isolation ratchet rejects.
#
# What is here PINS THE CONTRACT those regressions depend on, through the
# ``store=``/``retry_seconds=`` seams, patching nothing at all: an operation
# that cannot be recorded is refused, a checkpoint refuses both failure shapes
# the deployed backend produces, a success that returns ``None`` is still a
# success, and progress notes stay tolerant.

# Never a key any claim uses: a refused _new_operation releases its claim, and
# the release is a real delete against the shared cache.
UNHELD_CLAIM = "mojo:aws:capacity:claim:capacity-gates-never-held"


class _FakeStore:
    """A dict-backed cache, as a healthy backend behaves.

    ``result`` is what ``set`` returns: ``True`` like Redis, or ``None`` like
    the Django backends that return nothing on success. Both are successes.
    """

    def __init__(self, result=True):
        self.values = {}
        self.result = result

    def set(self, key, value, timeout=None):
        self.values[key] = value
        return self.result


def _broken_store(error=None):
    """A cache broken the way the deployed backend actually breaks.

    ``MojoRedisCache.set`` catches every exception itself and returns
    ``False``, so a dead Redis is a falsy RETURN, not an exception — which is
    exactly what the discarded return value could not see. ``error`` covers
    the other shape: a backend that lets the exception out.
    """
    store = mock.Mock()
    if error is None:
        store.set.return_value = False
    else:
        store.set.side_effect = error
    return store


def _recorded(capacity, store, action=None):
    """One live operation record, written through a HEALTHY store."""
    return capacity._new_operation(action or capacity.ACTION_DRAIN_NODE,
                                   NODE_A, _actor(), UNHELD_CLAIM, store=store)


@th.django_unit_test("a request whose operation cannot be recorded is refused, not started")
def test_new_operation_refuses_when_the_record_cannot_be_recorded(opts):
    from mojo.apps.aws.services import capacity

    # A 200 here would promise work that can never run: the job looks the
    # record up by id, finds nothing, and the claim wedges the resource for
    # the full CLAIM_TTL while /status answers 404.
    with th.assert_raises(capacity.CapacityError) as caught:
        capacity._new_operation(capacity.ACTION_DRAIN_NODE, NODE_A, _actor(),
                                UNHELD_CLAIM, store=_broken_store())
    assert caught.exception.error_code == "cache_unavailable", \
        (f"an unrecordable operation was refused as "
         f"{caught.exception.error_code}, not cache_unavailable")
    assert caught.exception.status == 503, \
        f"an unrecordable operation answered {caught.exception.status}"


@th.django_unit_test("a step that cannot record its progress refuses to enter its phase")
def test_checkpoint_refuses_a_cache_that_refuses_the_write(opts):
    from mojo.apps.aws.services import capacity

    record = _recorded(capacity, _FakeStore())
    with th.assert_raises(capacity.CapacityPersistenceError):
        capacity._checkpoint(record, "draining",
                             "taking the node out of the serving path",
                             store=_broken_store(), retry_seconds=0)


@th.django_unit_test("a cache that raises refuses the step too, not just a falsy one")
def test_checkpoint_refuses_a_cache_that_raises(opts):
    from mojo.apps.aws.services import capacity

    record = _recorded(capacity, _FakeStore())
    with th.assert_raises(capacity.CapacityPersistenceError):
        capacity._checkpoint(record, "terminating", "terminating the node",
                             store=_broken_store(RuntimeError("redis is gone")),
                             retry_seconds=0)


@th.django_unit_test("a backend that returns None on a successful write is a success")
def test_checkpoint_accepts_a_store_that_returns_none(opts):
    from mojo.apps.aws.services import capacity

    # Guards `wrote is False` against a falsiness regression: `not None` is
    # true, and reading it as a refusal would abort every capacity operation
    # on a backend that simply returns nothing.
    store = _FakeStore(result=None)
    record = _recorded(capacity, store)
    returned = capacity._checkpoint(record, "draining",
                                    "taking the node out of the serving path",
                                    store=store, retry_seconds=0)
    assert returned is record, \
        "a successful checkpoint did not return the record it wrote"
    written = store.values.get(capacity._operation_key(record["id"])) or {}
    assert written.get("phase") == "draining", \
        f"the checkpoint did not record the phase it entered: {written!r}"


@th.django_unit_test("progress notes stay tolerant of a cache that will not take them")
def test_progress_notes_stay_tolerant(opts):
    from mojo.apps.aws.services import capacity

    # The whole point of the split: losing an observation loses nothing
    # report() cannot re-derive, so a note must never abort a live operation.
    record = _recorded(capacity, _FakeStore())
    returned = capacity._write_operation(record, store=_broken_store())
    assert returned is record, \
        "a discarded progress note did not return the record"
    returned = capacity._write_operation(
        record, store=_broken_store(RuntimeError("redis is gone")))
    assert returned is record, \
        "a raising cache aborted a tolerant progress note"
