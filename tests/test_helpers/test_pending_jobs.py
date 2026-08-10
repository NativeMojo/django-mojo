"""Isolation contracts for testit's in-process job executor."""

import uuid
from unittest import mock

from testit import helpers as th


@th.django_unit_test("run_pending_jobs scopes ownership and tolerates a deleted row")
def test_run_pending_jobs_isolation(opts):
    from mojo.apps.jobs.models import Job

    prefix = "testit.helpers.isolation"
    Job.objects.filter(func__startswith=prefix).delete()
    owned = Job.objects.create(
        id=uuid.uuid4().hex, channel="default", func=f"{prefix}.owned",
        payload={"owner": "one"})
    other = Job.objects.create(
        id=uuid.uuid4().hex, channel="default", func=f"{prefix}.other",
        payload={"owner": "two"})

    def _delete_while_running(job):
        job.delete()

    try:
        with mock.patch(
                "mojo.apps.jobs.job_engine.load_job_function",
                return_value=_delete_while_running):
            executed = th.run_pending_jobs(
                func=owned.func, payload={"owner": "one"})

        assert executed == 1, f"only the owned job should execute, got {executed}"
        other.refresh_from_db()
        assert other.status == "pending", \
            f"the helper consumed another module's job: {other.status}"
    finally:
        Job.objects.filter(func__startswith=prefix).delete()
