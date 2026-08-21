"""Cloud domain tools — what is healthy, what needs attention, and the
bounded set of cloud/fleet changes the built-in Admin already offers.

Split the way the security domain is: reads and mutations in separate modules,
both self-registering via @tool on import.
"""
from . import reads  # noqa: F401
