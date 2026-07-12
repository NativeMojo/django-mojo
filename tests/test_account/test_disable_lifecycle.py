"""Tests for the disable-lifecycle service, REST actions, and throttle-read endpoint."""
from unittest import mock
from testit import helpers as th


ADMIN_USERNAME = "disable_admin@test.com"
ADMIN_PASSWORD = "disable_admin_pw_99"
TARGET_USERNAME = "disable_target@test.com"
SECONDARY_USERNAME = "disable_secondary@test.com"
NONADMIN_USERNAME = "disable_nonadmin@test.com"
NONADMIN_PASSWORD = "disable_nonadmin_pw_99"
GROUPSONLY_USERNAME = "disable_groupsonly@test.com"
GROUPSONLY_PASSWORD = "disable_groupsonly_pw_99"
GROUP_NAME = "disable_lifecycle_group"


@th.django_unit_setup()
def setup_disable_lifecycle(opts):
    from mojo.apps.account.models import User, Group

    # Clean up any leftover test data so the suite is repeatable.
    User.objects.filter(email__in=[
        ADMIN_USERNAME, TARGET_USERNAME, SECONDARY_USERNAME, NONADMIN_USERNAME,
        GROUPSONLY_USERNAME,
    ]).delete()
    Group.objects.filter(name=GROUP_NAME).delete()

    admin = User.objects.create_user(username=ADMIN_USERNAME, email=ADMIN_USERNAME, password=ADMIN_PASSWORD)
    admin.is_active = True
    admin.is_email_verified = True
    admin.requires_mfa = False
    admin.save()
    admin.add_permission("manage_users")
    admin.add_permission("manage_groups")
    opts.admin_id = admin.pk
    opts.admin_username = ADMIN_USERNAME

    target = User.objects.create_user(username=TARGET_USERNAME, email=TARGET_USERNAME, password="pw")
    target.is_active = True
    target.metadata = {}
    target.save()
    opts.target_id = target.pk

    secondary = User.objects.create_user(username=SECONDARY_USERNAME, email=SECONDARY_USERNAME, password="pw")
    secondary.is_active = True
    secondary.metadata = {}
    secondary.save()
    opts.secondary_id = secondary.pk

    nonadmin = User.objects.create_user(username=NONADMIN_USERNAME, email=NONADMIN_USERNAME, password=NONADMIN_PASSWORD)
    nonadmin.is_active = True
    nonadmin.is_email_verified = True
    nonadmin.requires_mfa = False
    nonadmin.metadata = {}
    nonadmin.save()
    nonadmin.add_permission("view_users")  # can view but not manage
    opts.nonadmin_id = nonadmin.pk

    # ITEM-035: bare "groups" is view_groups+manage_groups combined — enough
    # to disable/reactivate a group on its own.
    groupsonly = User.objects.create_user(username=GROUPSONLY_USERNAME, email=GROUPSONLY_USERNAME, password=GROUPSONLY_PASSWORD)
    groupsonly.is_active = True
    groupsonly.is_email_verified = True
    groupsonly.requires_mfa = False
    groupsonly.metadata = {}
    groupsonly.save()
    groupsonly.add_permission("groups")
    opts.groupsonly_id = groupsonly.pk

    group = Group.objects.create(name=GROUP_NAME, is_active=True)
    opts.group_id = group.pk


# ---------------------------------------------------------------------------
# Service-level tests
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_service_disable_writes_namespace(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services import disable as disable_service

    target = User.objects.get(pk=opts.target_id)
    User.objects.filter(pk=target.pk).update(is_active=True, metadata={})
    target.refresh_from_db()
    admin = User.objects.get(pk=opts.admin_id)

    with mock.patch("mojo.apps.incident.report_event"):
        disable_service.disable_entity(target, reason="admin", by_user=admin, note="testing")

    target.refresh_from_db()
    block = (target.metadata or {}).get("protected", {}).get("disable", {})
    assert target.is_active is False, "disabled user should have is_active=False"
    assert block.get("reason") == "admin", f"reason should be 'admin', got: {block.get('reason')}"
    assert block.get("by_user_id") == admin.pk, f"by_user_id should be admin pk, got: {block.get('by_user_id')}"
    assert block.get("by_username") == ADMIN_USERNAME, f"by_username should be admin username"
    assert block.get("note") == "testing", "note should be persisted"
    assert block.get("at"), "at timestamp should be set"


@th.django_unit_test()
def test_service_disable_already_disabled_rejected(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services import disable as disable_service
    from mojo import errors as merrors

    target = User.objects.get(pk=opts.target_id)
    User.objects.filter(pk=target.pk).update(is_active=False)
    target.refresh_from_db()

    raised = False
    try:
        with mock.patch("mojo.apps.incident.report_event"):
            disable_service.disable_entity(target, reason="admin", by_user=None)
    except merrors.ValueException:
        raised = True

    assert raised, "double-disable should raise ValueException"


@th.django_unit_test()
def test_service_reactivate_appends_history(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services import disable as disable_service

    target = User.objects.get(pk=opts.target_id)
    User.objects.filter(pk=target.pk).update(is_active=True, metadata={})
    target.refresh_from_db()
    admin = User.objects.get(pk=opts.admin_id)

    with mock.patch("mojo.apps.incident.report_event"):
        disable_service.disable_entity(target, reason="admin", by_user=admin, note="bad apple")
        target.refresh_from_db()
        disable_service.reactivate_entity(target, by_user=admin, note="appeal granted")

    target.refresh_from_db()
    block = (target.metadata or {}).get("protected", {}).get("disable", {})
    assert target.is_active is True, "reactivated user should have is_active=True"
    assert block.get("reason") is None, f"live reason should be cleared, got: {block.get('reason')}"
    history = block.get("history") or []
    assert len(history) == 1, f"history should have 1 entry, got: {len(history)}"
    entry = history[0]
    assert entry["reason"] == "admin", "history entry should preserve disable reason"
    assert entry["note"] == "bad apple", "history entry should preserve disable note"
    assert entry["reactivated_by_user_id"] == admin.pk, "history entry should record reactivator"
    assert entry["reactivated_note"] == "appeal granted", "history entry should record reactivate note"
    assert entry["reactivated_at"], "history entry should have reactivated_at"


@th.django_unit_test()
def test_service_reactivate_never_disabled_rejected(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services import disable as disable_service
    from mojo import errors as merrors

    target = User.objects.get(pk=opts.target_id)
    User.objects.filter(pk=target.pk).update(is_active=True)
    target.refresh_from_db()

    raised = False
    try:
        with mock.patch("mojo.apps.incident.report_event"):
            disable_service.reactivate_entity(target, by_user=None)
    except merrors.ValueException:
        raised = True

    assert raised, "reactivating an already-active user should raise ValueException"


@th.django_unit_test()
def test_service_history_cap_at_20(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services import disable as disable_service

    target = User.objects.get(pk=opts.secondary_id)
    User.objects.filter(pk=target.pk).update(is_active=True, metadata={})
    target.refresh_from_db()
    admin = User.objects.get(pk=opts.admin_id)

    with mock.patch("mojo.apps.incident.report_event"):
        for cycle in range(disable_service.HISTORY_CAP + 1):
            disable_service.disable_entity(target, reason="admin", by_user=admin, note=f"cycle-{cycle}")
            target.refresh_from_db()
            disable_service.reactivate_entity(target, by_user=admin)
            target.refresh_from_db()

    history = (target.metadata or {}).get("protected", {}).get("disable", {}).get("history") or []
    assert len(history) == disable_service.HISTORY_CAP, \
        f"history should be capped at {disable_service.HISTORY_CAP}, got: {len(history)}"
    # Oldest entry should have been dropped — the first cycle is no longer in history.
    notes = [e.get("note") for e in history]
    assert "cycle-0" not in notes, f"oldest entry should be evicted, history notes: {notes}"
    assert "cycle-{}".format(disable_service.HISTORY_CAP) in notes, \
        f"newest entry should be present, history notes: {notes}"


@th.django_unit_test()
def test_pii_anonymize_active_user_writes_namespace(opts):
    from mojo.apps.account.models import User

    user = User.objects.create_user(username="anon_active@test.com", email="anon_active@test.com", password="pw")
    user.is_active = True
    user.metadata = {"timezone": "UTC"}  # simulate non-disable PII metadata that should be wiped
    user.save()

    user.pii_anonymize()
    user.refresh_from_db()

    block = (user.metadata or {}).get("protected", {}).get("disable", {})
    assert user.is_active is False, "anonymized user should have is_active=False"
    assert block.get("reason") == "anonymized", f"reason should be 'anonymized', got: {block.get('reason')}"
    assert block.get("at"), "at timestamp should be set"
    history = block.get("history") or []
    assert history == [], f"active user has no prior cycle to record; history should be empty, got: {history}"
    # PII metadata key (timezone) should have been wiped
    assert "timezone" not in (user.metadata or {}), \
        f"non-disable metadata should be wiped on anonymize, got: {user.metadata}"

    User.objects.filter(pk=user.pk).delete()


@th.django_unit_test()
def test_pii_anonymize_disabled_user_pushes_to_history(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services import disable as disable_service

    user = User.objects.create_user(username="anon_disabled@test.com", email="anon_disabled@test.com", password="pw")
    user.is_active = True
    user.metadata = {}
    user.save()
    admin = User.objects.get(pk=opts.admin_id)

    with mock.patch("mojo.apps.incident.report_event"):
        disable_service.disable_entity(user, reason="abuse", by_user=admin, note="pre-anonymize")

    user.refresh_from_db()
    user.pii_anonymize()
    user.refresh_from_db()

    block = (user.metadata or {}).get("protected", {}).get("disable", {})
    assert block.get("reason") == "anonymized", f"live reason should be 'anonymized', got: {block.get('reason')}"
    history = block.get("history") or []
    assert len(history) == 1, f"prior disable should be in history, got: {len(history)} entries"
    entry = history[0]
    assert entry["reason"] == "abuse", f"history should preserve prior reason, got: {entry.get('reason')}"
    assert entry["note"] == "pre-anonymize", "history should preserve prior note"
    assert entry["reactivated_at"] is None, \
        f"anonymized prior cycle should have reactivated_at=None, got: {entry.get('reactivated_at')}"
    assert "Anonymized" in (entry.get("reactivated_note") or ""), \
        f"reactivated_note should mark the cycle as anonymized, got: {entry.get('reactivated_note')}"

    User.objects.filter(pk=user.pk).delete()


# ---------------------------------------------------------------------------
# Inactive sweep tests
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_sweep_writes_new_warning_shape(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services.inactive import warn_inactive_users
    from mojo.helpers import dates

    User.objects.filter(email__in=["sweep_warn@test.com"]).delete()
    user = User.objects.create_user(username="sweep_warn@test.com", email="sweep_warn@test.com", password="pw")
    user.is_active = True
    user.metadata = {}
    user.last_activity = dates.subtract(days=85)
    user.save()

    with mock.patch("mojo.apps.incident.report_event"), \
         mock.patch.object(User, "send_template_email"):
        warn_inactive_users()

    user.refresh_from_db()
    warning = (user.metadata or {}).get("protected", {}).get("disable", {}).get("warning") or {}
    assert warning.get("sent_at"), f"warning.sent_at should be set, metadata: {user.metadata}"
    assert warning.get("days_until_disable_at_send") is not None, \
        "warning.days_until_disable_at_send should be set"

    User.objects.filter(pk=user.pk).delete()


@th.django_unit_test()
def test_sweep_writes_new_disable_shape(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services.inactive import disable_inactive_users
    from mojo.helpers import dates

    User.objects.filter(email__in=["sweep_disable@test.com"]).delete()
    user = User.objects.create_user(username="sweep_disable@test.com", email="sweep_disable@test.com", password="pw")
    user.is_active = True
    user.metadata = {}
    user.last_activity = dates.subtract(days=100)
    user.save()

    with mock.patch("mojo.apps.incident.report_event"):
        disable_inactive_users()

    user.refresh_from_db()
    block = (user.metadata or {}).get("protected", {}).get("disable", {})
    assert user.is_active is False, "inactive user should be disabled by sweep"
    assert block.get("reason") == "inactive", \
        f"reason should be 'inactive', got: {block.get('reason')}"

    User.objects.filter(pk=user.pk).delete()


@th.django_unit_test()
def test_sweep_reads_legacy_warning(opts):
    """Sweep should treat a user marked with legacy disable_warned=True as already warned."""
    from mojo.apps.account.models import User
    from mojo.apps.account.services.inactive import warn_inactive_users
    from mojo.helpers import dates

    User.objects.filter(email__in=["sweep_legacy_warn@test.com"]).delete()
    user = User.objects.create_user(
        username="sweep_legacy_warn@test.com", email="sweep_legacy_warn@test.com", password="pw")
    user.is_active = True
    user.last_activity = dates.subtract(days=85)
    user.metadata = {"protected": {"disable_warned": True, "disable_warn_date": str(dates.subtract(days=2))}}
    user.save()

    with mock.patch("mojo.apps.incident.report_event"), \
         mock.patch.object(User, "send_template_email") as send_email:
        warn_inactive_users()

    # Send count for THIS specific user — already warned via legacy flag, should not re-warn
    user.refresh_from_db()
    sent_to_this_user = any(
        getattr(call.args[0], "username", None) == user.username
        for call in send_email.call_args_list
    ) if send_email.call_args_list else False
    # Most reliable check: the legacy flag is still present, no new namespace warning was added
    protected = (user.metadata or {}).get("protected") or {}
    assert protected.get("disable_warned") is True, "legacy disable_warned should be untouched"
    new_warning = (protected.get("disable") or {}).get("warning")
    assert new_warning is None, \
        f"sweep should not re-warn a user with legacy warning marker; got new warning: {new_warning}"

    User.objects.filter(pk=user.pk).delete()


@th.django_unit_test()
def test_sweep_honors_new_exempt_flag(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services.inactive import disable_inactive_users
    from mojo.helpers import dates

    User.objects.filter(email__in=["sweep_new_exempt@test.com"]).delete()
    user = User.objects.create_user(
        username="sweep_new_exempt@test.com", email="sweep_new_exempt@test.com", password="pw")
    user.is_active = True
    user.last_activity = dates.subtract(days=120)
    user.metadata = {"protected": {"disable": {"exempt_from_auto_disable": True}}}
    user.save()

    with mock.patch("mojo.apps.incident.report_event"):
        disable_inactive_users()

    user.refresh_from_db()
    assert user.is_active is True, \
        f"user with new-shape exempt flag should NOT be disabled, got is_active={user.is_active}"

    User.objects.filter(pk=user.pk).delete()


@th.django_unit_test()
def test_sweep_honors_legacy_exempt_flag(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services.inactive import disable_inactive_users
    from mojo.helpers import dates

    User.objects.filter(email__in=["sweep_legacy_exempt@test.com"]).delete()
    user = User.objects.create_user(
        username="sweep_legacy_exempt@test.com", email="sweep_legacy_exempt@test.com", password="pw")
    user.is_active = True
    user.last_activity = dates.subtract(days=120)
    user.metadata = {"protected": {"no_disable": True}}
    user.save()

    with mock.patch("mojo.apps.incident.report_event"):
        disable_inactive_users()

    user.refresh_from_db()
    assert user.is_active is True, \
        f"user with legacy no_disable flag should NOT be disabled, got is_active={user.is_active}"

    User.objects.filter(pk=user.pk).delete()


# ---------------------------------------------------------------------------
# migrate_legacy tests
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_migrate_legacy_rewrites_keys(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services import disable as disable_service

    User.objects.filter(email__in=["migrate_legacy@test.com"]).delete()
    user = User.objects.create_user(
        username="migrate_legacy@test.com", email="migrate_legacy@test.com", password="pw")
    user.metadata = {
        "protected": {
            "no_disable": True,
            "disable_warned": True,
            "disable_warn_date": "2026-04-01T00:00:00+00:00",
        }
    }
    user.save()

    changed = disable_service.migrate_legacy(user)
    user.refresh_from_db()

    assert changed is True, "migrate_legacy should report it changed an entity with legacy keys"
    block = (user.metadata or {}).get("protected", {}).get("disable", {})
    assert block.get("exempt_from_auto_disable") is True, \
        f"legacy no_disable should map to exempt_from_auto_disable, got: {block}"
    warning = block.get("warning") or {}
    assert warning.get("sent_at") == "2026-04-01T00:00:00+00:00", \
        f"warning.sent_at should mirror legacy disable_warn_date, got: {warning}"
    # Legacy keys should still be present (one-release dual-read).
    protected = user.metadata.get("protected") or {}
    assert protected.get("no_disable") is True, "legacy no_disable should NOT be removed"
    assert protected.get("disable_warned") is True, "legacy disable_warned should NOT be removed"

    User.objects.filter(pk=user.pk).delete()


@th.django_unit_test()
def test_migrate_legacy_idempotent(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services import disable as disable_service

    User.objects.filter(email__in=["migrate_idempotent@test.com"]).delete()
    user = User.objects.create_user(
        username="migrate_idempotent@test.com", email="migrate_idempotent@test.com", password="pw")
    user.metadata = {"protected": {"no_disable": True}}
    user.save()

    first = disable_service.migrate_legacy(user)
    user.refresh_from_db()
    second = disable_service.migrate_legacy(user)

    assert first is True, "first migrate should report changed"
    assert second is False, f"second migrate should be a no-op, got: {second}"

    User.objects.filter(pk=user.pk).delete()


# ---------------------------------------------------------------------------
# REST endpoint tests
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_rest_disable_user(opts):
    from mojo.apps.account.models import User

    target = User.objects.get(pk=opts.target_id)
    User.objects.filter(pk=target.pk).update(is_active=True, metadata={})

    assert opts.client.login(ADMIN_USERNAME, ADMIN_PASSWORD), "admin login failed"
    with mock.patch("mojo.apps.incident.report_event"):
        resp = opts.client.post(
            f"/api/user/{target.pk}",
            {"disable": {"reason": "admin", "note": "rest-test"}},
        )
    opts.client.logout()

    assert resp.status_code == 200, \
        f"disable POST should succeed, got {resp.status_code}: {opts.client.last_response.body}"
    target.refresh_from_db()
    block = (target.metadata or {}).get("protected", {}).get("disable", {})
    assert target.is_active is False, "REST disable should flip is_active to False"
    assert block.get("reason") == "admin", f"REST disable should write reason, got: {block.get('reason')}"
    assert block.get("note") == "rest-test", "REST disable should persist note"


@th.django_unit_test()
def test_rest_reactivate_user(opts):
    from mojo.apps.account.models import User

    target = User.objects.get(pk=opts.target_id)
    User.objects.filter(pk=target.pk).update(
        is_active=False,
        metadata={"protected": {"disable": {
            "reason": "admin", "at": "2026-05-01T00:00:00+00:00",
            "by_user_id": opts.admin_id, "by_username": ADMIN_USERNAME, "note": "old"
        }}},
    )

    assert opts.client.login(ADMIN_USERNAME, ADMIN_PASSWORD), "admin login failed"
    with mock.patch("mojo.apps.incident.report_event"):
        resp = opts.client.post(
            f"/api/user/{target.pk}",
            {"reactivate": {"note": "second chance"}},
        )
    opts.client.logout()

    assert resp.status_code == 200, \
        f"reactivate POST should succeed, got {resp.status_code}: {opts.client.last_response.body}"
    target.refresh_from_db()
    block = (target.metadata or {}).get("protected", {}).get("disable", {})
    assert target.is_active is True, "REST reactivate should flip is_active to True"
    assert block.get("reason") is None, f"live reason should be cleared, got: {block.get('reason')}"
    history = block.get("history") or []
    assert len(history) == 1, f"reactivate should append to history, got: {len(history)}"


@th.django_unit_test()
def test_rest_disable_invalid_reason_rejected(opts):
    from mojo.apps.account.models import User

    target = User.objects.get(pk=opts.target_id)
    User.objects.filter(pk=target.pk).update(is_active=True, metadata={})

    assert opts.client.login(ADMIN_USERNAME, ADMIN_PASSWORD), "admin login failed"
    with mock.patch("mojo.apps.incident.report_event"):
        # 'inactive' is server-only — REST callers cannot use it
        resp = opts.client.post(
            f"/api/user/{target.pk}",
            {"disable": {"reason": "inactive"}},
        )
    opts.client.logout()

    assert resp.status_code != 200, \
        f"server-only reason should be rejected, got {resp.status_code}: {opts.client.last_response.body}"
    target.refresh_from_db()
    assert target.is_active is True, "rejected disable should NOT flip is_active"


@th.django_unit_test()
def test_rest_disable_requires_manage_users(opts):
    from mojo.apps.account.models import User

    target = User.objects.get(pk=opts.target_id)
    User.objects.filter(pk=target.pk).update(is_active=True, metadata={})

    assert opts.client.login(NONADMIN_USERNAME, NONADMIN_PASSWORD), "nonadmin login failed"
    with mock.patch("mojo.apps.incident.report_event"):
        resp = opts.client.post(
            f"/api/user/{target.pk}",
            {"disable": {"reason": "admin"}},
        )
    opts.client.logout()

    assert resp.status_code != 200, \
        f"non-manager should be rejected, got {resp.status_code}: {opts.client.last_response.body}"
    target.refresh_from_db()
    assert target.is_active is True, "non-manager call should not disable"


@th.django_unit_test()
def test_rest_group_disable_reactivate(opts):
    from mojo.apps.account.models import Group

    group = Group.objects.get(pk=opts.group_id)
    Group.objects.filter(pk=group.pk).update(is_active=True, metadata={})

    assert opts.client.login(ADMIN_USERNAME, ADMIN_PASSWORD), "admin login failed"
    with mock.patch("mojo.apps.incident.report_event"):
        resp1 = opts.client.post(
            f"/api/group/{group.pk}",
            {"disable": {"reason": "archived", "note": "old project"}},
        )
        group.refresh_from_db()
        resp2 = opts.client.post(
            f"/api/group/{group.pk}",
            {"reactivate": {"note": "back in action"}},
        )
    opts.client.logout()

    assert resp1.status_code == 200, \
        f"group disable should succeed, got {resp1.status_code}: {opts.client.last_response.body}"
    assert resp2.status_code == 200, \
        f"group reactivate should succeed, got {resp2.status_code}: {opts.client.last_response.body}"
    group.refresh_from_db()
    block = (group.metadata or {}).get("protected", {}).get("disable", {})
    assert group.is_active is True, "group should be reactivated"
    history = block.get("history") or []
    assert len(history) == 1, f"history should have 1 entry, got: {len(history)}"
    assert history[0]["reason"] == "archived", "history should preserve archived reason"


@th.django_unit_test()
def test_rest_group_disable_reactivate_bare_groups(opts):
    """ITEM-035: bare "groups" is view_groups+manage_groups combined into one
    term — a holder must be able to disable/reactivate a group, mirroring the
    User analog which accepts ["users", "manage_users"]. Before the fix the
    inner gate required literal manage_groups and returned a surprise 403."""
    from mojo.apps.account.models import Group

    group = Group.objects.get(pk=opts.group_id)
    Group.objects.filter(pk=group.pk).update(is_active=True, metadata={})

    assert opts.client.login(GROUPSONLY_USERNAME, GROUPSONLY_PASSWORD), "groups-only login failed"
    with mock.patch("mojo.apps.incident.report_event"):
        resp1 = opts.client.post(
            f"/api/group/{group.pk}",
            {"disable": {"reason": "archived", "note": "bare groups"}},
        )
        group.refresh_from_db()
        resp2 = opts.client.post(
            f"/api/group/{group.pk}",
            {"reactivate": {"note": "bare groups"}},
        )
    opts.client.logout()

    assert resp1.status_code == 200, \
        f"bare-'groups' disable should succeed, got {resp1.status_code}: {opts.client.last_response.body}"
    assert resp2.status_code == 200, \
        f"bare-'groups' reactivate should succeed, got {resp2.status_code}: {opts.client.last_response.body}"
    group.refresh_from_db()
    assert group.is_active is True, "group should be reactivated"


# ---------------------------------------------------------------------------
# Throttle-read endpoint tests
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_throttle_read_no_counter(opts):
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits

    target = User.objects.get(pk=opts.target_id)
    clear_rate_limits(key="login", account_id=target.pk)

    assert opts.client.login(ADMIN_USERNAME, ADMIN_PASSWORD), "admin login failed"
    resp = opts.client.get(f"/api/auth/manage/throttle?user_id={target.pk}")
    opts.client.logout()

    assert resp.status_code == 200, \
        f"throttle GET should succeed, got {resp.status_code}: {opts.client.last_response.body}"
    data = resp.response.data
    assert data["count"] == 0, f"count should be 0 with no counter, got: {data['count']}"
    assert data["retry_after_seconds"] == 0, \
        f"retry_after should be 0 when not throttled, got: {data['retry_after_seconds']}"


@th.django_unit_test()
def test_throttle_read_under_limit(opts):
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits, check_account_attempt

    target = User.objects.get(pk=opts.target_id)
    clear_rate_limits(key="login", account_id=target.pk)
    # Bump the counter by 3 attempts.
    for _ in range(3):
        check_account_attempt("login", target.pk, 100, 900)

    assert opts.client.login(ADMIN_USERNAME, ADMIN_PASSWORD), "admin login failed"
    resp = opts.client.get(f"/api/auth/manage/throttle?user_id={target.pk}")
    opts.client.logout()
    clear_rate_limits(key="login", account_id=target.pk)

    assert resp.status_code == 200, \
        f"throttle GET should succeed, got {resp.status_code}"
    data = resp.response.data
    assert data["count"] == 3, f"count should be 3 after 3 attempts, got: {data['count']}"
    assert data["retry_after_seconds"] == 0, \
        f"retry_after should be 0 when under limit, got: {data['retry_after_seconds']}"


@th.django_unit_test()
def test_throttle_read_over_limit(opts):
    from mojo.apps.account.models import User
    from mojo.helpers.settings import settings
    from mojo.decorators.limits import clear_rate_limits, check_account_attempt

    target = User.objects.get(pk=opts.target_id)
    clear_rate_limits(key="login", account_id=target.pk)
    limit = settings.get("LOGIN_USERNAME_LIMIT", 10, kind="int")
    for _ in range(limit + 1):
        check_account_attempt("login", target.pk, limit, 900)

    assert opts.client.login(ADMIN_USERNAME, ADMIN_PASSWORD), "admin login failed"
    resp = opts.client.get(f"/api/auth/manage/throttle?user_id={target.pk}")
    opts.client.logout()
    clear_rate_limits(key="login", account_id=target.pk)

    assert resp.status_code == 200, f"throttle GET should succeed, got {resp.status_code}"
    data = resp.response.data
    assert data["count"] >= limit, f"count should be >= limit, got: {data['count']}"
    assert data["retry_after_seconds"] > 0, \
        f"retry_after should be > 0 when over limit, got: {data['retry_after_seconds']}"


@th.django_unit_test()
def test_throttle_read_username_lookup(opts):
    from mojo.decorators.limits import clear_rate_limits

    clear_rate_limits(key="login", account_id=opts.target_id)

    assert opts.client.login(ADMIN_USERNAME, ADMIN_PASSWORD), "admin login failed"
    resp = opts.client.get(f"/api/auth/manage/throttle?username={TARGET_USERNAME}")
    opts.client.logout()

    assert resp.status_code == 200, \
        f"throttle GET by username should succeed, got {resp.status_code}: {opts.client.last_response.body}"


@th.django_unit_test()
def test_throttle_read_unknown_user(opts):
    assert opts.client.login(ADMIN_USERNAME, ADMIN_PASSWORD), "admin login failed"
    resp = opts.client.get("/api/auth/manage/throttle?user_id=999999999")
    opts.client.logout()

    assert resp.status_code != 200, \
        f"unknown user_id should be rejected, got {resp.status_code}: {opts.client.last_response.body}"


@th.django_unit_test()
def test_throttle_read_unsupported_key(opts):
    assert opts.client.login(ADMIN_USERNAME, ADMIN_PASSWORD), "admin login failed"
    resp = opts.client.get(f"/api/auth/manage/throttle?user_id={opts.target_id}&key=password_reset")
    opts.client.logout()

    assert resp.status_code != 200, \
        f"unsupported key should be rejected, got {resp.status_code}: {opts.client.last_response.body}"


@th.django_unit_test()
def test_throttle_read_requires_manage_users(opts):
    assert opts.client.login(NONADMIN_USERNAME, NONADMIN_PASSWORD), "nonadmin login failed"
    resp = opts.client.get(f"/api/auth/manage/throttle?user_id={opts.target_id}")
    opts.client.logout()

    assert resp.status_code != 200, \
        f"non-manager should be rejected from throttle read, got {resp.status_code}"
