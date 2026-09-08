from types import SimpleNamespace
from unittest import mock

from testit import helpers as th


ADDRESS = {
    "address1": "123 Main St",
    "city": "Anytown",
    "state": "CA",
    "postal_code": "12345",
}


def _validate(
        *, provider=None, credentials=("client-id", "client-secret"),
        usps_result=None, google_result=None, usps_error=None,
        google_error=None):
    from mojo.helpers import location, logit
    from mojo.helpers.location import google, usps

    address = dict(ADDRESS)
    if provider is not None:
        address["provider"] = provider
    configured = SimpleNamespace(
        USPS_CLIENT_ID=credentials[0],
        USPS_CLIENT_SECRET=credentials[1],
    )
    usps_call = mock.Mock(return_value=usps_result, side_effect=usps_error)
    google_call = mock.Mock(return_value=google_result, side_effect=google_error)
    with mock.patch.object(location, "settings", configured), \
            mock.patch.object(usps, "validate_address", usps_call), \
            mock.patch.object(google, "validate_address", google_call), \
            mock.patch.object(logit, "warning") as warning:
        result = location.validate_address(address)
    return result, usps_call, google_call, warning


@th.django_unit_test("USPS exceptions fall through once to Google")
def test_usps_exception_falls_back_to_google(opts):
    from mojo.helpers.location.usps import USPSAuthenticationError

    google_result = {
        "valid": True,
        "source": "google_address_validation",
        "standardized_address": {"line1": "123 Main Street"},
    }
    result, usps_call, google_call, warning = _validate(
        usps_error=USPSAuthenticationError(
            "credential secret and 123 Main St must not reach the warning"),
        google_result=google_result,
    )

    assert result["valid"] is True, "Google success should govern after a USPS exception"
    assert result["provider"] == "google", "Fallback success should identify Google"
    assert result["source"] == "google_address_validation", \
        "Fallback should preserve Google's native source field"
    assert "provider" not in google_result, \
        "The router should not mutate Google's provider-owned mapping"
    assert usps_call.call_count == 1, "Configured USPS should be attempted exactly once"
    assert google_call.call_count == 1, "Google should be attempted exactly once after USPS fails"
    assert warning.call_count == 1, "USPS-to-Google failover should emit exactly one warning"
    assert warning.call_args.args == (
        "location address failover provider=usps "
        "class=USPSAuthenticationError status=False",
    ), "Failover warning should contain only bounded provider, class, and status data"


@th.django_unit_test("Google may supersede a USPS semantic rejection")
def test_usps_non_success_falls_back_to_google(opts):
    usps_result = {
        "valid": False,
        "error": "Address is missing secondary information (apt, suite, etc.)",
        "original_address": dict(ADDRESS),
    }
    google_result = {
        "valid": True,
        "source": "google_address_validation",
        "metadata": {"verdict": "PREMISE"},
    }
    result, usps_call, google_call, warning = _validate(
        usps_result=usps_result,
        google_result=google_result,
    )

    assert result["valid"] is True, "Google's validation policy should govern the final success"
    assert result["provider"] == "google", "Semantic fallback should identify Google"
    assert result["metadata"] == {"verdict": "PREMISE"}, \
        "Semantic fallback should preserve Google metadata"
    assert "provider" not in usps_result, "The router should not mutate the rejected USPS mapping"
    assert "provider" not in google_result, "The router should not mutate the successful Google mapping"
    assert usps_call.call_count == 1, "USPS semantic validation should run once"
    assert google_call.call_count == 1, "Google should run once after USPS rejects the address"
    assert warning.call_count == 1, "Semantic failover should emit exactly one warning"
    warning_text = warning.call_args.args[0]
    assert warning_text == (
        "location address failover provider=usps "
        "class=ValidationFailure status=False"
    ), "Semantic failover warning should be stable and bounded"
    assert usps_result["error"] not in warning_text, \
        "Semantic failover warning must not expose provider error text"


@th.django_unit_test("USPS success is returned without calling Google")
def test_usps_success_does_not_call_google(opts):
    usps_result = {
        "valid": True,
        "source": "usps_v3",
        "standardized_address": {"line1": "123 MAIN ST"},
        "corrections": {"corrections_applied": False},
    }
    result, usps_call, google_call, warning = _validate(usps_result=usps_result)

    assert result["provider"] == "usps", "Preferred-provider success should identify USPS"
    assert result["source"] == "usps_v3", "USPS success should preserve its native source"
    assert result["corrections"] == {"corrections_applied": False}, \
        "USPS success should preserve native correction fields"
    assert "provider" not in usps_result, "The router should not mutate the USPS result mapping"
    assert usps_call.call_count == 1, "USPS should be attempted exactly once"
    assert google_call.call_count == 0, "Google must not run after a USPS success"
    assert warning.call_count == 0, "Successful USPS validation should not emit a failover warning"


@th.django_unit_test("Explicit Google remains Google-only")
def test_explicit_google_never_calls_usps(opts):
    google_result = {"valid": True, "source": "google_address_validation"}
    result, usps_call, google_call, warning = _validate(
        provider="google",
        google_result=google_result,
    )

    assert result["provider"] == "google", "Explicit Google success should identify Google"
    assert usps_call.call_count == 0, "Explicit Google must never call USPS"
    assert google_call.call_count == 1, "Explicit Google should call Google exactly once"
    assert warning.call_count == 0, "Google-only validation should not emit a failover warning"


@th.django_unit_test("Incomplete USPS configuration routes directly to Google")
def test_missing_or_partial_usps_credentials_skip_usps(opts):
    credential_states = ((None, None), ("client-id", None), (None, "client-secret"))
    for credentials in credential_states:
        result, usps_call, google_call, warning = _validate(
            credentials=credentials,
            google_result={"valid": True, "source": "google_address_validation"},
        )
        assert result["provider"] == "google", \
            f"Credential state {credentials!r} should route directly to Google"
        assert usps_call.call_count == 0, \
            f"Credential state {credentials!r} should not attempt USPS"
        assert google_call.call_count == 1, \
            f"Credential state {credentials!r} should call Google exactly once"
        assert warning.call_count == 0, \
            f"Credential state {credentials!r} should not emit a failover warning"


@th.django_unit_test("Unknown providers retain USPS-like compatibility routing")
def test_unknown_provider_uses_usps_first_or_google_when_unavailable(opts):
    ready, usps_call, google_call, warning = _validate(
        provider="legacy-provider",
        usps_result={"valid": True, "source": "usps_v3"},
    )
    assert ready["provider"] == "usps", \
        "Unknown providers should remain USPS-first when USPS is configured"
    assert usps_call.call_count == 1, "Unknown-provider routing should attempt configured USPS"
    assert google_call.call_count == 0, "Unknown-provider routing should stop after USPS success"
    assert warning.call_count == 0, "Unknown-provider USPS success should not warn"

    unavailable, usps_call, google_call, warning = _validate(
        provider="legacy-provider",
        credentials=(None, None),
        google_result={"valid": True, "source": "google_address_validation"},
    )
    assert unavailable["provider"] == "google", \
        "Unknown providers should use Google when USPS is unavailable"
    assert usps_call.call_count == 0, "Unavailable USPS should be skipped for unknown providers"
    assert google_call.call_count == 1, "Unknown-provider fallback should call Google once"
    assert warning.call_count == 0, "Skipping unavailable USPS should not warn"


@th.django_unit_test("Both provider failures retain final native fields and combined errors")
def test_both_provider_failures_are_combined(opts):
    usps_result = {
        "valid": False,
        "error": "USPS could not confirm delivery",
        "original_address": dict(ADDRESS),
    }
    google_result = {
        "valid": False,
        "error": "Google could not validate address",
        "source": "google_address_validation",
        "metadata": {"verdict": "UNCONFIRMED_BUT_PLAUSIBLE"},
    }
    result, usps_call, google_call, warning = _validate(
        usps_result=usps_result,
        google_result=google_result,
    )

    assert result["status"] is False, "An all-provider failure should have false router status"
    assert result["provider"] == "google", "All-provider failure should identify the final provider"
    assert result["valid"] is False, "Final provider's native valid field should survive"
    assert result["source"] == "google_address_validation", \
        "Final provider's native source should survive"
    assert result["metadata"] == {"verdict": "UNCONFIRMED_BUT_PLAUSIBLE"}, \
        "Final provider's native metadata should survive"
    assert result["error"] == "Google could not validate address", \
        "Top-level error should be the final provider's message"
    assert result["errors"] == {
        "usps": "USPS could not confirm delivery",
        "google": "Google could not validate address",
    }, "All-provider failure should retain errors in attempt order"
    assert usps_call.call_count == 1, "USPS should be attempted once"
    assert google_call.call_count == 1, "Google should be attempted once after USPS failure"
    assert warning.call_count == 1, "Two-provider failure should emit one transition warning"


@th.django_unit_test("Google-only failure has stable provider and error attribution")
def test_google_only_failure_is_attributed(opts):
    google_result = {
        "valid": False,
        "error": "Address validation inconclusive",
        "metadata": {"verdict": "UNCONFIRMED_BUT_PLAUSIBLE"},
    }
    result, usps_call, google_call, warning = _validate(
        provider="google",
        google_result=google_result,
    )

    assert result["status"] is False, "Google-only failure should have false router status"
    assert result["provider"] == "google", "Google-only failure should identify Google"
    assert result["error"] == "Address validation inconclusive", \
        "Google-only failure should expose Google's final message"
    assert result["errors"] == {"google": "Address validation inconclusive"}, \
        "Google-only failure should contain only Google's error"
    assert result["metadata"] == {"verdict": "UNCONFIRMED_BUT_PLAUSIBLE"}, \
        "Google-only failure should preserve native fields"
    assert "provider" not in google_result, "The router should not mutate Google's failure mapping"
    assert usps_call.call_count == 0, "Google-only failure must not call USPS"
    assert google_call.call_count == 1, "Google-only failure should call Google once"
    assert warning.call_count == 0, "Google-only failure should not emit a failover warning"


@th.django_unit_test("Non-mapping results become stable attributed failures")
def test_non_mapping_result_is_normalized(opts):
    result, usps_call, google_call, warning = _validate(
        provider="google",
        google_result=None,
    )

    assert result == {
        "status": False,
        "provider": "google",
        "error": "Google address validator returned an invalid response",
        "errors": {"google": "Google address validator returned an invalid response"},
    }, "Non-mapping Google output should become a stable failure response"
    assert usps_call.call_count == 0, "Non-mapping Google output must not cause USPS fallback"
    assert google_call.call_count == 1, "Non-mapping Google output should follow one Google call"
    assert warning.call_count == 0, "Google-only invalid output should not warn"


@th.django_unit_test("Only the boolean true value counts as provider success")
def test_truthy_non_boolean_valid_value_falls_back(opts):
    result, usps_call, google_call, warning = _validate(
        usps_result={"valid": 1, "error": "Ambiguous USPS response"},
        google_result={"valid": True, "source": "google_address_validation"},
    )

    assert result["provider"] == "google", "Integer valid=1 should not count as USPS success"
    assert usps_call.call_count == 1, "USPS should be called once for a non-boolean valid result"
    assert google_call.call_count == 1, "Non-boolean USPS valid result should fall through to Google"
    assert warning.call_count == 1, "Non-boolean USPS valid result should emit one failover warning"
