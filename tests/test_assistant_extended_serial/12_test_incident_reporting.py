"""
WS-handler incident reporting test moved from
tests/test_assistant/12_test_incident_reporting.py — it mock.patches the
shared settings singleton (mojo.helpers.settings.settings.get) around an
in-process handler call, which is unsafe under the parallel default tier
(maestro item #1839). Runs opt-in (`extended`) and serial.
"""
from unittest import mock
from testit import helpers as th
from testit.helpers import assert_eq


TEST_PASSWORD = 'TestPass1!'
NOPERM_EMAIL = "evt-noperm@example.com"


def _clear_events(user, category):
    from mojo.apps.incident.models import Event
    Event.objects.filter(uid=user.pk, category=category).delete()


def _event(user, category):
    from mojo.apps.incident.models import Event
    return Event.objects.filter(uid=user.pk, category=category).latest("pk")


def _real_reporter():
    # Import the concrete callable, not the package alias that other parallel
    # packages intentionally patch while testing their own call sites.
    from mojo.apps.incident.reporter import report_event
    return report_event


@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
@th.requires_app("mojo.apps.incident")
def setup_reporting_serial(opts):
    from mojo.apps.account.models import User

    # Long-lived DB: clean anything a previous run left behind BEFORE creating.
    User.objects.filter(email=NOPERM_EMAIL).delete()


@th.django_unit_test()
def test_handler_permission_denied_fires_event(opts):
    """WS handler permission denied should fire an event."""
    from mojo.apps.assistant.handler import _handle_message
    from mojo.apps.account.models import User

    # Create user without view_admin
    email = NOPERM_EMAIL
    User.objects.filter(email=email).delete()
    noperm = User.objects.create_user(username=email, email=email, password=TEST_PASSWORD)
    noperm.is_email_verified = True
    noperm.save()

    # Mock settings.get to return True for LLM_ADMIN_ENABLED so we reach permission check
    from mojo.helpers.settings import settings
    orig_get = settings.get

    def patched_get(name, *args, **kwargs):
        if name == "LLM_ADMIN_ENABLED":
            return True
        return orig_get(name, *args, **kwargs)

    _clear_events(noperm, "assistant:permission_denied")
    with mock.patch.object(settings, "get", side_effect=patched_get):
        result = _handle_message(
            noperm, {"type": "assistant_message", "message": "hello"},
            _reporter=_real_reporter())
    assert_eq(result["type"], "assistant_error", "Should return error")
    event = _event(noperm, "assistant:permission_denied")
    assert_eq(
        event.category,
        "assistant:permission_denied",
        "Category should be assistant:permission_denied",
    )
