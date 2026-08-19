"""REST boundary for the Admin portal's Text messages (SMS) page.

    GET  /api/account/admin/messaging-sms/summary - system config, overrides,
                                                    verification state
    POST /api/account/admin/messaging-sms         - one body action:
                                                    save | test_connection |
                                                    send_test

The reads and operator actions need the phone-config tier. ``action: "save"``
targets the SYSTEM PhoneConfig row (group=None) — the row every OTP, MFA,
magic-login and password-reset SMS on the installation routes through — so it
additionally requires a literal superuser, checked in the body because
``requires_global_perms`` composes its list with OR and cannot express
"manage_phone_config AND is_superuser" (see aws/rest/capacity.py).

phonehub is optional: both endpoints answer with a clean not-installed
envelope instead of importing it when it is absent (D8, maestro #2189).
"""

from django.apps import apps

from mojo import decorators as md
from mojo import errors as merrors
from mojo.helpers import logit


PERMS = ("manage_phone_config", "manage_groups", "comms", "admin")


def _installed():
    return apps.is_installed("mojo.apps.phonehub")


def _not_installed():
    return {
        "schema_version": 1,
        "installed": False,
        "error": "Text messaging is not installed on this platform",
        "error_code": "not_installed",
    }


def _require_superuser(request):
    """The AND half of the system-row write gate.

    ``requires_global_perms`` already proved the phone-config tier. This
    proves the caller is a literal superuser, which the decorator cannot
    express — whoever writes the system row controls where every second
    factor on the installation is delivered.
    """
    if getattr(request.user, "is_superuser", False):
        return
    logit.error(
        f"messaging-sms save denied: "
        f"{getattr(request.user, 'username', request.user)} is not a superuser")
    raise merrors.PermissionDeniedException(
        "Changing the system text-message provider requires a superuser "
        "account in addition to manage_phone_config.")


@md.GET("account/admin/messaging-sms/summary")
@md.requires_global_perms(*PERMS)
def on_admin_sms_summary(request):
    if not _installed():
        return _not_installed()
    from mojo.apps.phonehub.services import admin_sms
    from mojo.apps.account.services import provider_setup
    report = admin_sms.summary()
    report["capabilities"] = {
        "view": True,
        "system_write": bool(request.user.is_superuser),
    }
    try:
        report["configuration_revision"] = provider_setup.sms_revision_token()
    except Exception as err:
        # A reader degrades where a writer fails closed: the page can render,
        # but a save with a None token will be refused until a reload works.
        logit.warning(f"messaging-sms revision token unavailable: "
                      f"{err.__class__.__name__}")
        report["configuration_revision"] = None
        report["revision_error"] = err.__class__.__name__
    return report


@md.POST("account/admin/messaging-sms")
@md.denies_key_backed_session()
@md.requires_fresh_auth(seconds=600)
@md.requires_global_perms(*PERMS)
def on_admin_sms_mutate(request):
    if not _installed():
        return _not_installed()
    from mojo.apps.phonehub.services import admin_sms
    action = request.DATA.get("action")
    if action == "save":
        _require_superuser(request)
        return admin_sms.save_config(request.user, request.DATA)
    if action == "test_connection":
        return admin_sms.test_connection(request.DATA.get("config_id"))
    if action == "send_test":
        return admin_sms.send_test(request.user, request.DATA.get("to_number"))
    raise merrors.ValueException(
        "action must be save, test_connection, or send_test")
