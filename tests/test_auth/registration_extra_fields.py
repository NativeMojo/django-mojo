"""Regression coverage for hosted registration extra-field presentation."""

import json
import re
from urllib.parse import parse_qs, urlsplit

from testit import helpers as th


TESTIT_TIER = "bug"


# Deliberately independent of register_schema.RESERVED_EXTRA_FIELDS: changing
# the production constant cannot make the expected namespace test itself.
EXPECTED_RESERVED_EXTRA_NAMES = (
    "first_name", "last_name", "email", "phone", "dob", "password",
    "username", "phone_number",
    "group", "group_uuid",
    "redirect", "next", "returnTo", "back", "webapp_base_url",
    "redirect_uri",
    "force_reauth", "auth_theme", "auth_appearance",
    "token", "code", "state", "auth_code", "bouncer_token",
    "verified_phone_token", "session_token", "mfa_token", "access_token",
    "refresh_token", "recovery_code", "current_password", "new_password",
    "duid", "muid", "fp",
    "client_id", "response_type", "scope", "code_challenge",
    "code_challenge_method", "code_verifier", "grant_type", "resource",
    "challenge_id", "credential",
)

# These controls are not among the separately handled challenge navigation
# parameters, so none may appear in a challenge destination even if a broken
# deployment attempts to declare them as extras.
NON_FORWARDABLE_RESERVED_NAMES = (
    "username", "phone_number", "webapp_base_url", "redirect_uri",
    "auth_code", "bouncer_token", "verified_phone_token", "session_token",
    "mfa_token", "access_token", "refresh_token", "recovery_code",
    "current_password", "new_password", "duid", "muid", "fp", "client_id",
    "response_type", "scope", "code_challenge", "code_challenge_method",
    "code_verifier", "grant_type", "resource", "challenge_id", "credential",
)


def _request(path, query, extra_fields):
    from django.test import RequestFactory

    return RequestFactory(REMOTE_ADDR="127.0.0.1").get(
        path, query,
        HTTP_X_MOJO_TEST_REGISTER_EXTRA_FIELDS=json.dumps(extra_fields),
    )


def _challenge_destination(request, page_type="registration", tier=1):
    from mojo.apps.account.rest.bouncer.views import _serve_challenge

    html = _serve_challenge(
        request, challenge_tier=tier, page_type=page_type,
    ).content.decode("utf-8")
    match = re.search(r'redirectUrl:\s*"([^"]*)"', html)
    assert match is not None, "the challenge must render one redirectUrl string"
    return json.loads(f'"{match.group(1)}"'), html


@th.django_unit_test("bouncer challenge preserves schema-declared registration attribution")
def test_challenge_preserves_declared_registration_extras(opts):
    import objict

    query = {
        "ref": "partner 42",
        "promo": "WELCOME&100",
        "tracking": "https://tracker.test/a?b=1",
        "utm_source": "must-drop",
    }
    request = _request("/register", query, [
        "ref",
        {"name": "promo", "capture_only": True},
        {"name": "tracking"},
    ])
    request.DATA = objict.objict(query)

    destination, _ = _challenge_destination(request)
    params = parse_qs(urlsplit(destination).query)

    assert params.get("ref") == ["partner 42"], \
        f"the challenge must preserve a declared referral value, got {destination!r}"
    assert params.get("promo") == ["WELCOME&100"], \
        f"capture-only promo attribution must survive the challenge, got {destination!r}"
    assert params.get("tracking") == ["https://tracker.test/a?b=1"], \
        f"scheme-looking values must remain encoded data, got {destination!r}"
    assert "utm_source" not in params, \
        f"undeclared campaign parameters must not be forwarded, got {destination!r}"

    from django.test import RequestFactory
    repeated_request = RequestFactory(REMOTE_ADDR="127.0.0.1").get(
        "/register?ref=first&ref=second",
        HTTP_X_MOJO_TEST_REGISTER_EXTRA_FIELDS=json.dumps(["ref"]),
    )
    repeated_destination, _ = _challenge_destination(repeated_request)
    assert "ref=" not in repeated_destination, \
        f"a repeated real query param must be dropped, got {repeated_destination!r}"


@th.django_unit_test("legacy capture allowlist alone does not authorize a challenge hop")
def test_legacy_capture_allowlist_does_not_forward(opts):
    import objict
    from django.test import RequestFactory

    request = RequestFactory(REMOTE_ADDR="127.0.0.1").get(
        "/register", {"legacy_ref": "partner-42"},
        HTTP_X_MOJO_TEST_REGISTRATION_EXTRA_FIELDS=json.dumps(["legacy_ref"]),
    )
    request.DATA = objict.objict({"legacy_ref": "partner-42"})
    destination, _ = _challenge_destination(request)
    assert "legacy_ref=" not in destination, \
        f"REGISTRATION_EXTRA_FIELDS must capture at POST only, got {destination!r}"


@th.django_unit_test("challenge uses deployment and inherited registration-extra config")
def test_challenge_uses_resolved_extra_field_config(opts):
    import objict
    from django.test import RequestFactory
    from mojo.apps.account.models import Group

    parent_uuid = "attr-parent-3699"
    child_uuid = "attr-child-3699"
    Group.objects.filter(uuid__in=[parent_uuid, child_uuid]).delete()
    parent = Group.objects.create(
        name="Attribution parent 3699", uuid=parent_uuid, kind="platform",
        metadata={"auth_config": {"registration": {
            "extra_fields": ["ref", {"name": "promo", "capture_only": True}],
        }}},
    )
    child = Group.objects.create(
        name="Attribution child 3699", uuid=child_uuid, kind="operator",
        parent=parent,
    )
    try:
        query = {"ref": "inherited-ref", "promo": "inherited-promo"}
        deployment_request = RequestFactory(REMOTE_ADDR="127.0.0.1").get(
            "/register", query,
            HTTP_X_MOJO_TEST_AUTH_CONFIG=json.dumps({
                "registration": {"extra_fields": ["ref", {"name": "promo"}]},
            }),
        )
        deployment_request.DATA = objict.objict(query)
        deployment_destination, _ = _challenge_destination(deployment_request)
        deployment_params = parse_qs(urlsplit(deployment_destination).query)
        assert deployment_params.get("ref") == ["inherited-ref"] \
            and deployment_params.get("promo") == ["inherited-promo"], \
            f"deployment-wide resolved config must authorize both wire shapes, got {deployment_destination!r}"

        request = RequestFactory(REMOTE_ADDR="127.0.0.1").get("/register", query)
        request.DATA = objict.objict(query)
        destination, _ = _challenge_destination(request)
        no_group_params = parse_qs(urlsplit(destination).query)
        assert "ref" not in no_group_params and "promo" not in no_group_params, \
            f"group config must not leak into deployment-wide resolution, got {destination!r}"

        from mojo.apps.account.rest.bouncer.views import _serve_challenge
        html = _serve_challenge(
            request, challenge_tier=1, page_type="registration", group=child,
        ).content.decode("utf-8")
        match = re.search(r'redirectUrl:\s*"([^"]*)"', html)
        assert match is not None, "the inherited-config challenge must render redirectUrl"
        inherited_destination = json.loads(f'"{match.group(1)}"')
        params = parse_qs(urlsplit(inherited_destination).query)
        assert params.get("ref") == ["inherited-ref"], \
            f"string shorthand inherited from a parent must forward, got {inherited_destination!r}"
        assert params.get("promo") == ["inherited-promo"], \
            f"object-form capture_only config inherited from a parent must forward, got {inherited_destination!r}"
    finally:
        parent.delete()


@th.django_unit_test("all challenge tiers share one safe attribution destination")
def test_all_challenge_tiers_share_safe_destination(opts):
    import objict

    query = {"ref": "partner-42"}
    destinations = []
    for tier in (1, 2, 3):
        request = _request("/auth", query, ["ref"])
        request.DATA = objict.objict(query)
        destination, html = _challenge_destination(
            request, page_type="login", tier=tier)
        destinations.append(destination)
        assert html.count("window.location.href = CFG.redirectUrl") == 2, \
            f"tier {tier} success and error paths must use CFG.redirectUrl"

    assert destinations == ["/auth?ref=partner-42"] * 3, \
        f"all challenge tiers must use the same destination, got {destinations!r}"


@th.django_unit_test("auth/register switchers preserve extras but passkey does not")
def test_switchers_preserve_extras_without_passkey_leak(opts):
    import objict
    from mojo.apps.account.rest.bouncer.views import _auth_context

    query = {"ref": "partner 42", "redirect": "/lobby"}
    request = _request("/auth", query, ["ref"])
    request.DATA = objict.objict(query)
    context = _auth_context(
        request, group=None, include_registration_extras=True)

    assert "ref=partner+42" in context["register_url"], \
        f"the auth-to-register switcher must preserve the extra, got {context['register_url']!r}"
    assert "ref=partner+42" in context["auth_url"], \
        f"the register-to-auth switcher must preserve the extra, got {context['auth_url']!r}"
    assert "ref=" not in context["passkey_url"], \
        f"passkey destinations must not receive attribution, got {context['passkey_url']!r}"
    assert context["register_extra_values"] == {"ref": "partner 42"}, \
        f"the template must receive the server-sanitized extra map, got {context['register_extra_values']!r}"


@th.django_unit_test("contact and OAuth contexts never propagate registration extras")
def test_non_registration_contexts_drop_extras(opts):
    import objict
    from mojo.apps.account.rest.bouncer.views import _auth_context

    for path in ("/contact", "/api/auth/oauth/consent"):
        request = _request(path, {"ref": "partner-42"}, ["ref"])
        request.DATA = objict.objict({"ref": "partner-42"})
        context = _auth_context(request, group=None)
        assert "ref=" not in context["auth_url"], \
            f"{path} must not propagate extras to auth, got {context['auth_url']!r}"
        assert "ref=" not in context["register_url"], \
            f"{path} must not propagate extras to register, got {context['register_url']!r}"

    contact_request = _request("/contact", {"ref": "partner-42"}, ["ref"])
    contact_request.DATA = objict.objict({"ref": "partner-42"})
    destination, _ = _challenge_destination(
        contact_request, page_type="public_message")
    assert "ref=" not in destination, \
        f"the public-message challenge must drop registration extras, got {destination!r}"


@th.django_unit_test("extra-value sanitizer rejects ambiguity, controls, and oversize values")
def test_extra_value_sanitizer_contract(opts):
    from django.http import QueryDict
    from mojo.apps.account.services import register_schema as schema

    repeated = QueryDict("ref=first&ref=second")
    assert schema.extract_extra_values(repeated, ["ref"]) == {}, \
        "a repeated query value must be rejected, not resolved first- or last-wins"

    values = {
        "empty": "",
        "list_value": ["one", "two"],
        "control": "line\nbreak",
        "delete": "bad\x7fvalue",
        "too_long": "x" * 513,
        "boundary": "x" * 512,
        "unicode": "café-🎟️",
        "scheme": "javascript:alert(1)",
        "token": "must-not-capture",
    }
    safe = schema.extract_extra_values(values, values.keys())

    assert safe == {
        "boundary": "x" * 512,
        "unicode": "café-🎟️",
        "scheme": "javascript:alert(1)",
    }, f"sanitizer must preserve only valid scalar data without interpreting it, got {safe!r}"


@th.django_unit_test("reserved registration-extra names are rejected and normalized away")
def test_reserved_extra_names_are_invalid(opts):
    from mojo import errors as merrors
    from mojo.apps.account.services import register_schema as schema

    assert schema.RESERVED_EXTRA_FIELDS == frozenset(EXPECTED_RESERVED_EXTRA_NAMES), \
        "the production reserved namespace must match the independently audited contract"

    for name in EXPECTED_RESERVED_EXTRA_NAMES:
        normalized = schema._normalize_extra_field_list([name])
        assert normalized == [], \
            f"legacy/deployment config must normalize reserved name {name!r} away"
        try:
            schema.validate_extra_fields_config([name])
            assert False, f"config writes must reject reserved name {name!r}"
        except merrors.ValueException as exc:
            assert "reserved" in str(exc), \
                f"reserved-name error must explain the rejection, got {exc!s}"


@th.django_unit_test("credential and control names never forward as registration extras")
def test_reserved_controls_do_not_forward(opts):
    import objict

    query = {name: f"value-for-{name}" for name in NON_FORWARDABLE_RESERVED_NAMES}
    configured = [
        name if index % 2 == 0 else {"name": name, "capture_only": True}
        for index, name in enumerate(NON_FORWARDABLE_RESERVED_NAMES)
    ]
    request = _request("/register", query, configured)
    request.DATA = objict.objict(query)

    destination, _ = _challenge_destination(request)
    params = parse_qs(urlsplit(destination).query)
    assert not set(NON_FORWARDABLE_RESERVED_NAMES).intersection(params), \
        f"reserved credential/control names must not reach the destination, got {destination!r}"


def _render(extra_fields):
    from django.shortcuts import render
    from django.test import RequestFactory
    from mojo.apps.account.rest.bouncer.views import _auth_context
    from mojo.apps.account.services import register_schema

    request = RequestFactory().get("/register?ref=partner-42")
    context = _auth_context(
        request, group=None, include_registration_extras=True)
    context["page_mode"] = "register"
    context["page_title"] = "Create Account"
    context["register_extra_fields"] = \
        register_schema._normalize_extra_field_list(extra_fields)
    response = render(request, "account/register.html", context)
    return response.content.decode("utf-8")


@th.django_unit_test("extra-field schema normalizes capture policy and help text")
def test_extra_field_schema_normalizes_presentation_properties(opts):
    from mojo.apps.account.services import register_schema as schema

    fields = schema._normalize_extra_field_list([
        "legacy_ref",
        {
            "name": "promo",
            "label": "Promo code",
            "help_text": "Provided by your event organizer.",
        },
        {
            "name": "ref",
            "required": True,
            "capture_only": True,
            "help_text": "Not displayed",
        },
        {
            "name": "persisted_bad_types",
            "capture_only": "yes",
            "help_text": ["not", "text"],
        },
    ])

    assert fields[0] == {
        "name": "legacy_ref",
        "label": "Legacy Ref",
        "required": False,
        "capture_only": False,
        "help_text": "",
    }, f"legacy string shorthand must keep working with safe defaults, got {fields[0]!r}"
    assert fields[1]["capture_only"] is False, \
        f"visible fields must default capture_only to False, got {fields[1]!r}"
    assert fields[1]["help_text"] == "Provided by your event organizer.", \
        f"help text must survive normalization, got {fields[1]!r}"
    assert fields[2]["capture_only"] is True, \
        f"an explicit capture-only field must remain capture-only, got {fields[2]!r}"
    assert fields[2]["required"] is False, \
        f"invalid persisted capture_only+required config must fail safe to optional, got {fields[2]!r}"
    assert fields[3]["capture_only"] is False and fields[3]["help_text"] == "", \
        f"invalid persisted presentation types must normalize to safe defaults, got {fields[3]!r}"


@th.django_unit_test("extra-field config validates strict presentation property types")
def test_extra_field_schema_validates_presentation_properties(opts):
    from mojo import errors as merrors
    from mojo.apps.account.services import auth_config
    from mojo.apps.account.services import register_schema as schema

    normalized = schema.validate_extra_fields_config([{
        "name": "ref",
        "capture_only": True,
        "help_text": "Captured from the invitation link.",
    }])
    assert normalized[0]["capture_only"] is True, \
        f"valid capture_only must be accepted, got {normalized!r}"
    assert normalized[0]["help_text"] == "Captured from the invitation link.", \
        f"valid help_text must be accepted, got {normalized!r}"

    invalid = [
        ({"name": "ref", "capture_only": "yes"}, "capture_only"),
        ({"name": "ref", "help_text": ["not", "text"]}, "help_text"),
        ({"name": "ref", "capture_only": True, "required": True}, "required"),
    ]
    for entry, expected in invalid:
        try:
            auth_config.validate_auth_config({
                "registration": {"extra_fields": [entry]},
            })
            assert False, f"validator must reject invalid extra-field config {entry!r}"
        except merrors.ValueException as exc:
            assert expected in str(exc), \
                f"error for {entry!r} must identify {expected!r}, got {exc!s}"


@th.django_unit_test("public auth config preserves raw extra-field wire shapes")
def test_public_auth_config_preserves_extra_field_wire_shape(opts):
    from mojo.apps.account.services import auth_config

    raw = [
        "legacy_ref",
        {
            "name": "promo",
            "label": "Promo code",
            "capture_only": True,
            "help_text": "Captured from the campaign link.",
        },
    ]
    config = auth_config.resolve_auth_config(group=None)
    config.registration.extra_fields = raw
    public = auth_config.public_auth_config(config)

    assert list(public.registration.extra_fields) == raw, \
        f"the public config must pass string and object forms through unchanged, got {public.registration.extra_fields!r}"


@th.django_unit_test("hosted register hides capture-only fields and labels visible fields accessibly")
def test_hosted_register_extra_field_presentation(opts):
    html = _render([
        {
            "name": "promo",
            "label": "Promo & offers",
            "help_text": "Use <your> invitation code.",
        },
        {"name": "ref", "capture_only": True},
    ])

    assert '<label class="mat-label" for="reg-extra-promo">Promo &amp; offers</label>' in html, \
        "visible extra fields must render an explicit escaped label bound to the input"
    assert 'aria-describedby="reg-extra-help-promo"' in html, \
        "a visible field with help text must link its input to the help element"
    assert 'id="reg-extra-help-promo"' in html and \
        "Use &lt;your&gt; invitation code." in html, \
        "help text must render through Django autoescaping"
    assert 'id="reg-extra-row-ref"' not in html and 'id="reg-extra-ref"' not in html, \
        "capture-only fields must emit no editable row or input"
    assert '"name": "ref"' in html and '"capture_only": true' in html, \
        "capture-only fields must remain in the serialized collector config"
    assert "var REG_EXTRA_VALUES = JSON.parse" in html, \
        "the hosted collector must consume the server-sanitized extra map"
    assert "new URLSearchParams(window.location.search)" not in html, \
        "the browser must not choose a value again from an ambiguous query string"
    assert 'maxlength="512"' in html, \
        "visible extra-field inputs must expose the shared 512-character cap"
    assert "if (v) payload[ef.name] = v;" in html, \
        "a non-empty query value must still be submitted without a DOM input"
