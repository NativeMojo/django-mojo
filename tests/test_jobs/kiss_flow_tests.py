from testit import helpers as th

from datetime import datetime, timedelta, date
from typing import List

@th.django_unit_setup()
def setup_environment(opts):
    """
    Prepare a clean channel, redis adapter, and keys for KISS tests.
    """
    from django.utils import timezone
    from mojo.apps.jobs.manager import get_manager
    from mojo.apps.jobs.adapters import get_adapter
    from mojo.apps.jobs.keys import JobKeys

    opts.channel = "kiss_tests"
    opts.now = timezone.now()
    opts.redis = get_adapter()
    opts.keys = JobKeys()

    # Ensure clean slate on our test channel
    get_manager().clear_channel(opts.channel, cancel_db_pending=True)


@th.django_unit_test()
def test_publish_routing_immediate_and_delayed(opts):
    """
    Verify publish() routes to streams for immediate jobs and two ZSETs for delayed jobs.
    """
    from django.utils import timezone
    from mojo.apps.jobs import publish
    from mojo.apps.jobs.keys import JobKeys

    keys: JobKeys = opts.keys
    redis = opts.redis
    channel = opts.channel

    # Ensure empty
    redis.delete(keys.stream(channel))
    redis.delete(keys.stream_broadcast(channel))
    redis.delete(keys.sched(channel))
    redis.delete(keys.sched_broadcast(channel))

    # Immediate non-broadcast
    job1 = publish(
        "mojo.apps.jobs.examples.sample_jobs.generate_report",
        payload={
            "report_type": "immediate_nb",
            "start_date": date.today().isoformat(),
            "end_date": date.today().isoformat(),
            "format": "pdf",
        },
        channel=channel,
    )

    # Immediate broadcast
    job2 = publish(
        "mojo.apps.jobs.examples.sample_jobs.generate_report",
        payload={
            "report_type": "immediate_b",
            "start_date": date.today().isoformat(),
            "end_date": date.today().isoformat(),
            "format": "pdf",
        },
        channel=channel,
        broadcast=True,
    )

    # Delayed non-broadcast
    run_at_nb = timezone.now() + timedelta(minutes=5)
    job3 = publish(
        "mojo.apps.jobs.examples.sample_jobs.generate_report",
        payload={
            "report_type": "delayed_nb",
            "start_date": date.today().isoformat(),
            "end_date": date.today().isoformat(),
            "format": "pdf",
        },
        channel=channel,
        run_at=run_at_nb,
    )

    # Delayed broadcast
    run_at_b = timezone.now() + timedelta(minutes=7)
    job4 = publish(
        "mojo.apps.jobs.examples.sample_jobs.generate_report",
        payload={
            "report_type": "delayed_b",
            "start_date": date.today().isoformat(),
            "end_date": date.today().isoformat(),
            "format": "pdf",
        },
        channel=channel,
        run_at=run_at_b,
        broadcast=True,
    )

    # Streams should contain two immediate jobs
    info_main = redis.xinfo_stream(keys.stream(channel))
    info_bcast = redis.xinfo_stream(keys.stream_broadcast(channel))
    assert info_main.get("length", 0) >= 1, f"Immediate non-broadcast job not in main stream: key={keys.stream(channel)}, info={info_main}"
    assert info_bcast.get("length", 0) >= 1, f"Immediate broadcast job not in broadcast stream: key={keys.stream_broadcast(channel)}, info={info_bcast}"

    # ZSETs should contain the two delayed jobs, one in each zset
    score_nb = redis.zscore(keys.sched(channel), job3)
    score_b = redis.zscore(keys.sched_broadcast(channel), job4)
    assert score_nb is not None, f"Delayed non-broadcast job not in sched zset: zset={keys.sched(channel)}, score={score_nb}"
    assert score_b is not None, f"Delayed broadcast job not in sched_broadcast zset: zset={keys.sched_broadcast(channel)}, score={score_b}"


@th.django_unit_test()
def test_scheduler_pops_due_entries_from_two_zsets(opts):
    """
    Verify scheduler pops due entries from both ZSETs (non-broadcast and broadcast) and enqueues to streams.
    """
    from django.utils import timezone
    from mojo.apps.jobs import publish
    from mojo.apps.jobs.scheduler import Scheduler
    from mojo.apps.jobs.keys import JobKeys
    from mojo.apps.jobs.models import JobEvent

    keys: JobKeys = opts.keys
    redis = opts.redis
    channel = opts.channel

    # Clean channel keys
    redis.delete(keys.stream(channel))
    redis.delete(keys.stream_broadcast(channel))
    redis.delete(keys.sched(channel))
    redis.delete(keys.sched_broadcast(channel))

    # Create two due jobs (one NB, one B) in the past
    run_at_past = timezone.now() - timedelta(seconds=2)
    job_nb = publish(
        "mojo.apps.jobs.examples.sample_jobs.generate_report",
        payload={
            "report_type": "due_nb",
            "start_date": date.today().isoformat(),
            "end_date": date.today().isoformat(),
            "format": "pdf",
        },
        channel=channel,
        run_at=run_at_past,
    )
    job_b = publish(
        "mojo.apps.jobs.examples.sample_jobs.generate_report",
        payload={
            "report_type": "due_b",
            "start_date": date.today().isoformat(),
            "end_date": date.today().isoformat(),
            "format": "pdf",
        },
        channel=channel,
        run_at=run_at_past,
        broadcast=True,
    )

    # Sanity: score exists in both zsets
    assert redis.zscore(keys.sched(channel), job_nb) is not None
    assert redis.zscore(keys.sched_broadcast(channel), job_b) is not None

    # Run scheduler for this channel
    sch = Scheduler(channels=[channel])
    now = timezone.now()
    now_ms = now.timestamp() * 1000.0
    sch._process_channel(channel, now, now_ms)

    # Assert moved to correct streams
    main_info = redis.xinfo_stream(keys.stream(channel))
    bcast_info = redis.xinfo_stream(keys.stream_broadcast(channel))
    assert main_info.get("length", 0) >= 1, "Due NB job was not enqueued to main stream"
    assert bcast_info.get("length", 0) >= 1, "Due B job was not enqueued to broadcast stream"

    # ZSETs should be empty for those jobs (popped)
    assert redis.zscore(keys.sched(channel), job_nb) is None
    assert redis.zscore(keys.sched_broadcast(channel), job_b) is None

    # Event 'queued' recorded with scheduled_at
    events_nb = JobEvent.objects.filter(job_id=job_nb, event='queued')
    events_b = JobEvent.objects.filter(job_id=job_b, event='queued')
    assert events_nb.exists(), f"No queued event for NB job {job_nb} on channel {channel}"
    assert events_b.exists(), f"No queued event for B job {job_b} on channel {channel}"
    # scheduled_at detail exists
    assert 'scheduled_at' in (events_nb.first().details or {}), f"Missing scheduled_at in NB queued event: details={(events_nb.first().details if events_nb.exists() else None)}"
    assert 'scheduled_at' in (events_b.first().details or {}), f"Missing scheduled_at in B queued event: details={(events_b.first().details if events_b.exists() else None)}"


@th.django_unit_test()
def test_engine_executes_and_acks_with_events(opts):
    """
    Verify JobEngine claims, executes, ACKs after DB updates, and emits running/completed events.
    """
    from django.utils import timezone
    from mojo.apps.jobs.job_engine import JobEngine
    from mojo.apps.jobs.keys import JobKeys
    from mojo.apps.jobs.models import Job, JobEvent
    from mojo.apps.jobs import publish

    keys: JobKeys = opts.keys
    redis = opts.redis
    channel = opts.channel

    # Clean stream and groups for deterministic behavior
    redis.delete(keys.stream(channel))
    redis.delete(keys.stream_broadcast(channel))

    # Publish an immediate NB job
    job_id = publish(
        "mojo.apps.jobs.examples.sample_jobs.generate_report",
        payload={
            "report_type": "engine_exec",
            "start_date": date.today().isoformat(),
            "end_date": date.today().isoformat(),
            "format": "pdf",
        },
        channel=channel,
    )

    # Start engine (init only) and ensure groups exist
    engine = JobEngine(channels=[channel], max_workers=1)
    engine.initialize()

    # Claim one job
    claimed = engine.claim_jobs(1)
    assert claimed, f"Engine failed to claim job from stream: stream={keys.stream(channel)} group={keys.group_workers(channel)}"
    stream_key, msg_id, jid = claimed[0]
    assert jid == job_id, f"Claimed job_id mismatch: claimed={jid} expected={job_id}"

    # Execute job (this will update DB and then ACK)
    engine.execute_job(stream_key, msg_id, jid)

    # Validate DB state and events
    job = Job.objects.get(id=jid)
    assert job.status == 'completed', f"Job status not completed: {job.status}"
    assert job.finished_at is not None

    ev_running = JobEvent.objects.filter(job=job, event='running').exists()
    ev_completed = JobEvent.objects.filter(job=job, event='completed').exists()
    assert ev_running, "Missing running event"
    assert ev_completed, "Missing completed event"

    # Ensure no pending messages remain for workers group
    pending_info = redis.xpending(keys.stream(channel), keys.group_workers(channel))
    pending_count = pending_info.get('pending', 0) if pending_info else 0
    assert pending_count == 0, f"Message still pending after execution (ACK not applied): pending_info={pending_info}, stream={keys.stream(channel)}, group={keys.group_workers(channel)}"


@th.django_unit_test()
def test_pause_resume_and_clear_channel(opts):
    """
    Verify pause/resume flags and clear_channel behavior (including DB pending cancellation).
    """
    from django.utils import timezone
    from mojo.apps.jobs.manager import get_manager
    from mojo.apps.jobs.keys import JobKeys
    from mojo.apps.jobs.models import Job

    manager = get_manager()
    keys: JobKeys = opts.keys
    redis = opts.redis
    channel = opts.channel

    # Create a DB pending job to be canceled by clear_channel
    job = Job.objects.create(
        id="deadbeefdeadbeefdeadbeefdeadbeef",
        channel=channel,
        func="mojo.apps.jobs.examples.sample_jobs.generate_report",
        payload={"report_type": "to_cancel",
                 "start_date": date.today().isoformat(),
                 "end_date": date.today().isoformat(),
                 "format": "pdf"},
        status="pending",
    )

    # Pause the channel
    assert manager.pause_channel(channel) is True
    assert redis.get(keys.channel_pause(channel)), "Pause flag not set"

    # Clear the channel (should cancel DB pending)
    result = manager.clear_channel(channel, cancel_db_pending=True)
    assert result.get('status', True) is True
    assert result.get('db_pending_canceled', 0) >= 1

    # Verify job canceled in DB
    job.refresh_from_db()
    assert job.status == 'canceled', "Pending job was not canceled by clear_channel"

    # Streams and ZSETs should be empty
    main_len = (redis.xinfo_stream(keys.stream(channel)) or {}).get('length', 0)
    bcast_len = (redis.xinfo_stream(keys.stream_broadcast(channel)) or {}).get('length', 0)
    sched_cnt = redis.zcard(keys.sched(channel)) or 0
    sched_b_cnt = redis.zcard(keys.sched_broadcast(channel)) or 0
    assert main_len == 0
    assert bcast_len == 0
    assert sched_cnt == 0
    assert sched_b_cnt == 0

    # Resume the channel
    assert manager.resume_channel(channel) is True
    assert not redis.get(keys.channel_pause(channel)), "Pause flag not cleared after resume"
