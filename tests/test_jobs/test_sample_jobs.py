"""
Sample Jobs Tests - the shipped examples in mojo.apps.jobs.examples.sample_jobs.

Calls the shipped functions themselves, not a copy of their pattern.

Run in your Django project:
    python manage.py testit test_jobs.test_sample_jobs
"""
from testit import helpers as th


class _FakeJob:
    def __init__(self, payload):
        self.payload = payload
        self.metadata = {}

    def check_cancel_requested(self):
        return False


@th.django_unit_test()
def test_shipped_cleanup_old_records_runs(opts):
    """#7387: the shipped cleanup_old_records runs to completion on Django 5."""
    from datetime import datetime, timedelta
    from mojo.apps.jobs.examples import sample_jobs

    job = _FakeJob({"model_name": "JobEvent", "days_old": 30, "batch_size": 100, "dry_run": True})

    # The example sleeps 0.5s in each of its 5 batches; it is called as shipped.
    result = sample_jobs.cleanup_old_records(job)

    assert result == "completed", f"Expected completed, got: {result}"
    for key in ("started_at", "completed_at"):
        value = datetime.fromisoformat(job.metadata[key])
        assert value.utcoffset() == timedelta(0), \
            f"{key} should carry the UTC timezone, got: {job.metadata[key]}"
    assert job.metadata["total_deleted"] == 500, \
        f"Expected 5 batches of 100, got: {job.metadata.get('total_deleted')}"
