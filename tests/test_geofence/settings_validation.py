"""Setting write-validation (ITEM-023) — validator registry + posture keys.

Every geofence-consumed Setting must reject garbage at write time through EVERY
path, and a posture write must invalidate the decision cache
(Setting.GEOFENCE_KEYS drives both). This module keeps the in-process
shell-path coverage (Setting.set(), .save(), the validator registry, cache
invalidation); the /api/settings REST write matrices moved to
tests/test_geofence_extended_serial/settings_validation.py because they write
protected GEOFENCE_* keys through the live server (maestro item #1839).

Parallel-safety (config_plane rules apply — these tests write REAL global
Setting rows): valid-write tests persist only DEFAULT-EQUAL values (zero
behavioral impact if a finally is missed), everything is restored in finally
via Setting.remove + decision-cache invalidation, the DB allowlist/127.0.0.1
are never touched, and no strict=true row is ever persisted.
"""

TESTIT_TIER = "extended"
from testit import helpers as th

# default-equal valid payloads for every geofence-consumed posture key
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


@th.django_unit_setup()
def setup_settings_validation(opts):
    # Long-lived DB: clear anything a previous run left behind BEFORE creating.
    _cleanup_settings()


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
