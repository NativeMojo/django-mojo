"""Setting write-validation (ITEM-023) — validator registry + posture keys.

Every geofence-consumed Setting must reject garbage at write time through EVERY
path (generic /api/settings REST, Setting.set(), shell .save()), and a posture
write must invalidate the decision cache (Setting.GEOFENCE_KEYS drives both).

Parallel-safety (config_plane rules apply — these tests write REAL global
Setting rows): valid-write tests persist only DEFAULT-EQUAL values (zero
behavioral impact if a finally is missed), everything is restored in finally
via Setting.remove + decision-cache invalidation, the DB allowlist/127.0.0.1
are never touched, and no strict=true row is ever persisted.
"""
import uuid as _uuid
from testit import helpers as th

# key -> (garbage payloads that must 400, default-equal valid payload)
GARBAGE = {
    "GEOFENCE_ENABLED": ["garbage"],                    # not JSON at all
    "GEOFENCE_FAIL_CLOSED": ["[1,2"],                   # truncated JSON
    "GEOFENCE_ALLOW_PRIVATE_IPS": ['"yes"'],            # JSON string, not boolean
    "GEOFENCE_CACHE_TTL": ["true", "-5"],               # bool-as-int trap; negative
    "GEOFENCE_FAIL_CLOSED_SCOPES": ['{"a": 1}', '["payments", ""]'],
}
VALID = {
    "GEOFENCE_ENABLED": "true",
    "GEOFENCE_FAIL_CLOSED": "false",
    "GEOFENCE_ALLOW_PRIVATE_IPS": "true",
    "GEOFENCE_CACHE_TTL": "300",
    "GEOFENCE_FAIL_CLOSED_SCOPES": '["item023-scope"]',
}
CACHE_IP = "203.0.113.99"  # TEST-NET-3 — never a real client in this suite


def _cleanup_settings():
    from mojo.apps.account.models.setting import Setting
    from mojo.apps.account.services.geofence import cache as gf_cache
    for key in VALID:
        Setting.remove(key)
    Setting.remove("ITEM023_TEST_KEY")
    Setting.remove("ITEM023_FREE_KEY")
    gf_cache.invalidate_all()


def _row(key):
    from mojo.apps.account.models.setting import Setting
    return Setting.objects.filter(key=key, group=None).first()


def _login(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1", key="login")
    ok = opts.client.login(opts.admin_email, opts.admin_password)
    assert ok, f"admin login failed: {opts.client.last_response.body}"


@th.django_unit_setup()
def setup_settings_validation(opts):
    from mojo.apps.account.models import User

    # Long-lived DB: clear anything a previous run left behind BEFORE creating.
    _cleanup_settings()

    suffix = _uuid.uuid4().hex[:8]
    opts.admin_email = f"item023_admin_{suffix}@geofence.test"
    opts.admin_password = "Item023##admin99"
    admin = User.objects.create_user(
        username=opts.admin_email, email=opts.admin_email, password=opts.admin_password)
    admin.is_email_verified = True
    admin.requires_mfa = False
    admin.add_permission("manage_settings")
    admin.save()


@th.django_unit_test("settings: /api/settings rejects garbage for every geofence posture key")
def test_backdoor_garbage_rejected(opts):
    _login(opts)
    try:
        for key, payloads in GARBAGE.items():
            for payload in payloads:
                resp = opts.client.post("/api/settings", {"key": key, "value": payload})
                assert resp.status_code == 400, (
                    f"{key}={payload!r} must be rejected at write time, "
                    f"got {resp.status_code}: {opts.client.last_response.body}"
                )
                assert _row(key) is None, \
                    f"rejected write of {key} must not persist a row"
    finally:
        _cleanup_settings()
        opts.client.logout()


@th.django_unit_test("settings: /api/settings accepts valid (default-equal) posture values")
def test_backdoor_valid_accepted(opts):
    _login(opts)
    try:
        for key, payload in VALID.items():
            resp = opts.client.post("/api/settings", {"key": key, "value": payload})
            assert resp.status_code == 200, (
                f"valid {key}={payload!r} must save, "
                f"got {resp.status_code}: {opts.client.last_response.body}"
            )
            assert _row(key) is not None, f"valid write of {key} must persist"
    finally:
        _cleanup_settings()
        opts.client.logout()


@th.django_unit_test("settings: group-scoped rows rejected for newly-validated posture keys")
def test_backdoor_group_scoped_rejected(opts):
    from mojo.apps.account.models.group import Group
    _login(opts)
    grp = Group.objects.create(
        name=f"Item023 SettingScope {_uuid.uuid4().hex[:8]}", is_active=True)
    try:
        resp = opts.client.post(
            "/api/settings",
            {"key": "GEOFENCE_ENABLED", "value": "true", "group": grp.pk})
        assert resp.status_code == 400, \
            f"group-scoped GEOFENCE_ENABLED must 400, got {resp.status_code}"
        assert "global-only" in str(opts.client.last_response.body), \
            f"rejection must explain why: {opts.client.last_response.body}"
    finally:
        grp.delete()
        _cleanup_settings()
        opts.client.logout()


@th.django_unit_test("settings: shell/programmatic writes are validated too (save-level hook)")
def test_shell_write_validated(opts):
    from mojo import errors as merrors
    from mojo.apps.account.models.setting import Setting
    try:
        raised = False
        try:
            Setting(key="GEOFENCE_ENABLED", value="garbage").save()
        except merrors.ValueException:
            raised = True
        assert raised, \
            "direct Setting(...).save() with garbage must raise (shell back door)"

        raised = False
        try:
            Setting.set("GEOFENCE_CACHE_TTL", -1)
        except merrors.ValueException:
            raised = True
        assert raised, "Setting.set() with an invalid value must raise"
    finally:
        _cleanup_settings()


@th.django_unit_test("settings: per-key validator registry — apps can register their own keys")
def test_validator_registry(opts):
    from mojo import errors as merrors
    from mojo.apps.account.models.setting import Setting

    def _only_ok(key, parsed):
        if parsed != {"ok": True}:
            raise ValueError(f"{key} accepts only {{\"ok\": true}}")

    Setting.register_validator("ITEM023_TEST_KEY", _only_ok)
    try:
        raised = False
        try:
            Setting.set("ITEM023_TEST_KEY", {"nope": 1})
        except merrors.ValueException:
            raised = True
        assert raised, "registered validator must reject a bad value"

        Setting.set("ITEM023_TEST_KEY", {"ok": True})
        assert _row("ITEM023_TEST_KEY") is not None, \
            "registered validator must accept a valid value"

        # Unregistered keys keep accepting arbitrary values.
        Setting.set("ITEM023_FREE_KEY", "anything at all")
        assert _row("ITEM023_FREE_KEY") is not None, \
            "unregistered keys must remain unvalidated"
    finally:
        Setting.VALIDATORS.pop("ITEM023_TEST_KEY", None)
        _cleanup_settings()


@th.django_unit_test("settings: a posture-key write invalidates cached geofence decisions")
def test_posture_write_invalidates_decision_cache(opts):
    from mojo.apps.account.models.setting import Setting
    from mojo.apps.account.services.geofence import cache as gf_cache
    try:
        gf_cache.set(CACHE_IP, None, {"allowed": True, "reason": "item023-test"}, 300)
        assert gf_cache.get(CACHE_IP, None) is not None, \
            "precondition: decision must be cached"
        Setting.set("GEOFENCE_ALLOW_PRIVATE_IPS", True)
        assert gf_cache.get(CACHE_IP, None) is None, (
            "an ALLOW_PRIVATE_IPS write must invalidate cached decisions "
            "(stale private_ip allows must not outlive a posture flip)"
        )
    finally:
        _cleanup_settings()
