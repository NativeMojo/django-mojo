"""Regression coverage for hosted registration extra-field presentation."""

from testit import helpers as th


TESTIT_TIER = "bug"


def _render(extra_fields):
    from django.shortcuts import render
    from django.test import RequestFactory
    from mojo.apps.account.rest.bouncer.views import _auth_context
    from mojo.apps.account.services import register_schema

    request = RequestFactory().get("/register?ref=partner-42")
    context = _auth_context(request, group=None)
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
    assert "var fromUrl = URL_PARAMS.get(ef.name);" in html, \
        "the hosted collector must read the matching query parameter directly"
    assert "if (v) payload[ef.name] = v;" in html, \
        "a non-empty query value must still be submitted without a DOM input"
