"""Missing-preserving evidence and provider bounds with isolated fake clients."""
from datetime import datetime, timezone, timedelta
from testit import helpers as th


class _Metrics:
    def __init__(self, duplicate=False):
        self.calls = []
        self.duplicate = duplicate

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
