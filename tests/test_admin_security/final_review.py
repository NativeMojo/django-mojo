"""Final security-review regressions for Admin Security compatibility."""

from datetime import timedelta
import json
import threading
import uuid

from django.db import connections
from django.db.models.signals import pre_save
from django.utils import timezone

from testit import helpers as th


PREFIX = f"admin-security-final-{uuid.uuid4().hex[:10]}"
PASSWORD = "AdminSecurityFinal##1"


def _operator():
    from mojo.apps.account.models import User

    user = User.objects.filter(username=f"{PREFIX}-operator").first()
    if user is None:
        user = User.objects.create_user(
            username=f"{PREFIX}-operator", email=f"{PREFIX}@example.test",
            password=PASSWORD)
    user.is_active = True
    user.add_permission("manage_security")
    user.add_permission("view_security")
    user.save()
    return user


def _expect_stale(callback, message):
    from mojo.apps.incident.services import admin_security

    try:
        callback()
    except admin_security.SecurityActionError as error:
        assert error.status == 409 and error.code == "stale_cursor", (
            f"{message}: wrong error {error.status}/{error.code}")
    else:
        assert False, message


@th.django_unit_test("mutable discovery snapshots hash actual ordered membership")
def test_snapshot_integrity_without_modified(opts):
    from mojo.apps.account.models import ApiKey, Group
    from mojo.apps.incident.models import MojoSecCase, RuleSet
    from mojo.apps.incident.services import admin_security

    group = Group.objects.create(
        name=f"{PREFIX}-snapshot", kind="organization")
    key, _token = ApiKey.create_for_group(
        group, f"{PREFIX}-snapshot-key",
        permissions={"view_security": True})
    authority = admin_security.SecurityAuthority(
        "group", "api_key", None, group_id=group.pk)
    now = timezone.now()
    cases = [MojoSecCase.objects.create(
        group=group, installation_key=key,
        first_seen=now - timedelta(minutes=index),
        last_seen=now - timedelta(minutes=index),
        window_start=now - timedelta(minutes=index + 5),
        window_end=now - timedelta(minutes=index),
        sensor_id=f"sensor-{index}", sensor_kind="web", family="request",
        correlation_key=f"{PREFIX}-case-{index}",
        window_key=f"{PREFIX}-window-{index}") for index in range(4)]
    equal_modified = now - timedelta(hours=1)
    MojoSecCase.objects.filter(pk__in=[row.pk for row in cases]).update(
        modified=equal_modified)
    first = admin_security.overview(
        {"sections": "cases", "limit": 2}, authority=authority)
    case_cursor = first["sections"]["cases"]["next_cursor"]
    assert case_cursor, "Case fixture did not produce a second page"
    MojoSecCase.objects.filter(pk=cases[-1].pk).update(
        last_seen=now - timedelta(seconds=15))
    _expect_stale(
        lambda: admin_security.overview(
            {"sections": "cases", "page_cursor": case_cursor},
            authority=authority),
        "a queryset last_seen update that omitted modified was not detected")

    rows = [RuleSet.objects.create(
        name=f"snapshot-{index}", category=f"{PREFIX}:snapshot:{index}",
        priority=index) for index in range(4)]
    RuleSet.objects.filter(pk__in=[row.pk for row in rows]).update(
        modified=equal_modified)
    first = admin_security.overview({"sections": "rules", "limit": 2})
    priority_cursor = first["sections"]["rules"]["next_cursor"]
    assert priority_cursor, "RuleSet fixture did not produce a second page"
    RuleSet.objects.filter(pk=rows[-1].pk).update(priority=0)
    _expect_stale(
        lambda: admin_security.overview(
            {"sections": "rules", "page_cursor": priority_cursor}),
        "an equal-timestamp priority update that omitted modified was not detected")

    stable = [RuleSet.objects.create(
        name=f"membership-{index}",
        category=f"{PREFIX}:membership:{index}", priority=20 + index)
        for index in range(4)]
    RuleSet.objects.filter(pk__in=[row.pk for row in stable]).update(
        modified=equal_modified)
    first = admin_security.overview({"sections": "rules", "limit": 2})
    membership_cursor = first["sections"]["rules"]["next_cursor"]
    assert membership_cursor, "membership fixture did not produce a second page"
    stable[0].delete()
    replacement = RuleSet.objects.create(
        name="replacement", category=f"{PREFIX}:membership:replacement",
        priority=0)
    RuleSet.objects.filter(pk=replacement.pk).update(
        created=equal_modified, modified=equal_modified)
    _expect_stale(
        lambda: admin_security.overview(
            {"sections": "rules", "page_cursor": membership_cursor}),
        "a simultaneous insert/delete with unchanged count/max timestamp was not detected")


@th.django_unit_test("direct Rule writes and governed replacement cannot deadlock")
def test_parent_first_rule_lock_order(opts):
    from mojo.apps.incident.models import Rule, RuleSet
    from mojo.apps.incident.services import admin_security, rule_validation

    actor = _operator()
    parent = RuleSet.objects.create(
        name="governed lock target", category=f"{PREFIX}:lock",
        is_active=False, metadata=rule_validation.mark_governed())
    child = Rule.objects.create(
        parent=parent, name="old", index=0, field_name="level",
        comparator=">=", value="1", value_type="int")
    initial_revision = parent.modified
    direct_at_save = threading.Event()
    release_direct = threading.Event()
    governed_has_parent = threading.Event()
    direct_thread_name = f"{PREFIX}-direct"
    governed_thread_name = f"{PREFIX}-governed"

    def pause_direct_save(sender, instance, **kwargs):
        if (instance.pk == child.pk and
                threading.current_thread().name == direct_thread_name):
            direct_at_save.set()
            if not release_direct.wait(timeout=8):
                raise TimeoutError("governed replacement never reached the lock race")

    def pause_governed_save(sender, instance, **kwargs):
        if (instance.pk == parent.pk and
                threading.current_thread().name == governed_thread_name):
            governed_has_parent.set()
            if not release_direct.wait(timeout=8):
                raise TimeoutError("direct Rule save was not released")

    replacement = {
        "name": "governed replacement", "category": parent.category,
        "priority": 50, "bundle_minutes": 30,
        "bundle_by": 4,
        "bundle_by_rule_set": parent.bundle_by_rule_set,
        "match_by": parent.match_by,
        "handlers": [{"type": "notify", "permission": "manage_security"}],
        "rules": [{"name": "new", "field": "level",
                   "operator": ">=", "value": 9, "value_type": "int"}],
        "is_active": False,
    }

    def direct_write():
        try:
            current = Rule.objects.get(pk=child.pk)
            current.value = "7"
            current.save(update_fields=["value"])
            return "saved"
        finally:
            connections.close_all()

    def governed_replace():
        try:
            for attempt in range(2):
                current = RuleSet.objects.get(pk=parent.pk)
                payload = {
                    "action": "ruleset.replace", "ruleset_id": parent.pk,
                    "expected_modified": current.modified.isoformat(),
                    "confirm": f"REPLACE RULESET {parent.pk}",
                    "ruleset": replacement,
                }
                try:
                    admin_security.apply_action(payload, actor)
                    return "replaced"
                except admin_security.SecurityActionError as error:
                    if error.code != "stale_revision" or attempt:
                        raise
            raise AssertionError("governed replacement exhausted its retry")
        finally:
            connections.close_all()

    pre_save.connect(
        pause_direct_save, sender=Rule, weak=False,
        dispatch_uid=f"{PREFIX}-parent-first")
    pre_save.connect(
        pause_governed_save, sender=RuleSet, weak=False,
        dispatch_uid=f"{PREFIX}-governed-parent")
    errors = []
    results = []

    def run(target):
        try:
            results.append(target())
        except Exception as error:
            errors.append(error)

    direct_thread = threading.Thread(
        target=run, args=(direct_write,), name=direct_thread_name, daemon=True)
    governed_thread = threading.Thread(
        target=run, args=(governed_replace,), name=governed_thread_name,
        daemon=True)
    try:
        direct_thread.start()
        assert direct_at_save.wait(timeout=5), (
            "direct Rule write never reached its database save")
        governed_thread.start()
        governed_has_parent.wait(timeout=1)
        release_direct.set()
        direct_thread.join(timeout=10)
        governed_thread.join(timeout=10)
    finally:
        release_direct.set()
        pre_save.disconnect(
            sender=Rule, dispatch_uid=f"{PREFIX}-parent-first")
        pre_save.disconnect(
            sender=RuleSet, dispatch_uid=f"{PREFIX}-governed-parent")
    assert not direct_thread.is_alive() and not governed_thread.is_alive(), (
        "concurrent Rule write/replacement exceeded the bounded timeout")
    assert not errors, f"parent/child lock order raised under concurrency: {errors!r}"
    assert sorted(results) == ["replaced", "saved"], results
    parent.refresh_from_db()
    children = list(parent.rules.order_by("index", "pk"))
    assert parent.modified != initial_revision, (
        "aggregate revision did not advance across concurrent writes")
    assert len(children) == 1 and children[0].value == "9", (
        "governed replacement did not leave one complete replacement aggregate")


@th.django_unit_test("secret scrubbing distinguishes auth context from operations")
def test_contextual_secret_scrubbing(opts):
    from mojo.apps.incident.services import admin_security_transport as transport

    evidence = {
        "session": {"state": "failed"},
        "mfa": "enabled",
        "requires_mfa": True,
        "worker": "session=worker-12 mfa=enabled",
        "headers": {
            "Cookie": "session=header-secret; mfa=header-mfa; theme=dark"},
        "cookies": {"session": "cookie-secret", "mfa": "cookie-mfa",
                    "theme": "dark"},
        "credentials": {"session": "credential-session",
                        "mfa": "credential-mfa", "username": "operator"},
        "log": ("request rejected api_secret=free-api-secret "
                "id_token:free-id-token "
                "X-Amz-Security-Token=free-aws-token after validation"),
    }
    rendered = transport.scrub(evidence)
    text = json.dumps(rendered, sort_keys=True)
    for secret in (
            "header-secret", "header-mfa", "cookie-secret", "cookie-mfa",
            "credential-session", "credential-mfa", "free-api-secret",
            "free-id-token", "free-aws-token"):
        assert secret not in text, f"authentication secret leaked: {secret}"
    assert rendered["session"] == {"state": "failed"}, (
        "operational session state was redacted outside auth context")
    assert rendered["mfa"] == "enabled", (
        "operational MFA state was redacted outside auth context")
    assert rendered["requires_mfa"] is True, (
        "an operational MFA capability flag was treated as a secret")
    assert rendered["worker"] == "session=worker-12 mfa=enabled", (
        "operational session/MFA text was redacted outside auth context")
    for retained in ("theme=dark", "operator", "request rejected",
                     "after validation"):
        assert retained in text, f"scrubber removed operational context: {retained}"


@th.django_unit_test("compatibility endpoints dispatch only exact REST methods")
def test_compatibility_method_dispatch(opts):
    from mojo.apps.incident.models import Rule, RuleSet

    user = _operator()
    assert opts.client.login(user.email, PASSWORD), "operator login failed"
    base = "/api/incident/event/ruleset"
    created = opts.client.post(base, {
        "name": "method target", "category": f"{PREFIX}:method"})
    assert created.status_code == 200, created.response
    row = RuleSet.objects.get(category=f"{PREFIX}:method")
    read = opts.client.get(f"{base}/{row.pk}")
    assert read.status_code == 200, read.response
    put = opts.client.put(f"{base}/{row.pk}", {"name": "put update"})
    patch = opts.client._make_request(
        "PATCH", f"{base}/{row.pk}", json={"name": "patch update"})
    assert put.status_code == 200 and patch.status_code == 200, (
        put.response, patch.response)
    row.refresh_from_db()
    assert row.name == "patch update", "detail PUT/PATCH did not update"

    forbidden = (
        opts.client.put(base, {"name": "collection put"}),
        opts.client._make_request(
            "PATCH", base, json={"name": "collection patch"}),
        opts.client.post(f"{base}/{row.pk}", {"name": "detail post"}),
        opts.client.delete(base),
        opts.client._make_request("TRACE", base),
    )
    assert [response.status_code for response in forbidden] == [405] * 5, (
        "unsupported compatibility methods must return 405")
    row.refresh_from_db()
    assert row.name == "patch update", (
        "an unsupported method mutated the compatibility policy")

    child_base = f"{base}/rule"
    child_created = opts.client.post(child_base, {
        "parent": row.pk, "name": "method child", "field_name": "level",
        "comparator": ">=", "value": "1", "value_type": "int"})
    assert child_created.status_code == 200, child_created.response
    child = Rule.objects.get(parent=row, name="method child")
    child_read = opts.client.get(f"{child_base}/{child.pk}")
    child_put = opts.client.put(
        f"{child_base}/{child.pk}", {"value": "2"})
    child_patch = opts.client._make_request(
        "PATCH", f"{child_base}/{child.pk}", json={"value": "3"})
    assert [response.status_code for response in (
        child_read, child_put, child_patch)] == [200] * 3, (
        "Rule detail GET/PUT/PATCH did not use generic dispatch")
    child_forbidden = (
        opts.client.put(child_base, {"parent": row.pk, "value": "4"}),
        opts.client.post(f"{child_base}/{child.pk}", {"value": "5"}),
        opts.client.delete(child_base),
    )
    assert [response.status_code for response in child_forbidden] == [405] * 3, (
        "unsupported Rule compatibility methods must return 405")
    child.refresh_from_db()
    assert child.value == "3", "an unsupported method mutated the Rule"
    child_deleted = opts.client.delete(f"{child_base}/{child.pk}")
    assert child_deleted.status_code == 200, child_deleted.response
    assert not Rule.objects.filter(pk=child.pk).exists(), (
        "Rule detail DELETE did not delete the compatibility child")

    deleted = opts.client.delete(f"{base}/{row.pk}")
    assert deleted.status_code == 200, deleted.response
    assert not RuleSet.objects.filter(pk=row.pk).exists(), (
        "detail DELETE did not delete the compatibility policy")
