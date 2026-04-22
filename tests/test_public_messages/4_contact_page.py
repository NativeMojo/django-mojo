"""
Contact page (GET /contact) — bouncer-gated HTML page renders kind-specific forms.
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq


@th.django_unit_test()
def test_contact_page_renders_contact_us_form(opts):
    resp = opts.client.get("/contact")
    # May render full page (200) or a challenge (200). Either is fine as long
    # as the response is HTML and mentions the contact kind context.
    assert_eq(resp.status_code, 200, f"contact page should return 200, got {resp.status_code}")
    body = resp.response if isinstance(resp.response, str) else (resp.text or "")
    # We either see the full form (contact_us fields) or the challenge page.
    is_challenge = "bouncer_challenge" in body or "mat-challenge" in body or "gate_challenge" in body.lower()
    has_contact = "Contact Us" in body or "name=\"message\"" in body
    assert_true(
        is_challenge or has_contact,
        f"expected contact page or challenge page markup, got: {body[:300]}",
    )


@th.django_unit_test()
def test_contact_page_renders_support_form(opts):
    resp = opts.client.get("/contact?kind=support")
    assert_eq(resp.status_code, 200, f"support contact page should return 200, got {resp.status_code}")
    body = resp.response if isinstance(resp.response, str) else (resp.text or "")
    is_challenge = "bouncer_challenge" in body or "mat-challenge" in body or "gate_challenge" in body.lower()
    # If we got past the challenge, we should see support-specific fields.
    has_support = ("Get Support" in body and "name=\"category\"" in body) or "severity" in body
    assert_true(
        is_challenge or has_support,
        f"expected support page or challenge page markup, got: {body[:300]}",
    )


@th.django_unit_test()
def test_contact_page_invalid_kind_falls_back(opts):
    """Unknown kind should not error; page renders contact_us form."""
    resp = opts.client.get("/contact?kind=garbage")
    assert_eq(
        resp.status_code, 200,
        f"unknown kind should not error, got {resp.status_code}",
    )
    body = resp.response if isinstance(resp.response, str) else (resp.text or "")
    # Should NOT leak the bogus kind back into the form action/submit surface.
    assert_true(
        "kind=garbage" not in body or "data-kind=\"contact_us\"" in body,
        f"invalid kind should fall back to contact_us, body snippet: {body[:500]}",
    )


@th.django_unit_test()
def test_contact_page_service_kind_schema(opts):
    """Service schema must expose contact_us and support kinds with expected fields."""
    from mojo.apps.account.services import public_message as svc

    assert_true("contact_us" in svc.KIND_SCHEMAS, "contact_us must be a known kind")
    assert_true("support" in svc.KIND_SCHEMAS, "support must be a known kind")

    contact_fields = {f["name"] for f in svc.KIND_SCHEMAS["contact_us"]["fields"]}
    assert_true(
        {"name", "email", "company", "message"}.issubset(contact_fields),
        f"contact_us must include name/email/company/message, got {contact_fields}",
    )

    support_fields = {f["name"] for f in svc.KIND_SCHEMAS["support"]["fields"]}
    assert_true(
        {"name", "email", "category", "severity", "message"}.issubset(support_fields),
        f"support must include name/email/category/severity/message, got {support_fields}",
    )
    assert_true(
        "company" not in support_fields,
        f"support kind should NOT include company, got {support_fields}",
    )

    ctx_good = svc.render_context_for_kind("support")
    assert_eq(ctx_good["kind"], "support", "resolved kind should match input")

    ctx_bad = svc.render_context_for_kind("nonsense")
    assert_eq(
        ctx_bad["kind"], svc.DEFAULT_KIND,
        f"unknown kind should fall back to DEFAULT_KIND, got {ctx_bad['kind']}",
    )
