"""Member writes and synchronous receiver cleanup must commit together."""
from contextlib import contextmanager

from testit import helpers as th


@contextmanager
def _members(label):
    from mojo.apps.account.models import Group, GroupMember, User

    name = f"member_save_transaction_{label}"
    Group.objects.filter(name=name).delete()
    User.objects.filter(username=name).delete()
    group = Group.objects.create(name=name)
    user = User.objects.create(username=name)
    member = GroupMember.objects.create(
        group=group, user=user, permissions={"support": True})
    related = GroupMember.objects.create(group=group, user=user)
    try:
        yield member, related
    finally:
        group.delete()
        user.delete()


@contextmanager
def _cleanup_receiver(member, related, callbacks, fail):
    from django.db import transaction
    from django.db.models.signals import post_save
    from mojo.apps.account.models import GroupMember

    def cleanup(sender, instance, using, **kwargs):
        # The receiver is registered globally but touches only this test's row.
        if instance.pk != member.pk:
            return
        GroupMember.objects.using(using).filter(pk=related.pk).delete()
        transaction.on_commit(lambda: callbacks.append("committed"), using=using)
        if fail:
            raise RuntimeError("test cleanup failed")

    post_save.connect(cleanup, sender=GroupMember, weak=False)
    try:
        yield
    finally:
        post_save.disconnect(cleanup, sender=GroupMember)


@th.django_unit_test()
def test_member_receiver_failure_rolls_back_permission_and_cleanup(opts):
    from django.db import transaction
    from mojo.apps.account.models import GroupMember

    assert transaction.get_autocommit(), "Regression must start outside a transaction"
    with _members("failure") as (member, related):
        callbacks = []
        with _cleanup_receiver(member, related, callbacks, fail=True):
            try:
                member.remove_permission("support")
            except RuntimeError as error:
                assert str(error) == "test cleanup failed", "Unexpected receiver error"
            else:
                raise AssertionError("Receiver failure must propagate to the caller")
        member.refresh_from_db()
        assert member.permissions.get("support"), "Failed cleanup must roll back role removal"
        assert GroupMember.objects.filter(pk=related.pk).exists(), "Cleanup deletion must roll back"
        assert callbacks == [], "Failed save must discard after-commit callbacks"


@th.django_unit_test()
def test_member_atomic_save_commits_receiver_cleanup(opts):
    from mojo.apps.account.models import GroupMember

    with _members("success") as (member, related):
        callbacks = []
        with _cleanup_receiver(member, related, callbacks, fail=False):
            member.permissions = {}
            member.atomic_save()
        member.refresh_from_db()
        assert member.permissions == {}, "Role removal must persist"
        assert not GroupMember.objects.filter(pk=related.pk).exists(), "Cleanup must persist"
        assert callbacks == ["committed"], "Callback must run once after successful save"


@th.django_unit_test()
def test_member_save_preserves_positional_alias_and_outer_transaction(opts):
    from django.db import transaction
    from mojo.apps.account.models import GroupMember

    with _members("nested") as (member, related):
        callbacks = []
        alias = member._state.db
        with _cleanup_receiver(member, related, callbacks, fail=False):
            with transaction.atomic(using=alias):
                member.is_active = False
                member.save(False, False, alias, ["is_active"])
                assert callbacks == [], "Nested save must defer callbacks to outer commit"
        member.refresh_from_db()
        assert not member.is_active, "Positional using/update_fields must persist the requested field"
        assert member.permissions.get("support"), "update_fields must preserve unrelated fields"
        assert not GroupMember.objects.filter(pk=related.pk).exists(), "Nested cleanup must commit"
        assert callbacks == ["committed"], "Outer commit must publish callback once"
