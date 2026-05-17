"""Unit tests for register_schema service — no HTTP, no DB writes."""
import datetime
from testit import helpers as th


@th.django_unit_test("resolve_fields returns the default config when AUTH_REGISTER_FIELDS is unset")
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
    # Drive through resolve_fields by monkeypatching settings via the public API.
    from mojo.helpers.settings import settings
    original_get = settings.get
    def patched(key, default=None, **kwargs):
        if key == "AUTH_REGISTER_FIELDS":
            return raw
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
        if key == "AUTH_REGISTER_FIELDS":
            return [{"name": "email", "required": True},
                    {"name": "evil_admin_flag", "required": True},
                    {"name": "password", "required": True}]
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


@th.django_unit_test("resolve_fields forces password required and adds it if missing")
def test_resolve_fields_forces_password(opts):
    from mojo.apps.account.services import register_schema as rs
    from mojo.helpers.settings import settings
    original_get = settings.get
    def patched(key, default=None, **kwargs):
        if key == "AUTH_REGISTER_FIELDS":
            return [{"name": "email", "required": True}]
        return original_get(key, default=default, **kwargs)
    settings.get = patched
    try:
        out = rs.resolve_fields(group=None)
    finally:
        settings.get = original_get
    by_name = {f["name"]: f for f in out}
    assert "password" in by_name, \
        "password must be appended even when operator omits it from the config"
    assert by_name["password"]["required"] is True, \
        "password must be required even if config marks it optional"


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
