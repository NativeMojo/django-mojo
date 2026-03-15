"""
Sysinfo Task - Broadcast-execute callable for collecting host info from runners.

Used by jobs.get_sysinfo() to gather system information from one or all runners
via broadcast_execute / execute_on_runner.
"""
from mojo.helpers.sysinfo import get_host_info


def collect_sysinfo(data):
    """
    Collect host system info from the current runner process.

    Called on each runner via broadcast_execute or execute_on_runner.
    The `data` argument is passed by the jobs control channel but is unused.

    Returns:
        dict: Result of sysinfo.get_host_info() — OS, CPU, memory, disk, network.
    """
    return get_host_info()