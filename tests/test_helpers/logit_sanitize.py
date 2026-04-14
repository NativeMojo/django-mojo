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
