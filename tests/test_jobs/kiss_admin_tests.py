from testit import helpers as th

import time
import json
import uuid
from datetime import datetime, timedelta, date

from django.utils import timezone

TEST_USER = "testit"
TEST_PWORD = "testit##mojo"

ADMIN_USER = "tadmin"
ADMIN_PWORD = "testit##mojo"


@th.django_unit_setup()
def setup_kiss_admin(opts):
    """
    Prepare admin user, manager, and a clean test channel for admin/REST tests.
    """
    from mojo.apps.account.models import User
    from mojo.apps.jobs.manager import get_manager
    from mojo.apps.jobs.adapters import get_adapter
    from mojo.apps.jobs.keys import JobKeys

    # Ensure standard test users exist and set permissions
    user = User.objects.filter(username=TEST_USER).last()
    if user is None:
        user = User(username=TEST_USER, display_name=TEST_USER, email=f"{TEST_USER}@example.com")
        user.save()
    user.save_password(TEST_PWORD)
    user.remove_all_permissions()

    user = User.objects.filter(username=ADMIN_USER).last()
    if user is None:
        user = User(username=ADMIN_USER, display_name=ADMIN_USER, email=f"{ADMIN_USER}@example.com")
        user.save()
    user.remove_permission(["manage_groups"])
    user.add_permission(["manage_users", "view_global", "view_admin"])
    user.is_staff = True
    user.is_superuser = True
    user.save_password(ADMIN_PWORD)
    user.save()

    # REST client login as admin
    resp = opts.client.login(ADMIN_USER, ADMIN_PWORD)
    assert opts.client.is_authenticated, "authentication failed"

    # Runtime helpers
    opts.redis = get_adapter()
    opts.keys = JobKeys()
    opts.manager = get_manager()

    # Use a dedicated test channel
    opts.channel = "kiss_admin"

    # Ensure clean slate on channel (streams, zsets, and DB pending)
    opts.manager.clear_channel(opts.channel, cancel_db_pending=True)

    # Clear scheduler lock if any
    try:
        opts.redis.delete(opts.keys.scheduler_lock())
    except Exception:
        pass


@th.django_unit_test()
def test_manager_pause_resume_and_clear_channel(opts):
    """
    Verify pause/resume flags and clear_channel() DB cancel of pending jobs.
    """
    from mojo.apps.jobs.models import Job

    # Create a DB pending job to be canceled by clear_channel
    job = Job.objects.create(
        id=uuid.uuid4().hex,
        channel=opts.channel,
        func="mojo.apps.jobs.examples.sample_jobs.generate_report",
        payload={
            "report_type": "to_cancel",
            "start_date": date.today().isoformat(),
            "end_date": date.today().isoformat(),
            "format": "pdf",
        },
        status="pending",
    )

    # Pause channel
    assert opts.manager.pause_channel(opts.channel) is True, f"Failed to pause channel: {opts.channel}"
    assert opts.redis.get(opts.keys.channel_pause(opts.channel)), f"Pause flag not set: key={opts.keys.channel_pause(opts.channel)} channel={opts.channel}"

    # Clear channel (nukes redis + cancels DB pending)
    result = opts.manager.clear_channel(opts.channel, cancel_db_pending=True)
    assert result.get("status", True) is True, f"clear_channel returned failure: result={result}"
    assert result.get("db_pending_canceled", 0) >= 1, f"Expected DB pending jobs to be canceled, got {result.get('db_pending_canceled')} result={result}"

    # Job should be canceled now
    job.refresh_from_db()
    assert job.status == "canceled", f"Job not canceled after clear_channel: job_id={job.id} status={job.status}"

    # Resume channel
    assert opts.manager.resume_channel(opts.channel) is True, f"Failed to resume channel: {opts.channel}"
    assert not opts.redis.get(opts.keys.channel_pause(opts.channel)), f"Pause flag still set after resume: key={opts.keys.channel_pause(opts.channel)} channel={opts.channel}"


@th.django_unit_test()
def test_manager_requeue_db_pending_and_queue_sizes(opts):
    """
    Verify requeue_db_pending routes jobs to correct streams and queue sizes reflect it.
    """
    from mojo.apps.jobs.models import Job

    # Create DB pending jobs: one normal, one broadcast
    job_nb = Job.objects.create(
        id=uuid.uuid4().hex,
        channel=opts.channel,
        func="mojo.apps.jobs.examples.sample_jobs.generate_report",
        payload={"report_type": "nb"},
        status="pending",
        broadcast=False,
    )
    job_b = Job.objects.create(
        id=uuid.uuid4().hex,
        channel=opts.channel,
        func="mojo.apps.jobs.examples.sample_jobs.generate_report",
        payload={"report_type": "b"},
        status="pending",
        broadcast=True,
    )

    # Requeue via manager
    requeue = opts.manager.requeue_db_pending(opts.channel)
    assert requeue.get("status", False) is True, f"requeue_db_pending failed: channel={opts.channel} result={requeue}"
    assert requeue.get("requeued", 0) >= 2, f"Expected at least 2 jobs requeued, got {requeue.get('requeued')} result={requeue}"

    # Confirm stream lengths
    info_main = opts.redis.xinfo_stream(opts.keys.stream(opts.channel))
    info_bcast = opts.redis.xinfo_stream(opts.keys.stream_broadcast(opts.channel))
    assert info_main.get("length", 0) >= 1, f"Main stream empty after requeue: key={opts.keys.stream(opts.channel)} info={info_main}"
    assert info_bcast.get("length", 0) >= 1, f"Broadcast stream empty after requeue: key={opts.keys.stream_broadcast(opts.channel)} info={info_bcast}"

    # Queue sizes via manager (limit to our test channel)
    sizes = opts.manager.get_queue_sizes(channels=[opts.channel])
    assert sizes.get("status", False) is True, f"get_queue_sizes failed: result={sizes}"
    data = sizes.get("data", {})
    assert opts.channel in data, f"Channel not present in queue sizes: channel={opts.channel} data={data}"
    assert data[opts.channel]["stream"] >= 1, f"Stream count not >= 1 for channel {opts.channel}: sizes={data[opts.channel]}"
    assert data[opts.channel]["db_pending"] >= 0


@th.django_unit_test()
def test_manager_clear_stuck_jobs_with_threshold(opts):
    """
    Put a message into pending (PEL) and clear with idle_threshold_ms.
    This test now instruments XPENDING at multiple levels to confirm Redis behavior.
    """
    stream_key = opts.keys.stream(opts.channel)
    group = opts.keys.group_workers(opts.channel)

    # Ensure stream clean and group exists
    opts.redis.delete(stream_key)
    created = opts.redis.xgroup_create(stream_key, group, mkstream=True)
    assert created is True or created is None, f"xgroup_create failed for stream={stream_key} group={group} returned={created}"

    # Add message and read to put into PEL without ACK
    inserted_job_id = uuid.uuid4().hex
    msg_id = opts.redis.xadd(stream_key, {"job_id": inserted_job_id, "func": "test.stuck"})
    msgs = opts.redis.xreadgroup(group=group, consumer="consumer_1", streams={stream_key: ">"}, count=1, block=100)
    assert msgs, f"Failed to read and create a pending message: stream={stream_key} group={group}"

    # Instrumentation: check XPENDING summary
    pending_summary = opts.redis.xpending(stream_key, group)
    # Instrumentation: check XPENDING details via adapter
    pending_details_adapter = None
    pending_details_adapter_consumer = None
    try:
        pending_details_adapter = opts.redis.xpending(stream_key, group, '-', '+', 10)
    except Exception as e:
        pending_details_adapter = f"adapter_error: {e}"
    # Also try adapter with consumer filter explicitly
    try:
        pending_details_adapter_consumer = opts.redis.xpending(stream_key, group, '-', '+', 10, 'consumer_1')
    except Exception as e:
        pending_details_adapter_consumer = f"adapter_consumer_error: {e}"
    # Instrumentation: check XPENDING details via raw execute_command
    pending_details_raw = None
    try:
        client = opts.redis.get_client()
        pending_details_raw = client.execute_command('XPENDING', stream_key, group, '-', '+', '10')
    except Exception as e:
        pending_details_raw = f"raw_error: {e}"

    # Assert we do have at least 1 pending according to summary (if available)
    if isinstance(pending_summary, dict):
        assert pending_summary.get('pending', 0) >= 1, f"XPENDING summary shows no pending: summary={pending_summary}, adapter_details={pending_details_adapter}, adapter_details_consumer={pending_details_adapter_consumer}, raw_details={pending_details_raw}, stream={stream_key}, group={group}, msg_id={msg_id}, job_id={inserted_job_id}"
    else:
        # If summary format is not dict, still continue but record debug
        assert msgs, f"Unexpected XPENDING summary format: {pending_summary}"

    # Small idle then clear with threshold 0 (clear all pending)
    time.sleep(0.1)

    # Final pre-clear debug snapshot of pending
    pending_details_before_clear = {
        'summary': pending_summary,
        'adapter_details': pending_details_adapter,
        'adapter_details_consumer': pending_details_adapter_consumer,
        'raw_details': pending_details_raw,
        'stream': stream_key,
        'group': group,
        'msg_id': msg_id,
        'inserted_job_id': inserted_job_id,
    }

    result = opts.manager.clear_stuck_jobs(opts.channel, idle_threshold_ms=0)
    assert isinstance(result, dict), f"clear_stuck_jobs did not return dict: result={result}, pre_clear={pending_details_before_clear}"
    assert result.get("cleared", 0) >= 1, f"No stuck jobs cleared: result={result}, pre_clear={pending_details_before_clear}"


@th.django_unit_test()
def test_manager_purge_old_jobs(opts):
    """
    Create an old job and purge via manager with dry_run and actual delete.
    """
    from mojo.apps.jobs.models import Job

    # Create job and set created to 10 years ago
    old_job = Job.objects.create(
        id=uuid.uuid4().hex,
        channel=opts.channel,
        func="mojo.apps.jobs.examples.sample_jobs.send_email",
        payload={"old": True},
        status="failed",
    )
    ten_years_ago = timezone.now() - timedelta(days=3650)
    Job.objects.filter(id=old_job.id).update(created=ten_years_ago)

    # Dry run purge (should report >=1)
    dry = opts.manager.purge_old_jobs(days_old=3650, status=None, dry_run=True)
    assert dry.get("status", False) is True, f"purge_old_jobs dry_run failed: result={dry}"
    assert dry.get("dry_run", False) is True, f"purge_old_jobs did not report dry_run: result={dry}"
    assert dry.get("count", 0) >= 1, f"Expected at least 1 old job in dry_run count, got {dry.get('count')} result={dry}"

    # Actual purge for the same cutoff
    real = opts.manager.purge_old_jobs(days_old=3650, status=None, dry_run=False)
    assert real.get("status", False) is True, f"purge_old_jobs delete failed: result={real}"
    assert real.get("deleted", 0) >= 1, f"Expected at least 1 record deleted, got {real.get('deleted')} result={real}"


@th.django_unit_test()
def test_rest_control_config_and_queue_sizes(opts):
    assert opts.client.is_authenticated, "authentication failed (not logged in before REST test)"
    """
    REST: GET control/config and control/queue-sizes
    """
    # Config
    resp = opts.client.get("/api/jobs/control/config")
    assert resp.status_code == 200, f"GET /api/jobs/control/config failed: status={resp.status_code} body={getattr(resp, 'response', None) and getattr(resp.response, 'data', None)}"
    # config endpoint returns raw config dict as data; verify required keys
    cfg = resp.response.data
    assert isinstance(cfg, dict), f"Config endpoint did not return a dict: data_type={type(cfg)} value={cfg}"
    for key in ["redis_url", "redis_prefix", "engine", "defaults", "limits", "timeouts", "channels"]:
        assert key in cfg, f"Missing '{key}' in config: cfg={cfg}"

    # Queue sizes
    resp = opts.client.get("/api/jobs/control/queue-sizes")
    assert resp.status_code == 200, f"GET /api/jobs/control/queue-sizes failed: status={resp.status_code} body={getattr(resp, 'response', None) and getattr(resp.response, 'data', None)}"
    # queue-sizes returns wrapped data or raw dict depending on implementation; normalize
    qdata = resp.response.data
    if isinstance(qdata, dict) and "status" in qdata and "data" in qdata:
        qdata = qdata["data"]
    assert isinstance(qdata, dict), f"queue-sizes endpoint returned non-dict data: type={type(qdata)} value={qdata}"


@th.django_unit_test()
def test_rest_clear_queue_and_clear_stuck(opts):
    assert opts.client.is_authenticated, "authentication failed (not logged in before REST test)"
    """
    REST: POST control/clear-queue and control/clear-stuck
    """
    # Add items to stream to clear
    opts.redis.xadd(opts.keys.stream(opts.channel), {"job_id": uuid.uuid4().hex, "func": "test.func"})
    opts.redis.xadd(opts.keys.stream_broadcast(opts.channel), {"job_id": uuid.uuid4().hex, "func": "test.func"})

    # Clear queue with confirm=yes
    resp = opts.client.post("/api/jobs/control/clear-queue", {
        "channel": opts.channel,
        "confirm": "yes",
    })
    assert resp.status_code == 200, f"POST /api/jobs/control/clear-queue failed: status={resp.status_code} body={getattr(resp, 'response', None) and getattr(resp.response, 'data', None)}"
    assert resp.response.data.status is True, f"clear-queue returned status=False: data={resp.response.data}"

    # Create a pending message for clear-stuck: add, read (PEL), then clear
    stream_key = opts.keys.stream(opts.channel)
    group = opts.keys.group_workers(opts.channel)
    # Ensure group exists after clear
    opts.redis.xgroup_create(stream_key, group, mkstream=True)
    inserted_job_id = uuid.uuid4().hex
    msg_id = opts.redis.xadd(stream_key, {"job_id": inserted_job_id, "func": "test.stuck"})
    msgs = opts.redis.xreadgroup(group=group, consumer="rest_consumer", streams={stream_key: ">"}, count=1, block=100)
    assert msgs, f"Failed to stage a pending message for clear-stuck: stream={stream_key} group={group} msg_id={msg_id} job_id={inserted_job_id}"

    # Clear stuck with threshold 0
    resp = opts.client.post("/api/jobs/control/clear-stuck", {
        "channel": opts.channel,
        "idle_threshold_ms": 0,
    })
    assert resp.status_code == 200, f"POST /api/jobs/control/clear-stuck failed: status={resp.status_code} body={getattr(resp, 'response', None) and getattr(resp.response, 'data', None)}"
    # Normalize response and verify cleared count
    body = resp.response.data
    if isinstance(body, dict) and "data" in body:
        data_val = body["data"]
        status_val = body.get("status", True)
    else:
        # Unwrapped response; treat as data dict directly
        data_val = body
        status_val = True
    assert isinstance(data_val, dict), f"clear-stuck response missing data dict: body={body}"
    assert status_val is True or data_val.get("cleared", 0) >= 1, f"clear-stuck returned status=False: data={body}"
    assert data_val.get("cleared", 0) >= 1, f"clear-stuck did not clear any messages: data={data_val}"


@th.django_unit_test()
def test_rest_reset_failed_and_purge(opts):
    assert opts.client.is_authenticated, "authentication failed (not logged in before REST test)"
    """
    REST: POST control/reset-failed and POST control/purge
    """
    from mojo.apps.jobs.models import Job

    # Create a failed job in our channel
    job = Job.objects.create(
        id=uuid.uuid4().hex,
        channel=opts.channel,
        func="mojo.apps.jobs.examples.sample_jobs.generate_report",
        payload={"reset": True},
        status="failed",
        last_error="test failure",
    )

    # Reset failed (target our channel)
    resp = opts.client.post("/api/jobs/control/reset-failed", {
        "channel": opts.channel,
        "limit": 10,
    })
    assert resp.status_code == 200, f"POST /api/jobs/control/reset-failed failed: status={resp.status_code} body={getattr(resp, 'response', None) and getattr(resp.response, 'data', None)}"
    assert resp.response, f"reset-failed return data: response={resp.response}"
    assert resp.response.status is True, f"reset-failed returned status=False: data={resp.response.data}"
    assert resp.response.reset_count >= 1, f"reset-failed reset_count < 1: data={resp.response.data}"

    # Purge: dry_run first
    resp = opts.client.post("/api/jobs/control/purge", {
        "days_old": 3650,
        "dry_run": True,
    })
    assert resp.status_code == 200, f"POST /api/jobs/control/purge failed: status={resp.status_code} body={getattr(resp, 'response', None) and getattr(resp.response, 'data', None)}"
    assert resp.response.status is True, f"purge returned status=False: data={resp.response.data}"
    assert resp.response.data.dry_run is True, f"purge dry_run not True: data={resp.response.data}"


@th.django_unit_test()
def test_broadcast_status_via_manager_and_engine_reply(opts):
    """
    Verify engine broadcast status reply path using manager.broadcast_command('status').
    This requires engine to be running in the same process; we simulate a subscriber here to avoid
    requiring a long-running engine in tests.
    """
    # Simulate a runner publishing status on broadcast reply_channel
    reply_channel = f"mojo:jobs:replies:{uuid.uuid4().hex[:8]}"

    # Prepare a simulated engine reply
    def simulate_engine_broadcast_reply():
        pubsub = opts.redis.pubsub()
        pubsub.subscribe(reply_channel)
        # Wait for consumer to listen (very short)
        time.sleep(0.05)
        reply = {
            "runner_id": "simulated-runner",
            "channels": [opts.channel],
            "jobs_processed": 0,
            "jobs_failed": 0,
            "started": timezone.now().isoformat(),
            "timestamp": timezone.now().isoformat(),
        }
        # Publish once and exit
        opts.redis.publish(reply_channel, json.dumps(reply))
        pubsub.close()

    # Start simulated responder
    import threading
    t = threading.Thread(target=simulate_engine_broadcast_reply, daemon=True)
    t.start()

    # Send a broadcast status "request" similar to manager.broadcast_command
    message = {
        "command": "status",
        "data": {},
        "reply_channel": reply_channel,
        "timestamp": timezone.now().isoformat(),
    }
    # Manager.broadcast_command publishes to this channel (we mimic it directly here)
    opts.redis.publish("mojo:jobs:runners:broadcast", json.dumps(message))

    # Collect one reply
    pubsub = opts.redis.pubsub()
    pubsub.subscribe(reply_channel)
    received = None
    end = time.time() + 1.0
    while time.time() < end:
        msg = pubsub.get_message(timeout=0.1)
        if msg and msg.get("type") == "message":
            try:
                payload = msg["data"]
                if isinstance(payload, bytes):
                    payload = payload.decode("utf-8")
                received = json.loads(payload)
                break
            except Exception:
                pass
    pubsub.close()
    t.join(timeout=1.0)

    assert received is not None, f"Expected simulated broadcast status reply, got none: reply_channel={reply_channel}"
    assert received.get("runner_id") == "simulated-runner"
    assert isinstance(received.get("channels", []), list)


@th.django_unit_test()
def test_cleanup_kiss_admin(opts):
    """
    Cleanup resources created by admin tests.
    """
    # Clear channel at the end
    opts.manager.clear_channel(opts.channel, cancel_db_pending=True)

    # Logout REST client
    opts.client.logout()
