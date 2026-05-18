"""Tests for jobs.publish_webhook(group=...) auto-signing and the matching
post_webhook handler-time signing path.
"""
from unittest import mock
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


# ---------------------------------------------------------------------------
# post_webhook — handler-time signing semantics
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_handler_injects_signature_header(opts):
    """When sign_group_id is set, handler injects X-Mojo-Signature and sends
    the same bytes it hashed (data=body_bytes, not json=data).
    """
    import json
    from mojo.apps.account.models import Group
    from mojo.apps.jobs.handlers import webhook as webhook_handler
    from mojo.helpers.crypto.sign import generate_signature, WEBHOOK_SIGNATURE_HEADER

    g = Group.objects.get(pk=opts.group_id)
    # Force a known secret so we can compute the expected signature.
    g.get_webhook_secret(auto_create=True)
    g.refresh_from_db()
    secret = g.get_webhook_secret()
    assert secret, "precondition: group must have a secret"

    job = _build_job(payload={
        "url": "https://example.test/hook",
        "data": {"event": "verified", "id": 7},
        "headers": {"Content-Type": "application/json"},
        "timeout": 30,
        "webhook_id": "test_signed",
        "sign_group_id": g.pk,
    })

    expected_body = json.dumps(job.payload["data"], sort_keys=True, separators=(",", ":")).encode()
    expected_sig = generate_signature(expected_body, secret)

    with mock.patch.object(webhook_handler, "requests") as mock_requests:
        mock_resp = mock.Mock()
        mock_resp.status_code = 200
        mock_resp.content = b""
        mock_resp.headers = {}
        mock_resp.raise_for_status = mock.Mock()
        mock_requests.post.return_value = mock_resp
        # requests.exceptions.* are accessed in the handler — preserve the real
        # exception classes so the except blocks still resolve correctly.
        import requests as real_requests
        mock_requests.exceptions = real_requests.exceptions

        result = webhook_handler.post_webhook(job)
        assert result == "success", f"handler must succeed, got {result!r}; metadata={job.metadata}"

        assert mock_requests.post.called, "requests.post must be invoked"
        _, kwargs = mock_requests.post.call_args
        assert kwargs.get("data") == expected_body, (
            f"signed path must call requests.post(data=body_bytes), got data={kwargs.get('data')!r}"
        )
        assert "json" not in kwargs, (
            "signed path must NOT use the json= kwarg — that would re-serialize and break HMAC"
        )
        headers_sent = kwargs.get("headers", {})
        sig_header = headers_sent.get(WEBHOOK_SIGNATURE_HEADER)
        assert sig_header == expected_sig, (
            f"X-Mojo-Signature mismatch: expected {expected_sig}, got {sig_header}"
        )
        assert headers_sent.get("Content-Type") == "application/json", (
            "Content-Type must remain application/json on signed sends"
        )


@th.django_unit_test()
def test_handler_signature_changes_after_rotation(opts):
    """Rotating the group secret produces a different signature on next send."""
    import json
    from mojo.apps.account.models import Group
    from mojo.apps.jobs.handlers import webhook as webhook_handler
    from mojo.helpers.crypto.sign import generate_signature

    g = Group.objects.get(pk=opts.group_id)
    g.get_webhook_secret(auto_create=True)

    data = {"event": "x"}
    expected_body = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()

    job1 = _build_job(payload={
        "url": "https://example.test/hook",
        "data": data,
        "headers": {"Content-Type": "application/json"},
        "timeout": 30,
        "sign_group_id": g.pk,
    })
    sig1 = _run_handler_capture_sig(job1, webhook_handler)

    g.rotate_webhook_secret()

    job2 = _build_job(payload={
        "url": "https://example.test/hook",
        "data": data,
        "headers": {"Content-Type": "application/json"},
        "timeout": 30,
        "sign_group_id": g.pk,
    })
    sig2 = _run_handler_capture_sig(job2, webhook_handler)

    assert sig1 != sig2, (
        f"signature must change after rotate_webhook_secret(), got identical {sig1!r}"
    )
    # And the second signature must match the current secret.
    g.refresh_from_db()
    expected_sig2 = generate_signature(expected_body, g.get_webhook_secret())
    assert sig2 == expected_sig2, (
        f"post-rotation signature must match current secret, got {sig2!r} vs {expected_sig2!r}"
    )


@th.django_unit_test()
def test_handler_missing_group_fails_without_retry(opts):
    """sign_group_id pointing at a non-existent Group → 'failed', no retry."""
    from mojo.apps.jobs.handlers import webhook as webhook_handler

    job = _build_job(payload={
        "url": "https://example.test/hook",
        "data": {"event": "x"},
        "headers": {"Content-Type": "application/json"},
        "timeout": 30,
        "sign_group_id": 99999999,  # very unlikely to exist
    })

    with mock.patch.object(webhook_handler, "requests") as mock_requests:
        import requests as real_requests
        mock_requests.exceptions = real_requests.exceptions
        result = webhook_handler.post_webhook(job)

        assert result == "failed", (
            f"missing sign group must return 'failed' (no retry), got {result!r}"
        )
        assert job.metadata.get("error_type") == "sign_group_missing", (
            f"error_type must be 'sign_group_missing', got {job.metadata.get('error_type')!r}"
        )
        assert not mock_requests.post.called, (
            "requests.post must NOT be called when the sign group is missing — "
            "the webhook would otherwise leak unsigned"
        )


@th.django_unit_test()
def test_handler_unsigned_path_still_uses_json_kwarg(opts):
    """Regression: payloads without sign_group_id keep the existing json=data path."""
    from mojo.apps.jobs.handlers import webhook as webhook_handler

    job = _build_job(payload={
        "url": "https://example.test/hook",
        "data": {"event": "noop"},
        "headers": {"Content-Type": "application/json"},
        "timeout": 30,
    })

    with mock.patch.object(webhook_handler, "requests") as mock_requests:
        mock_resp = mock.Mock()
        mock_resp.status_code = 200
        mock_resp.content = b""
        mock_resp.headers = {}
        mock_resp.raise_for_status = mock.Mock()
        mock_requests.post.return_value = mock_resp
        import requests as real_requests
        mock_requests.exceptions = real_requests.exceptions

        result = webhook_handler.post_webhook(job)
        assert result == "success", f"unsigned handler must succeed, got {result!r}"

        _, kwargs = mock_requests.post.call_args
        assert "json" in kwargs and kwargs["json"] == {"event": "noop"}, (
            f"unsigned path must use json=data kwarg, got kwargs={list(kwargs.keys())}"
        )
        assert "data" not in kwargs, (
            "unsigned path must NOT pass data=... (regression on existing callers)"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _StubJob:
    """Light stand-in for Job — handler only needs payload, metadata, id, attempt, cancel_requested."""
    def __init__(self, payload):
        self.payload = payload
        self.metadata = {}
        self.id = "stub-job-id"
        self.attempt = 1
        self.cancel_requested = False


def _build_job(payload):
    return _StubJob(payload=payload)


def _run_handler_capture_sig(job, webhook_handler):
    """Invoke post_webhook with requests.post patched; return the X-Mojo-Signature header
    value the handler attempted to send.
    """
    from mojo.helpers.crypto.sign import WEBHOOK_SIGNATURE_HEADER
    with mock.patch.object(webhook_handler, "requests") as mock_requests:
        mock_resp = mock.Mock()
        mock_resp.status_code = 200
        mock_resp.content = b""
        mock_resp.headers = {}
        mock_resp.raise_for_status = mock.Mock()
        mock_requests.post.return_value = mock_resp
        import requests as real_requests
        mock_requests.exceptions = real_requests.exceptions

        result = webhook_handler.post_webhook(job)
        assert result == "success", f"handler must succeed, got {result!r}; metadata={job.metadata}"
        _, kwargs = mock_requests.post.call_args
        return kwargs.get("headers", {}).get(WEBHOOK_SIGNATURE_HEADER)
