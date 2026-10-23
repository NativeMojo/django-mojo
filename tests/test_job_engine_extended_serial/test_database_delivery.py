"""JobEngine database leases and delivery retained across acquisition failures."""
import time
import uuid
from datetime import timedelta
from unittest import mock

from testit import helpers as th

FUNC = 'tests.test_job_engine_extended_serial.test_database_delivery.database_job'


def database_job(job):
    from mojo.apps.jobs.models import Job

    Job.objects.filter(pk=job.pk).exists()
    if job.payload.get('raise'):
        raise ValueError('expected handler failure')
    job.metadata['executed'] = True


@th.django_unit_setup()
def setup_database_delivery(opts):
    from mojo.apps.jobs.models import Job

    Job.objects.filter(func=FUNC).delete()


class OnePass:
    def __init__(self):
        self.stopped = False

    def is_set(self):
        return self.stopped

    def wait(self, seconds):
        self.stopped = True


class EngineFixture:
    def __enter__(self):
        from mojo.apps.jobs.job_engine import JobEngine

        self.channel = 'db-delivery-%s' % uuid.uuid4().hex[:10]
        self.engine = JobEngine(channels=[self.channel], max_workers=1)
        self.engine.running = True
        return self

    def __exit__(self, *args):
        from mojo.apps.jobs.models import Job

        self.engine.running = False
        self.engine.executor.shutdown(wait=True)
        self.engine.redis.delete(
            self.engine.keys.queue(self.channel),
            self.engine.keys.processing(self.channel),
            self.engine.keys.reaper_lock(self.channel))
        Job.objects.filter(channel=self.channel).delete()

    def job(self, **changes):
        from django.utils import timezone
        from mojo.apps.jobs.models import Job

        values = dict(id=uuid.uuid4().hex, channel=self.channel, func=FUNC,
                      status='pending', max_retries=2,
                      expires_at=timezone.now() + timedelta(minutes=5))
        values.update(changes)
        job = Job.objects.create(**values)
        self.engine.redis.zadd(self.engine.keys.processing(self.channel),
                               {job.pk: int(time.time() * 1000) - 3600000})
        return job

    def receipt(self, job):
        return self.engine.redis.zscore(
            self.engine.keys.processing(self.channel), job.pk)

    def reap(self):
        self.engine.redis.delete(self.engine.keys.reaper_lock(self.channel))
        self.engine.stop_event = OnePass()
        self.engine._reaper_loop()


def connection_open():
    from django.db import connection

    return connection.connection is not None


@th.django_unit_test()
def test_executor_returns_database_connection_after_success_and_handler_error(opts):
    with EngineFixture() as fixture:
        for raises in (False, True):
            job = fixture.job(payload={'raise': raises}, max_retries=0)
            fixture.engine.executor.submit(
                fixture.engine.execute_job, fixture.channel, job.pk).result(timeout=5)
            opened = fixture.engine.executor.submit(connection_open).result(timeout=5)
            job.refresh_from_db()
            assert job.status == ('failed' if raises else 'completed'), \
                'connection cleanup must preserve the actual handler outcome'
            assert job.attempt == 1, 'one handler execution must consume exactly one attempt'
            assert not opened, 'an idle executor thread must return its database connection on success and handler error'


@th.django_unit_test()
def test_initial_database_failure_keeps_delivery_for_the_existing_reaper(opts):
    from django.db import InterfaceError, OperationalError
    from mojo.apps.jobs.models import Job

    with EngineFixture() as fixture:
        for error in (OperationalError('pool unavailable'), InterfaceError('connection unavailable')):
            job = fixture.job()
            with mock.patch.object(Job.objects, 'get', side_effect=error):
                fixture.engine.execute_job(fixture.channel, job.pk)
            job.refresh_from_db()
            assert job.status == 'pending' and job.attempt == 0 and job.started_at is None, \
                'failure before durable claim must not start or consume a job attempt'
            assert fixture.receipt(job) is not None, \
                'temporary database acquisition failure must leave the existing processing receipt recoverable'
            fixture.reap()
            queued = fixture.engine.redis.brpop([fixture.engine.keys.queue(fixture.channel)], timeout=1)
            assert queued and queued[1] == job.pk, 'the existing reaper must recover the same durable job'
            fixture.engine.execute_job(fixture.channel, job.pk)
            fixture.engine.execute_job(fixture.channel, job.pk)
            job.refresh_from_db()
            assert job.status == 'completed' and job.attempt == 1, 'recovered delivery must execute once without a replacement job'


@th.django_unit_test()
def test_reaper_database_failure_retains_receipt_until_a_successful_pass(opts):
    from django.db import InterfaceError, OperationalError
    from mojo.apps.jobs.models import Job

    with EngineFixture() as fixture:
        for error in (OperationalError('pool unavailable'), InterfaceError('connection unavailable')):
            job = fixture.job()
            with mock.patch.object(Job.objects, 'get', side_effect=error):
                fixture.reap()
            assert fixture.receipt(job) is not None, 'a reaper database outage must preserve its original recovery evidence'
            fixture.reap()
            assert fixture.receipt(job) is None, 'a later successful reaper pass must release the recovered receipt'
            queued = fixture.engine.redis.brpop([fixture.engine.keys.queue(fixture.channel)], timeout=1)
            assert queued and queued[1] == job.pk, 'reaper recovery must reuse the same queue and durable job'


@th.django_unit_test()
def test_reaper_releases_database_connection_before_it_waits(opts):
    with EngineFixture() as fixture:
        job = fixture.job()
        fixture.engine.executor.submit(fixture.reap).result(timeout=5)
        opened = fixture.engine.executor.submit(connection_open).result(timeout=5)
        assert fixture.receipt(job) is None, 'normal reaping must still finish delivery recovery'
        assert not opened, 'the reaper must return its database connection between work units'


@th.django_unit_test()
def test_reaper_failed_expiry_write_keeps_receipt_until_terminal_commit(opts):
    from django.db import OperationalError
    from django.utils import timezone
    from mojo.apps.jobs.models import Job

    with EngineFixture() as fixture:
        for values, expected in (({'expires_at': timezone.now() - timedelta(seconds=1)}, 'expired'),
                                 ({'status': 'running', 'attempt': 1, 'max_retries': 1}, 'failed')):
            job = fixture.job(**values)
            original = job.status
            with mock.patch.object(Job, 'save', side_effect=OperationalError('write unavailable')):
                fixture.reap()
            job.refresh_from_db()
            assert job.status == original and fixture.receipt(job) is not None, \
                'a failed terminal commit must retain its recovery receipt and original durable state'
            fixture.reap()
            job.refresh_from_db()
            assert job.status == expected and fixture.receipt(job) is None, \
                'a successful terminal commit must preserve expiry/retry policy and then clean its receipt'


@th.django_unit_test()
def test_missing_jobs_and_permanent_errors_do_not_enter_transport_recovery(opts):
    from mojo.apps.jobs.models import Job

    with EngineFixture() as fixture:
        job = fixture.job()
        with mock.patch.object(Job.objects, 'get', side_effect=ValueError('invalid row data')):
            fixture.engine.execute_job(fixture.channel, job.pk)
        assert fixture.receipt(job) is None, 'a permanent non-transport load error must retain existing cleanup behavior'
        fixture.engine.redis.zadd(fixture.engine.keys.processing(fixture.channel), {job.pk: 0})
        with mock.patch.object(Job.objects, 'get', side_effect=ValueError('invalid row data')):
            fixture.reap()
        assert fixture.receipt(job) is None, 'a permanent non-transport reaper error must not loop indefinitely'
        missing = fixture.job()
        missing_id = missing.pk
        missing.delete()
        fixture.engine.execute_job(fixture.channel, missing_id)
        assert fixture.engine.redis.zscore(fixture.engine.keys.processing(fixture.channel), missing_id) is None, \
            'a genuinely deleted job must clean its processing receipt'


@th.django_unit_test()
def test_reaper_retry_event_failure_keeps_recoverable_delivery(opts):
    from django.db import OperationalError
    from mojo.apps.jobs.models import JobEvent

    with EngineFixture() as fixture:
        job = fixture.job()
        with mock.patch.object(JobEvent.objects, 'create', side_effect=OperationalError('event write unavailable')):
            fixture.reap()
        assert fixture.receipt(job) is not None, 'an interrupted reaper unit must retain its recovery receipt'
        queued = fixture.engine.redis.brpop([fixture.engine.keys.queue(fixture.channel)], timeout=1)
        assert queued and queued[1] == job.pk, 'a successful queue write must remain usable despite an audit write failure'
        fixture.engine.execute_job(fixture.channel, job.pk)
        fixture.reap()
        fixture.engine.execute_job(fixture.channel, job.pk)
        job.refresh_from_db()
        assert job.status == 'completed' and job.attempt == 1, 'recovery and duplicate deliveries must preserve the durable execution fence'
        assert fixture.receipt(job) is None, 'the terminal durable job must clear its surviving receipt on a successful pass'
