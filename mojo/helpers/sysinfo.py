import platform
import socket

from datetime import datetime
import time

try:
    import psutil
except ImportError:
    psutil = None


def get_hostname():
    """Get the hostname of the current system."""
    return socket.gethostname()

def get_host_ip():
    """Get the IP address associated with the current hostname."""
    hostname = socket.gethostname()
    return socket.gethostbyname(hostname)

def get_tcp_established_count():
    """Return the count of established TCP connections."""
    return len(get_tcp_established())

def get_tcp_established(filter_type=None):
    """
    Get a list of established TCP connections, optionally filtering by type.

    :param filter_type: Optionally filter by type ('https', 'redis', 'postgres', etc.)
    :return: List of connections according to filter
    """
    if psutil is None:
        raise RuntimeError("psutil is not installed. Install it with: pip install psutil")

    connections = psutil.net_connections(kind="tcp")
    established_connections = [c for c in connections if c.status == psutil.CONN_ESTABLISHED]
    return filter_connections(established_connections, filter_type)

def get_tcp_established_summary():
    connections = psutil.net_connections(kind="tcp")
    established_connections = [c for c in connections if c.status == psutil.CONN_ESTABLISHED]
    return {
        "total": len(established_connections),
        "https": len([c for c in established_connections if c.laddr.port == 443]),
        "redis": len([c for c in established_connections if c.raddr.port == 6379]),
        "postgres": len([c for c in established_connections if c.raddr.port == 5432]),
        "unknown": len([c for c in established_connections if c.raddr.port not in [5432, 6379] and c.laddr.port != 443])
    }

def filter_connections(connections, filter_type):
    """
    Filter connections based on the specified filter type.

    :param connections: List of connections
    :param filter_type: Type to filter (options: 'https', 'redis', 'postgres')
    :return: List of filtered connections
    """
    filters = {
        "https": lambda c: c.laddr.port == 443,
        "redis": lambda c: c.raddr.port == 6379,
        "postgres": lambda c: c.raddr.port == 5432,
        "unknown": lambda c: c.raddr.port not in [5432, 6379] and c.laddr.port != 443
    }

    if filter_type in filters:
        return [c for c in connections if filters[filter_type](c)]
    elif filter_type and ":" in filter_type:
        addr, port = filter_type.split(':')
        port = int(port)
        return [c for c in connections if (addr == "raddr" and c.raddr.port == port) or (addr == "laddr" and c.laddr.port == port)]

    return connections

def connections_to_dict(connections):
    """
    Convert a list of connections to a dictionary representation.

    :param connections: List of connections
    :return: List of dictionaries representing connections
    """
    return [
        {
            "id": idx,
            "type": c.type.name,
            "status": c.status,
            "family": c.family.name,
            "raddr": {
                "port": c.raddr.port if c.raddr else None,
                "ip": c.raddr.ip if c.raddr else None,
            },
            "laddr": {
                "port": c.laddr.port,
                "ip": c.laddr.ip
            }
        }
        for idx, c in enumerate(connections, start=1)
    ]


def get_host_info(diagnostic=False):
    """
    Gather information about the host.

    :return: Dictionary with host information
    """
    if diagnostic:
        return get_host_diagnostics()
    if psutil is None:
        raise RuntimeError("psutil is not installed. Install it with: pip install psutil")

    mem = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    net = psutil.net_io_counters()

    host_info = {
        "time": time.time(),
        "datetime": datetime.now().isoformat(),
        "os": {
            "system": platform.system(),
            "version": platform.version(),
            "hostname": platform.node(),
            "release": platform.release(),
            "processor": platform.processor(),
            "machine": platform.machine()
        },
        "boot_time": psutil.boot_time(),
        "cpu_load": psutil.cpu_percent(),
        "cpus_load": psutil.cpu_percent(percpu=True),
        "memory": {
            "total": mem.total,
            "used": mem.used,
            "available": mem.available,
            "percent": mem.percent
        },
        "disk": {
            "total": disk.total,
            "used": disk.used,
            "free": disk.free,
            "percent": disk.percent
        },
        "users": psutil.users(),
        "cpu": {
            "count": psutil.cpu_count(),
            "freq": psutil.cpu_freq()._asdict() if psutil.cpu_freq() else None,
        },
        "network": {
            "tcp_cons": get_tcp_established_count(),
            "bytes_sent": net.bytes_sent,
            "bytes_recv": net.bytes_recv,
            "packets_sent": net.packets_sent,
            "packets_recv": net.packets_recv,
            "errin": net.errin,
            "errout": net.errout,
            "dropin": net.dropin,
            "dropout": net.dropout
        }
    }

    return host_info


_DIAGNOSTIC_UNSET = object()


def get_host_diagnostics(psutil_module=_DIAGNOSTIC_UNSET, get_static=None,
                         os_module=None):
    """Bounded, independent metric families; no users, sockets or processes.

    Paths come exclusively from local file settings. Explicit data mounts must
    be actual mount points: a missing disk must never turn into root evidence.
    Dependencies are injectable so fixtures need no process-wide patches.
    """
    import os
    from datetime import timezone
    if psutil_module is _DIAGNOSTIC_UNSET:
        psutil_module = psutil
    if get_static is None:
        from mojo.helpers.settings import settings
        get_static = settings.get_static
    os_module = os_module or os
    observed = datetime.now(timezone.utc).isoformat()

    def family(unit, collect, requires_psutil=True):
        base = {"status": "ok", "reason": None, "source": "host",
                "observed_at": observed, "unit": unit}
        if psutil_module is None and requires_psutil:
            return dict(base, status="unavailable", reason="psutil_unavailable", value=None)
        try:
            base.update(collect())
        except (AttributeError, NotImplementedError):
            base.update(status="unsupported", reason="collector_unsupported", value=None)
        except PermissionError:
            base.update(status="unavailable", reason="permission_denied", value=None)
        except Exception:
            base.update(status="unavailable", reason="collector_failed", value=None)
        return base

    def memory():
        value = psutil_module.virtual_memory()
        return {key: getattr(value, key) for key in ("total", "used", "available", "percent")}

    def swap():
        value = psutil_module.swap_memory()
        return {key: getattr(value, key) for key in ("total", "used", "free", "percent")}

    def network():
        value = psutil_module.net_io_counters()
        return dict({key: getattr(value, key) for key in (
            "bytes_sent", "bytes_recv", "packets_sent", "packets_recv", "errin", "errout",
            "dropin", "dropout")}, counter_scope="cumulative_since_boot")

    result = {
        "cpu": family("percent", lambda: {
            "percent": psutil_module.cpu_percent(interval=0.1),
            "sampling_interval_seconds": 0.1, "sampling": "interval"}),
        "load": family("load", lambda: dict(zip(("one", "five", "fifteen"), os_module.getloadavg())), requires_psutil=False),
        "memory": family("bytes", memory),
        "swap": family("bytes", swap),
        "network": family("bytes_and_packets", network),
    }
    mounts = []
    omitted = 0
    missing = []
    configured = get_static("SYSINFO_MOUNTS", [])
    config_valid = (isinstance(configured, (list, tuple)) and len(configured) <= 8
                    and all(isinstance(path, str) and path.startswith("/")
                            and len(path) <= 1024 and "\x00" not in path
                            for path in configured))
    if not config_valid:
        configured = []
    try:
        partitions = (psutil_module.disk_partitions(all=True)
                      if psutil_module is not None else [])
        known = {os_module.path.realpath(row.mountpoint): row for row in partitions}
        known.setdefault("/", None)
        # Resolve app paths to the deepest real filesystem, deduplicating aliases.
        relevant = ["/"]
        for key in ("BASE_DIR", "APP_ROOT", "VAR_ROOT", "MEDIA_ROOT"):
            path = get_static(key, None)
            if path and isinstance(path, (str, bytes, os.PathLike)):
                path = os_module.path.realpath(path)
                if os_module.path.exists(path):
                    candidates = [mount for mount in known
                                  if path == mount or path.startswith(mount.rstrip("/") + "/")]
                    if candidates:
                        relevant.append(max(candidates, key=len))
        for path in configured:
            exact = os_module.path.realpath(path)
            if not os_module.path.exists(exact) or not os_module.path.ismount(exact):
                missing.append({"path": path, "status": "unavailable", "reason": "configured_mount_absent"})
            else:
                relevant.append(exact)
        unique = list(dict.fromkeys(relevant))
        omitted = max(0, len(unique) - 16)
        for mount in unique[:16]:
            def disk(mount=mount):
                value = psutil_module.disk_usage(mount)
                row = {key: getattr(value, key) for key in ("total", "used", "free", "percent")}
                if value.total == 0:
                    row.update(status="unknown", reason="zero_capacity", percent=None)
                return row
            row = family("bytes", disk)
            row["path"] = mount
            partition = known.get(mount)
            row["read_only"] = ("ro" in partition.opts.split(",") if partition else None)
            try:
                stat = os_module.statvfs(mount)
                row["inodes"] = {"status": "ok" if stat.f_files else "unsupported",
                                 "reason": None if stat.f_files else "inodes_unavailable",
                                 "source": "statvfs", "observed_at": observed, "unit": "inodes",
                                 "total": stat.f_files or None,
                                 "free": stat.f_favail if stat.f_files else None}
            except (AttributeError, NotImplementedError):
                row["inodes"] = {"status": "unsupported", "reason": "statvfs_unsupported",
                                 "source": "statvfs", "observed_at": observed, "unit": "inodes", "free": None}
            except Exception:
                row["inodes"] = {"status": "unavailable", "reason": "statvfs_failed",
                                 "source": "statvfs", "observed_at": observed, "unit": "inodes", "free": None}
            mounts.append(row)
        filesystem_status = "ok" if config_valid and not missing and not omitted else "unknown"
        filesystem_reason = ("invalid_mount_configuration" if not config_valid else
                             "configured_mount_absent" if missing else "mount_limit" if omitted else None)
    except Exception:
        filesystem_status, filesystem_reason = "unavailable", "mount_discovery_failed"
    result["filesystems"] = {"status": filesystem_status, "reason": filesystem_reason,
                             "source": "host", "observed_at": observed, "unit": "bytes",
                             "mounts": mounts, "missing": missing, "omitted_count": omitted}
    return result
