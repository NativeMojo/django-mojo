from testit import helpers as th


def _build_tree(parent_kind="org", child_kind="location"):
    """Build a parent group with three active children of one kind plus one
    inactive child and one mismatched-kind child for filter coverage. Returns
    (parent, matches, kiosk, inactive)."""
    from mojo.apps.account.models import Group

    Group.objects.filter(name__startswith="fanout_test_").delete()

    parent = Group(name="fanout_test_parent", kind=parent_kind, is_active=True)
    parent.save()

    matches = []
    for i in range(3):
        g = Group(name=f"fanout_test_child_{i}", kind=child_kind, parent=parent, is_active=True)
        g.save()
        matches.append(g)

    other = Group(name="fanout_test_kiosk", kind="kiosk", parent=parent, is_active=True)
    other.save()

    inactive = Group(name="fanout_test_inactive", kind=child_kind, parent=parent, is_active=False)
    inactive.save()

    return parent, matches, other, inactive


def _seed(slug, group, count):
    """Record ``count`` events at the current time into the group's metric
    account. Default-time recording lines up with default-range fetch — both
    apply METRICS_TIMEZONE normalization on `now`."""
    from mojo.apps import metrics
    account = f"group-{group.pk}"
    metrics.delete_metrics_slug(slug, account=account)
    for _ in range(count):
        metrics.record(slug, account=account, min_granularity="hours")


@th.django_unit_test()
def test_fanout_sum_correctness(opts):
    from mojo.apps.metrics.rest.helpers import fetch_group_fanout

    parent, matches, _, _ = _build_tree()
    counts = [3, 5, 7]
    for g, c in zip(matches, counts):
        _seed("fan_views", g, c)

    result = fetch_group_fanout(
        parent.pk, "location", ["fan_views"],
        granularity="hours", with_labels=True,
    )
    series = result["data"]["fan_views"]
    assert sum(series) == sum(counts), \
        f"Expected total {sum(counts)} across children, got {sum(series)}: {series}"


@th.django_unit_test()
def test_fanout_kind_filter_excludes_mismatched(opts):
    from mojo.apps.metrics.rest.helpers import fetch_group_fanout

    parent, matches, kiosk, _ = _build_tree()
    for g in matches:
        _seed("fan_kf", g, 2)
    _seed("fan_kf", kiosk, 99)

    result = fetch_group_fanout(
        parent.pk, "location", ["fan_kf"],
        granularity="hours", with_labels=True,
    )
    series = result["data"]["fan_kf"]
    assert sum(series) == 6, \
        f"Expected 6 (2*3 matches, kiosk excluded), got {sum(series)}: {series}"


@th.django_unit_test()
def test_fanout_excludes_inactive_children(opts):
    from mojo.apps.metrics.rest.helpers import fetch_group_fanout

    parent, matches, _, inactive = _build_tree()
    for g in matches:
        _seed("fan_inact", g, 1)
    _seed("fan_inact", inactive, 50)

    result = fetch_group_fanout(
        parent.pk, "location", ["fan_inact"],
        granularity="hours", with_labels=True,
    )
    series = result["data"]["fan_inact"]
    assert sum(series) == 3, \
        f"Expected 3 (3 active * 1, inactive excluded), got {sum(series)}: {series}"


@th.django_unit_test()
def test_fanout_recursive_descendants(opts):
    from mojo.apps.account.models import Group
    from mojo.apps.metrics.rest.helpers import fetch_group_fanout

    parent, matches, _, _ = _build_tree()

    grand = Group(name="fanout_test_grand", kind="location", parent=matches[0], is_active=True)
    grand.save()

    for g in matches:
        _seed("fan_rec", g, 2)
    _seed("fan_rec", grand, 4)

    result = fetch_group_fanout(
        parent.pk, "location", ["fan_rec"],
        granularity="hours", with_labels=True,
    )
    series = result["data"]["fan_rec"]
    assert sum(series) == 10, \
        f"Expected 10 (3*2 children + 4 grandchild), got {sum(series)}: {series}"


@th.django_unit_test()
def test_fanout_empty_children_returns_zero_filled(opts):
    from mojo.apps.account.models import Group
    from mojo.apps.metrics.rest.helpers import fetch_group_fanout

    Group.objects.filter(name__startswith="fanout_empty_").delete()
    parent = Group(name="fanout_empty_parent", kind="org", is_active=True)
    parent.save()

    result = fetch_group_fanout(
        parent.pk, "location", ["fan_empty"],
        granularity="hours", with_labels=True,
    )
    series = result["data"]["fan_empty"]
    assert all(v == 0 for v in series), \
        f"Expected all zeros for no children, got non-zero values: {series}"
    assert isinstance(result["labels"], list) and len(result["labels"]) == len(series), \
        f"Expected labels and series same length, labels={result['labels']}, series={series}"


@th.django_unit_test()
def test_fanout_cap_exceeded_in_process(opts):
    """Direct in-process call must enforce the cap when settings are patched
    via the django settings system. Uses ``settings.get_static`` lookup, which
    reads live."""
    from mojo.apps.metrics.rest.helpers import fetch_group_fanout
    import mojo.errors

    parent, matches, _, _ = _build_tree()
    for g in matches:
        _seed("fan_cap", g, 1)

    from django.conf import settings as dj_settings
    original = getattr(dj_settings, "METRICS_FANOUT_MAX_CHILDREN", None)
    dj_settings.METRICS_FANOUT_MAX_CHILDREN = 2
    try:
        raised = False
        try:
            fetch_group_fanout(
                parent.pk, "location", ["fan_cap"],
                granularity="hours", with_labels=True,
            )
        except mojo.errors.ValueException as e:
            raised = True
            assert "METRICS_FANOUT_MAX_CHILDREN" in str(e.reason), \
                f"Cap error must reference setting name, got: {e.reason}"
        assert raised, "fetch_group_fanout should have raised ValueException for cap exceeded"
    finally:
        if original is None:
            del dj_settings.METRICS_FANOUT_MAX_CHILDREN
        else:
            dj_settings.METRICS_FANOUT_MAX_CHILDREN = original


@th.unit_test()
def test_fanout_permission_member_succeeds(opts):
    from mojo.apps.account.models import User, Group
    from mojo.apps.account.models.member import GroupMember
    from mojo.apps import metrics

    user_name = "fanout_member"
    pword = "metrics##mojo99"

    user = User.objects.filter(username=user_name).last()
    if user is None:
        user = User(username=user_name, email=f"{user_name}@example.com")
        user.save()
    user.is_email_verified = True
    user.save_password(pword)
    user.remove_all_permissions()

    Group.objects.filter(name__startswith="fanout_perm_").delete()
    parent = Group(name="fanout_perm_parent", kind="org", is_active=True)
    parent.save()
    child = Group(name="fanout_perm_child", kind="location", parent=parent, is_active=True)
    child.save()

    GroupMember.objects.filter(user=user, group=parent).delete()
    ms = GroupMember(user=user, group=parent, is_active=True)
    ms.save()
    ms.add_permission("view_metrics")

    account = f"group-{child.pk}"
    metrics.delete_metrics_slug("fan_perm", account=account)
    for _ in range(4):
        metrics.record("fan_perm", account=account, min_granularity="hours")

    assert opts.client.login(user_name, pword), "parent member login failed"
    resp = opts.client.get(
        "/api/metrics/fetch",
        params=dict(slug="fan_perm", account=f"group-{parent.pk}",
                    child_kind="location", with_labels=True,
                    granularity="hours"),
    )
    assert resp.status_code == 200, \
        f"member fan-out expected 200, got {resp.status_code}: {resp.body}"
    series = resp.response.data.data.fan_perm
    assert sum(series) == 4, f"Expected sum 4, got {sum(series)}: {series}"


@th.unit_test()
def test_fanout_permission_outsider_denied(opts):
    from mojo.apps.account.models import User, Group

    user_name = "fanout_outsider"
    pword = "metrics##mojo99"

    user = User.objects.filter(username=user_name).last()
    if user is None:
        user = User(username=user_name, email=f"{user_name}@example.com")
        user.save()
    user.is_email_verified = True
    user.save_password(pword)
    user.remove_all_permissions()

    Group.objects.filter(name__startswith="fanout_outs_").delete()
    parent = Group(name="fanout_outs_parent", kind="org", is_active=True)
    parent.save()
    child = Group(name="fanout_outs_child", kind="location", parent=parent, is_active=True)
    child.save()

    assert opts.client.login(user_name, pword), "outsider login failed"
    resp = opts.client.get(
        "/api/metrics/fetch",
        params=dict(slug="fan_outs", account=f"group-{parent.pk}",
                    child_kind="location", with_labels=True),
    )
    assert resp.status_code == 403, \
        f"outsider fan-out expected 403, got {resp.status_code}: {resp.body}"


@th.unit_test()
def test_fanout_permission_ancestor_member_succeeds(opts):
    """Member of grandparent group can fan-out across descendants of one of its
    children. Exercises the parent-chain walk in
    Group.user_has_permission(check_parents=True)."""
    from mojo.apps.account.models import User, Group
    from mojo.apps.account.models.member import GroupMember
    from mojo.apps import metrics

    user_name = "fanout_grandmember"
    pword = "metrics##mojo99"

    user = User.objects.filter(username=user_name).last()
    if user is None:
        user = User(username=user_name, email=f"{user_name}@example.com")
        user.save()
    user.is_email_verified = True
    user.save_password(pword)
    user.remove_all_permissions()

    Group.objects.filter(name__startswith="fanout_anc_").delete()
    grand = Group(name="fanout_anc_grand", kind="org", is_active=True)
    grand.save()
    parent = Group(name="fanout_anc_parent", kind="region", parent=grand, is_active=True)
    parent.save()
    child = Group(name="fanout_anc_child", kind="location", parent=parent, is_active=True)
    child.save()

    GroupMember.objects.filter(user=user, group=grand).delete()
    ms = GroupMember(user=user, group=grand, is_active=True)
    ms.save()
    ms.add_permission("view_metrics")

    account = f"group-{child.pk}"
    metrics.delete_metrics_slug("fan_anc", account=account)
    for _ in range(2):
        metrics.record("fan_anc", account=account, min_granularity="hours")

    assert opts.client.login(user_name, pword), "grandparent member login failed"
    resp = opts.client.get(
        "/api/metrics/fetch",
        params=dict(slug="fan_anc", account=f"group-{parent.pk}",
                    child_kind="location", with_labels=True,
                    granularity="hours"),
    )
    assert resp.status_code == 200, \
        f"ancestor member fan-out expected 200, got {resp.status_code}: {resp.body}"


@th.django_unit_test()
def test_fanout_with_labels_parity(opts):
    """Labels from a fan-out fetch match labels from a single-account fetch
    over the same granularity."""
    from mojo.apps import metrics
    from mojo.apps.metrics.rest.helpers import fetch_group_fanout

    parent, matches, _, _ = _build_tree()
    for g in matches:
        _seed("fan_lbl", g, 1)

    fanout = fetch_group_fanout(
        parent.pk, "location", ["fan_lbl"],
        granularity="hours", with_labels=True,
    )
    single = metrics.fetch(
        ["fan_lbl"], granularity="hours",
        account=f"group-{matches[0].pk}", with_labels=True,
    )
    assert fanout["labels"] == single["labels"], \
        f"Fan-out labels {fanout['labels']} should equal single-account labels {single['labels']}"


@th.django_unit_test()
def test_fanout_multi_slug(opts):
    from mojo.apps.metrics.rest.helpers import fetch_group_fanout

    parent, matches, _, _ = _build_tree()
    for g, c in zip(matches, [1, 2, 3]):
        _seed("fan_a", g, c)
        _seed("fan_b", g, c * 10)

    result = fetch_group_fanout(
        parent.pk, "location", ["fan_a", "fan_b"],
        granularity="hours", with_labels=True,
    )
    assert sum(result["data"]["fan_a"]) == 6, \
        f"fan_a expected 6 (1+2+3), got {sum(result['data']['fan_a'])}"
    assert sum(result["data"]["fan_b"]) == 60, \
        f"fan_b expected 60 (10+20+30), got {sum(result['data']['fan_b'])}"


@th.unit_test()
def test_fanout_rejects_non_group_account(opts):
    resp = opts.client.get(
        "/api/metrics/fetch",
        params=dict(slug="fan_rej", account="public", child_kind="location"),
    )
    assert resp.status_code == 400, \
        f"non-group account + child_kind expected 400, got {resp.status_code}: {resp.body}"


@th.unit_test()
def test_fanout_missing_parent_group(opts):
    from mojo.apps.account.models import User, Group

    user_name = "fanout_missing"
    pword = "metrics##mojo99"

    user = User.objects.filter(username=user_name).last()
    if user is None:
        user = User(username=user_name, email=f"{user_name}@example.com")
        user.save()
    user.is_email_verified = True
    user.save_password(pword)
    user.remove_all_permissions()
    user.add_permission("view_metrics")

    max_id = Group.objects.order_by("-id").values_list("id", flat=True).first() or 0
    bogus_id = max_id + 99999

    assert opts.client.login(user_name, pword), "user login failed"
    resp = opts.client.get(
        "/api/metrics/fetch",
        params=dict(slug="fan_missing", account=f"group-{bogus_id}",
                    child_kind="location"),
    )
    # Either 400 (group not found) or 403 (perm helper fails first).
    # Both are correct fail-fast behaviors.
    assert resp.status_code in (400, 403), \
        f"missing parent expected 400 or 403, got {resp.status_code}: {resp.body}"
