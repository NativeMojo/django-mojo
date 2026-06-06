"""End-to-end tests for per-group extra (non-canonical) registration fields.

Covers the capture + persistence half of the configurable extra-fields feature:
  - a field a group declares in registration.extra_fields is captured even when
    the legacy global REGISTRATION_EXTRA_FIELDS allowlist is empty (union)
  - captured extras persist to user.metadata["registration"]
  - a field that is neither globally allowlisted nor group-declared is dropped
  - no extras configured → no metadata["registration"] is written

The group's extra_fields config is injected per-request via the
X-Mojo-Test-Register-Extra-Fields header (resolve_extra_fields honors it when
the test-mode gate passes), and the global allowlist via
X-Mojo-Test-Registration-Extra-Fields — both gated to loopback test traffic.

The render/JS half is covered in tests/test_auth/bouncer_forms.py.
"""
import json
import uuid as _uuid
from testit import helpers as th
from tests.test_register import _capture


HANDLER_REGISTER_OK = "tests.test_register._capture.capture_register"


def _fresh_email(suffix):
    return f"regx_{suffix}_{_uuid.uuid4().hex[:8]}@register.test"


def _post(opts, payload, *, extra_fields=None, global_extras=None):
    """POST /api/auth/register with a capture handler and optional per-request
    group extra_fields config + global allowlist. Returns (resp, capture_id)."""
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1", key="register")
    capture_id = _uuid.uuid4().hex
    _capture.clear_capture(capture_id)
    headers = {
        "X-Mojo-Test-Capture-Id": capture_id,
        "X-Mojo-Test-Allow-User-Registration": "1",
        "X-Mojo-Test-User-Registered-Handler": HANDLER_REGISTER_OK,
    }
    if extra_fields is not None:
        headers["X-Mojo-Test-Register-Extra-Fields"] = json.dumps(extra_fields)
    if global_extras is not None:
        headers["X-Mojo-Test-Registration-Extra-Fields"] = json.dumps(global_extras)
    resp = opts.client.post("/api/auth/register", payload, headers=headers)
    return resp, capture_id


@th.django_unit_test("extra fields: group-declared field is captured + persisted to metadata.registration")
def test_group_extra_field_captured_and_persisted(opts):
    from mojo.apps.account.models import User

    email = _fresh_email("grp")
    resp, capture_id = _post(
        opts, {"email": email, "password": "RegPass##99", "promo": "WELCOME100"},
        extra_fields=[{"name": "promo", "label": "Promo code"}],
        global_extras=[])  # legacy global allowlist empty — only the group declares promo

    assert resp.status_code == 200, \
        f"register with a group-declared extra must succeed, got {resp.status_code}: {opts.client.last_response.body}"

    reg_calls = _capture.read_capture(capture_id).get("register", [])
    assert len(reg_calls) == 1, f"register-handler must fire once, got {len(reg_calls)}"
    assert reg_calls[0]["extra"].get("promo") == "WELCOME100", \
        f"a group-declared extra must reach the handler even with an empty global " \
        f"allowlist (union), got extra={reg_calls[0]['extra']}"

    user = User.objects.filter(email=email).first()
    assert user is not None, "user row must exist after register"
    reg_meta = (user.metadata or {}).get("registration") or {}
    assert reg_meta.get("promo") == "WELCOME100", \
        f"captured extra must persist to user.metadata['registration'], got {user.metadata!r}"


@th.django_unit_test("extra fields: a field neither globally allowlisted nor group-declared is dropped")
def test_undeclared_extra_field_dropped(opts):
    from mojo.apps.account.models import User

    email = _fresh_email("drop")
    resp, capture_id = _post(
        opts,
        {"email": email, "password": "RegPass##99",
         "promo": "WELCOME100", "sneaky": "nope"},
        extra_fields=[{"name": "promo"}],
        global_extras=[])

    assert resp.status_code == 200, \
        f"register must succeed (undeclared keys silently dropped), got {resp.status_code}: {opts.client.last_response.body}"

    extra = _capture.read_capture(capture_id).get("register", [])[0]["extra"]
    assert extra.get("promo") == "WELCOME100", \
        f"the declared extra must be captured, got {extra}"
    assert "sneaky" not in extra, \
        f"an undeclared key must NOT reach the handler, got {extra}"

    user = User.objects.filter(email=email).first()
    reg_meta = (user.metadata or {}).get("registration") or {}
    assert "sneaky" not in reg_meta, \
        f"an undeclared key must NOT be persisted to metadata.registration, got {reg_meta}"


@th.django_unit_test("extra fields: legacy global REGISTRATION_EXTRA_FIELDS still captures + persists")
def test_global_allowlist_still_persists(opts):
    from mojo.apps.account.models import User

    email = _fresh_email("glob")
    resp, capture_id = _post(
        opts, {"email": email, "password": "RegPass##99", "promo": "LEGACY50"},
        global_extras=["promo"])  # no group extra_fields header → union reduces to global

    assert resp.status_code == 200, \
        f"register via the legacy global allowlist must succeed, got {resp.status_code}: {opts.client.last_response.body}"

    extra = _capture.read_capture(capture_id).get("register", [])[0]["extra"]
    assert extra.get("promo") == "LEGACY50", \
        f"a globally-allowlisted extra must still reach the handler (back-compat), got {extra}"

    user = User.objects.filter(email=email).first()
    reg_meta = (user.metadata or {}).get("registration") or {}
    assert reg_meta.get("promo") == "LEGACY50", \
        f"a globally-allowlisted extra must also persist to metadata.registration, got {user.metadata!r}"


@th.django_unit_test("extra fields: none configured → no metadata.registration written (default unchanged)")
def test_no_extras_no_registration_metadata(opts):
    from mojo.apps.account.models import User

    email = _fresh_email("none")
    resp, _ = _post(
        opts, {"email": email, "password": "RegPass##99"},
        global_extras=[])

    assert resp.status_code == 200, \
        f"plain register must succeed, got {resp.status_code}: {opts.client.last_response.body}"

    user = User.objects.filter(email=email).first()
    assert user is not None, "user row must exist after register"
    assert not (user.metadata or {}).get("registration"), \
        f"no extras configured must leave metadata.registration unset, got {user.metadata!r}"
