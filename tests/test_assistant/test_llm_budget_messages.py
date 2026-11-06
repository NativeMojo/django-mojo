from testit import helpers as th


@th.django_unit_test()
def test_budget_codes_have_plain_messages(opts):
    from mojo.apps.assistant.services import agent

    too_big = agent._llm_error_message("request_exceeds_budget")
    assert "too large" in too_big and "request_exceeds_budget" not in too_big, \
        f"a call that can never fit must get a plain, permanent message, got {too_big!r}"
    later = agent._llm_error_message("budget_exhausted", 42)
    assert "42 seconds" in later, \
        f"a used-up budget must say when to retry, got {later!r}"
    unknown = agent._llm_error_message("budget_exhausted")
    assert "Try again later" in unknown, \
        f"a used-up budget without a hint must still say to retry, got {unknown!r}"
    assert agent._llm_error_message("provider_authentication") == agent.PROVIDER_AUTH_ERROR, \
        "existing provider messages must be unchanged"
    assert agent._llm_error_message("provider_failed") == \
        "Assistant request failed safely (provider_failed).", \
        "other codes must keep the generic safe message"
