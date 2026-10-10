"""Maestro item #6226, Step 5 — ALLOW_PHONE_CHANGE=False holds on the account
save, end to end.

The rule itself is covered in tests/test_auth/phone_number_guard.py with the
setting passed in. This is the same thing with the setting really off on the
server, which is why it lives in the serial package.
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

PWORD = "po##mojo99Off"
OWNER_PHONE = "+15550006239"
FIRST_PHONE = "+15550006240"
TARGET_PHONE = "+15550006241"
USERS = {
    "po_owner": OWNER_PHONE,
    "po_first": None,
    "po_target": TARGET_PHONE,
    "po_admin": None,
}


def _fresh(pk):
    from mojo.apps.account.models import User
    return User.objects.get(pk=pk)


@th.django_unit_setup()
def setup_phone_change_off(opts):
    from mojo.apps.account.models import User, Setting

    Setting.objects.filter(key="ALLOW_PHONE_CHANGE", group=None).delete()
    User.objects.filter(username__in=list(USERS)).delete()
    User.objects.filter(phone_number__in=[OWNER_PHONE, FIRST_PHONE, TARGET_PHONE]).update(phone_number=None)
    for name, phone in USERS.items():
        user = User(username=name, display_name=name, email=f"{name}@example.com")
        user.phone_number = phone
        user.save()
        user.is_email_verified = True
        user.is_phone_verified = bool(phone)
        user.save_password(PWORD)
        user.remove_all_permissions()
        if name == "po_admin":
            user.add_permission(["manage_users"])
        setattr(opts, f"{name}_id", user.pk)


@th.django_unit_test("phone changes off: the owner can't clear or replace the number on file")
def test_owner_cannot_clear_or_replace(opts):
    pk = opts.po_owner_id
    with th.server_settings(ALLOW_PHONE_CHANGE=False):
        assert_true(opts.client.login("po_owner", PWORD), "the user must be able to log in")
        cleared = opts.client.post("/api/user/me", {"phone_number": ""})
        replaced = opts.client.post("/api/user/me", {"phone_number": FIRST_PHONE})
        opts.client.logout()
    assert_eq(cleared.status_code, 403, f"with changes off clearing must be refused, got {cleared.status_code}")
    assert_eq(replaced.status_code, 403, f"with changes off replacing must be refused, got {replaced.status_code}")
    user = _fresh(pk)
    assert_eq(user.phone_number, OWNER_PHONE, "the number on file must be unchanged")
    assert_true(user.is_phone_verified, "a refused change must leave the number verified")


@th.django_unit_test("phone changes off: a first number and an admin still work")
def test_first_number_and_admin_still_work(opts):
    with th.server_settings(ALLOW_PHONE_CHANGE=False):
        assert_true(opts.client.login("po_first", PWORD), "the user must be able to log in")
        first = opts.client.post("/api/user/me", {"phone_number": FIRST_PHONE})
        opts.client.logout()
        assert_true(opts.client.login("po_admin", PWORD), "the admin must be able to log in")
        admin = opts.client.post(f"/api/user/{opts.po_target_id}", {"phone_number": ""})
        opts.client.logout()
    assert_eq(first.status_code, 200, f"a first number must be accepted with changes off, got {first.status_code}")
    assert_eq(_fresh(opts.po_first_id).phone_number, FIRST_PHONE, "the first number must be stored")
    assert_eq(admin.status_code, 200, f"an admin must still be able to clear a number, got {admin.status_code}")
    assert_true(not _fresh(opts.po_target_id).phone_number, "the admin's clear must take effect")
