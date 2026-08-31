from unittest import mock

from testit import helpers as th


@th.django_unit_test()
def test_explicit_admin_credential_never_falls_back_to_handler(opts):
    from mojo.apps.account.services import llm_safety

    def configured(key, default=None):
        if key == "LLM_HANDLER_API_KEY":
            return "handler-secret"
        return default

    route = {"credential": "admin"}
    with mock.patch.object(llm_safety.settings, "get", side_effect=configured):
        try:
            llm_safety._credential(route)
            assert False, "an explicit admin route must deny when the admin key is absent"
        except llm_safety.LLMSafetyError as err:
            assert err.code == "credential_missing", \
                f"explicit admin denial must use credential_missing, got {err.code}"
