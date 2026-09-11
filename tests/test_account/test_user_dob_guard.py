"""Regression for maestro WMWX #3015 — `User.dob` is immutable to the account
holder once set.

On an age-gated deployment the date of birth is the eligibility record, not a
profile preference: the value on the row is what a downstream KYC provider is
told at customer creation, so a self-edit after registration is an age-gate
bypass. The refusal mirrors the existing `phone_number` block in
`_handle_existing_user_pre_save`:

  * change, clear-to-null and re-set are all refused for a non-admin caller
    once the row HAS a stored `dob` — on both `/api/user/<own pk>` and
    `/api/user/me`;
  * setting one for the FIRST time (stored value NULL) stays open, so a
    consumer that collects DOB after signup keeps working;
  * an identical re-post is a 200 no-op — `set_dob` normalizes the posted
    value to a `date` before the change is recorded, so a client that
    round-trips the whole user object never trips the guard and never
    spuriously resets `is_dob_verified`;
  * the admin tier (`users` / `manage_users` / superuser) can still correct a
    DOB, and that correction is audited as `dob:changed` with before/after.

No tier declaration: inherits the package's `default_core`, so it runs in the
bare `core` preset.
"""
from testit import helpers as th

SELF_USERNAME = "dob_guard_self@test.com"
SELF_PASSWORD = "dob_guard_self_pw_99"
ADMIN_USERNAME = "dob_guard_admin@test.com"
ADMIN_PASSWORD = "dob_guard_admin_pw_99"
FRESH_USERNAME = "dob_guard_fresh@test.com"
FRESH_PASSWORD = "dob_guard_fresh_pw_99"

STORED_DOB = "1990-05-15"
OTHER_DOB = "2001-01-01"


def _make_user(User, username, password, dob=None):
    user = User.objects.create_user(username=username, email=username, password=password)
    user.is_active = True
    user.is_email_verified = True
    user.requires_mfa = False
    if dob is not None:
        import datetime
        user.dob = datetime.date.fromisoformat(dob)
    user.save()
    return user


@th.django_unit_setup()
def setup_dob_guard(opts):
    from mojo.apps.account.models import User

    User.objects.filter(email__in=[SELF_USERNAME, ADMIN_USERNAME, FRESH_USERNAME]).delete()

    self_user = _make_user(User, SELF_USERNAME, SELF_PASSWORD, dob=STORED_DOB)
    opts.self_id = self_user.pk

    admin = _make_user(User, ADMIN_USERNAME, ADMIN_PASSWORD)
    admin.add_permission("manage_users")
    opts.admin_id = admin.pk

    fresh = _make_user(User, FRESH_USERNAME, FRESH_PASSWORD)
    opts.fresh_id = fresh.pk


def _reset_self_dob(User, opts):
    import datetime
    User.objects.filter(pk=opts.self_id).update(
        dob=datetime.date.fromisoformat(STORED_DOB), is_dob_verified=False)


@th.django_unit_test("owner cannot change a stored dob by pk — 403, row unchanged")
def test_owner_change_by_pk_refused(opts):
    """THE regression: before the fix this was a 200 and the row moved."""
    from mojo.apps.account.models import User

    _reset_self_dob(User, opts)
    assert opts.client.login(SELF_USERNAME, SELF_PASSWORD), "self login failed"
    resp = opts.client.post(f"/api/user/{opts.self_id}", {"dob": OTHER_DOB})
    opts.client.logout()

    assert resp.status_code == 403, (
        f"owner dob change must be refused like email/username, "
        f"got {resp.status_code}: {resp.body}")
    user = User.objects.get(pk=opts.self_id)
    assert user.dob.isoformat() == STORED_DOB, (
        f"stored dob must be untouched after the refusal, got {user.dob}")


@th.django_unit_test("owner cannot change or clear a stored dob via /user/me")
def test_owner_change_and_clear_via_me_refused(opts):
    from mojo.apps.account.models import User

    _reset_self_dob(User, opts)
    assert opts.client.login(SELF_USERNAME, SELF_PASSWORD), "self login failed"
    changed = opts.client.post("/api/user/me", {"dob": OTHER_DOB})
    cleared = opts.client.post("/api/user/me", {"dob": None})
    blanked = opts.client.post("/api/user/me", {"dob": ""})
    opts.client.logout()

    assert changed.status_code == 403, (
        f"/user/me is the same handler as /user/<pk> and must refuse too, "
        f"got {changed.status_code}: {changed.body}")
    assert cleared.status_code == 403, (
        f"clearing a stored dob must be refused — clear-then-reset is the "
        f"obvious loophole, got {cleared.status_code}: {cleared.body}")
    assert blanked.status_code == 403, (
        f"an empty-string dob is a clear and must be refused, "
        f"got {blanked.status_code}: {blanked.body}")
    user = User.objects.get(pk=opts.self_id)
    assert user.dob is not None and user.dob.isoformat() == STORED_DOB, (
        f"stored dob must survive every refused write, got {user.dob}")


@th.django_unit_test("re-posting the identical dob is a 200 no-op")
def test_owner_identical_repost_is_noop(opts):
    """A client that round-trips the whole user object must not break, and the
    no-op must not reset is_dob_verified — the posted string has to be
    normalized to a date BEFORE the change is recorded."""
    from mojo.apps.account.models import User

    _reset_self_dob(User, opts)
    User.objects.filter(pk=opts.self_id).update(is_dob_verified=True)
    assert opts.client.login(SELF_USERNAME, SELF_PASSWORD), "self login failed"
    resp = opts.client.post("/api/user/me", {"dob": STORED_DOB, "display_name": "Same DOB"})
    opts.client.logout()

    assert resp.status_code == 200, (
        f"an unchanged dob must be a clean no-op, got {resp.status_code}: {resp.body}")
    user = User.objects.get(pk=opts.self_id)
    assert user.dob.isoformat() == STORED_DOB, f"dob must be unchanged, got {user.dob}"
    assert user.is_dob_verified is True, (
        "an identical re-post must not enter changed_fields — is_dob_verified "
        "was spuriously reset")
    assert user.display_name == "Same DOB", "the rest of the post must still apply"
    User.objects.filter(pk=opts.self_id).update(is_dob_verified=False)


@th.django_unit_test("admin tier can correct a dob; correction resets verification and is audited")
def test_admin_correction_allowed_and_logged(opts):
    from mojo.apps.account.models import User
    from mojo.apps.logit.models import Log

    _reset_self_dob(User, opts)
    User.objects.filter(pk=opts.self_id).update(is_dob_verified=True)
    Log.objects.filter(model_name="account.User", model_id=opts.self_id, kind="dob:changed").delete()

    assert opts.client.login(ADMIN_USERNAME, ADMIN_PASSWORD), "admin login failed"
    resp = opts.client.post(f"/api/user/{opts.self_id}", {"dob": OTHER_DOB})
    opts.client.logout()

    assert resp.status_code == 200, (
        f"manage_users must still be able to correct a dob, got {resp.status_code}: {resp.body}")
    user = User.objects.get(pk=opts.self_id)
    assert user.dob.isoformat() == OTHER_DOB, f"admin correction must land, got {user.dob}"
    assert user.is_dob_verified is False, "a changed dob must reset is_dob_verified"
    entry = Log.objects.filter(
        model_name="account.User", model_id=opts.self_id, kind="dob:changed").first()
    assert entry is not None, "an admin dob correction must leave a dob:changed audit line"
    assert STORED_DOB in (entry.log or "") and OTHER_DOB in (entry.log or ""), (
        f"the audit line must carry before and after, got: {entry.log!r}")
    _reset_self_dob(User, opts)


@th.django_unit_test("owner may set a dob for the first time when none is stored")
def test_owner_first_set_allowed(opts):
    """The gate is the row's own stored value, not a setting: NULL -> value stays
    open for deployments that collect DOB after signup. A future date is still
    refused as a 400, not silently stored."""
    from mojo.apps.account.models import User

    User.objects.filter(pk=opts.fresh_id).update(dob=None)
    assert opts.client.login(FRESH_USERNAME, FRESH_PASSWORD), "fresh login failed"
    future = opts.client.post("/api/user/me", {"dob": "2999-01-01"})
    garbage = opts.client.post("/api/user/me", {"dob": "not-a-date"})
    first = opts.client.post("/api/user/me", {"dob": STORED_DOB})
    second = opts.client.post("/api/user/me", {"dob": OTHER_DOB})
    opts.client.logout()

    assert future.status_code == 400, (
        f"a future dob must be a validation error, got {future.status_code}: {future.body}")
    assert garbage.status_code == 400, (
        f"an unparseable dob must be a validation error, got {garbage.status_code}: {garbage.body}")
    assert first.status_code == 200, (
        f"first-time set on a NULL row must be allowed, got {first.status_code}: {first.body}")
    assert second.status_code == 403, (
        f"the second write is a change and must be refused, got {second.status_code}: {second.body}")
    user = User.objects.get(pk=opts.fresh_id)
    assert user.dob.isoformat() == STORED_DOB, f"first set must land and stick, got {user.dob}"
    User.objects.filter(pk=opts.fresh_id).update(dob=None)
