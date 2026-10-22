"""
Microsoft sign-in: provider registration and the /begin and /callback legs
(maestro #9105, plan #8967 B1-B2).

MICROSOFT_CLIENT_ID and MICROSOFT_TENANT are pinned in the test project
settings (bin/create_testproject), so nothing here changes settings.
Parallel-safe.
"""
from testit import helpers as th

PROVIDER = "microsoft"
TEST_CLIENT_ID = "00000000-0000-0000-0000-000000000001"


def _begin(opts):
    from urllib.parse import urlsplit, parse_qs
    resp = opts.client.get(f"/api/auth/oauth/{PROVIDER}/begin")
    assert resp.status_code == 200, f"begin must succeed, got {resp.status_code}: {resp.response}"
    data = resp.response.data
    assert data.auth_url and data.state, f"begin must return auth_url and state, got {data}"
    parts = urlsplit(data.auth_url)
    return data, parts, {k: v[0] for k, v in parse_qs(parts.query).items()}


@th.django_unit_test("microsoft oauth: provider is registered and discoverable")
def test_ms_provider_registered(opts):
    from mojo.apps.account.services.oauth import get_provider, PROVIDERS
    assert "microsoft" in PROVIDERS, "microsoft must be in the provider registry"
    assert get_provider("microsoft").name == "microsoft", "provider name must be microsoft"


@th.django_unit_test("microsoft oauth: begin builds the v2.0 authorize URL with PKCE S256, a nonce and response_mode=query")
def test_ms_begin_url(opts):
    data, parts, query = _begin(opts)
    assert parts.scheme == "https" and parts.netloc == "login.microsoftonline.com", \
        f"auth_url must go to login.microsoftonline.com, got {parts.netloc}"
    assert parts.path == "/common/oauth2/v2.0/authorize", \
        f"auth_url must use the tenant segment and v2.0 authorize path, got {parts.path}"
    assert query.get("client_id") == TEST_CLIENT_ID, f"client_id, got {query.get('client_id')}"
    assert query.get("response_type") == "code", f"response_type, got {query.get('response_type')}"
    assert query.get("response_mode") == "query", f"response_mode, got {query.get('response_mode')}"
    assert query.get("code_challenge_method") == "S256", "PKCE must use S256"
    assert len(query.get("code_challenge") or "") == 43, \
        f"code_challenge must be a base64url SHA-256 (43 chars), got {query.get('code_challenge')!r}"
    assert query.get("nonce"), "a nonce must be sent"
    assert query.get("state") == data.state, "the state in the URL must be the returned state"
    assert query.get("scope") == "openid profile email", f"scope, got {query.get('scope')}"
    assert query.get("redirect_uri", "").endswith(f"/api/auth/oauth/{PROVIDER}/callback"), \
        f"redirect_uri must be the backend callback, got {query.get('redirect_uri')}"
    for leaked in ("client_secret", "code_verifier"):
        assert leaked not in query, f"{leaked} must never be in the authorize URL"


@th.django_unit_test("microsoft oauth: the server-side state holds the verifier and nonce that match the URL")
def test_ms_begin_state_record(opts):
    import base64
    import hashlib
    from mojo.apps.account.services.oauth import get_provider
    data, parts, query = _begin(opts)
    record = get_provider(PROVIDER).peek_state(data.state)
    assert record, "the state record must exist"
    verifier = record.get("code_verifier")
    assert verifier and len(verifier) >= 43, "the state record must hold a PKCE verifier"
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == query.get("code_challenge"), "the URL challenge must be S256 of the stored verifier"
    assert record.get("nonce") == query.get("nonce"), "the stored nonce must be the one sent"
    assert verifier not in data.auth_url, "the verifier must never leave the server"


@th.django_unit_test("microsoft oauth: the callback bounce carries neither verifier nor nonce")
def test_ms_callback_bounce_clean(opts):
    from mojo.apps.account.services.oauth import get_provider
    svc = get_provider(PROVIDER)
    extra = svc.begin_state()
    extra.update({
        "redirect_uri": "http://localhost:9009/api/auth/oauth/microsoft/callback",
        "frontend_uri": "http://localhost:9009/api/login",
    })
    state = svc.create_state(extra=extra)
    resp = opts.client.get(
        f"/api/auth/oauth/{PROVIDER}/callback?code=mscode123&state={state}",
        allow_redirects=False)
    assert resp.status_code == 302, f"callback must 302, got {resp.status_code}: {resp.response}"
    headers = {k.lower(): v for k, v in opts.client.last_response.headers.items()}
    location = headers.get("location", "")
    assert "code=mscode123" in location and f"state={state}" in location, \
        f"bounce must carry code and state, got {location}"
    assert extra["code_verifier"] not in location, "the verifier must not reach the frontend"
    assert extra["nonce"] not in location, "the nonce must not reach the frontend"
    assert "code_verifier" not in location and "nonce" not in location, \
        f"no PKCE or nonce parameter may appear in the bounce, got {location}"
