TESTIT = {
    # Live assistant tests: they need a real LLM key (auto-skip without one) and
    # use th.server_settings() to enable the assistant at the server — a reload,
    # legal only in a serial/opt-in package. Moved out of the parallel
    # test_assistant package (maestro #2791).
    "requires_apps": ["mojo.apps.assistant"],
    "requires_extra": ["slow"],
    "serial": True,
}
