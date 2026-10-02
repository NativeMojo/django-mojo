"""Maestro item #6226, Steps 5 and 6 — a phone number on file can't be swapped
by clearing it first, and removing a verified number is not silent.

The account save always let its owner clear the phone number and set a first
one, and sent a replacement through the phone change flow. Clear-then-set was
therefore a replacement with no check at all, and it worked even with
ALLOW_PHONE_CHANGE turned off.

Step 5: with ALLOW_PHONE_CHANGE off, a non-admin can neither clear nor replace
a number on file. A first number, and an admin, still work. The rule takes the
setting as a parameter here, so no server setting is touched; the end-to-end
case with the setting really off is in tests/test_user_mgmt_extended_serial.

Step 6: where changes are allowed, removing a VERIFIED number files a
security event and sends a notice to the account's email.
"""
import uuid

from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TESTIT_TIER = "core"

PWORD = "pg##mojo99Guard"
OWNER_PHONE = "+15550006235"
FIRST_PHONE = "+15550006236"
OTHER_PHONE = "+15550006237"
NOTICE_PHONE = "+15550006238"
USERS = {
    "pg_owner": OWNER_PHONE,
    "pg_notice": NOTICE_PHONE,
}


def _new_ip():
    octets = uuid.uuid4().int
    return "10.%d.%d.%d" % ((octets >> 16) & 0xFF, (octets >> 8) & 0xFF, octets & 0xFF)


def _fresh(pk):
    from mojo.apps.account.models import User
    return User.objects.get(pk=pk)


def _events(pk):
    from mojo.apps.incident.models.event import Event
    return Event.objects.filter(uid=pk, category="phone:removed").count()


class _Sender:
    """Stands in for user.send_template_email."""

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def __call__(self, template_name, context=None, **kwargs):
        self.calls.append(dict(template_name=template_name, context=context or {}))
        if self.fail:
            raise RuntimeError("mail provider down")
        return True


@th.django_unit_setup()
def setup_phone_number_guard(opts):
    from mojo.apps.account.models import User
    from testit.client import RestClient

    opts.pg = RestClient(opts.client.host)
    opts.pg.headers["X-Real-IP"] = _new_ip()
    phones = [OWNER_PHONE, FIRST_PHONE, OTHER_PHONE, NOTICE_PHONE]
    User.objects.filter(username__in=list(USERS)).delete()
    User.objects.filter(phone_number__in=phones).update(phone_number=None)
    for name, phone in USERS.items():
        user = User(username=name, display_name=name, email=f"{name}@example.com")
        user.phone_number = phone
        user.save()
        user.is_email_verified = True
        user.is_phone_verified = True
        user.save_password(PWORD)
        user.remove_all_permissions()
        setattr(opts, f"{name}_id", user.pk)


def _rule(pk, new_phone, old_phone, admin_caller, allow_change):
    """What the save's rule says about this change. None means allowed."""
    from mojo import errors as merrors

    user = _fresh(pk)
    user.phone_number = new_phone
    try:
        user.check_phone_number_change(old_phone, admin_caller, allow_change=allow_change)
    except merrors.PermissionDeniedException as err:
        return err
    return None


@th.django_unit_test("phone number: with changes off, a non-admin can't clear or replace a number on file")
def test_rule_with_changes_off(opts):
    pk = opts.pg_owner_id

    refused = _rule(pk, None, OWNER_PHONE, False, False)
    assert_true(refused is not None, "with changes off a non-admin must not be able to clear the number on file")
    assert_eq(refused.reason, "Phone number change is not allowed", "the refusal must say changes are off")

    refused = _rule(pk, OTHER_PHONE, OWNER_PHONE, False, False)
    assert_true(refused is not None, "with changes off a non-admin must not be able to replace the number on file")
    assert_eq(refused.reason, "Phone number change is not allowed",
              "with changes off the refusal must not point at a change flow that is also off")

    assert_true(_rule(pk, FIRST_PHONE, None, False, False) is None,
                "a first number must still be accepted with changes off")
    assert_true(_rule(pk, None, OWNER_PHONE, True, False) is None,
                "an admin must still be able to clear a number with changes off")
    assert_true(_rule(pk, OTHER_PHONE, OWNER_PHONE, True, False) is None,
                "an admin must still be able to replace a number with changes off")


@th.django_unit_test("phone number: with changes on, the save behaves as before")
def test_rule_with_changes_on(opts):
    pk = opts.pg_owner_id

    assert_true(_rule(pk, None, OWNER_PHONE, False, True) is None,
                "with changes on the owner must still be able to clear the number")
    assert_true(_rule(pk, FIRST_PHONE, None, False, True) is None,
                "with changes on a first number must still be accepted")
    refused = _rule(pk, OTHER_PHONE, OWNER_PHONE, False, True)
    assert_true(refused is not None, "a replacement must still go through the phone change flow")
    assert_eq(refused.reason, "Use the phone change flow to update an existing phone number",
              "the refusal must still point at the change flow")


@th.django_unit_test("phone number over HTTP: clearing a verified number works, and is recorded")
def test_clearing_a_verified_number_is_recorded(opts):
    pk = opts.pg_owner_id
    before = _events(pk)
    assert_true(opts.pg.login("pg_owner", PWORD), "the user must be able to log in")

    resp = opts.pg.post("/api/user/me", {"phone_number": ""})
    assert_eq(resp.status_code, 200, f"with changes on the owner must be able to clear the number, "
                                     f"got {resp.status_code}: {opts.pg.last_response.body}")
    user = _fresh(pk)
    assert_true(not user.phone_number, "the number must be gone")
    assert_true(not user.is_phone_verified, "an account with no number must not read as phone-verified")
    assert_eq(_events(pk), before + 1, "removing a verified number must file one phone:removed event")

    resp = opts.pg.post("/api/user/me", {"phone_number": FIRST_PHONE})
    assert_eq(resp.status_code, 200, f"a first number must be accepted, "
                                     f"got {resp.status_code}: {opts.pg.last_response.body}")
    user = _fresh(pk)
    assert_eq(user.phone_number, FIRST_PHONE, "the first number must be stored")
    assert_true(not user.is_phone_verified, "a number set through the save must start unverified")

    resp = opts.pg.post("/api/user/me", {"phone_number": OTHER_PHONE})
    assert_eq(resp.status_code, 403, f"a replacement through the save must be refused, got {resp.status_code}")
    assert_eq(_fresh(pk).phone_number, FIRST_PHONE, "a refused replacement must leave the number as it was")

    resp = opts.pg.post("/api/user/me", {"phone_number": ""})
    assert_eq(resp.status_code, 200, f"clearing an unverified number must work, got {resp.status_code}")
    assert_eq(_events(pk), before + 1, "removing an UNVERIFIED number must not file a phone:removed event")


@th.django_unit_test("phone number: the removal notice goes to the account email and never fails the save")
def test_removal_notice(opts):
    from mojo.apps.account.models import User

    user = _fresh(opts.pg_notice_id)
    sender = _Sender()
    user.notify_phone_removed(NOTICE_PHONE, send=sender)
    assert_eq(len(sender.calls), 1, "one notice must be sent")
    assert_eq(sender.calls[0]["template_name"], "phone_removed_notify", "the notice must use its own template")
    assert_eq(sender.calls[0]["context"].get("phone_last4"), NOTICE_PHONE[-4:],
              "the notice must name the number by its last four digits only")
    assert_true(NOTICE_PHONE not in str(sender.calls[0]["context"]),
                "the full number must not be put in the email")

    failing = _Sender(fail=True)
    user.notify_phone_removed(NOTICE_PHONE, send=failing)
    assert_eq(len(failing.calls), 1, "a failing mail provider must be tried once and must not raise")

    no_email = User(username="pg_no_email", email=None)
    silent = _Sender()
    no_email.pk = user.pk
    no_email.notify_phone_removed(NOTICE_PHONE, send=silent)
    assert_eq(len(silent.calls), 0, "an account with no email has nowhere to send the notice")


@th.django_unit_test("phone number: the removal notice template ships with the framework")
def test_removal_notice_template_ships(opts):
    from mojo.apps.aws.services.email_templates import load_shipped_templates

    shipped = {row["name"]: row for row in load_shipped_templates()}
    assert_true("phone_removed_notify" in shipped, "the phone_removed_notify template must ship as a seed")
    template = shipped["phone_removed_notify"]
    for part in ("subject_template", "text_template", "html_template"):
        assert_true(template[part].strip(), f"the template's {part} must not be empty")
    assert_true("phone_last4" in template["text_template"], "the text must name the number by its last four digits")
    assert_true("phone_last4" in template["html_template"], "the html must name the number by its last four digits")
