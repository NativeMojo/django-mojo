"""Unit tests for register_schema service — no HTTP, no DB writes."""
import datetime
from testit import helpers as th


@th.django_unit_test("resolve_fields returns the default config when no auth config is set")
def test_resolve_fields_default(opts):
    from mojo.apps.account.services import register_schema as rs
    fields = rs.resolve_fields(group=None)
    names = [f["name"] for f in fields]
    assert names == ["first_name", "last_name", "email", "password"], \
        f"Default field set must preserve today's email-based form, got {names}"
    by_name = {f["name"]: f for f in fields}
    assert by_name["email"]["required"] is True, \
        "email must be required in the default config"
    assert by_name["password"]["required"] is True, \
        "password must be required in the default config"
    assert by_name["email"]["verify"] == "email", \
        "email's verify channel must be 'email' in the default config"


@th.django_unit_test("resolve_fields normalizes a phone-only project config")
def test_resolve_fields_phone_only(opts):
    from mojo.apps.account.services import register_schema as rs
    raw = [
        {"name": "first_name", "required": True},
        {"name": "last_name",  "required": True},
        {"name": "phone",      "required": True, "verify": "sms"},
        {"name": "dob",        "required": True},
        {"name": "password",   "required": True},
    ]
    fields = rs._normalize_entry  # touch to confirm import
    # Drive through resolve_fields by monkeypatching settings via the public
    # API. Register fields now come from the auth config's AUTH_CONFIG.
    from mojo.helpers.settings import settings
    original_get = settings.get
    def patched(key, default=None, **kwargs):
        if key == "AUTH_CONFIG":
            return {"registration": {"fields": raw}}
        return original_get(key, default=default, **kwargs)
    settings.get = patched
    try:
        out = rs.resolve_fields(group=None)
    finally:
        settings.get = original_get
    names = [f["name"] for f in out]
    assert names == ["first_name", "last_name", "phone", "dob", "password"], \
        f"Phone-only config must produce exactly the configured fields in order, got {names}"
    by_name = {f["name"]: f for f in out}
    assert by_name["phone"]["verify"] == "sms", \
        "phone field must carry verify='sms' through normalization"


@th.django_unit_test("resolve_fields drops unknown field names silently")
def test_resolve_fields_drops_unknown(opts):
    from mojo.apps.account.services import register_schema as rs
    from mojo.helpers.settings import settings
    original_get = settings.get
    def patched(key, default=None, **kwargs):
        if key == "AUTH_CONFIG":
            return {"registration": {"fields": [
                {"name": "email", "required": True},
                {"name": "evil_admin_flag", "required": True},
                {"name": "password", "required": True}]}}
        return original_get(key, default=default, **kwargs)
    settings.get = patched
    try:
        out = rs.resolve_fields(group=None)
    finally:
        settings.get = original_get
    names = [f["name"] for f in out]
    assert "evil_admin_flag" not in names, \
        f"Unknown canonical names must be silently dropped, got {names}"
    assert names == ["email", "password"], \
        f"Remaining names must keep order, got {names}"


@th.django_unit_test("resolve_fields does not auto-add password, but forces it required when present")
def test_resolve_fields_password_handling(opts):
    from mojo.apps.account.services import register_schema as rs
    from mojo.helpers.settings import settings
    original_get = settings.get

    # Case 1: a schema that omits password stays passwordless — resolve_fields
    # must NOT auto-append a password field.
    def patched_nopass(key, default=None, **kwargs):
        if key == "AUTH_CONFIG":
            return {"registration": {"fields": [{"name": "email", "required": True}]}}
        return original_get(key, default=default, **kwargs)
    settings.get = patched_nopass
    try:
        out = rs.resolve_fields(group=None)
    finally:
        settings.get = original_get
    assert [f["name"] for f in out] == ["email"], \
        f"resolve_fields must NOT auto-append password — a schema may omit it " \
        f"for passwordless registration, got {[f['name'] for f in out]}"

    # Case 2: when password IS present there is no optional-password state —
    # it is forced required even if the config marks it optional.
    def patched_optpass(key, default=None, **kwargs):
        if key == "AUTH_CONFIG":
            return {"registration": {"fields": [
                {"name": "email", "required": True},
                {"name": "password", "required": False}]}}
        return original_get(key, default=default, **kwargs)
    settings.get = patched_optpass
    try:
        out2 = rs.resolve_fields(group=None)
    finally:
        settings.get = original_get
    by_name = {f["name"]: f for f in out2}
    assert "password" in by_name, \
        f"a configured password field must be kept, got {[f['name'] for f in out2]}"
    assert by_name["password"]["required"] is True, \
        "a password field, when present, must always be required (no optional-password state)"


@th.django_unit_test("resolve_identity_field auto-picks email when both are required")
def test_resolve_identity_email_wins(opts):
    from mojo.apps.account.services import register_schema as rs
    fields = [
        {"name": "email",    "required": True, "verify": "email"},
        {"name": "phone",    "required": True, "verify": "sms"},
        {"name": "password", "required": True, "verify": None},
    ]
    out = rs.resolve_identity_field(fields, group=None)
    assert out == "email", \
        f"email should win when both fields are required, got '{out}'"


@th.django_unit_test("resolve_identity_field returns 'phone' for phone-only config")
def test_resolve_identity_phone_only(opts):
    from mojo.apps.account.services import register_schema as rs
    fields = [
        {"name": "phone",    "required": True, "verify": "sms"},
        {"name": "password", "required": True, "verify": None},
    ]
    out = rs.resolve_identity_field(fields, group=None)
    assert out == "phone", \
        f"phone must be the identity when email isn't configured, got '{out}'"


@th.django_unit_test("validate_payload rejects missing identity field")
def test_validate_payload_missing_identity(opts):
    from mojo.apps.account.services import register_schema as rs
    from mojo import errors as merrors
    fields = [
        {"name": "phone",    "required": True, "verify": "sms"},
        {"name": "password", "required": True, "verify": None},
    ]
    try:
        rs.validate_payload(fields, {"password": "Abcd1234!"},
                            identity_field="phone", min_age=None)
        assert False, "validator must reject missing required phone"
    except merrors.ValueException as e:
        assert "phone" in str(e).lower() or "required" in str(e).lower(), \
            f"Missing-identity error must mention the field, got {e}"


@th.django_unit_test("validate_payload normalizes email and phone")
def test_validate_payload_normalizes(opts):
    from mojo.apps.account.services import register_schema as rs
    fields = [
        {"name": "email",    "required": True, "verify": "email"},
        {"name": "phone",    "required": False, "verify": None},
        {"name": "password", "required": True, "verify": None},
    ]
    out = rs.validate_payload(
        fields,
        {"email": "Alice@Example.COM ", "phone": "+1 (415) 555-1212", "password": "Abcd1234!"},
        identity_field="email", min_age=None)
    assert out["email"] == "alice@example.com", \
        f"email must be lowercased + trimmed, got '{out['email']}'"
    assert out["phone"].startswith("+1"), \
        f"phone must be normalized through phonehub, got '{out['phone']}'"


@th.django_unit_test("validate_payload age-gates dob when AUTH_MIN_AGE_YEARS is set")
def test_validate_payload_age_gate(opts):
    from mojo.apps.account.services import register_schema as rs
    from mojo import errors as merrors
    fields = [
        {"name": "phone",    "required": True, "verify": "sms"},
        {"name": "dob",      "required": True, "verify": None},
        {"name": "password", "required": True, "verify": None},
    ]
    today = datetime.date.today()
    too_young = today.replace(year=today.year - 10).isoformat()
    try:
        rs.validate_payload(
            fields,
            {"phone": "+14155551212", "dob": too_young, "password": "Abcd1234!"},
            identity_field="phone", min_age=13)
        assert False, "validator must reject DOB below min age"
    except merrors.ValueException as e:
        assert "13" in str(e), f"Age-gate error must include the threshold, got {e}"


@th.django_unit_test("validate_payload rejects future DOB")
def test_validate_payload_future_dob(opts):
    from mojo.apps.account.services import register_schema as rs
    from mojo import errors as merrors
    fields = [
        {"name": "phone",    "required": True, "verify": "sms"},
        {"name": "dob",      "required": True, "verify": None},
        {"name": "password", "required": True, "verify": None},
    ]
    future = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
    try:
        rs.validate_payload(
            fields,
            {"phone": "+14155551212", "dob": future, "password": "Abcd1234!"},
            identity_field="phone", min_age=None)
        assert False, "validator must reject DOB in the future"
    except merrors.ValueException as e:
        assert "date of birth" in str(e).lower(), \
            f"Future-DOB error must mention DOB, got {e}"


@th.django_unit_test("partition_for_stepped_flow: phone-verify schema returns 3 buckets")
def test_partition_phone_verify(opts):
    from mojo.apps.account.services import register_schema as rs
    fields = [
        {"name": "first_name", "required": True, "verify": None},
        {"name": "last_name",  "required": True, "verify": None},
        {"name": "phone",      "required": True, "verify": "sms"},
        {"name": "dob",        "required": True, "verify": None},
        {"name": "password",   "required": True, "verify": None},
    ]
    step1, step2_active, step3_rows = rs.partition_for_stepped_flow(fields)
    assert [f["name"] for f in step1] == ["phone"], \
        f"Step 1 must be just the phone field for SMS-verify schema, got {step1}"
    assert step2_active is True, \
        "step2_active must be True when phone has verify=sms"
    step3_names = [f["name"] for row in step3_rows for f in row]
    assert step3_names == ["first_name", "last_name", "dob", "password"], \
        f"Step 3 must include every non-phone field in order, got {step3_names}"
    # Name-pair grouping preserved
    assert len(step3_rows[0]) == 2 and step3_rows[0][0]["name"] == "first_name", \
        f"Step 3 first row must group first+last, got {step3_rows[0]}"


@th.django_unit_test("partition_for_stepped_flow: default email schema does not activate steps")
def test_partition_email_default(opts):
    from mojo.apps.account.services import register_schema as rs
    fields = rs.DEFAULT_FIELDS  # email-based default
    step1, step2_active, step3_rows = rs.partition_for_stepped_flow(fields)
    assert step1 == [], \
        f"Step 1 must be empty when no phone-with-SMS-verify is configured, got {step1}"
    assert step2_active is False, \
        "step2_active must be False for the email-based default schema"
    step3_names = [f["name"] for row in step3_rows for f in row]
    assert "email" in step3_names and "password" in step3_names, \
        f"single-pane fallback must include all fields, got {step3_names}"


@th.django_unit_test("partition_for_stepped_flow: phone without verify stays in single-pane")
def test_partition_phone_no_verify(opts):
    from mojo.apps.account.services import register_schema as rs
    fields = [
        {"name": "phone",    "required": True, "verify": None},
        {"name": "password", "required": True, "verify": None},
    ]
    step1, step2_active, step3_rows = rs.partition_for_stepped_flow(fields)
    assert step2_active is False, \
        "step2_active must be False when phone is present but verify is not 'sms'"
    assert step1 == [], "step1 must be empty when stepped flow doesn't engage"
    step3_names = [f["name"] for row in step3_rows for f in row]
    assert "phone" in step3_names, \
        f"phone must still appear in the single-pane field list, got {step3_names}"


@th.django_unit_test("resolve_extra_fields returns [] when no extra_fields configured")
def test_resolve_extra_fields_default(opts):
    from mojo.apps.account.services import register_schema as rs
    out = rs.resolve_extra_fields(group=None)
    assert out == [], \
        f"Default (no config) must yield no extra fields — keeps every existing " \
        f"deployment unchanged, got {out}"


@th.django_unit_test("resolve_extra_fields normalizes name/label/required")
def test_resolve_extra_fields_normalizes(opts):
    from mojo.apps.account.services import register_schema as rs
    from mojo.helpers.settings import settings
    raw = [
        {"name": "promo", "label": "Promo code", "required": True},
        "ref",                                   # string shorthand
        {"name": "tracking"},                    # label defaults from name
    ]
    original_get = settings.get
    def patched(key, default=None, **kwargs):
        if key == "AUTH_CONFIG":
            return {"registration": {"extra_fields": raw}}
        return original_get(key, default=default, **kwargs)
    settings.get = patched
    try:
        out = rs.resolve_extra_fields(group=None)
    finally:
        settings.get = original_get
    by_name = {ef["name"]: ef for ef in out}
    assert [ef["name"] for ef in out] == ["promo", "ref", "tracking"], \
        f"extra fields must keep configured order, got {[ef['name'] for ef in out]}"
    assert by_name["promo"]["label"] == "Promo code", \
        f"explicit label must be preserved, got {by_name['promo']['label']!r}"
    assert by_name["promo"]["required"] is True, \
        "required flag must be coerced and preserved"
    assert by_name["ref"]["label"] == "Ref", \
        f"string-shorthand entry must get a humanized default label, got {by_name['ref']['label']!r}"
    assert by_name["tracking"]["label"] == "Tracking" and by_name["tracking"]["required"] is False, \
        f"missing label must humanize the name and required must default False, got {by_name['tracking']}"


@th.django_unit_test("resolve_extra_fields drops canonical-colliding, blank, and duplicate names")
def test_resolve_extra_fields_drops_bad(opts):
    from mojo.apps.account.services import register_schema as rs
    from mojo.helpers.settings import settings
    raw = [
        {"name": "email"},        # collides with a canonical field -> dropped
        {"name": ""},             # blank -> dropped
        {"name": "bad name!"},    # non-identifier chars -> dropped
        {"name": "promo"},
        {"name": "promo"},        # duplicate -> dropped
        {"label": "no name"},     # missing name -> dropped
    ]
    original_get = settings.get
    def patched(key, default=None, **kwargs):
        if key == "AUTH_CONFIG":
            return {"registration": {"extra_fields": raw}}
        return original_get(key, default=default, **kwargs)
    settings.get = patched
    try:
        out = rs.resolve_extra_fields(group=None)
    finally:
        settings.get = original_get
    assert [ef["name"] for ef in out] == ["promo"], \
        f"canonical-colliding, blank, nameless, and duplicate entries must all be " \
        f"dropped, leaving only ['promo'], got {[ef['name'] for ef in out]}"


@th.django_unit_test("validate_extra_fields_config accepts a good list and rejects bad shapes")
def test_validate_extra_fields_config(opts):
    from mojo.apps.account.services import register_schema as rs
    from mojo import errors as merrors

    out = rs.validate_extra_fields_config([{"name": "promo", "label": "Promo code"}])
    assert [ef["name"] for ef in out] == ["promo"], \
        f"a valid extra_fields config must normalize and return, got {out}"

    bad_cases = [
        ("not a list", "must be a list"),
        ([{"label": "x"}], "name"),                         # missing name
        ([{"name": "email"}], "collides"),                  # canonical collision
        ([{"name": "pro mo"}], "identifier"),               # non-identifier chars
        ([{"name": "promo", "label": 5}], "label"),         # non-string label
        ([{"name": "promo", "required": "yes"}], "required"),  # non-bool required
    ]
    for raw, expect in bad_cases:
        try:
            rs.validate_extra_fields_config(raw)
            assert False, f"validate_extra_fields_config must reject {raw!r}"
        except merrors.ValueException as e:
            assert expect in str(e).lower() or expect in str(e), \
                f"error for {raw!r} must mention {expect!r}, got {e}"


@th.django_unit_test("field_rows groups adjacent first/last into a single row")
def test_field_rows_groups_names(opts):
    from mojo.apps.account.services import register_schema as rs
    fields = [
        {"name": "first_name", "required": True, "verify": None},
        {"name": "last_name",  "required": True, "verify": None},
        {"name": "phone",      "required": True, "verify": "sms"},
        {"name": "password",   "required": True, "verify": None},
    ]
    rows = rs.field_rows(fields)
    assert len(rows) == 3, \
        f"Expected 3 rows (names paired + phone + password), got {len(rows)}: {rows}"
    assert [f["name"] for f in rows[0]] == ["first_name", "last_name"], \
        f"Row 0 must be the paired name fields, got {rows[0]}"
    assert [f["name"] for f in rows[1]] == ["phone"], \
        f"Row 1 must be phone alone, got {rows[1]}"
