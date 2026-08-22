TESTIT = {
    # Exhaustive Admin settings/platform/system-setup matrices: they patch
    # production module attributes and write protected Setting rows, which is
    # unsafe under the parallel default tier (maestro item #1839).
    # The assistant app registers the MCP resource and the ASSISTANT_MCP_*
    # descriptors that test_assistant_setup.py drives end to end.
    "requires_apps": ["mojo.apps.account", "mojo.apps.assistant"],
    "requires_extra": ["extended"],
    "serial": True,
}
