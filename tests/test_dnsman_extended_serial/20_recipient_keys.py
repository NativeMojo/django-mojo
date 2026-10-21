"""Named protected recipient identity and least-privilege key issuance."""
import uuid
from contextlib import contextmanager
from types import SimpleNamespace

from testit import helpers as th


@contextmanager
def fixture():
    from mojo.apps.account.models import User, Group, ApiKey
    token = uuid.uuid4().hex
    actor = User.objects.create(username="testit_recipient_" + token, is_active=True, is_superuser=True)
    group = Group.objects.create(name="Original recipient " + token, uuid=uuid.uuid4().hex, is_active=True)
    request = SimpleNamespace(user=actor, bearer="bearer", api_key=None, user_api_key=None,
                              group_token=None, auth_token=None, group=None)
    try:
        yield request, actor, group
    finally:
        ApiKey.objects.filter(group=group).delete()
        Group.objects.filter(pk=group.pk).delete()
        User.objects.filter(pk=actor.pk).delete()


@th.django_unit_test()
def test_named_recipient_issues_only_protected_unlinked_group_key(opts):
    from mojo.apps.dnsman.services import recipient_keys as service
    from mojo.apps.account.models import ApiKey
    with fixture() as (request, actor, group):
        state = service.lookup(request, {"consumer_project_uuid": group.uuid})
        assert state["consumer_label"] == group.name and state["can_issue"], "Lookup must confirm the real original active hub Group"
        issued = service.issue(request, {"consumer_project_uuid": group.uuid, "expected_consumer_label": group.name,
                                         "label": "Test shared validation", "purpose": service.PURPOSE})
        key = ApiKey.objects.get(pk=issued["api_key"]["id"])
        assert key.group_id == group.pk and key.permissions == {service.PERMISSION: True}, "Named issuance must grant exactly federation to the selected Group"
        assert key.user_id is None and key.override_user is False, "Protected consumer keys must be unlinked and non-override"
        assert issued["token"] == key.get_token(), "Issuance must use the existing recoverable-token secret response contract"
        assert "token" not in state and "permissions" not in state, "Recipient lookup must not expose a token or general grant editor"
        assert ApiKey.objects.filter(group=group).count() == 1, "Named issuance must not create competing keys or grant trust implicitly"


@th.django_unit_test()
def test_recipient_rechecks_identity_activity_and_exact_payload(opts):
    from mojo.apps.dnsman.services import recipient_keys as service
    from mojo.apps.account.models import Group, ApiKey
    from mojo import errors as me
    with fixture() as (request, actor, group):
        payload = dict(consumer_project_uuid=group.uuid, expected_consumer_label=group.name, label="Test", purpose=service.PURPOSE)
        for delta in ({"permissions": {"groups": True}}, {"user": actor.pk}, {"override_user": True}, {"purpose": "other"}, {"label": ""}):
            with th.assert_raises(me.ValueException):
                service.issue(request, dict(payload, **delta))
        Group.objects.filter(pk=group.pk).update(name="Changed recipient")
        try:
            service.issue(request, payload)
            raise AssertionError("Changed recipient label was accepted")
        except me.ValueException as error:
            assert error.status == 409, "Recipient changes must require a fresh human confirmation"
        Group.objects.filter(pk=group.pk).update(is_active=False)
        with th.assert_raises(me.ValueException):
            service.lookup(request, {"consumer_project_uuid": group.uuid})
        assert not ApiKey.objects.filter(group=group).exists(), "Invalid or inactive recipient issuance must have no key side effect"


@th.django_unit_test()
def test_tenant_keys_and_stale_superusers_cannot_issue(opts):
    from mojo.apps.dnsman.services import recipient_keys as service
    from mojo.apps.account.models import User, ApiKey
    from mojo import errors as me
    with fixture() as (request, actor, group):
        payload = dict(consumer_project_uuid=group.uuid, expected_consumer_label=group.name, label="Test", purpose=service.PURPOSE)
        key, unused = ApiKey.create_for_group(group, "Tenant manager", permissions={"groups": True})
        request.api_key = key
        assert not key.can_change_permission(service.PERMISSION, True, request), "A key-backed tenant manager must not grant protected federation"
        with th.assert_raises(me.PermissionDeniedException):
            service.issue(request, payload)
        request.api_key = None
        User.objects.filter(pk=actor.pk).update(is_superuser=False, permissions={"admin": True, "groups": True})
        with th.assert_raises(me.PermissionDeniedException):
            service.issue(request, payload)
        User.objects.filter(pk=actor.pk).update(is_superuser=True, is_active=False)
        with th.assert_raises(me.PermissionDeniedException):
            service.issue(request, payload)
        assert ApiKey.objects.filter(group=group).count() == 1, "Denied issuance must not mint a successor credential"


@th.django_unit_test()
def test_inactive_parent_and_unknown_uuid_are_unavailable(opts):
    from mojo.apps.dnsman.services import recipient_keys as service
    from mojo.apps.account.models import Group
    from mojo import errors as me
    with fixture() as (request, actor, group):
        parent = Group.objects.create(name="Inactive parent", is_active=False)
        try:
            Group.objects.filter(pk=group.pk).update(parent=parent)
            for value in (group.uuid, uuid.uuid4().hex):
                try:
                    service.lookup(request, {"consumer_project_uuid": value})
                    raise AssertionError("Unavailable original recipient was resolved")
                except me.ValueException as error:
                    assert error.status == 404, "Inactive and missing recipients must share the safe unavailable response"
        finally:
            Group.objects.filter(pk=group.pk).update(parent=None)
            parent.delete()
