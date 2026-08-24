"""
The deploy webhook endpoints (maestro item #1458, D2) — serial half.

Webhook cases go through the REAL server (`requests` against the test uvicorn,
raw bytes — the HMAC covers the exact body, so the client must not re-encode
it), with the secret installed via `th.server_settings`. `server_settings`
reloads the shared test server, so these cases live in the serial sibling
(maestro #2792); the manual-deploy cases, which need no server reload, stay in
the parallel `test_edge/16_deploy_webhook.py`.
"""
import hashlib
import hmac
import json as jsonlib

import requests as rq
from testit import helpers as th

from tests.test_edge._helpers import (
    declare_edge_runner, login, make_group_member, make_user,
)

SECRET = "testit-deploy-hook-3f9c"
SHA_1 = "d" * 40
SHA_2 = "e" * 40
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


def _signed_post(opts, payload, secret=SECRET, event="push"):
    body = jsonlib.dumps(payload).encode("utf-8")
    signature = "sha256=" + hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    url = f"{opts.client.host}api/github/deploy/webhook"
    return rq.post(url, data=body, timeout=10, headers={
        "Content-Type": "application/json",
        "X-GitHub-Event": event,
        "X-Hub-Signature-256": signature,
    })


def _push(sha, ref="refs/heads/main", deleted=False):
    return dict(ref=ref, after=sha, deleted=deleted, pusher=dict(name="tester"))


@th.django_unit_test("webhook: bad signatures, non-push events and other branches never deploy")
def test_webhook_refusals(opts):
    from mojo.apps.edge.services import deploy

    with th.server_settings(GITHUB_WEBHOOK_SECRET=SECRET):
        resp = _signed_post(opts, _push(SHA_1), secret="the-wrong-secret")
        th.assert_eq(resp.status_code, 403,
                     f"a bad signature must be refused, got {resp.status_code}: {resp.text}")

        resp = _signed_post(opts, dict(zen="Design for failure."), event="ping")
        th.assert_eq(resp.status_code, 200,
                     f"a ping must be acknowledged, got {resp.status_code}: {resp.text}")
        th.assert_true(resp.json().get("ignored"), "a ping must be ignored, not deployed")

        resp = _signed_post(opts, _push(SHA_1, ref="refs/heads/feature-x"))
        th.assert_eq(resp.status_code, 200,
                     f"a feature-branch push must be acknowledged, got {resp.status_code}")
        th.assert_true(resp.json().get("ignored"),
                       "a push to a non-deploy branch must never deploy")

        resp = _signed_post(opts, _push("0" * 40, deleted=True))
        th.assert_true(resp.json().get("ignored"),
                       "a branch deletion (zero sha) must never deploy")

    th.assert_eq(deploy.get_target(), None,
                 "none of the refused webhooks may have recorded a target")
    th.assert_eq(deploy.get_status(), None,
                 "none of the refused webhooks may have armed a deploy")


@th.django_unit_test("webhook: a push deploys the payload's sha; a second push is recorded, not started")
def test_webhook_deploy_flow(opts):
    from mojo.apps.edge.services import deploy
    from mojo.apps.jobs.models import Job

    declare_edge_runner()
    with th.server_settings(GITHUB_WEBHOOK_SECRET=SECRET):
        resp = _signed_post(opts, _push(SHA_1))
        th.assert_eq(resp.status_code, 202,
                     f"a deploy-branch push must be accepted, got {resp.status_code}: {resp.text}")
        th.assert_true(resp.json().get("queued"),
                       f"the first push must start a deploy, got {resp.text}")

        target = deploy.get_target()
        th.assert_eq(target and target["sha"], SHA_1,
                     f"the deployed commit comes from the PAYLOAD, got {target!r}")
        status = deploy.get_status()
        th.assert_eq(status and status["state"], deploy.STATUS_MIGRATING,
                     f"the push must arm the deploy, got {status!r}")

        resp = _signed_post(opts, _push(SHA_2))
        th.assert_eq(resp.status_code, 202,
                     f"a mid-deploy push must still be accepted, got {resp.status_code}")
        th.assert_true(not resp.json().get("queued"),
                       "a mid-deploy push must be recorded, never a second deploy")

    target = deploy.get_target()
    th.assert_eq(target and target["sha"], SHA_2,
                 f"the mid-deploy push must overwrite the target, got {target!r}")
    status = deploy.get_status()
    th.assert_eq(status and status["sha"], SHA_1,
                 f"the armed deploy must still be the first push, got {status!r}")
    rows = Job.objects.filter(func=deploy.DEPLOY_ORCHESTRATE_JOB)
    th.assert_eq(rows.count(), 1,
                 f"exactly one orchestrate may exist across both pushes (NX), got {rows.count()}")
    deploy.clear_status(status["deployment"])
