"""
Built-in tool registration for the admin assistant.

Imports each domain module which defines tool handlers, then registers
them all via the public ``register_tool()`` API.
"""
from mojo.apps.assistant import register_tool

from . import security
from . import jobs
from . import users
from . import groups
from . import metrics


def _register_domain(domain_name, tool_list):
    for tool in tool_list:
        register_tool(
            name=tool["name"],
            description=tool["description"],
            input_schema=tool["input_schema"],
            handler=tool["handler"],
            permission=tool["permission"],
            mutates=tool.get("mutates", False),
            domain=domain_name,
        )


_register_domain("security", security.TOOLS)
_register_domain("jobs", jobs.TOOLS)
_register_domain("users", users.TOOLS)
_register_domain("groups", groups.TOOLS)
_register_domain("metrics", metrics.TOOLS)
