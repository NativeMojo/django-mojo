"""Tests for jobs.publish_webhook(group=...) auto-signing — the parallel-safe
remainder.

The handler-time signing tests (and the setting-override tests) moved to
tests/test_jobs_extended_serial/test_signed_webhook.py: they patch the shared
mojo.apps.jobs.handlers.webhook module's `requests` attribute in-process and
mutate django.conf.settings, which is unsafe under the parallel default tier
(maestro item #1839).
"""
from testit import helpers as th


GROUP_NAME = "signed_wh_group"


@th.django_unit_setup()
def setup_signed_webhook(opts):
    from mojo.apps.account.models import Group

    Group.objects.filter(name=GROUP_NAME).delete()
    g = Group.objects.create(name=GROUP_NAME, kind="organization")
    opts.group_id = g.pk


# ---------------------------------------------------------------------------
# publish_webhook(group=...) — payload shape (no secret in queue)
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_publish_stores_sign_group_id_not_secret(opts):
    """publish_webhook(group=g) records sign_group_id and never the raw secret."""
    from mojo.apps.account.models import Group
    from mojo.apps.jobs.models import Job
    from mojo.apps import jobs

    g = Group.objects.get(pk=opts.group_id)
    # Ensure a secret exists so we can assert it does NOT appear in the payload
    g.get_webhook_secret(auto_create=True)
    g.refresh_from_db()
    secret = g.get_webhook_secret()
    assert secret and secret.startswith("wsec_"), "precondition: group secret must exist"

    Job.objects.filter(channel="webhooks").delete()
    job_id = jobs.publish_webhook(
        url="https://example.test/hook",
        data={"event": "ping"},
        group=g,
    )
    job = Job.objects.get(id=job_id)

    assert job.payload.get("sign_group_id") == opts.group_id, (
        f"payload.sign_group_id must be the group pk, got {job.payload.get('sign_group_id')!r}"
    )
    payload_str = str(job.payload)
    assert secret not in payload_str, (
        "raw webhook secret must NEVER appear in the job payload (queue snapshot)"
    )
    Job.objects.filter(id=job_id).delete()


@th.django_unit_test()
def test_publish_without_group_has_no_sign_group_id(opts):
    """Regression guard: existing unsigned callers stay exactly unchanged."""
    from mojo.apps.jobs.models import Job
    from mojo.apps import jobs

    Job.objects.filter(channel="webhooks").delete()
    job_id = jobs.publish_webhook(
        url="https://example.test/hook",
        data={"event": "noop"},
    )
    job = Job.objects.get(id=job_id)
    assert job.payload.get("sign_group_id") is None, (
        f"unsigned publish must not set sign_group_id, got {job.payload.get('sign_group_id')!r}"
    )
    Job.objects.filter(id=job_id).delete()
