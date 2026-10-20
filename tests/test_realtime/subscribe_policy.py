"""`REALTIME_SUBSCRIBE_HANDLER` decided in-process through the keyword-only
seams of `mojo.apps.realtime.policy.allows` (maestro #8874). The live-server
cases are in `tests/test_chat_policy_extended_serial/`."""
from testit import helpers as th

TESTIT_TIER = "core"

TOPIC = "chat:1"
POLICY = "mojo.apps.realtime.policy"


class _Calls:
    def __init__(self, result=True, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def __call__(self, user, topic, default):
        self.calls.append((user, topic, default))
        if self.error is not None:
            raise self.error
        return self.result


@th.django_unit_test()
def test_unset_hook_returns_the_builtin_verdict(opts):
    from mojo.apps.realtime import policy

    for default in (True, False, None, 1, 0):
        got = policy.allows("user", TOPIC, default, handler=None)
        assert got is default, f"with no hook {default!r} must come back unchanged, got {got!r}"
        got = policy.allows("user", TOPIC, default, path=None)
        assert got is default, f"with no path {default!r} must come back unchanged, got {got!r}"


@th.django_unit_test()
def test_refusing_builtin_never_calls_the_hook(opts):
    from mojo.apps.realtime import policy

    for default in (False, None, 0):
        hook = _Calls(result=True)
        got = policy.allows("user", TOPIC, default, handler=hook)
        assert got is False, f"a refusing built-in verdict {default!r} must refuse, got {got!r}"
        assert hook.calls == [], f"the hook must not be asked when the built-in refuses, got {hook.calls}"


@th.django_unit_test()
def test_only_literal_true_allows(opts):
    from mojo.apps.realtime import policy

    hook = _Calls(result=True)
    assert policy.allows("u", TOPIC, True, handler=hook) is True, "a hook returning True must allow"
    assert hook.calls == [("u", TOPIC, True)], f"the hook gets (user, topic, default), got {hook.calls}"
    hook = _Calls(result=True)
    assert policy.allows("u", TOPIC, 1, handler=hook) is True, "a truthy built-in reaches the hook"
    for result in (False, None, 1, "True", [True], object()):
        got = policy.allows("u", TOPIC, True, handler=_Calls(result=result))
        assert got is False, f"a hook returning {result!r} must refuse, got {got!r}"


@th.django_unit_test()
def test_broken_hook_fails_closed(opts):
    from mojo.apps.realtime import policy

    got = policy.allows("u", TOPIC, True, handler=_Calls(error=RuntimeError("boom")))
    assert got is False, f"a hook that raises must refuse, got {got!r}"
    got = policy.allows("u", TOPIC, True, handler="not callable")
    assert got is False, f"a non-callable hook must refuse, got {got!r}"
    got = policy.allows("u", TOPIC, True, path=f"{POLICY}.no_such_function")
    assert got is False, f"a path that does not load must refuse, got {got!r}"
    got = policy.allows("u", TOPIC, True, path="no_such_package_8874.module.func")
    assert got is False, f"a missing module must refuse, got {got!r}"
    got = policy.allows("u", TOPIC, True, path=f"{POLICY}.HANDLER_KEY")
    assert got is False, f"a path naming a non-callable must refuse, got {got!r}"


@th.django_unit_test()
def test_shipped_hooks_behave_as_named(opts):
    from mojo.apps.realtime import policy

    assert policy.allows("u", TOPIC, True, path=f"{POLICY}.allow_all") is True, "allow_all must allow"
    assert policy.allows("u", TOPIC, True, path=f"{POLICY}.deny_all") is False, "deny_all must refuse"
    assert policy.allows("u", TOPIC, False, path=f"{POLICY}.allow_all") is False, \
        "allow_all must never widen a refusing built-in verdict"
