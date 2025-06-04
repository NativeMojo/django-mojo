from testit import helpers as th
from testit import faker
import datetime

TEST_USER = "testit"
TEST_PWORD = "testit##mojo"

ADMIN_USER = "tadmin"
ADMIN_PWORD = "testit##mojo"

@th.django_unit_test()
def test_metrics_utils_generate_granulariities(opts):
    from mojo.apps.metrics import utils

    # test utils.generate_granulariities
    result = utils.generate_granularities("days", "months")
    # write an assert statement to test that result is an array of ["days", "weeks", "months"]
    assert result == ["days", "weeks", "months"], "days -> months failed"

    result = utils.generate_granularities("minutes", "months")
    # write an assert statement to test that result is an array of ["days", "weeks", "months"]
    assert result == ["minutes", "hours", "days", "weeks", "months"], "minutes -> months failed"


@th.django_unit_test()
def test_metrics_utils_generate_slug(opts):
    from mojo.apps.metrics import utils

    now = datetime.datetime(year=2025, month=5, day=2, hour=1, minute=23)
    # test utils.generate_slug
    result = utils.generate_slug("example", now, "minutes")
    # write an assert statement to test that result is an array of ["days", "weeks", "months"]
    assert result == "mets:global::example:min:2025-05-02T01-23", f"minutes slug: {result}"

    # test utils.generate_slug
    result = utils.generate_slug("example", now, "hours")
    # write an assert statement to test that result is an array of ["days", "weeks", "months"]
    assert result == "mets:global::example:hr:2025-05-02T01", f"hours slug: {result}"

    # test utils.generate_slug
    result = utils.generate_slug("example", now, "days")
    # write an assert statement to test that result is an array of ["days", "weeks", "months"]
    assert result == "mets:global::example:day:2025-05-02", f"days slug: {result}"

    # test utils.generate_slug
    result = utils.generate_slug("example", now, "weeks")
    # write an assert statement to test that result is an array of ["days", "weeks", "months"]
    assert result == "mets:global::example:wk:2025-17", f"week slug: {result}"

    # test utils.generate_slug
    result = utils.generate_slug("example", now, "months")
    # write an assert statement to test that result is an array of ["days", "weeks", "months"]
    assert result == "mets:global::example:mon:2025-05", f"months slug: {result}"

    # test utils.generate_slug
    result = utils.generate_slug("example", now, "years")
    # write an assert statement to test that result is an array of ["days", "weeks", "months"]
    assert result == "mets:global::example:yr:2025", f"years slug: {result}"


    # test utils.generate_slug
    result = utils.generate_slug("example", now, "years", "mojo")
    # write an assert statement to test that result is an array of ["days", "weeks", "months"]
    assert result == "mets:mojo::example:yr:2025", f"mojo years slug: {result}"



@th.django_unit_test()
def test_metrics_utils_generate_slugs_for_range(opts):
    from mojo.apps.metrics import utils

    start = datetime.datetime(year=2025, month=5, day=2, hour=1, minute=23)
    end = datetime.datetime(year=2025, month=5, day=2, hour=13, minute=0)
    # test utils.generate_slug
    result = utils.generate_slugs_for_range("example", start, end, "hours", "mojo")
    # write an assert statement to test that result is an array of ["days", "weeks", "months"]
    exp = ['mets:mojo::example:hr:2025-05-02T01', 'mets:mojo::example:hr:2025-05-02T02',
        'mets:mojo::example:hr:2025-05-02T03', 'mets:mojo::example:hr:2025-05-02T04',
        'mets:mojo::example:hr:2025-05-02T05', 'mets:mojo::example:hr:2025-05-02T06',
        'mets:mojo::example:hr:2025-05-02T07', 'mets:mojo::example:hr:2025-05-02T08',
        'mets:mojo::example:hr:2025-05-02T09', 'mets:mojo::example:hr:2025-05-02T10',
        'mets:mojo::example:hr:2025-05-02T11', 'mets:mojo::example:hr:2025-05-02T12']
    assert result == exp, f"hours slugs: {result}"


@th.django_unit_test()
def test_metrics_utils_periods_from_dr_slugs(opts):
    from mojo.apps.metrics import utils

    start = None
    end = datetime.datetime(year=2025, month=5, day=2, hour=14, minute=0)
    # test utils.generate_slug minutes
    slugs = utils.generate_slugs_for_range("example", start, end, "minutes", "mojo")
    periods = utils.periods_from_dr_slugs(slugs)
    minutes = ['13:31', '13:32', '13:33', '13:34', '13:35', '13:36', '13:37', '13:38', '13:39', '13:40',
        '13:41', '13:42', '13:43', '13:44', '13:45', '13:46', '13:47', '13:48', '13:49', '13:50',
        '13:51', '13:52', '13:53', '13:54', '13:55', '13:56', '13:57', '13:58', '13:59', '14:00']
    assert periods == minutes, f"minutes slugs: {periods}"

    # test utils.generate_slug
    slugs = utils.generate_slugs_for_range("example", start, end, "hours", "mojo")
    periods = utils.periods_from_dr_slugs(slugs)
    hours = ['03:00', '04:00', '05:00', '06:00', '07:00', '08:00',
        '09:00', '10:00', '11:00', '12:00', '13:00', '14:00']
    assert periods == hours, f"hours slugs: {periods}"

    # test utils.generate_slug
    slugs = utils.generate_slugs_for_range("example", start, end, "days", "mojo")
    periods = utils.periods_from_dr_slugs(slugs)
    days = ['2025-04-21', '2025-04-22', '2025-04-23', '2025-04-24', '2025-04-25', '2025-04-26',
        '2025-04-27', '2025-04-28', '2025-04-29', '2025-04-30', '2025-05-01', '2025-05-02']
    assert periods == days, f"days slugs: {periods}"

    # test utils.generate_slug
    slugs = utils.generate_slugs_for_range("example", start, end, "weeks", "mojo")
    periods = utils.periods_from_dr_slugs(slugs)
    weeks = ['2025-06', '2025-07', '2025-08', '2025-09', '2025-10', '2025-11',
        '2025-12', '2025-13', '2025-14', '2025-15', '2025-16', '2025-17']
    assert periods == weeks, f"weeks slugs: {periods}"

    # test utils.generate_slug
    slugs = utils.generate_slugs_for_range("example", start, end, "months", "mojo")
    periods = utils.periods_from_dr_slugs(slugs)
    months = ['2024-05', '2024-06', '2024-07', '2024-08', '2024-09', '2024-10',
        '2024-11', '2024-12', '2025-01', '2025-02', '2025-03', '2025-04', '2025-05']
    assert periods == months, f"months slugs: {periods}"

    # test utils.generate_slug
    slugs = utils.generate_slugs_for_range("example", start, end, "years", "mojo")
    periods = utils.periods_from_dr_slugs(slugs)
    years = ['2014', '2015', '2016', '2017', '2018', '2019', '2020',
        '2021', '2022', '2023', '2024', '2025']
    assert periods == years, f"years slugs: {periods}"


@th.django_unit_test()
def test_basic_metrics_record(opts):
    from mojo.apps import metrics
    from mojo.apps.metrics import utils
    metrics.record("test", min_granularity="minutes", account="test")
    records = {}
    # check basic recording works
    for gran in utils.GRANULARITIES:
        data = metrics.fetch("test", granularity=gran, account="test")
        assert data[-1] > 0, f"{gran} check failed (now)"
        records[gran] = data[-1]
    # validate counts increase
    metrics.record("test", min_granularity="minutes", account="test")
    # now lets get back our current counts
    for gran in utils.GRANULARITIES:
        data = metrics.fetch("test", granularity=gran, account="test")
        assert data[-1] > records[gran], f"{gran} increase check failed (now)"

    metrics.record("test", min_granularity="minutes", max_granularity="hours", account="mojo")
    # now lets get back our current counts
    for gran in utils.GRANULARITIES:
        data = metrics.fetch("test", granularity=gran, account="mojo")
        if gran in ["minutes", "hours"]:
            assert data[-1] > 0, f"{gran} check failed (now)\n{data}"
        else:
            assert data[-1] == 0, f"{gran} check failed (now)\n{data}"


@th.django_unit_test()
def test_basic_metrics_categories(opts):
    from mojo.apps import metrics
    from mojo.apps.metrics import utils

    cats = metrics.get_categories(account="test")
    assert cats == set(), "get_categories returns something"
    slugs = metrics.get_category_slugs("blue", account="test")
    assert slugs == set(), slugs

    metrics.record("c1", category="blue", account="test")
    metrics.record("c2", category="blue", account="test")

    cats = metrics.get_categories(account="test")
    assert cats == {"blue"}, cats
    slugs = metrics.get_category_slugs("blue", account="test")
    assert slugs == {"c1", "c2"}, slugs

    for gran in utils.GRANULARITIES:
        data = metrics.fetch_by_category("blue", granularity=gran, account="test")
        assert "c1" in data, f"missing c1: {data}"
        assert data.c1[-1] > 0, f"{gran}.c1 check failed (now)\n{data.c1}"

        assert "c2" in data, "missing c2"
        assert data.c2[-1] > 0, f"{gran}.c2 check failed (now)\n{data.c2}"

    # now delete the category
    metrics.delete_category("blue", account="test")

    cats = metrics.get_categories(account="test")
    assert cats == set(), "get_categories returns something"
    slugs = metrics.get_category_slugs("blue", account="test")
    assert slugs == set(), slugs


@th.django_unit_test()
def test_fetch_with_labels(opts):
    from mojo.apps import metrics
    from mojo.apps.metrics import utils

    end = datetime.datetime(year=2025, month=5, day=2, hour=14, minute=0)

    # now lets get back our current counts
    gran = "hours"
    data = metrics.fetch("test", dt_end=end, granularity=gran, account="test", with_labels=True)
    assert isinstance(data, dict), "result is not dict"
    assert isinstance(data.labels, list), f"data.labels is not list, {data}"
    assert data.labels[-1] == "14:00", f"period label is 14:00: {data.labels[-1]}"
    assert isinstance(data.data, dict), f"data.data is not dict {data}"
    assert "test" in data.data, f"slug is not test: {data.data}"
    assert isinstance(data["data"]["test"], list), "data.data.values is not list"
    assert data["data"]["test"][-1] >= 0, "values exist"
    assert len(data["data"]["test"]) == len(data["labels"]), "number of labels vs values is wrong"
    # metrics.record("test", min_granularity="minutes", max_granularity="hours", account="mojo")
    # # now lets get back our current counts
    # for gran in utils.GRANULARITIES:
    #     data = metrics.fetch("test", granularity=gran, account="mojo")
    #     if gran in ["minutes", "hours"]:
    #         assert data[-1] > 0, f"{gran} check failed (now)\n{data}"
    #     else:
    #         assert data[-1] == 0, f"{gran} check failed (now)\n{data}"


@th.django_unit_test()
def test_account_permissions(opts):
    from mojo.apps import metrics
    from mojo.apps.metrics import utils

    account = "test"

    # clear out any existing
    metrics.set_write_perms(account, None)
    assert metrics.get_write_perms(account) is None, f"{account} write perms not None"
    metrics.set_view_perms(account, None)
    assert metrics.get_view_perms(account) is None, f"{account} view perms not None"

    view_p = "view_test_metrics"
    write_p = "write_test_metrics"
    metrics.set_write_perms(account, write_p)
    r = metrics.get_write_perms(account)
    assert r == write_p, f"{account} write perms not {write_p} but {repr(r)}"
    metrics.set_view_perms(account, view_p)
    r = metrics.get_view_perms(account)
    assert r == view_p, f"{account} view perms not {view_p} but {repr(r)}"


@th.unit_test()
def test_metrics_api(opts):
    new_name = faker.generate_name()
    resp = opts.client.post(f"/api/metrics/record", dict(slug="c3", account="test"))
    assert resp.status_code == 403, f"test -> Expected status_code is 403 but got {resp.status_code}"

    resp = opts.client.post(f"/api/metrics/record", dict(slug="c3", account="global"))
    assert resp.status_code == 403, f"global -> Expected status_code is 403 but got {resp.status_code}"

    # now lets use the public API
    resp = opts.client.post(f"/api/metrics/record", dict(slug="c3"))
    assert resp.status_code == 200, f"public -> Expected status_code is 200 but got {resp.status_code}"

    resp = opts.client.get(f"/api/metrics/fetch", params=dict(slugs="c3"))
    assert resp.status_code == 200, f"fetch public Expected status_code is 200 but got {resp.status_code}"
    assert resp.response.data, "missing resp.data"
    data = resp.response.data
    assert isinstance(data, dict), "result is not dict"
    assert isinstance(data.labels, list), "data.label is not list"
    # assert data.label[-1] == "15:00", f"period label is 15:00: {data.periods[-1]}"
    assert isinstance(data.data, dict), f"data.data is not dict {data}"
    assert "c3" in data.data, f"slug is not test: {data.data}"
    assert isinstance(data["data"]["c3"], list), "data.data.values is not list"
    assert data["data"]["c3"][-1] >= 0, "values exist"
    assert len(data["data"]["c3"]) == len(data["labels"]), "number of labels vs values is wrong"
