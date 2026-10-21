"""Diagnostic families/mounts without process-wide settings or psutil patches."""
from types import SimpleNamespace
from testit import helpers as th


@th.django_unit_test()
def test_independent_mount_and_metric_evidence(opts):
    from mojo.helpers.sysinfo import get_host_diagnostics
    usage = lambda path: SimpleNamespace(total=100, used=0, free=100, percent=0)
    fake = SimpleNamespace(
        cpu_percent=lambda interval: 0,
        virtual_memory=lambda: SimpleNamespace(total=100, used=0, available=100, percent=0),
        swap_memory=lambda: (_ for _ in ()).throw(PermissionError()),
        net_io_counters=lambda: SimpleNamespace(**{key: 0 for key in (
            "bytes_sent", "bytes_recv", "packets_sent", "packets_recv", "errin", "errout", "dropin", "dropout")}),
        disk_usage=usage, disk_partitions=lambda all: [
            SimpleNamespace(mountpoint="/", opts="rw"), SimpleNamespace(mountpoint="/data", opts="ro")])
    path = SimpleNamespace(realpath=lambda value: value, exists=lambda value: value != "/lost",
                           ismount=lambda value: value in ("/", "/data"))
    local_os = SimpleNamespace(path=path, getloadavg=lambda: (0, 1, 2),
                               statvfs=lambda mount: SimpleNamespace(f_files=100, f_favail=90))
    config = {"SYSINFO_MOUNTS": ["/data", "/lost"], "BASE_DIR": "/data/app", "MEDIA_ROOT": "/data/media"}
    result = get_host_diagnostics(fake, config.get, local_os)
    assert result["cpu"]["percent"] == 0, "a real zero CPU sample must survive"
    assert result["cpu"]["sampling_interval_seconds"] == .1, "CPU evidence must declare the sampling interval"
    assert result["swap"]["status"] == "unavailable", "swap failure must remain an independent unknown"
    assert result["memory"]["percent"] == 0, "swap failure must not discard memory evidence"
    filesystems = result["filesystems"]
    assert [m["path"] for m in filesystems["mounts"]] == ["/", "/data"], "same-mount aliases must be deduplicated"
    assert filesystems["mounts"][1]["read_only"] is True, "read-only mount posture must survive"
    assert filesystems["mounts"][1]["inodes"]["free"] == 90, "inode availability must be collected"
    assert filesystems["missing"][0]["path"] == "/lost", "missing configured disk must not silently become root"
    assert result["network"]["counter_scope"] == "cumulative_since_boot", "one network sample cannot imply a rate"


@th.django_unit_test()
def test_missing_psutil_is_explicit(opts):
    from mojo.helpers.sysinfo import get_host_diagnostics
    result = get_host_diagnostics(None, lambda key, default=None: default)
    assert result["cpu"]["reason"] == "psutil_unavailable", "missing optional psutil must be explicit"
    assert result["memory"]["status"] == "unavailable", "missing collector cannot imply zero memory usage"


@th.django_unit_test()
def test_unsupported_load_inodes_and_zero_capacity(opts):
    from mojo.helpers.sysinfo import get_host_diagnostics
    def unsupported(*args):
        raise NotImplementedError()
    fake = SimpleNamespace(cpu_percent=lambda interval: 3,
                           virtual_memory=unsupported, swap_memory=unsupported, net_io_counters=unsupported,
                           disk_partitions=lambda all: [],
                           disk_usage=lambda path: SimpleNamespace(total=0, used=0, free=0, percent=0))
    local_os = SimpleNamespace(path=SimpleNamespace(realpath=lambda p: p, exists=lambda p: True, ismount=lambda p: True),
                               getloadavg=unsupported, statvfs=unsupported)
    result = get_host_diagnostics(fake, lambda key, default=None: default, local_os)
    root = result["filesystems"]["mounts"][0]
    assert root["percent"] is None and root["reason"] == "zero_capacity", "zero total filesystem cannot establish a 0% healthy disk"
    assert root["inodes"]["status"] == "unsupported", "unsupported statvfs must remain explicit"
    assert result["load"]["status"] == "unsupported", "unsupported load averages must not abort other metric families"
    assert result["cpu"]["percent"] == 3, "unsupported peers must preserve CPU evidence"
