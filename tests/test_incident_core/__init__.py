TESTIT = {
    # Core incident persistence contract — parallel-safe: every row this
    # package touches is uniquely identified by a per-run UUID category, so a
    # concurrent module's events are never asserted on or deleted.
    "requires_apps": ["mojo.apps.incident"],
}
