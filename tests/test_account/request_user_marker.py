"""Maestro item #9125 — a signed-out visitor is not a request user.

The signed-out user (ANONYMOUS_USER) is an objict, which answers every
attribute, so the old `hasattr(user, "is_request_user")` test called it a real
user. Code that trusted that test ran user-only logic for strangers: a group
membership lookup handed it to the ORM and crashed with a 500.

A user identity now needs the marker AND to be signed in. The system stand-in
request (system_request) is signed in by construction and keeps counting:
server code relies on it to pass permission checks on its own saves.
"""
from testit import helpers as th

TESTIT_TIER = "bug"

PREFIX = "rum_"


@th.django_unit_setup()
def setup_request_user_marker(opts):
    from mojo.apps.account.models import Group, User
    User.objects.filter(email__startswith=PREFIX).delete()
    Group.objects.filter(name__startswith=PREFIX).delete()
    email = f"{PREFIX}person@account.test"
    opts.user_id = User.objects.create_user(username=email, email=email, password="Rum##marker99").pk
    opts.group_id = Group.objects.create(name=f"{PREFIX}group", kind="organization").pk


@th.django_unit_test("#9125: a signed-out request is not a request user")
def test_signed_out_is_not_a_request_user(opts):
    from objict import objict
    from mojo.apps.account.models import User
    from mojo.helpers import request as request_helpers
    from mojo.helpers.request import is_request_user
    from mojo.middleware.mojo import ANONYMOUS_USER
    from mojo.models.rest import system_request

    assert not is_request_user(objict(user=ANONYMOUS_USER)), \
        "a signed-out request must not count as a request user"
    assert not request_helpers.is_signed_in_user(ANONYMOUS_USER), \
        "the signed-out user must not count as a signed-in user"
    assert not is_request_user(objict(user=None)), "a request with no user must not count as a request user"

    person = User.objects.get(pk=opts.user_id)
    assert is_request_user(objict(user=person)), "a real User must still count as a request user"
    assert is_request_user(system_request()), \
        "the system stand-in request must still count: server code relies on it passing its own checks"


@th.django_unit_test("#9125: group membership checks refuse the signed-out user instead of crashing")
def test_group_checks_refuse_signed_out(opts):
    from mojo.apps.account.models import Group
    from mojo.middleware.mojo import ANONYMOUS_USER

    group = Group.objects.get(pk=opts.group_id)
    assert group.get_member_for_user(ANONYMOUS_USER, check_parents=True) is None, \
        "a signed-out user has no membership anywhere"
    assert group.user_has_permission(ANONYMOUS_USER, ["manage_group"], check_user=False) is False, \
        "a signed-out user holds no member grant"
