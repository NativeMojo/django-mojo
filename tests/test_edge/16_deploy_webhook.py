"""
The manual deploy endpoint (maestro item #1458, D2) — parallel half.

The manual endpoint's big test is the escalation `requires_global_perms`
exists to refuse: a member-level `manage_deploy` grant plus `?group=<own
group>` must stay 403 — a tenant admin must not be able to move the fleet.

The webhook cases (which install the secret via `th.server_settings`, reloading
the shared test server) live in the serial sibling
`test_edge_serial/16_deploy_webhook.py` (maestro #2792).
"""
from testit import helpers as th

from tests.test_edge._helpers import (
    declare_edge_runner, login, make_group_member, make_user,
)

SHA_3 = "f" * 40


@th.django_unit_setup()
def setup_webhook(opts):
    from mojo.apps.edge.services import deploy
    from mojo.apps.edge.models import PlatformDeployment
    from mojo.apps.jobs.models import Job

    deploy.get_client().delete(deploy.TARGET_KEY, deploy.STATUS_KEY)
    PlatformDeployment.objects.all().delete()
    Job.objects.filter(
        func__in=[deploy.DEPLOY_ORCHESTRATE_JOB, deploy.DEPLOY_NODE_JOB]).delete()

    opts.op_user, opts.op_email, opts.op_password = make_user(perms=["manage_deploy"])
    opts.plain_user, opts.plain_email, opts.plain_password = make_user()
    (opts.member_user, opts.member_email,
     opts.member_password, opts.member_group) = make_group_member(["manage_deploy"])


@th.django_unit_test("manual deploy: anonymous and unprivileged callers are refused")
def test_manual_deploy_refusals(opts):
    from mojo.apps.edge.services import deploy

    opts.client.logout()
    resp = opts.client.post("/api/edge/deploy", dict(sha=SHA_3))
    th.assert_in(resp.status_code, (401, 403),
                 f"anonymous manual deploy must be refused, got {resp.status_code}")

    login(opts, opts.plain_email, opts.plain_password)
    resp = opts.client.post("/api/edge/deploy", dict(sha=SHA_3))
    th.assert_eq(resp.status_code, 403,
                 f"a user without manage_deploy must be refused, got {resp.status_code}")
    opts.client.logout()

    th.assert_eq(deploy.get_status(), None,
                 "refused manual deploys must arm nothing")


@th.django_unit_test("manual deploy: a member-level manage_deploy grant must NOT move the fleet")
def test_manual_deploy_member_grant_refused(opts):
    from mojo.apps.edge.services import deploy

    login(opts, opts.member_email, opts.member_password)
    # The exact escalation shape requires_global_perms exists to refuse:
    # a member-scoped grant plus the caller's own group id.
    resp = opts.client.post(
        "/api/edge/deploy",
        dict(sha=SHA_3, group=opts.member_group.id))
    th.assert_eq(resp.status_code, 403,
                 f"a member-level grant must never satisfy the global gate, "
                 f"got {resp.status_code}: {resp.json}")
    opts.client.logout()
    th.assert_eq(deploy.get_status(), None,
                 "the refused member deploy must arm nothing")


@th.django_unit_test("manual deploy: a global manage_deploy holder deploys a named commit")
def test_manual_deploy_allowed(opts):
    from mojo.apps.edge.services import deploy
    from mojo.apps.jobs.models import Job

    deploy.get_client().delete(deploy.TARGET_KEY, deploy.STATUS_KEY)
    Job.objects.filter(func=deploy.DEPLOY_ORCHESTRATE_JOB).delete()
    declare_edge_runner()

    login(opts, opts.op_email, opts.op_password)
    resp = opts.client.post("/api/edge/deploy", dict(sha="not-a-sha"))
    th.assert_eq(resp.status_code, 400,
                 f"an invalid sha must be a 400, got {resp.status_code}: {resp.json}")

    resp = opts.client.post("/api/edge/deploy", dict(sha=SHA_3.upper()))
    th.assert_eq(resp.status_code, 202,
                 f"the operator deploy must be accepted, got {resp.status_code}: {resp.json}")
    target = deploy.get_target()
    th.assert_eq(target and target["sha"], SHA_3,
                 f"the sha must be normalised to lowercase and recorded, got {target!r}")
    th.assert_eq(target and target["actor"], f"manual:{opts.op_email}",
                 f"the manual deploy must be attributed to the operator, got {target!r}")
    rows = Job.objects.filter(func=deploy.DEPLOY_ORCHESTRATE_JOB)
    th.assert_eq(rows.count(), 1,
                 f"the operator deploy must publish one orchestrate, got {rows.count()}")
    opts.client.logout()
    status = deploy.get_status()
    deploy.clear_status(status["deployment"])

