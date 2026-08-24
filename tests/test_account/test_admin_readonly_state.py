"""Retained default-tier regressions for sensitive-setting state reporting.

The exhaustive Admin Settings redaction matrix moved to
tests/test_account_admin_extended_serial/test_admin_settings.py (maestro
item #1839) because it patches production module attributes and writes
protected Setting rows. These two checks are the read-only half worth
keeping in the default tier: ``admin_settings._static_state`` on a locally
constructed descriptor touches no database row and patches nothing, and
still proves the live-secret-key contract — a configured sensitive
deployment setting (``SECRET_KEY`` is always configured in a running
process) reports only ``{"configured": True}`` with deployment provenance,
never its value.
"""

from testit import helpers as th

TESTIT_TIER = "admin"


@th.django_unit_test("a configured sensitive deployment setting reports configured state only")
def test_live_secret_key_reports_configured_state(opts):
    from mojo.apps.account.services import admin_settings
    configured_sensitive = admin_settings.Descriptor(
        "SECRET_KEY", "Test configured secret", "Security & operations",
        "Test only.", "configured", sensitivity="configured_only")
    value, source, ignored = admin_settings._static_state(
        configured_sensitive, [])
    assert value == {"configured": True} and source == "deployment" and not ignored, \
        "a configured sensitive setting exposed its value or lost provenance"


@th.django_unit_test("an absent sensitive setting never claims deployment provenance")
def test_absent_sensitive_setting_reports_default(opts):
    from mojo.apps.account.services import admin_settings
    sensitive = admin_settings.Descriptor(
        "TEST_SECRET", "Test secret", "Security & operations", "Test only.",
        "configured", sensitivity="configured_only")
    value, source, ignored = admin_settings._static_state(sensitive, [])
    assert value == {"configured": False} and source == "default" and not ignored, \
        "an absent sensitive setting reported false deployment provenance"
