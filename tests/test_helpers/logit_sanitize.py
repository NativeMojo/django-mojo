from testit import helpers as th


@th.unit_test("sanitize_dict strips password from flat dict")
def test_sanitize_flat_password(opts):
    from mojo.helpers.logit import sanitize_dict
    data = {"username": "alice", "password": "SuperSecret123"}
    result = sanitize_dict(data)
    assert result["password"] == "*****", f"Expected '*****', got {result['password']}"
    assert result["username"] == "alice", f"Username should be preserved, got {result['username']}"


@th.unit_test("sanitize_dict strips all known sensitive keys")
def test_sanitize_all_sensitive_keys(opts):
    from mojo.helpers.logit import sanitize_dict, SENSITIVE_KEYS
    data = {key: f"value_{key}" for key in SENSITIVE_KEYS}
    data["safe_field"] = "keep_me"
    result = sanitize_dict(data)
    for key in SENSITIVE_KEYS:
        assert result[key] == "*****", f"Key '{key}' should be redacted, got {result[key]}"
    assert result["safe_field"] == "keep_me", f"safe_field should be preserved, got {result['safe_field']}"


@th.unit_test("sanitize_dict recurses into nested dicts")
def test_sanitize_nested(opts):
    from mojo.helpers.logit import sanitize_dict
    data = {
        "user": "bob",
        "credentials": {
            "password": "nested_secret",
            "token": "abc123",
            "role": "admin",
        }
    }
    result = sanitize_dict(data)
    assert result["credentials"]["password"] == "*****", \
        f"Nested password should be redacted, got {result['credentials']['password']}"
    assert result["credentials"]["token"] == "*****", \
        f"Nested token should be redacted, got {result['credentials']['token']}"
    assert result["credentials"]["role"] == "admin", \
        f"Nested safe field should be preserved, got {result['credentials']['role']}"
    assert result["user"] == "bob", f"Top-level safe field should be preserved, got {result['user']}"


@th.unit_test("sanitize_dict is case-insensitive on keys")
def test_sanitize_case_insensitive(opts):
    from mojo.helpers.logit import sanitize_dict
    data = {"Password": "secret1", "API_KEY": "secret2", "username": "alice"}
    result = sanitize_dict(data)
    assert result["Password"] == "*****", f"Mixed-case Password should be redacted, got {result['Password']}"
    assert result["API_KEY"] == "*****", f"Upper-case API_KEY should be redacted, got {result['API_KEY']}"
    assert result["username"] == "alice", f"username should be preserved, got {result['username']}"


@th.unit_test("sanitize_dict returns non-dict input unchanged")
def test_sanitize_non_dict(opts):
    from mojo.helpers.logit import sanitize_dict
    assert sanitize_dict("hello") == "hello", "String input should pass through unchanged"
    assert sanitize_dict(42) == 42, "Int input should pass through unchanged"
    assert sanitize_dict(None) is None, "None input should pass through unchanged"


@th.unit_test("sanitize_dict returns a copy, not mutating original")
def test_sanitize_no_mutation(opts):
    from mojo.helpers.logit import sanitize_dict
    original = {"password": "secret", "name": "alice"}
    result = sanitize_dict(original)
    assert original["password"] == "secret", f"Original should be unchanged, got {original['password']}"
    assert result["password"] == "*****", f"Result should be redacted, got {result['password']}"


@th.unit_test("sanitize_dict handles login-like request data")
def test_sanitize_login_payload(opts):
    from mojo.helpers.logit import sanitize_dict
    request_data = {
        "username": "devin@example.com",
        "password": "W@9vytVdLx-V2Pk6Gjkcz",
    }
    result = sanitize_dict(request_data)
    assert result["password"] == "*****", \
        f"Login password must be redacted, got {result['password']}"
    assert result["username"] == "devin@example.com", \
        f"Username should be preserved, got {result['username']}"


@th.unit_test("sanitize_dict handles list of dicts")
def test_sanitize_list_of_dicts(opts):
    from mojo.helpers.logit import sanitize_dict
    data = {"records": [{"username": "alice", "password": "secret1"}, {"username": "bob", "token": "abc"}]}
    result = sanitize_dict(data)
    assert result["records"][0]["password"] == "*****", \
        f"Password in list item should be redacted, got {result['records'][0]['password']}"
    assert result["records"][0]["username"] == "alice", \
        f"Username in list item should be preserved, got {result['records'][0]['username']}"
    assert result["records"][1]["token"] == "*****", \
        f"Token in list item should be redacted, got {result['records'][1]['token']}"


@th.unit_test("sanitize_dict handles bare list input")
def test_sanitize_bare_list(opts):
    from mojo.helpers.logit import sanitize_dict
    data = [{"password": "secret"}, {"name": "safe"}]
    result = sanitize_dict(data)
    assert result[0]["password"] == "*****", \
        f"Password in bare list should be redacted, got {result[0]['password']}"
    assert result[1]["name"] == "safe", \
        f"Safe field in bare list should be preserved, got {result[1]['name']}"


@th.unit_test("sanitize_dict covers MFA and OAuth keys")
def test_sanitize_mfa_oauth_keys(opts):
    from mojo.helpers.logit import sanitize_dict
    data = {"otp": "123456", "mfa_code": "654321", "refresh_token": "rt_abc", "id_token": "id_xyz", "username": "alice"}
    result = sanitize_dict(data)
    assert result["otp"] == "*****", f"otp should be redacted, got {result['otp']}"
    assert result["mfa_code"] == "*****", f"mfa_code should be redacted, got {result['mfa_code']}"
    assert result["refresh_token"] == "*****", f"refresh_token should be redacted, got {result['refresh_token']}"
    assert result["id_token"] == "*****", f"id_token should be redacted, got {result['id_token']}"
    assert result["username"] == "alice", f"username should be preserved, got {result['username']}"


@th.unit_test("incident _create_event_dict sanitizes request_data")
def test_incident_reporter_sanitizes(opts):
    from mojo.apps.incident.reporter import _create_event_dict
    event = _create_event_dict(
        "test error",
        title="test",
        category="mojo_rest_error",
        level=5,
        request=None,
        scope="global",
        request_data={"username": "alice", "password": "exposed_password", "action": "login"},
    )
    metadata = event["metadata"]
    req_data = metadata.get("request_data", {})
    assert req_data.get("password") == "*****", \
        f"password in incident metadata must be redacted, got {req_data.get('password')}"
    assert req_data.get("username") == "alice", \
        f"username should be preserved in incident metadata, got {req_data.get('username')}"
    assert req_data.get("action") == "login", \
        f"non-sensitive fields should be preserved, got {req_data.get('action')}"


# ---------------------------------------------------------------------------
# mask_sensitive_data — derived from SENSITIVE_KEYS
# ---------------------------------------------------------------------------

@th.unit_test("mask_sensitive_data covers every SENSITIVE_KEYS entry (key=value)")
def test_mask_sensitive_covers_all_keys_keyvalue(opts):
    from mojo.helpers.logit import mask_sensitive_data, SENSITIVE_KEYS
    for key in SENSITIVE_KEYS:
        line = f"prefix {key}=supersecret99 suffix"
        result = mask_sensitive_data(line)
        assert "supersecret99" not in result, (
            f"Value for key '{key}' should be masked in '{line}', got: {result}"
        )
        assert "*****" in result, (
            f"Mask placeholder missing for key '{key}', got: {result}"
        )


@th.unit_test("mask_sensitive_data covers every SENSITIVE_KEYS entry (JSON form)")
def test_mask_sensitive_covers_all_keys_json(opts):
    from mojo.helpers.logit import mask_sensitive_data, SENSITIVE_KEYS
    for key in SENSITIVE_KEYS:
        line = f'{{"{key}": "jsonsecret42", "other": "keep"}}'
        result = mask_sensitive_data(line)
        assert "jsonsecret42" not in result, (
            f"JSON value for key '{key}' should be masked in '{line}', got: {result}"
        )
        assert "keep" in result, (
            f"Non-sensitive JSON field should be preserved, got: {result}"
        )


@th.unit_test("mask_sensitive_data regex is derived from SENSITIVE_KEYS (drift guard)")
def test_mask_sensitive_derived_from_frozenset(opts):
    """If someone hardcodes a parallel key list this test catches it."""
    from mojo.helpers.logit import _SENSITIVE_KEY_PATTERN, SENSITIVE_KEYS
    pattern = _SENSITIVE_KEY_PATTERN.pattern
    for key in SENSITIVE_KEYS:
        assert key in pattern, (
            f"Key '{key}' missing from compiled pattern — drift detected. "
            f"Pattern: {pattern}"
        )


@th.unit_test("mask_sensitive_data is case-insensitive across cases")
def test_mask_sensitive_case_insensitive(opts):
    from mojo.helpers.logit import mask_sensitive_data
    cases = [
        "PASSWORD=topsecret1",
        "Password=topsecret2",
        "password=topsecret3",
        "PasSwOrD=topsecret4",
    ]
    for line in cases:
        result = mask_sensitive_data(line)
        assert "topsecret" not in result, (
            f"Case variant should be masked: input={line}, got={result}"
        )


# ---------------------------------------------------------------------------
# mask_token
# ---------------------------------------------------------------------------

@th.unit_test("mask_token reveals only last 4 for long tokens")
def test_mask_token_long(opts):
    from mojo.helpers.logit import mask_token
    result = mask_token("abcdef1234567890")
    assert result == "****7890", f"Expected '****7890', got {result!r}"
    assert "abcdef" not in result, f"Leading chars must not leak: {result!r}"


@th.unit_test("mask_token fully masks short tokens (no reveal)")
def test_mask_token_short(opts):
    from mojo.helpers.logit import mask_token
    # 3 chars, default visible=4 — token is <= visible, must reveal nothing
    result = mask_token("abc")
    assert result == "*****", f"Short token should be fully masked, got {result!r}"
    # Exactly equal to visible length still reveals nothing
    result2 = mask_token("abcd")
    assert result2 == "*****", (
        f"Token of length == visible should be fully masked, got {result2!r}"
    )


@th.unit_test("mask_token respects custom visible length")
def test_mask_token_custom_visible(opts):
    from mojo.helpers.logit import mask_token
    result = mask_token("abcdef1234567890", visible=6)
    assert result == "****567890", f"Expected '****567890', got {result!r}"


@th.unit_test("mask_token returns empty/None unchanged")
def test_mask_token_empty_and_none(opts):
    from mojo.helpers.logit import mask_token
    assert mask_token("") == "", f"Empty string should pass through, got {mask_token('')!r}"
    assert mask_token(None) is None, f"None should pass through, got {mask_token(None)!r}"


# ---------------------------------------------------------------------------
# incident reporter — bearer masking
# ---------------------------------------------------------------------------

@th.unit_test("_create_event_dict masks request.bearer in metadata")
def test_create_event_dict_masks_bearer(opts):
    import objict
    from mojo.apps.incident.reporter import _create_event_dict

    raw_token = "abc123def456xyz789"
    # Synthetic request just detailed enough to exercise the auth branch.
    req = objict.objict()
    req.ip = "10.0.0.1"
    req.path = "/api/test"
    req.method = "POST"
    req.META = {}
    req.bearer = raw_token
    req.user = objict.objict()
    req.user.is_authenticated = True
    req.user.id = 42
    req.user.display_name = "Test User"
    req.user.email = "test@example.com"

    event = _create_event_dict(
        "test event",
        title="test",
        category="test_cat",
        level=3,
        request=req,
        scope="global",
    )

    bearer_val = event["metadata"].get("bearer")
    assert bearer_val is not None, "bearer should be recorded in metadata"
    assert bearer_val != raw_token, (
        f"Raw bearer must not be stored in metadata, got {bearer_val!r}"
    )
    assert bearer_val.endswith(raw_token[-4:]), (
        f"Masked bearer should end with last 4 chars, got {bearer_val!r}"
    )
    assert raw_token[:8] not in bearer_val, (
        f"Leading chars of raw token must not appear, got {bearer_val!r}"
    )
