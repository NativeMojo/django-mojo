"""The plain-words summary of an LLM safety policy change (llm_policy_change).

Pure functions over two policy dicts: no database, no settings.
"""

import copy

from testit import helpers as th


def _limits(**overrides):
    value = {
        "requests_minute": 10, "requests_hour": 100, "requests_day": 1000,
        "tokens_minute": 10000, "tokens_hour": 100000, "tokens_day": 1000000,
        "concurrency": 2, "max_input_bytes": 120000, "max_output_tokens": 1024,
        "timeout_seconds": 30, "max_loop_calls": 4,
    }
    value.update(overrides)
    return value


def _policy():
    return {
        "version": 1,
        "routes": {
            "chat": {"provider": "bedrock", "model": "us.amazon.nova-2-lite-v1:0",
                     "credential": "aws", "capabilities": ["text", "tools"],
                     "region": "us-east-1", "reasoning_efforts": ["off"]},
        },
        "shared": _limits(max_input_bytes=200000),
        "features": {"chat": _limits()},
        "breaker": {"auth_failures": 2, "rate_failures": 5,
                    "server_failures": 5, "open_seconds": 60},
    }


@th.django_unit_test("policy change: nothing changed says so")
def test_nothing_changed(opts):
    from mojo.apps.account.services import llm_policy_change

    result = llm_policy_change.describe(_policy(), _policy())
    assert result["changes"] == [], f"no change expected, got {result['changes']}"
    assert result["tldr"] == "Nothing in the policy changed.", result["tldr"]
    assert result["first"] is False, f"a compared change is not a first record: {result}"


@th.django_unit_test("policy change: an added feature and raised limits are named")
def test_added_feature_and_raised_limits(opts):
    from mojo.apps.account.services import llm_policy_change

    old = _policy()
    new = copy.deepcopy(old)
    new["routes"]["call_notes"] = {
        "provider": "bedrock", "model": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "credential": "aws", "capabilities": ["text"], "region": "us-east-1",
        "reasoning_efforts": ["off"]}
    new["features"]["call_notes"] = _limits(max_input_bytes=450000)
    new["shared"]["max_input_bytes"] = 450000
    new["hash"] = "ignored"
    result = llm_policy_change.describe(new, old)
    assert result["tldr"] == "This policy adds call_notes; raises 1 limit.", result["tldr"]
    assert any(line.startswith("Adds call_notes: bedrock us.anthropic.claude-haiku")
               for line in result["changes"]), result["changes"]
    assert ("all features together: input size (bytes) raised 200,000 → 450,000."
            in result["changes"]), result["changes"]
    assert not any(line.startswith("call_notes:") for line in result["changes"]), \
        f"a new feature's own limits are not raises: {result['changes']}"


@th.django_unit_test("policy change: model, settings, removal, lowered limits and breaker")
def test_model_capability_removal_and_lowered(opts):
    from mojo.apps.account.services import llm_policy_change

    old = _policy()
    old["routes"]["mail"] = copy.deepcopy(old["routes"]["chat"])
    old["features"]["mail"] = _limits()
    new = copy.deepcopy(old)
    del new["routes"]["mail"]
    del new["features"]["mail"]
    new["routes"]["chat"]["model"] = "us.amazon.nova-2-pro-v1:0"
    new["routes"]["chat"]["capabilities"] = ["text", "tools", "images"]
    new["features"]["chat"]["requests_day"] = 500
    new["breaker"]["open_seconds"] = 120
    result = llm_policy_change.describe(new, old)
    changes = result["changes"]
    assert "Removes mail (was bedrock us.amazon.nova-2-lite-v1:0 in us-east-1, with tools)." \
        in changes, changes
    assert ("chat: model bedrock us.amazon.nova-2-lite-v1:0 → bedrock "
            "us.amazon.nova-2-pro-v1:0.") in changes, changes
    assert "chat: can now use images." in changes, changes
    assert "chat: requests a day lowered 1,000 → 500." in changes, changes
    assert "Failure breaker: seconds it stays tripped 60 → 120." in changes, changes
    assert result["tldr"] == ("This policy removes mail; changes the model or "
                              "settings of chat; lowers 1 limit; changes the "
                              "failure breaker."), result["tldr"]


@th.django_unit_test("policy change: with no earlier record the features are listed")
def test_first_record_lists_the_policy(opts):
    from mojo.apps.account.services import llm_policy_change

    result = llm_policy_change.describe(_policy(), None)
    assert result["first"] is True, f"no record means a first record: {result}"
    assert result["tldr"] == ("No earlier policy was recorded to compare with. This "
                              "policy allows 1 AI feature: chat."), result["tldr"]
    assert result["changes"] == [
        "chat: bedrock us.amazon.nova-2-lite-v1:0 in us-east-1, with tools."], \
        result["changes"]


@th.django_unit_test("policy change: a breaker-only change still has a TL;DR")
def test_breaker_only(opts):
    from mojo.apps.account.services import llm_policy_change

    new = _policy()
    new["breaker"]["auth_failures"] = 3
    result = llm_policy_change.describe(new, _policy())
    assert result["tldr"] == "This policy changes the failure breaker.", result["tldr"]
    assert result["changes"] == [
        "Failure breaker: sign-in failures before it trips 2 → 3."], result["changes"]


@th.django_unit_test("policy change: a reorder that changes the hash is named, not hidden")
def test_reorder_only(opts):
    from mojo.apps.account.services import llm_policy_change

    new = _policy()
    new["routes"]["chat"]["capabilities"] = ["tools", "text"]
    result = llm_policy_change.describe(new, _policy())
    assert result["tldr"] == ("This policy only reorders entries; nothing it allows "
                              "changes."), result["tldr"]


@th.django_unit_test("policy change: a long list is capped with a count of the rest")
def test_long_list_is_capped(opts):
    from mojo.apps.account.services import llm_policy_change

    old, new = _policy(), _policy()
    for index in range(80):
        name = f"feature_{index:02d}"
        new["routes"][name] = copy.deepcopy(old["routes"]["chat"])
        new["features"][name] = _limits()
    result = llm_policy_change.describe(new, old)
    changes = result["changes"]
    kept = len(changes) - 1
    assert 0 < kept < llm_policy_change.MAX_CHANGES, \
        f"80 changes must be cut below the line cap, got {len(changes)}"
    assert sum(len(line.encode()) for line in changes) <= \
        llm_policy_change.MAX_CHANGES_BYTES + 100, \
        "the list must stay under the byte cap so the approval result keeps the TL;DR"
    assert changes[-1] == f"…and {80 - kept} more changes.", \
        f"the last line must count what was left out, got {changes[-1]!r}"
    assert result["tldr"].startswith("This policy adds feature_00, feature_01, "
                                     "feature_02 and 77 more"), result["tldr"]
