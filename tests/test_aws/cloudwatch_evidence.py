"""Missing-preserving evidence and provider bounds with isolated fake clients."""
from datetime import datetime, timezone, timedelta
from testit import helpers as th


class _Metrics:
    def __init__(self, duplicate=False):
        self.calls = []
        self.duplicate = duplicate

    def close(self):
        pass

    def list_metrics(self, **params):
        self.calls.append(params)
        metric = params["MetricName"]
        dims = [{"Name": "InstanceId", "Value": "i-one"}, {"Name": "ImageId", "Value": "ami-x"}]
        if metric == "disk_used_percent":
            dims += [{"Name": "path", "Value": "/data"}, {"Name": "device", "Value": "nvme1"}, {"Name": "fstype", "Value": "ext4"}]
        rows = [{"Namespace": params["Namespace"], "MetricName": metric, "Dimensions": dims}]
        if self.duplicate and metric == "mem_used_percent":
            rows.append(dict(rows[0], Dimensions=dims + [{"Name": "host", "Value": "other"}]))
        return {"Metrics": rows}

    def get_metric_statistics(self, **params):
        self.calls.append(params)
        return {"Datapoints": [{"Timestamp": params["StartTime"] + timedelta(hours=1),
                                "Average": 0, "Unit": "Percent"}]}


@th.django_unit_test()
def test_nulls_dimensions_and_real_zero(opts):
    from mojo.helpers.aws.cloudwatch import CloudWatchHelper
    client = _Metrics()
    helper = CloudWatchHelper(access_key="fake", secret_key="fake", region="us-east-1")
    result = helper.get_node_evidence("i-one", "mojo/custom", ["/data"], client=client,
                                      now=datetime(2026, 10, 20, 12, tzinfo=timezone.utc))
    memory = result["memory"]
    assert memory["sample_count"] == 1, "missing buckets cannot count as samples"
    assert memory["average"] == 0, "a true zero must survive missing-preserving aggregation"
    assert memory["buckets"][0]["value"] is None, "missing datapoint must stay null"
    assert memory["buckets"][1]["value"] == 0, "real zero must not become missing"
    assert memory["namespace"] == "mojo/custom", "provision namespace must be honored"
    data = next(m for m in result["filesystems"] if m["path"] == "/data")
    assert {d["Name"] for d in data["dimensions"]} == {"InstanceId", "ImageId", "path", "device", "fstype"}, "full CWAgent dimension set is required"
    root = next(m for m in result["filesystems"] if m["path"] == "/")
    assert root["reason"] == "no_series", "absent root disk must not become 0% usage"
    assert result["calls"] <= 12, "node evidence must cap all provider calls"


@th.django_unit_test()
def test_duplicate_series_and_legacy_zero_fill(opts):
    from mojo.helpers.aws.cloudwatch import CloudWatchHelper, _build_buckets
    client = _Metrics(duplicate=True)
    helper = CloudWatchHelper(access_key="fake", secret_key="fake")
    result = helper.get_node_evidence("i-one", client=client)
    assert result["memory"]["reason"] == "series_ambiguous", "multiple dimension sets cannot be selected arbitrarily"
    start = datetime(2026, 10, 20, tzinfo=timezone.utc)
    helper._cw = client
    buckets = _build_buckets(start, start + timedelta(hours=3), 3600)
    values = helper._fetch_values("AWS/EC2", "CPUUtilization", [], start, start + timedelta(hours=3), 3600, "Average", buckets)
    assert values == [0, 0, 0], "additive evidence must preserve chart zero-fill compatibility"


@th.django_unit_test()
def test_provider_deadline_rejects_late_results(opts):
    from mojo.helpers.aws.provider_call import DeadlineProviderClient, ProviderCallError
    clock = [0]
    class Fake:
        def get_metric_statistics(self, **kwargs):
            clock[0] = 2
            return {"Datapoints": []}
    provider = DeadlineProviderClient("cloudwatch", 1, client=Fake(), clock=lambda: clock[0])
    try:
        provider.get_metric_statistics()
    except ProviderCallError as exc:
        assert exc.provider_code == "deadline_exceeded", "late provider response must carry deadline reason"
    else:
        raise AssertionError("late provider response must never be accepted")


@th.django_unit_test()
def test_utc_windows_never_exceed_sixty_buckets(opts):
    from mojo.helpers.aws.cloudwatch import CloudWatchHelper
    helper = CloudWatchHelper(access_key="selected-key", secret_key="selected-secret")
    for hours in (1, 24, 25, 60, 120, 168):
        result = helper.get_node_evidence(
            "i-one", hours=hours, client=_Metrics(),
            now=datetime(2026, 10, 20, 13, 45, tzinfo=timezone.utc))
        cpu = result["cpu"]
        assert len(cpu["buckets"]) <= 60, f"{hours}-hour odd-boundary window must stay within 60 buckets"
        assert (datetime.fromisoformat(cpu["end"]) - datetime.fromisoformat(cpu["start"])).total_seconds() == hours * 3600, "period selection must not relabel the requested history window"


@th.django_unit_test()
def test_provider_constructor_expiry_and_explicit_credentials(opts):
    from mojo.helpers.aws.provider_call import DeadlineProviderClient, ProviderCallError
    clock, calls = [0], []
    class Target:
        def close(self):
            calls.append("close")
        def describe_instances(self, **params):
            calls.append("operation")
            return {}
    def factory(*args, **kwargs):
        calls.append(kwargs)
        clock[0] = 2
        return Target()
    provider = DeadlineProviderClient("ec2", 1, session=object(), client_factory=factory,
                                      clock=lambda: clock[0])
    for _ in range(2):
        try:
            provider.describe_instances()
        except ProviderCallError as exc:
            assert exc.provider_code == "deadline_exceeded", "constructor expiry must retain its deadline refusal"
        else:
            raise AssertionError("constructor-expired provider must refuse")
    assert len(calls) == 2 and calls[-1] == "close", "expired construction must close and prevent provider operation/next construction"
    config = calls[0]["config"]
    assert config.retries["total_max_attempts"] == 1, "provider retries must be disabled"
    assert config.connect_timeout <= .25 and config.read_timeout <= .25, "constructor must receive remaining connect/read limits"
    # Exercise the helper's selected identity at its own injection seam. A
    # deliberately opaque test session avoids live credential lookup entirely.
    observed = []
    def selected_factory(*args, **kwargs):
        observed.append(kwargs)
        return _Metrics()
    from mojo.helpers.aws.cloudwatch import CloudWatchHelper
    helper = CloudWatchHelper(access_key="selected-key", secret_key="selected-secret",
                               session=object(), client_factory=selected_factory)
    helper.get_node_evidence("i-one")
    assert observed and all(row["session"] is helper.session for row in observed), "evidence must preserve the helper's explicitly selected session"


@th.django_unit_test()
def test_supplied_refreshable_session_refuses_before_threshold_race(opts):
    from types import SimpleNamespace
    from mojo.helpers.aws.provider_call import DeadlineProviderClient, ProviderCallError
    calls = []
    class Session:
        def get_scoped_config(self):
            return {}
    class Refreshable:
        def refresh_needed(self):
            calls.append("threshold_check")
            return False
    botocore_session = Session()
    botocore_session._credentials = Refreshable()
    provider = DeadlineProviderClient("cloudwatch", 100,
                                      session=SimpleNamespace(_session=botocore_session),
                                      client_factory=lambda *args, **kwargs: calls.append("construct"),
                                      clock=lambda: 0)
    try:
        provider.get_metric_statistics()
    except ProviderCallError as exc:
        assert exc.provider_code == "credential_transport_unsupported", "a supplied refresh callback cannot promise the absolute collection budget"
    else:
        raise AssertionError("refreshable supplied session must refuse before its threshold can race")
    assert not calls, "bounded caller must neither invoke an advisory refresh check nor construct the unbounded client"
