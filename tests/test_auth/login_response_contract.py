"""The JWT login/refresh response shape is a published contract.

Nothing asserted it before, so the docs drifted from the code unnoticed: the
primary web-developer reference told clients the token arrived as `data.token`
(it never did), and `"expires_in": 21600` appeared in every login example across
both doc tracks even though no code path has ever returned it — 21600 is the
JWT_TOKEN_EXPIRY *setting*, which reaches clients only inside the token's `exp`
claim.

These tests pin the exact top-level key set rather than just checking the keys
we care about. An exact set is deliberate: adding a field to the response is a
documented API change, and this failing is the reminder to update
docs/web_developer/core/authentication.md and docs/web_developer/account/*.md.
"""
from testit import helpers as th

TESTIT_TIER = "core"

CONTRACT_USER = "login_contract_user"
CONTRACT_PWORD = "contract##mojo99"

# What jwt_login() actually returns: JWToken.create() builds the two tokens and
# jwt_login attaches the user graph. Only the OAuth new-account path adds a key
# (is_new_user), and that flow is covered in test_oauth.
LOGIN_KEYS = {"access_token", "refresh_token", "user"}

# on_refresh_token returns the bare token package — no user graph.
REFRESH_KEYS = {"access_token", "refresh_token"}

DOCS_HINT = (
    "If you changed the login response on purpose, update "
    "docs/web_developer/core/authentication.md and the response examples in "
    "docs/web_developer/account/*.md to match."
)


@th.django_unit_setup()
def setup_contract_user(opts):
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits

    clear_rate_limits(ip="127.0.0.1")
    # Long-lived DB — clear before creating, per testing conventions.
    User.objects.filter(username=CONTRACT_USER).delete()

    user = User(
        username=CONTRACT_USER,
        display_name="Login Contract",
        email=f"{CONTRACT_USER}@example.com",
    )
    user.save()
    user.is_email_verified = True
    user.save_password(CONTRACT_PWORD)
    user.remove_all_permissions()
    opts.contract_user_id = user.pk


@th.unit_test("login returns exactly access_token, refresh_token and user")
def test_login_response_key_set(opts):
    resp = opts.client.post(
        "/api/login",
        dict(username=CONTRACT_USER, password=CONTRACT_PWORD),
    )
    assert resp.status_code == 200, \
        f"login must succeed, got {resp.status_code}: {resp.response}"

    data = resp.response.data
    assert data, f"login response carried no data payload: {resp.response}"

    actual = set(data.keys())
    assert actual == LOGIN_KEYS, (
        f"login response keys drifted — expected {sorted(LOGIN_KEYS)}, "
        f"got {sorted(actual)}. {DOCS_HINT}"
    )

    assert isinstance(data.access_token, str) and data.access_token, \
        f"access_token must be a non-empty string, got {data.access_token!r}"
    assert isinstance(data.refresh_token, str) and data.refresh_token, \
        f"refresh_token must be a non-empty string, got {data.refresh_token!r}"
    assert data.access_token != data.refresh_token, \
        "access_token and refresh_token must be distinct tokens"

    assert isinstance(data.user, dict), \
        f"user must be an object, got {type(data.user).__name__}: {data.user!r}"
    assert data.user.id == opts.contract_user_id, \
        f"user.id must be the authenticated user {opts.contract_user_id}, got {data.user.id}"
    assert data.user.username == CONTRACT_USER, \
        f"user.username must be {CONTRACT_USER}, got {data.user.username!r}"


@th.unit_test("refresh returns exactly access_token and refresh_token")
def test_refresh_response_key_set(opts):
    # Mint a token here rather than borrowing one from the test above: if the
    # login contract breaks, this test should still report its OWN verdict
    # instead of cascading a confusing second failure.
    login = opts.client.post(
        "/api/login",
        dict(username=CONTRACT_USER, password=CONTRACT_PWORD),
    )
    assert login.status_code == 200, \
        f"login must succeed to obtain a refresh token, got {login.status_code}: {login.response}"
    refresh_token = login.response.data.refresh_token
    assert refresh_token, f"login did not return a refresh token: {login.response.data}"

    resp = opts.client.post(
        "/api/refresh_token",
        dict(refresh_token=refresh_token),
    )
    assert resp.status_code == 200, \
        f"refresh must succeed, got {resp.status_code}: {resp.response}"

    data = resp.response.data
    assert data, f"refresh response carried no data payload: {resp.response}"

    actual = set(data.keys())
    assert actual == REFRESH_KEYS, (
        f"refresh response keys drifted — expected {sorted(REFRESH_KEYS)}, "
        f"got {sorted(actual)}. {DOCS_HINT}"
    )

    assert isinstance(data.access_token, str) and data.access_token, \
        f"refreshed access_token must be a non-empty string, got {data.access_token!r}"
    assert isinstance(data.refresh_token, str) and data.refresh_token, \
        f"refreshed refresh_token must be a non-empty string, got {data.refresh_token!r}"
