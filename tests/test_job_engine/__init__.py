TESTIT = {
    "requires_apps": ["mojo.apps.jobs"],
    "serial": True,  # JobEngine/Scheduler use signal handlers (main thread only)
}
