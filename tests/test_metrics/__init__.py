TESTIT = {
    "requires_apps": ["mojo.apps.account"],
    "serial": True,  # fanout.py uses th.server_settings() for METRICS_FANOUT_MAX_CHILDREN
}
