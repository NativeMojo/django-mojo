"""
Tests for `_mode=top` with signed, zero, fractional and null aggregate
values against /api/incident/event (#8607).

`_min_count` is a minimum number of rows per group for every `_agg`;
`_min_value` is the separate, signed threshold on the aggregate value.
Groups whose value is null sort last.
"""
from testit import helpers as th


TEST_USER = "evt_aggsig_admin"
TEST_PWORD = "evtaggsig##mojo99"

CAT_SUM = "evt_aggsig:sum"
CAT_AVG = "evt_aggsig:avg"
CAT_MINMAX = "evt_aggsig:minmax"
CAT_NULL = "evt_aggsig:null"

# Seed S: (ip, levels) -> sums A 15, B 2, N -1870, Z 0, M -500, S1 -40.
SEED_S = [
    ("10.9.0.1", [5, 5, 5]),
    ("10.9.0.2", [1, 1]),
    ("10.9.0.3", [-100] * 18 + [-70]),
    ("10.9.0.4", [3, -3]),
    ("10.9.0.5", [-250, -250]),
    ("10.9.0.6", [-40]),
]
A, B, N, Z, M, S1 = (ip for ip, _ in SEED_S)


def _reset_admin(username, password):
    from mojo.apps.account.models import User
    user = User.objects.filter(username=username).last()
    if user is None:
        user = User(username=username, email=f"{username}@example.com")
        user.save()
    user.is_email_verified = True
    user.save_password(password)
    user.remove_all_permissions()
    user.add_permission("view_security")
    user.add_permission("manage_security")
    user.save()
    return user


@th.django_unit_setup()
def setup_event_aggregation_signed_top(opts):
    from mojo.apps.incident.models import Event
    Event.objects.filter(category__in=[CAT_SUM, CAT_AVG, CAT_MINMAX, CAT_NULL]).delete()
    _reset_admin(TEST_USER, TEST_PWORD)
    opts.user_name = TEST_USER
    opts.pword = TEST_PWORD


def _seed(category, groups, field="level"):
    """Replace this category's events with one row per value per group."""
    from mojo.apps.incident.models import Event
    Event.objects.filter(category=category).delete()
    for ip, values in groups:
        for value in values:
            Event.objects.create(category=category, source_ip=ip, **{field: value})


def _top(opts, category, **params):
    assert opts.client.login(opts.user_name, opts.pword), "admin login failed"
    query = {"_mode": "top", "_field": "source_ip", "category": category}
    query.update(params)
    return opts.client.get("/api/incident/event", params=query)


def _sum_top(opts, **params):
    _seed(CAT_SUM, SEED_S)
    return _top(opts, CAT_SUM, _agg="sum", _agg_field="level", **params)


def _pairs(resp):
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.body}"
    return [(row["key"], row["value"]) for row in resp.response["data"]]


@th.django_unit_test()
def test_sum_top_defaults_keep_signed_groups_in_order(opts):
    resp = _sum_top(opts, _size=10)
    pairs = _pairs(resp)
    assert pairs == [(A, 15), (B, 2), (Z, 0), (S1, -40), (M, -500), (N, -1870)], (
        f"sum top at defaults must list every group by value, got {pairs}"
    )
    row = next(r for r in resp.response["data"] if r["key"] == N)
    assert row.get("first_seen") is not None, f"negative group needs first_seen: {row}"
    assert row.get("last_seen") is not None, f"negative group needs last_seen: {row}"


@th.django_unit_test()
def test_sum_top_defaults_keep_zero_group(opts):
    values = dict(_pairs(_sum_top(opts)))
    assert Z in values, f"zero-sum group must be present, got {values}"
    assert values[Z] == 0, f"zero-sum group value must be 0, got {values[Z]}"


@th.django_unit_test()
def test_avg_top_keeps_fractional_and_negative(opts):
    _seed(CAT_AVG, [("10.9.1.1", [0, 1]), ("10.9.1.2", [-1, 0])])
    values = dict(_pairs(_top(opts, CAT_AVG, _agg="avg", _agg_field="level")))
    assert values.get("10.9.1.1") == 0.5, f"avg 0.5 group expected, got {values}"
    assert values.get("10.9.1.2") == -0.5, f"avg -0.5 group expected, got {values}"


@th.django_unit_test()
def test_min_and_max_top_keep_negative(opts):
    _seed(CAT_MINMAX, [("10.9.2.1", [-7, -2]), ("10.9.2.2", [4, 9])])
    pairs = _pairs(_top(opts, CAT_MINMAX, _agg="min", _agg_field="level"))
    assert pairs == [("10.9.2.2", 4), ("10.9.2.1", -7)], f"min top got {pairs}"
    pairs = _pairs(_top(opts, CAT_MINMAX, _agg="max", _agg_field="level"))
    assert pairs == [("10.9.2.2", 9), ("10.9.2.1", -2)], f"max top got {pairs}"


@th.django_unit_test()
def test_count_top_min_count_matches_orm(opts):
    from django.db.models import Count
    from mojo.apps.incident.models import Event
    _seed(CAT_SUM, SEED_S)
    for k in (1, 2):
        expected = {
            row["source_ip"]: row["c"]
            for row in Event.objects.filter(category=CAT_SUM)
            .values("source_ip").annotate(c=Count("id")).filter(c__gte=k)
        }
        got = dict(_pairs(_top(opts, CAT_SUM, _min_count=k)))
        assert got == expected, f"count top _min_count={k}: expected {expected}, got {got}"
    assert S1 not in got, f"_min_count=2 must drop the 1-row group, got {got}"


@th.django_unit_test()
def test_sum_top_min_count_counts_rows(opts):
    keys = [k for k, _ in _pairs(_sum_top(opts, _min_count=2))]
    assert S1 not in keys, f"_min_count=2 must drop the 1-row group, got {keys}"
    for ip in (M, N, Z):
        assert ip in keys, f"_min_count=2 must keep {ip} (2+ rows), got {keys}"


@th.django_unit_test()
def test_sum_top_min_value_signed_threshold(opts):
    keys = [k for k, _ in _pairs(_sum_top(opts, _min_value=-1000))]
    assert M in keys and S1 in keys, f"_min_value=-1000 must keep -500 and -40, got {keys}"
    assert N not in keys, f"_min_value=-1000 must drop -1870, got {keys}"
    for bad in ("abc", "NaN"):
        resp = _sum_top(opts, _min_value=bad)
        assert resp.status_code == 400, (
            f"_min_value={bad} must be 400, got {resp.status_code}: {resp.body}"
        )


@th.django_unit_test()
def test_sum_top_min_value_fraction_rounds_up_for_integers(opts):
    keys = [k for k, _ in _pairs(_sum_top(opts, _min_value=2.5))]
    assert B not in keys, f"_min_value=2.5 must drop a total of 2, got {keys}"
    assert A in keys, f"_min_value=2.5 must keep a total of 15, got {keys}"


@th.django_unit_test()
def test_sum_top_null_group_sorts_last(opts):
    _seed(CAT_NULL, [("10.9.3.1", [None, None]), ("10.9.3.2", [5]), ("10.9.3.3", [-3])],
          field="uid")
    pairs = _pairs(_top(opts, CAT_NULL, _agg="sum", _agg_field="uid"))
    assert pairs == [("10.9.3.2", 5), ("10.9.3.3", -3), ("10.9.3.1", None)], (
        f"null-sum group must be present and last, got {pairs}"
    )


@th.django_unit_test()
def test_top_negative_min_count_rejected(opts):
    resp = _sum_top(opts, _min_count=-1)
    assert resp.status_code == 400, f"_min_count=-1 must be 400, got {resp.status_code}"


@th.django_unit_test()
def test_sum_top_values_add_up_to_summary(opts):
    total = sum(v for _, v in _pairs(_sum_top(opts, _size=100)))
    resp = _top(opts, CAT_SUM, _mode="summary", _agg="sum", _agg_field="level")
    assert resp.status_code == 200, f"summary expected 200, got {resp.status_code}: {resp.body}"
    assert resp.response["value"] == -2393, f"summary sum must be -2393: {resp.response}"
    assert total == resp.response["value"], (
        f"top values must add up to summary {resp.response['value']}, got {total}"
    )


@th.django_unit_test()
def test_sum_top_small_size_keeps_highest(opts):
    keys = [k for k, _ in _pairs(_sum_top(opts, _size=3))]
    assert keys == [A, B, Z], f"_size=3 must keep the three highest, got {keys}"
