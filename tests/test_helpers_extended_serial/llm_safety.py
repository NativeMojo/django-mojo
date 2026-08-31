from unittest import mock
import uuid

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


@th.django_unit_test()
def test_stored_probe_uses_exact_target_and_candidate_requires_owner(opts):
    from mojo import errors as merrors
    from mojo.apps.account.models import User
    from mojo.apps.account.services import llm_safety

    captured = []

    def fixed(actor, credential, identity, operation):
        captured.append((credential, identity, operation))
        return True

    def configured(key, default=None, **kwargs):
        return {
            "LLM_ADMIN_API_KEY": "admin-exact-secret",
            "LLM_HANDLER_API_KEY": "handler-exact-secret",
        }.get(key, default)

    with mock.patch.object(llm_safety.settings, "get", side_effect=configured), \
            mock.patch.object(llm_safety, "_fixed_configuration_probe", side_effect=fixed):
        assert llm_safety.verify_stored_key("admin") is True, \
            "the exact stored admin target did not verify"
        assert llm_safety.verify_stored_key("handler") is True, \
            "the exact stored handler target did not verify"
    assert [row[0] for row in captured] == [
        "admin-exact-secret", "handler-exact-secret"], \
        f"stored verification discarded or fell back from its target: {captured}"
    assert all(row[1] != "candidate-installation" for row in captured), \
        f"stored probes must never receive the candidate recovery identity: {captured}"

    with mock.patch.object(
            llm_safety.settings, "get",
            side_effect=lambda key, default=None, **kwargs:
            "handler-only" if key == "LLM_HANDLER_API_KEY" else default):
        try:
            llm_safety.verify_stored_key("admin")
            assert False, "missing exact admin target must not fall back to handler"
        except llm_safety.LLMSafetyError as err:
            assert err.code == "credential_missing", \
                f"missing exact target used wrong safe code: {err.code}"

    regular = User.objects.create_user(
        username=f"candidate-non-owner-{uuid.uuid4().hex}",
        email=f"candidate-non-owner-{uuid.uuid4().hex}@test.com",
        password="Candidate_owner_test_99")
    regular.is_active = True
    regular.save()
    try:
        with th.assert_raises(merrors.PermissionDeniedException):
            llm_safety.verify_candidate(regular, "candidate-secret")
        regular.is_superuser = True
        regular.save(update_fields=["is_superuser"])
        for invalid in (None, "", "   ", " candidate-secret "):
            try:
                llm_safety.verify_candidate(regular, invalid)
                assert False, f"invalid candidate input was accepted: {invalid!r}"
            except llm_safety.LLMSafetyError as err:
                assert err.code == "credential_missing", \
                    f"invalid candidate used wrong safe code: {err.code}"
    finally:
        regular.delete()


@th.django_unit_test()
def test_candidate_boundary_audits_outcomes_and_fails_before_provider(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services import llm_safety
    from mojo.apps.logit.models import Log

    owner = User.objects.create_user(
        username=f"candidate-audit-{uuid.uuid4().hex}",
        email=f"candidate-audit-{uuid.uuid4().hex}@test.com",
        password="Candidate_audit_test_99")
    owner.is_active = True
    owner.is_superuser = True
    owner.save()
    Log.objects.filter(
        kind="llm:candidate_probe", model_name="account.User",
        model_id=owner.pk).delete()
    try:
        with mock.patch.object(
                llm_safety, "_fixed_configuration_probe", return_value=True):
            assert llm_safety.verify_candidate(owner, "accepted-candidate") is True, \
                "an accepted direct candidate probe did not return success"
        accepted = Log.objects.filter(
            kind="llm:candidate_probe", model_name="account.User",
            model_id=owner.pk).order_by("created").last()
        assert accepted and accepted.log == \
            "LLM candidate recovery probe outcome=accepted", \
            f"accepted direct probe lacks its bounded actor audit: {accepted}"

        with mock.patch.object(
                llm_safety, "_fixed_configuration_probe",
                side_effect=llm_safety.LLMSafetyError(
                    "provider_authentication")):
            try:
                llm_safety.verify_candidate(owner, "rejected-candidate")
                assert False, "a provider-rejected candidate returned success"
            except llm_safety.LLMSafetyError as err:
                assert err.code == "provider_authentication", \
                    f"provider rejection lost its safe code: {err.code}"
        rejected = Log.objects.filter(
            kind="llm:candidate_probe", model_name="account.User",
            model_id=owner.pk).order_by("created").last()
        assert rejected and rejected.log == \
            "LLM candidate recovery probe outcome=rejected", \
            f"rejected direct probe lacks its bounded actor audit: {rejected}"

        provider = mock.Mock()
        with mock.patch.object(
                User, "log", side_effect=RuntimeError("audit unavailable")), \
                mock.patch.object(
                    llm_safety, "_fixed_configuration_probe", provider):
            try:
                llm_safety.verify_candidate(owner, "audit-failed-candidate")
                assert False, "an unaudited candidate probe was allowed"
            except llm_safety.LLMSafetyError as err:
                assert err.code == "safety_unavailable", \
                    f"audit failure used the wrong safe code: {err.code}"
        assert provider.call_count == 0, \
            f"audit failure reached the provider path {provider.call_count} time(s)"
    finally:
        Log.objects.filter(
            kind="llm:candidate_probe", model_name="account.User",
            model_id=owner.pk).delete()
        owner.delete()


@th.django_unit_test()
def test_fixed_probe_is_text_only_uncached_and_stored_stop_is_enforced(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services import llm_safety

    calls = []

    class Adapter:
        def supports(self, capability):
            return capability == "text"

        def call(self, **kwargs):
            calls.append(kwargs)
            return {"id": "probe-one", "usage": {
                "input_tokens": 2, "output_tokens": 1}}

    policy = {
        "version": 1,
        "routes": {"configuration": {
            "provider": "anthropic", "model": "claude-config-exact",
            "credential": "admin", "capabilities": ["text"],
        }},
        "shared": {
            "requests_minute": 2, "requests_hour": 10, "requests_day": 20,
            "tokens_minute": 1000, "tokens_hour": 5000, "tokens_day": 10000,
            "concurrency": 4, "max_input_bytes": 4096, "max_output_tokens": 128,
            "timeout_seconds": 10, "max_loop_calls": 2,
        },
        "features": {},
        "breaker": {"auth_failures": 2, "rate_failures": 2,
                    "server_failures": 2, "open_seconds": 60},
    }
    policy["features"]["configuration"] = dict(policy["shared"])
    policy = llm_safety.parse_policy(policy)
    owner = User.objects.create_user(
        username=f"candidate-owner-{uuid.uuid4().hex}",
        email=f"candidate-owner-{uuid.uuid4().hex}@test.com",
        password="Candidate_owner_test_99")
    owner.is_active = True
    owner.is_superuser = True
    owner.save()
    candidate = f"candidate-{uuid.uuid4().hex}"
    try:
        with mock.patch.object(llm_safety, "parse_policy", return_value=policy), \
                mock.patch.object(llm_safety, "_policy_agreement", lambda value: None), \
                mock.patch.object(llm_safety, "emergency_stopped", return_value=True), \
                mock.patch("mojo.helpers.llm_providers.get_provider",
                           side_effect=lambda name, api_key: Adapter()):
            assert llm_safety.verify_candidate(owner, candidate) is True, \
                "the actor-bound fixed candidate probe did not complete while stopped"
    finally:
        owner.delete()
    assert len(calls) == 1, f"one probe must equal one adapter call, got {len(calls)}"
    assert calls[0]["messages"] == [{"role": "user", "content": "Reply OK"}] \
        and calls[0]["system"] is None and calls[0]["tools"] is None, \
        f"configuration probe shape is not fixed and text-only: {calls[0]}"
    assert calls[0]["model"] == "claude-config-exact" \
        and calls[0]["max_tokens"] == 4 and calls[0]["cache_enabled"] is False, \
        f"configuration probe did not bind route model/4 tokens/no cache: {calls[0]}"

    with mock.patch.object(llm_safety, "parse_policy", return_value=policy), \
            mock.patch.object(llm_safety, "_policy_agreement", lambda value: None), \
            mock.patch.object(llm_safety, "emergency_stopped", return_value=True), \
            mock.patch.object(llm_safety.settings, "get", return_value="stored-secret"), \
            mock.patch("mojo.helpers.llm_providers.get_provider",
                       side_effect=lambda name, api_key: Adapter()):
        try:
            llm_safety.verify_stored_key("admin")
            assert False, "the stored probe bypassed the emergency stop"
        except llm_safety.LLMSafetyError as err:
            assert err.code == "emergency_stopped", \
                f"stored stop denial used wrong code: {err.code}"


@th.django_unit_test()
def test_malformed_emergency_controls_fail_closed_before_provider(opts):
    from mojo.apps.account.models import Setting
    from mojo.apps.account.services import llm_safety

    limits = {
        "requests_minute": 2, "requests_hour": 10, "requests_day": 20,
        "tokens_minute": 1000, "tokens_hour": 5000, "tokens_day": 10000,
        "concurrency": 2, "max_input_bytes": 4096, "max_output_tokens": 128,
        "timeout_seconds": 10, "max_loop_calls": 2,
    }
    raw = {
        "version": 1,
        "routes": {"assistant": {
            "provider": "anthropic", "model": "claude-exact",
            "credential": "admin", "capabilities": ["text"],
        }},
        "shared": dict(limits), "features": {"assistant": dict(limits)},
        "breaker": {"auth_failures": 2, "rate_failures": 2,
                    "server_failures": 2, "open_seconds": 60},
    }
    policy = llm_safety.parse_policy(raw)
    provider = mock.Mock()
    with mock.patch.object(llm_safety, "parse_policy", return_value=policy), \
            mock.patch.object(llm_safety, "_policy_agreement", lambda value: None), \
            mock.patch.object(llm_safety.settings, "get_static", return_value="maybe"), \
            mock.patch.object(llm_safety.settings, "get", return_value="admin-secret"), \
            mock.patch("mojo.helpers.llm_providers.get_provider", provider):
        state = llm_safety.route_state("assistant")
    assert state["ready"] is False and state["error"] == "control_state_unknown", \
        f"malformed static stop did not fail closed: {state}"
    assert provider.call_count == 0, \
        "malformed static stop reached a provider adapter"

    Setting.objects.filter(key="LLM_EMERGENCY_STOP", group=None).delete()
    Setting.objects.bulk_create([
        Setting(key="LLM_EMERGENCY_STOP", group=None, value="maybe")])
    try:
        with mock.patch.object(llm_safety, "parse_policy", return_value=policy), \
                mock.patch.object(llm_safety, "_policy_agreement", lambda value: None), \
                mock.patch.object(llm_safety.settings, "get_static", return_value=False), \
                mock.patch.object(llm_safety.settings, "get", return_value="admin-secret"), \
                mock.patch("mojo.helpers.llm_providers.get_provider", provider):
            state = llm_safety.route_state("assistant")
        assert state["ready"] is False and state["error"] == "control_state_unknown", \
            f"malformed database stop did not fail closed: {state}"
        assert provider.call_count == 0, \
            "malformed database stop reached a provider adapter"
    finally:
        Setting.objects.filter(key="LLM_EMERGENCY_STOP", group=None).delete()
