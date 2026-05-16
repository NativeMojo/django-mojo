"""
Public message submit endpoint — covers kind validation, bouncer-token gating,
rate limiting, content_guard moderation, and notification fan-out.
"""
from unittest import mock

from testit import helpers as th
from testit.helpers import assert_true, assert_eq


SUBMIT_PATH = "/api/account/bouncer/message"
TEST_IP = "127.0.0.1"


def _mint_token(duid="", page_type="public_message", ip=TEST_IP):
    from mojo.apps.account.services.bouncer.token_manager import TokenManager
    return TokenManager.issue(
        duid=duid,
        fingerprint_id="",
        ip=ip,
        risk_score=5,
        page_type=page_type,
    )


@th.django_unit_setup()
def setup_public_messages(opts):
    from mojo.apps.account.models import PublicMessage
    from mojo.decorators.limits import clear_rate_limits

    # Rate limits are per-IP — clear across the key so no bleed from prior runs.
    clear_rate_limits(ip=TEST_IP, key="public_message_submit")

    # Wipe test records — tests run against long-lived DB.
    PublicMessage.objects.filter(
        email__in=[
            "submit-contact@example.com",
            "submit-support@example.com",
            "reject-kind@example.com",
            "reject-token@example.com",
            "reject-required@example.com",
            "reject-long@example.com",
            "reject-email@example.com",
            "reject-spam@example.com",
            "rate-limit@example.com",
            "notify-fail@example.com",
        ]
    ).delete()


@th.django_unit_test()
def test_submit_contact_us_happy_path(opts):
    from mojo.apps.account.models import PublicMessage

    # Notification fan-out is tested directly in 2_notify.py. With no
    # flagged users in the DB, notify_admins is a no-op here — we just
    # assert the row is saved correctly through the real endpoint.
    token = _mint_token()
    resp = opts.client.post(SUBMIT_PATH, {
        "kind": "contact_us",
        "name": "Jane Doe",
        "email": "submit-contact@example.com",
        "company": "Acme Inc.",
        "message": "I would like to learn more.",
        "bouncer_token": token,
    })

    assert_eq(resp.status_code, 200, f"expected 200, got {resp.status_code}: {resp.response}")
    assert_true(resp.response.status is True, f"expected status True, got {resp.response}")
    new_id = resp.response.data.id
    assert_true(new_id, f"expected new message id, got {resp.response.data}")

    msg = PublicMessage.objects.get(pk=new_id)
    assert_eq(msg.kind, "contact_us", "expected kind=contact_us on saved row")
    assert_eq(msg.name, "Jane Doe", "expected name to persist")
    assert_eq(msg.email, "submit-contact@example.com", "expected email to persist")
    assert_eq(msg.metadata.get("company"), "Acme Inc.", "expected company to land in metadata")
    assert_eq(msg.status, "open", "expected status=open on new row")
    assert_eq(msg.ip_address, TEST_IP, "expected client IP to be captured")


@th.django_unit_test()
def test_submit_support_happy_path(opts):
    from mojo.apps.account.models import PublicMessage

    token = _mint_token()
    resp = opts.client.post(SUBMIT_PATH, {
        "kind": "support",
        "name": "Jane Doe",
        "email": "submit-support@example.com",
        "category": "bug",
        "severity": "high",
        "message": "I cannot log in.",
        "bouncer_token": token,
    })

    assert_eq(resp.status_code, 200, f"expected 200, got {resp.status_code}: {resp.response}")
    msg = PublicMessage.objects.get(pk=resp.response.data.id)
    assert_eq(msg.kind, "support", "expected kind=support on saved row")
    assert_eq(msg.metadata.get("category"), "bug", "category should be stored in metadata")
    assert_eq(msg.metadata.get("severity"), "high", "severity should be stored in metadata")
    assert_true(
        "company" not in msg.metadata,
        f"support kind should not carry company, got metadata={msg.metadata}",
    )


@th.django_unit_test()
def test_rejects_invalid_kind(opts):
    from mojo.apps.account.models import PublicMessage
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip=TEST_IP, key="public_message_submit")

    token = _mint_token()
    resp = opts.client.post(SUBMIT_PATH, {
        "kind": "bogus_kind",
        "name": "Jane",
        "email": "reject-kind@example.com",
        "message": "hi",
        "bouncer_token": token,
    })

    assert_eq(resp.status_code, 400, f"expected 400 for bad kind, got {resp.status_code}")
    saved = PublicMessage.objects.filter(email="reject-kind@example.com").count()
    assert_eq(saved, 0, "no row should be saved on invalid kind")


@th.django_unit_test()
def test_rejects_missing_token_enforced(opts):
    from mojo.apps.account.models import PublicMessage
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip=TEST_IP, key="public_message_submit")

    # With BOUNCER_REQUIRE_TOKEN=True, missing token → 403.
    # Use per-request test-mode header (parallel-safe, no server reload).
    resp = opts.client.post(SUBMIT_PATH, {
        "kind": "contact_us",
        "name": "Jane",
        "email": "reject-token@example.com",
        "message": "hello",
    }, headers={"X-Mojo-Test-Bouncer-Require-Token": "1"})

    assert_eq(resp.status_code, 403, f"expected 403 without token, got {resp.status_code}")
    saved = PublicMessage.objects.filter(email="reject-token@example.com").count()
    assert_eq(saved, 0, "no row should be saved when token is missing")


@th.django_unit_test()
def test_rejects_reused_token(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip=TEST_IP, key="public_message_submit")

    token = _mint_token()
    resp1 = opts.client.post(SUBMIT_PATH, {
        "kind": "contact_us",
        "name": "Jane",
        "email": "reject-token@example.com",
        "message": "first submission",
        "bouncer_token": token,
    })
    assert_eq(resp1.status_code, 200, f"first submit should succeed, got {resp1.status_code}")

    # Use per-request test-mode header (parallel-safe, no server reload).
    resp2 = opts.client.post(SUBMIT_PATH, {
        "kind": "contact_us",
        "name": "Jane",
        "email": "reject-token@example.com",
        "message": "replay attempt",
        "bouncer_token": token,
    }, headers={"X-Mojo-Test-Bouncer-Require-Token": "1"})
    assert_eq(
        resp2.status_code, 403,
        f"replayed token should be rejected with 403, got {resp2.status_code}",
    )


@th.django_unit_test()
def test_rejects_missing_required_fields(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip=TEST_IP, key="public_message_submit")

    token = _mint_token()
    resp = opts.client.post(SUBMIT_PATH, {
        "kind": "contact_us",
        "name": "Jane",
        "email": "reject-required@example.com",
        # missing "message"
        "bouncer_token": token,
    })
    assert_eq(resp.status_code, 400, f"missing required field should 400, got {resp.status_code}")


@th.django_unit_test()
def test_rejects_overlong_message(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip=TEST_IP, key="public_message_submit")

    token = _mint_token()
    big_msg = "x" * 5000  # cap is 4000 chars

    resp = opts.client.post(SUBMIT_PATH, {
        "kind": "contact_us",
        "name": "Jane",
        "email": "reject-long@example.com",
        "message": big_msg,
        "bouncer_token": token,
    })
    assert_eq(resp.status_code, 400, f"overlong message should 400, got {resp.status_code}")


@th.django_unit_test()
def test_rejects_invalid_email(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip=TEST_IP, key="public_message_submit")

    token = _mint_token()
    resp = opts.client.post(SUBMIT_PATH, {
        "kind": "contact_us",
        "name": "Jane",
        "email": "not-an-email",
        "message": "hello",
        "bouncer_token": token,
    })
    assert_eq(resp.status_code, 400, f"invalid email should 400, got {resp.status_code}")


@th.django_unit_test()
def test_content_guard_blocks_spam(opts):
    """Service-level: a mocked check_text returning decision=block raises ValueError.

    Note: we test this in-process against validate_submission() because
    mock.patch does not reach the separate test server process.
    """
    from objict import objict as _obj
    from mojo.apps.account.services import public_message as svc

    with mock.patch(
        "mojo.helpers.content_guard.check_text",
        return_value=_obj(decision="block", reasons=["deny_hit"], matches=[], score=95),
    ):
        try:
            svc.validate_submission("contact_us", {
                "name": "Jane",
                "email": "reject-spam@example.com",
                "message": "anything",
            })
            assert_true(False, "expected ValueError when content_guard blocks")
        except ValueError as err:
            err_str = str(err)
            assert_true(
                ":blocked" in err_str,
                f"expected 'field:blocked' error, got {err_str}",
            )


@th.django_unit_test()
def test_content_guard_failure_is_fail_open(opts):
    """content_guard raising should not block a submission (fail-open)."""
    from mojo.apps.account.services import public_message as svc

    with mock.patch(
        "mojo.helpers.content_guard.check_text",
        side_effect=RuntimeError("moderation engine down"),
    ):
        common, metadata = svc.validate_submission("contact_us", {
            "name": "Jane",
            "email": "ok@example.com",
            "message": "totally fine message",
        })
    assert_eq(common.get("name"), "Jane", "happy path should still validate on check_text error")
    assert_eq(common.get("message"), "totally fine message", "message should survive fail-open")


@th.django_unit_test()
def test_notification_failure_is_caught_per_recipient(opts):
    """Service-level: a send that raises does not propagate from notify_admins.

    Mocked at the service boundary in-process — the HTTP endpoint wraps the
    service call in its own try/except as a second line of defense.
    """
    from mojo.apps.account.models import User, PublicMessage
    from mojo.apps.account.services import public_message as svc

    User.objects.filter(email="notify-svc-fail@example.com").delete()
    PublicMessage.objects.filter(email="notify-fail-svc@example.com").delete()

    user = User.objects.create_user(
        username="notify-svc-fail@example.com",
        email="notify-svc-fail@example.com",
        password="x",
    )
    user.is_active = True
    user.save()
    user.set_protected_metadata("notify_public_messages", True)

    msg = PublicMessage.objects.create(
        kind="contact_us",
        name="Caller",
        email="notify-fail-svc@example.com",
        message="hello",
    )

    with mock.patch.object(User, "send_template_email", side_effect=RuntimeError("mailbox down")):
        # Must not raise.
        sent = svc.notify_admins(msg)

    assert_eq(
        sent, 0,
        f"failed sends should not count toward sent total, got {sent}",
    )


@th.django_unit_test()
def test_client_metadata_passthrough(opts):
    """Clients can attach free-form tracking metadata alongside the form."""
    from mojo.apps.account.models import PublicMessage
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip=TEST_IP, key="public_message_submit")

    PublicMessage.objects.filter(email="metadata-extras@example.com").delete()

    token = _mint_token()
    resp = opts.client.post(SUBMIT_PATH, {
        "kind": "contact_us",
        "name": "Jane",
        "email": "metadata-extras@example.com",
        "company": "Acme",
        "message": "Hello from the landing page.",
        "bouncer_token": token,
        "metadata": {
            "utm_source": "google",
            "utm_campaign": "spring-2026",
            "referrer": "https://example.com/blog/post",
            "landing_page": "/pricing",
        },
    })
    assert_eq(resp.status_code, 200, f"expected 200, got {resp.status_code}: {resp.response}")

    msg = PublicMessage.objects.get(pk=resp.response.data.id)
    assert_eq(msg.metadata.get("company"), "Acme", "kind-schema metadata preserved")
    assert_eq(msg.metadata.get("utm_source"), "google", "client utm_source stored")
    assert_eq(msg.metadata.get("utm_campaign"), "spring-2026", "client utm_campaign stored")
    assert_eq(msg.metadata.get("referrer"), "https://example.com/blog/post", "client referrer stored")
    assert_eq(msg.metadata.get("landing_page"), "/pricing", "client landing_page stored")


@th.django_unit_test()
def test_client_metadata_hardening(opts):
    """Service-level: reserved keys can't be spoofed, and shape is enforced."""
    from mojo.apps.account.services import public_message as svc

    common, metadata = svc.validate_submission("support", {
        "name": "Jane",
        "email": "shape@example.com",
        "category": "bug",
        "severity": "low",
        "message": "hello",
        "metadata": {
            # Attempt to spoof kind-schema keys — must be ignored.
            "category": "billing",
            "severity": "high",
            # Invalid key characters — must be dropped.
            "not a key": "x",
            "weird$key!": "x",
            # Nested / non-primitive — must be dropped.
            "nested": {"deep": "value"},
            "list_val": [1, 2, 3],
            # Valid primitives — must be kept.
            "utm_source": "facebook",
            "retries": 3,
            "is_returning": True,
            "note": None,
        },
    })

    assert_eq(
        metadata.get("severity"), "low",
        f"kind-schema severity must win over client attempt, got {metadata.get('severity')}",
    )
    assert_eq(
        metadata.get("category"), "bug",
        f"kind-schema category must win over client attempt, got {metadata.get('category')}",
    )
    assert_true("not a key" not in metadata, "space in key must be dropped")
    assert_true("weird$key!" not in metadata, "special chars in key must be dropped")
    assert_true("nested" not in metadata, "nested dict value must be dropped")
    assert_true("list_val" not in metadata, "list value must be dropped")
    assert_eq(metadata.get("utm_source"), "facebook", "primitive str must survive")
    assert_eq(metadata.get("retries"), 3, "primitive int must survive")
    assert_eq(metadata.get("is_returning"), True, "primitive bool must survive")
    assert_true("note" in metadata, "explicit None must survive as a stored key")


@th.django_unit_test()
def test_client_metadata_caps(opts):
    """Too-many keys, oversized keys/values are trimmed."""
    from mojo.apps.account.services import public_message as svc

    big = {f"k{i}": f"v{i}" for i in range(40)}  # 40 keys, cap is 25
    big["overlong_value"] = "x" * 10_000
    big["x" * 200] = "dropped-because-key-too-long"

    _, metadata = svc.validate_submission("contact_us", {
        "name": "Jane",
        "email": "caps@example.com",
        "message": "hi",
        "metadata": big,
    })

    # Kind-schema never wrote any keys, so metadata is purely extras here.
    extras_keys = [k for k in metadata if k.startswith("k") or k == "overlong_value"]
    assert_true(
        len(extras_keys) <= 25,
        f"key count should be capped to 25, got {len(extras_keys)}",
    )
    assert_true(
        not any(k.startswith("x" * 200) for k in metadata),
        "oversized keys must be dropped",
    )
    if "overlong_value" in metadata:
        assert_true(
            len(metadata["overlong_value"]) <= 500,
            f"string values must be capped to 500 chars, got {len(metadata['overlong_value'])}",
        )


@th.django_unit_test()
def test_rate_limit(opts):
    from mojo.apps.account.models import PublicMessage
    from mojo.decorators.limits import clear_rate_limits

    # Use a unique per-test IP so parallel modules calling
    # `clear_rate_limits(ip="127.0.0.1")` (broad, no-key wildcard wipe across
    # every srl:*:ip:127.0.0.1 key) don't clobber our counter mid-loop.
    rl_ip = "10.99.99.42"
    forwarded = {"X-Forwarded-For": rl_ip}
    clear_rate_limits(ip=rl_ip, key="public_message_submit")

    PublicMessage.objects.filter(email="rate-limit@example.com").delete()

    ok_count = 0
    rate_limited = False
    for i in range(7):  # cap is 5 in 300s
        # Mint the bouncer token for the same IP we're spoofing via header,
        # since bouncer tokens are bound to a specific IP at mint time.
        token = _mint_token(ip=rl_ip)
        resp = opts.client.post(SUBMIT_PATH, {
            "kind": "contact_us",
            "name": f"Jane {i}",
            "email": "rate-limit@example.com",
            "message": f"submission {i}",
            "bouncer_token": token,
        }, headers=forwarded)
        if resp.status_code == 200:
            ok_count += 1
        elif resp.status_code == 429:
            rate_limited = True
            break

    assert_true(rate_limited, f"expected a 429 within 7 attempts, got {ok_count} successes")
    assert_true(ok_count <= 5, f"rate limit allowed more than 5, got {ok_count}")
