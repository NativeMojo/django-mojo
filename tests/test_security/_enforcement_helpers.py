"""Module-level helpers loaded by enforcement tests via dotted path.

Cannot be inside a test function because BOUNCER_SESSION_FREEZE_HANDLER is
resolved by load_function() on the server side.
"""


def raising_handler(user, device, risk_score):
    """Freeze handler that always raises — verifies framework swallows it."""
    raise RuntimeError("simulated freeze-handler failure")


def recording_handler(user, device, risk_score):
    """Freeze handler that records its invocation in a module-level list."""
    recording_handler.calls.append((
        getattr(user, 'username', '?'),
        getattr(device, 'muid', '?'),
        risk_score,
    ))


recording_handler.calls = []
