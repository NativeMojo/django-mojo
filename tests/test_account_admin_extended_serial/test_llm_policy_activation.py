"""Activating a deployed LLM safety policy says what it changed.

Overrides LLM_SAFETY_POLICY / LLM_HOST_FEATURES in-process and writes the two
protected policy rows, so it lives in this serial package. Both rows are
restored afterwards.
"""

import copy
from contextlib import contextmanager

from testit import helpers as th


ROOT_EMAIL = "llm-policy-root@example.com"
PLAIN_EMAIL = "llm-policy-plain@example.com"


@contextmanager
def _override_setting(name, value):
    import django.conf
    sentinel = object()
    original = getattr(django.conf.settings, name, sentinel)
    setattr(django.conf.settings, name, value)
    try:
        yield
    finally:
        if original is sentinel:
            delattr(django.conf.settings, name)
        else:
            setattr(django.conf.settings, name, original)


@contextmanager
def _policy_rows():
    """Start with no activation recorded; put the original rows back after."""
    from mojo.apps.account.models import Setting
    from mojo.apps.account.services import llm_safety
    keys = (llm_safety.EXPECTED_POLICY_HASH_KEY, llm_safety.ACTIVATED_POLICY_KEY)
    originals = [(row.pk, row.key, row.value) for row in
                 Setting.objects.filter(key__in=keys, group=None).order_by("pk")]
    Setting.objects.filter(key__in=keys, group=None).delete()
    try:
        yield
    finally:
        Setting.objects.filter(key__in=keys, group=None).delete()
        redis = Setting._redis()
        if redis:
            redis.hdel(Setting._redis_key(), *keys)
        for pk, key, raw in originals:
            restored = Setting(pk=pk, key=key, group=None, is_secret=False, value=raw)
            restored.save(_protected_writer=key, _skip_cache=True)


def _write_hash_only(value):
    """What an older release's activation left: the hash, no record."""
    from mojo.apps.account.models import Setting
    from mojo.apps.account.services import llm_safety
    key = llm_safety.EXPECTED_POLICY_HASH_KEY
    Setting.objects.filter(key=key, group=None).delete()
    row = Setting(key=key, group=None, is_secret=False)
    row.set_value(value)
    row.save(_protected_writer=key, _skip_cache=True)


def _limits(**overrides):
    value = {
        "requests_minute": 50, "requests_hour": 50, "requests_day": 50,
        "tokens_minute": 100000, "tokens_hour": 100000, "tokens_day": 100000,
        "concurrency": 2, "max_input_bytes": 4096, "max_output_tokens": 128,
        "timeout_seconds": 10, "max_loop_calls": 4,
    }
    value.update(overrides)
    return value


def _policy():
    return {
        "version": 1,
        "routes": {
            "support_test": {
                "provider": "anthropic", "model": "claude-test",
                "credential": "handler", "capabilities": ["text"],
            },
        },
        "shared": _limits(),
        "features": {"support_test": _limits(requests_minute=10, requests_hour=20,
                                             requests_day=20)},
        "breaker": {"auth_failures": 2, "rate_failures": 2,
                    "server_failures": 2, "open_seconds": 60},
    }


def _user(email, superuser):
    from mojo.apps.account.models import User
    User.objects.filter(email=email).delete()
    user = User.objects.create_user(username=email, email=email, password="TestPass1!")
    user.is_email_verified = True
    user.is_superuser = superuser
    user.save()
    user.add_permission("admin")
    return user


def _handler(name):
    from mojo.apps.assistant import get_registry
    return get_registry()[name]["handler"]


@th.django_unit_test("policy activation: records the policy and says what changed")
def test_activation_records_and_summarizes(opts):
    from mojo.apps.account.models import Setting
    from mojo.apps.account.services import llm_safety

    root = _user(ROOT_EMAIL, True)
    first = _policy()
    second = copy.deepcopy(first)
    second["routes"]["support_test_two"] = {
        "provider": "anthropic", "model": "claude-test-two",
        "credential": "handler", "capabilities": ["text", "tools"]}
    second["features"]["support_test_two"] = _limits()
    second["features"]["support_test"]["requests_day"] = 40
    activate = _handler("activate_llm_policy")
    read = _handler("get_llm_policy_change")
    try:
        with _policy_rows(), \
                _override_setting("LLM_HOST_FEATURES", ["support_test", "support_test_two"]):
            with _override_setting("LLM_SAFETY_POLICY", first):
                change = read({}, root)
                assert change["active"] is False and change["first_recorded"] is True, \
                    f"nothing activated yet: {change}"
                done = activate({"hash": change["hash"]}, root)
                assert done.get("activated") is True and done["already_active"] is False, \
                    f"the first activation must succeed: {done}"
                assert done["tldr"].startswith("No earlier policy was recorded"), done
                stored = Setting.objects.get(
                    key=llm_safety.EXPECTED_POLICY_HASH_KEY, group=None).get_value()
                assert stored == change["hash"], "activation must write the deployed hash"
                again = read({}, root)
                assert again["active"] is True and again["record_needed"] is False, \
                    f"activated and recorded: {again}"

            with _override_setting("LLM_SAFETY_POLICY", second):
                change = read({}, root)
                assert change["active"] is False, f"the new policy is not active: {change}"
                assert change["tldr"] == ("This policy adds support_test_two; "
                                          "raises 1 limit."), change["tldr"]
                assert ("support_test: requests a day raised 20 → 40."
                        in change["changes"]), change["changes"]

                stale = activate({"hash": "0" * 64}, root)
                assert stale.get("error_code") == "policy_changed", \
                    f"a hash other than the deployed one must be refused: {stale}"
                assert llm_safety.policy_change(root)["active"] is False, \
                    "a refused activation must not activate"

                result = activate({"hash": change["hash"]}, root)
                assert result.get("activated") is True, result
                assert result["tldr"] == change["tldr"], \
                    f"the activation must report what the read promised: {result}"
                assert "owns this installation" in result["next"], result

                repeat = activate({"hash": change["hash"]}, root)
                assert repeat["already_active"] is True and repeat["changes"] == [], \
                    f"activating an active policy changes nothing: {repeat}"
                assert "already active" in repeat["tldr"], repeat

            with _override_setting("LLM_SAFETY_POLICY", first):
                back = llm_safety.policy_change(root)
                assert back["tldr"] == ("This policy removes support_test_two; "
                                        "lowers 1 limit."), back["tldr"]
    finally:
        from mojo.apps.account.models import User
        User.objects.filter(email=ROOT_EMAIL).delete()


@th.django_unit_test("policy activation: a hash-only activation from an older release")
def test_hash_without_record(opts):
    from mojo.apps.account.services import llm_safety

    root = _user(ROOT_EMAIL, True)
    other = _policy()
    other["features"]["support_test"]["concurrency"] = 3
    try:
        with _policy_rows(), _override_setting("LLM_HOST_FEATURES", ["support_test"]):
            with _override_setting("LLM_SAFETY_POLICY", _policy()):
                deployed = llm_safety.parse_policy()["hash"]
                _write_hash_only(deployed)
                change = llm_safety.policy_change(root)
                assert change["active"] is True and change["record_needed"] is True, \
                    f"active but unrecorded must ask for one activation: {change}"
                done = llm_safety.activate_with_summary(root, expected_hash=deployed)
                assert done["already_active"] is True and done["changes"] == [], done
                assert "now recorded" in done["tldr"], done["tldr"]
                assert llm_safety.policy_change(root)["record_needed"] is False, \
                    "one activation records it"
            with _override_setting("LLM_SAFETY_POLICY", other):
                # The record no longer matches a repaired/foreign hash row.
                _write_hash_only(llm_safety.parse_policy()["hash"][::-1])
                change = llm_safety.policy_change(root)
                assert change["tldr"].startswith("The saved record did not match"), \
                    f"a record that does not match the active hash is not a baseline: {change}"
    finally:
        from mojo.apps.account.models import User
        User.objects.filter(email=ROOT_EMAIL).delete()


@th.django_unit_test("policy activation: only an active superuser may see or activate it")
def test_activation_needs_a_superuser(opts):
    from mojo.apps.account.models import Setting
    from mojo.apps.account.services import admin_settings, llm_safety
    from mojo.apps.assistant import get_registry, user_can_use_tool
    from mojo.apps.assistant.services import mcp_execution

    plain = _user(PLAIN_EMAIL, False)
    root = _user(ROOT_EMAIL, True)
    registry = get_registry()
    try:
        entry = registry["activate_llm_policy"]
        assert entry["mutates"] is True and entry["fresh_auth_seconds"] is None \
            and entry["preview"] is None and entry["authorize"] is not None, \
            "the owner's decision: a mutating tool with no fresh-auth or preview gate"
        assert mcp_execution.requires_approval(entry) is False, \
            "an MCP execution grant must be able to run the activation directly"
        for name in ("activate_llm_policy", "get_llm_policy_change"):
            assert user_can_use_tool(plain, registry[name]) is False, \
                f"{name} must not be offered to a non-superuser holding admin"
            assert user_can_use_tool(root, registry[name]) is True, \
                f"{name} must be offered to a superuser"
        with _policy_rows(), _override_setting("LLM_HOST_FEATURES", ["support_test"]), \
                _override_setting("LLM_SAFETY_POLICY", _policy()):
            for name in ("activate_llm_policy", "get_llm_policy_change"):
                result = _handler(name)({"hash": "x"}, plain)
                assert result.get("error_code") == "permission_denied", (name, result)
            assert not Setting.objects.filter(
                key__in=[llm_safety.EXPECTED_POLICY_HASH_KEY,
                         llm_safety.ACTIVATED_POLICY_KEY], group=None).exists(), \
                "a refused activation must write nothing"
        assert llm_safety.ACTIVATED_POLICY_KEY in admin_settings.ASSISTANT_KEYS, \
            "the recorded policy must be refused by the generic settings writers"
        assert llm_safety.ACTIVATED_POLICY_KEY in admin_settings.PROTECTED_WRITER_KEYS, \
            "the activation must be able to write the recorded policy"
    finally:
        from mojo.apps.account.models import User
        User.objects.filter(email__in=[PLAIN_EMAIL, ROOT_EMAIL]).delete()


@th.django_unit_test("policy activation: a policy that does not parse is refused")
def test_invalid_policy_is_refused(opts):
    from mojo.apps.account.models import Setting
    from mojo.apps.account.services import llm_safety

    root = _user(ROOT_EMAIL, True)
    broken = _policy()
    broken["routes"]["not_a_feature"] = broken["routes"]["support_test"]
    try:
        with _policy_rows(), _override_setting("LLM_HOST_FEATURES", ["support_test"]), \
                _override_setting("LLM_SAFETY_POLICY", broken):
            result = _handler("activate_llm_policy")({"hash": "x"}, root)
            assert result.get("error_code") == "policy_invalid", result
            assert not Setting.objects.filter(
                key=llm_safety.EXPECTED_POLICY_HASH_KEY, group=None).exists(), \
                "a policy that does not parse must not be activated"
    finally:
        from mojo.apps.account.models import User
        User.objects.filter(email=ROOT_EMAIL).delete()
