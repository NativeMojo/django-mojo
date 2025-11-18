import platform
import socket

from datetime import datetime
import time

try:
    import psutil
except ImportError:
    psutil = None


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


def get_host_info(include_versions=False, include_blocked=False):
    """
    Gather information about the host.

    :param include_versions: Include software versions if True
    :param include_blocked: Include blocked hosts if True
    :return: Dictionary with host information
    """
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
