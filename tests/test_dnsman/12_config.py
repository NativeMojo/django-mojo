"""dnsman config endpoint — capability discovery without probing a gated action."""

from testit import helpers as th

from tests.test_dnsman._helpers import make_user, login, assert_no_secrets


@th.django_unit_setup()
def setup_config(opts):
    opts.nobody, opts.nobody_email, opts.nobody_pw = make_user()
    opts.viewer, opts.viewer_email, opts.viewer_pw = make_user(["view_dns"])


@th.django_unit_test("config requires view_dns")
def test_config_requires_perm(opts):
    login(opts, opts.nobody_email, opts.nobody_pw)
    resp = opts.client.get("/api/dnsman/config")
    assert resp.status_code in (401, 403), \
        f"a user with no dns permission read /config (status {resp.status_code})"


@th.django_unit_test("config reports purchase_enabled without a quote attempt")
def test_config_reports_purchase_state(opts):
    login(opts, opts.viewer_email, opts.viewer_pw)
    resp = opts.client.get("/api/dnsman/config")
    assert resp.status_code == 200, f"config returned {resp.status_code}"

    # A plain dict return gets wrapped by dispatch_error_handler as
    # {"status": True, "code": 200, "data": <dict>} -- the payload is under
    # "data", not the top level.
    body = resp.response["data"]
    assert "purchase_enabled" in body, "config must report purchase_enabled"
    # DNSMAN_PURCHASE_ENABLED defaults False -- this is the exact fact a client
    # could previously learn only by calling registrar/quote and reading a 400.
    assert body["purchase_enabled"] is False, \
        f"purchase_enabled should reflect the default kill switch, got {body['purchase_enabled']}"

    for key in ("registrant_contact_configured", "max_domain_price", "currency",
                "quote_ttl_minutes", "allowed_record_types", "providers", "acme",
                "cert_renew_days", "search_batch_limit", "suggestions_enabled"):
        assert key in body, f"config response is missing {key!r}"

    assert isinstance(body["registrant_contact_configured"], bool), \
        "registrant_contact_configured must be a boolean"
    assert isinstance(body["allowed_record_types"], list), \
        "allowed_record_types must be a list"
    assert "TXT" in body["allowed_record_types"], \
        "TXT must be an allowed record type"
    assert body["search_batch_limit"] == 10, \
        f"search_batch_limit should reflect the default cap, got {body['search_batch_limit']}"
    assert body["suggestions_enabled"] is True, \
        "suggestions_enabled is the feature-presence flag and must be True"

    providers = {p["name"]: p for p in body["providers"]}
    assert providers["route53"]["purchase"] is True, "route53 must report purchase support"
    assert providers["godaddy"]["purchase"] is False, "godaddy must report management-only"
    assert providers["godaddy"]["requires_credential"] is True, \
        "godaddy must report that it requires a linked credential"

    assert "staging" in body["acme"], "acme.staging must be reported"
    # ACME defaults to Let's Encrypt staging -- a client must be able to tell
    # a publicly-untrusted cert apart from a production-issued one.
    assert body["acme"]["staging"] is True, \
        "acme.staging should be True against the default (unconfigured) directory"


@th.django_unit_test("config never echoes the registrant contact or any secret")
def test_config_no_pii_or_secrets(opts):
    from mojo.helpers.settings import settings

    login(opts, opts.viewer_email, opts.viewer_pw)
    with th.server_settings(DNSMAN_REGISTRANT_CONTACT={
        "FirstName": "Jane", "LastName": "Doe", "Email": "jane@example.com",
        "PhoneNumber": "+1.5551234567", "AddressLine1": "1 Main St",
        "City": "Springfield", "State": "IL", "CountryCode": "US", "ZipCode": "00000",
        "ContactType": "PERSON",
    }):
        resp = opts.client.get("/api/dnsman/config")
        assert resp.status_code == 200, f"config returned {resp.status_code}"
        assert resp.response["data"]["registrant_contact_configured"] is True, \
            "a complete contact should flip registrant_contact_configured to True"
        assert_no_secrets(resp.response, "/api/dnsman/config")
        blob = str(resp.response)
        assert "Jane" not in blob and "jane@example.com" not in blob, \
            "config must never echo the registrant contact -- it is PII"
