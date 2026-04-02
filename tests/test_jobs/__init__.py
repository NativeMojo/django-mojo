"""
Refactored Jobs System Tests

Simplified tests focusing on core functionality without decorator testing.
"""
TESTIT = {
    "requires_apps": ["mojo.apps.jobs"],
    "serial": True,  # job engine uses signals (main thread only)
}
