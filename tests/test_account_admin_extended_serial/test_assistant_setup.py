"""The Assistant setup writer: encryption, precedence, the cache flip, and 440.

Opt-in and serial: every test here writes installation-wide Setting rows and
patches the provider verification seam, which is unsafe under the parallel
default tier. The read-only decorator, catalog-protection and asset contracts
stay in tests/test_account/test_admin_assistant.py.

Remote agent access (MCP) is exercised here too, for the same reason plus one
more: ``settings.get`` is DATABASE-first, so a stranded global
``ASSISTANT_MCP_ENABLED`` row would silently shadow a sibling module's
``th.server_settings(ASSISTANT_MCP_ENABLED=True)`` and make its assertions lie.
The key is therefore wiped — row AND Redis hash entry — in setup and in every
``finally``, exactly like the other six. The two live-endpoint tests run under
the OAuth wire module's ``BASE_URL`` pattern: rows deleted through the queryset
(``Setting.delete()`` refuses protected keys), the key dropped from the Redis
settings hash, then ``th.server_settings(BASE_URL=…)``.
"""

import time
from unittest import mock

import ujson
from testit import helpers as th


ADMIN_EMAIL = "assistant-setup-admin@test.com"
ADMIN_PASSWORD = "Assistant_setup_Admin_99"
REGULAR_EMAIL = "assistant-setup-regular@test.com"
REGULAR_PASSWORD = "Assistant_setup_Regular_99"
AGENT_EMAIL = "assistant-setup-agent@test.com"
AGENT_PASSWORD = "Assistant_setup_Agent_99"

KEYS = ("LLM_ADMIN_ENABLED", "LLM_ADMIN_API_KEY", "LLM_ADMIN_MODEL",
        "LLM_ADMIN_VERIFY_STATE", "LLM_HANDLER_API_KEY",
        "LLM_HANDLER_VERIFY_STATE", "ASSISTANT_MCP_ENABLED",
        "LLM_EMERGENCY_STOP", "LLM_AUTONOMOUS_INCIDENT_TRIAGE_ENABLED",
        "LLM_AUTONOMOUS_INCIDENT_TRIAGE_ACTIVATED_AT",
        "LLM_SAFETY_POLICY_EXPECTED_HASH")
PLATFORM_KEY = "sk-ant-platform-setup-wxyz5678"

STORED_KEY = "sk-ant-assistant-setup-abcd1234"

DISCOVERY_CACHE_KEY = "assistant:mcp:discovery"

BASE = "https://oauth.testit.example"
OTHER_BASE = "https://other.testit.example"
MCP_PATH = "/api/assistant/mcp"
RESOURCE = BASE + MCP_PATH
PRM_PATH = "/.well-known/oauth-protected-resource" + MCP_PATH
PRM_URL = BASE + PRM_PATH
CLIENT_IDS = ("assistant-setup-mcp-client-a", "assistant-setup-mcp-client-b")
OTHER_PATH = "/api/other/door"
OTHER_RESOURCE = BASE + OTHER_PATH
# The second resource this surface owns: the REST API root, where an `api`
# grant is bound. Listed, counted and swept together with the MCP door.
API_ROOT_PATH = "/api"
API_ROOT_RESOURCE = BASE + API_ROOT_PATH
KEY_GROUP = "assistant setup mcp key group"
KEY_NAME = "assistant setup mcp key"
AUTH_TIME = 1700000000


def _wipe():
    """Delete every assistant row AND its Redis entry.

    Setting.resolve reads Redis first, so a row deleted through the queryset
    (which is how a protected key has to be removed) would otherwise stay live
    in the cache for the next test in this module. The discovery verdict cache
    and this module's own OAuth rows go with them: a stranded verdict or grant
    would make the next test read a state it did not create.
    """
    from mojo.apps.account.models import (
        ApiKey, Group, OAuthClient, OAuthGrant, Setting)
    from mojo.helpers.redis import get_connection

    Setting.objects.filter(key__in=KEYS).delete()
    redis = Setting._redis()
    if redis:
        for key in KEYS:
            redis.hdel(Setting._redis_key(), key)
    OAuthGrant.objects.filter(client__client_id__in=CLIENT_IDS).delete()
    # Sibling opt-in modules (the MCP wire flow) leave their grants active at
    # BOTH paths this surface owns, and this module asserts installation-wide
    # totals for them, so anything it does not own is put out of the count
    # first. The API root belongs in that sweep for the same reason the MCP
    # path does: a stranded full-API grant would drift every total below.
    for path in (MCP_PATH, API_ROOT_PATH):
        OAuthGrant.objects.filter(
            is_active=True, resource__endswith=path,
        ).exclude(client__client_id__in=CLIENT_IDS).update(
            is_active=False, revoked_reason="test_wipe")
    OAuthClient.objects.filter(client_id__in=CLIENT_IDS).delete()
    ApiKey.objects.filter(name=KEY_NAME).delete()
    Group.objects.filter(name=KEY_GROUP).delete()
    try:
        get_connection().delete(DISCOVERY_CACHE_KEY)
    except Exception:
        pass


def _clear_base_url():
    """Drop the DB/Redis BASE_URL that would out-rank the file-plane override."""
    from mojo.apps.account.models import Setting

    Setting.objects.filter(key="BASE_URL", group=None).delete()
    try:
        Setting._redis().hdel(Setting._redis_key(), "BASE_URL")
    except Exception:
        pass


@th.django_unit_setup()
def setup_assistant_setup(opts):
    from mojo.apps.account.models import User

    _wipe()
    User.objects.filter(
        email__in=[ADMIN_EMAIL, REGULAR_EMAIL, AGENT_EMAIL]).delete()
    admin = User.objects.create_user(
        username=ADMIN_EMAIL, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)
    admin.is_active = True
    admin.is_email_verified = True
    admin.requires_mfa = False
    admin.is_superuser = True
    admin.save()
    admin.get_auth_key()
    regular = User.objects.create_user(
        username=REGULAR_EMAIL, email=REGULAR_EMAIL, password=REGULAR_PASSWORD)
    regular.is_active = True
    regular.is_email_verified = True
    regular.requires_mfa = False
    regular.permissions = {"manage_settings": True}
    regular.save()
    # A second operator who may use the MCP door but owns nothing here: the
    # Admin lists grants for EVERY user, and one grant has to belong to
    # somebody other than the owner for that to mean anything.
    agent = User.objects.create_user(
        username=AGENT_EMAIL, email=AGENT_EMAIL, password=AGENT_PASSWORD)
    agent.is_active = True
    agent.is_email_verified = True
    agent.requires_mfa = False
    agent.permissions = {"view_admin": True}
    agent.save()
    agent.get_auth_key()
    opts.assistant_admin_id = admin.pk
    opts.assistant_regular_id = regular.pk
    opts.assistant_agent_id = agent.pk


def _admin(opts):
    from mojo.apps.account.models import User
    return User.objects.get(pk=opts.assistant_admin_id)


def _accepts():
    """Patch the provider check so it accepts, without reaching Anthropic."""
    from mojo.apps.account.services import assistant_setup
    return mock.patch.object(
        assistant_setup, "_verify_candidate",
        lambda actor, candidate=None, stored_target=None: {
            "ok": True, "code": "verified", "message": "Anthropic accepted this key."})


def _rejects():
    from mojo.apps.account.services import assistant_setup
    return mock.patch.object(
        assistant_setup, "_verify_candidate",
        lambda actor, candidate=None, stored_target=None: {
            "ok": False, "code": "invalid_key", "message": "Anthropic rejected this key."})


@th.django_unit_test("the stored credential is encrypted and never returned")
def test_saved_key_is_encrypted_and_write_only(opts):
    from mojo.apps.account.models import Setting
    from mojo.apps.account.services import assistant_setup
    from mojo.helpers import llm

    _wipe()
    try:
        with _accepts():
            state = assistant_setup.save(
                _admin(opts), enabled=True, model="", api_key=STORED_KEY)

        row = Setting.objects.get(key="LLM_ADMIN_API_KEY", group=None)
        assert row.is_secret is True, "the credential row is not marked secret"
        assert row.value == "", f"the plaintext key was stored in `value`: {row.value!r}"
        assert row.mojo_secrets, "the encrypted payload is empty"
        assert STORED_KEY not in (row.mojo_secrets or ""), \
            "the credential appears verbatim in the encrypted column"
        assert row.get_value() == STORED_KEY, "the stored credential does not decrypt back"

        assert set(state["key"]) == {"configured", "hint", "source"}, \
            f"the key state carries more than presence, hint and provenance: {state['key']!r}"
        assert state["key"]["configured"] is True and state["key"]["source"] == "admin", \
            f"a saved credential is not reported as Admin-owned: {state['key']!r}"
        assert state["key"]["hint"] == STORED_KEY[-4:], \
            f"the hint is not the last four characters: {state['key']['hint']!r}"

        # Every field of every read, not just the one the browser looks at.
        serialized = ujson.dumps(assistant_setup.state())
        assert STORED_KEY not in serialized, \
            "the stored credential appears somewhere in the setup state payload"
        assert llm.get_api_key() == STORED_KEY, \
            "the saved credential is not what the LLM helper resolves"
    finally:
        _wipe()


@th.django_unit_test("Admin storage outranks the deployment fallback, and says so")
def test_key_precedence_and_provenance(opts):
    from mojo.apps.account.models import Setting
    from mojo.apps.account.services import assistant_setup
    from mojo.helpers import llm

    _wipe()
    try:
        # A pre-existing fallback row, created the only way a protected key can
        # be: through the queryset, bypassing the guarded save path.
        Setting.objects.bulk_create([
            Setting(key="LLM_HANDLER_API_KEY", group=None, value="sk-fallback-999988887777")])
        state = assistant_setup.state()
        assert state["key"]["source"] == "fallback", \
            f"a deployment-fallback-only installation reported {state['key']!r}"
        assert state["key"]["hint"] == "7777", \
            f"the fallback hint is not its last four characters: {state['key']!r}"

        with _accepts():
            assistant_setup.save(_admin(opts), enabled=True, model="", api_key=STORED_KEY)
        state = assistant_setup.state()
        assert state["key"]["source"] == "admin", \
            f"a stored Admin credential did not outrank the fallback: {state['key']!r}"
        assert llm.get_api_key() == STORED_KEY, \
            "resolution still prefers the fallback over the Admin credential"

        # Clearing is allowed while enabled; the readiness sentence has to stay
        # honest rather than claiming a credential is configured here.
        with _accepts():
            assistant_setup.save(_admin(opts), enabled=True, model="", clear_api_key=True)
        state = assistant_setup.state()
        assert state["key"]["source"] == "fallback", \
            f"clearing the Admin key did not fall back honestly: {state['key']!r}"
    finally:
        _wipe()


@th.django_unit_test("a credential nobody proved is never stored")
def test_save_refuses_an_unverified_key(opts):
    from mojo import errors as merrors
    from mojo.apps.account.models import Setting
    from mojo.apps.account.services import assistant_setup

    _wipe()
    try:
        with _rejects():
            with th.assert_raises(merrors.ValueException):
                assistant_setup.save(
                    _admin(opts), enabled=True, model="", api_key="sk-not-a-real-key")
        assert not Setting.objects.filter(key="LLM_ADMIN_API_KEY").exists(), \
            "a rejected credential left a row behind"
        assert not Setting.objects.filter(key="LLM_ADMIN_ENABLED").exists(), \
            "a refused save still flipped the feature flag"
    finally:
        _wipe()


@th.django_unit_test("only a check of the STORED key is recorded")
def test_verify_records_only_the_stored_key(opts):
    from mojo.apps.account.models import Setting
    from mojo.apps.account.services import assistant_setup

    _wipe()
    try:
        with _accepts():
            assistant_setup.save(_admin(opts), enabled=True, model="", api_key=STORED_KEY)
        Setting.objects.filter(key="LLM_ADMIN_VERIFY_STATE").delete()

        # A draft candidate is not the configuration this installation runs, so
        # recording it would describe something that does not exist.
        with _accepts():
            assistant_setup.verify(_admin(opts), "sk-ant-some-other-candidate")
        assert not Setting.objects.filter(key="LLM_ADMIN_VERIFY_STATE").exists(), \
            "checking an unsaved candidate was recorded as the stored state"

        with _accepts():
            result = assistant_setup.verify(_admin(opts))
        assert result["ok"] is True and result["code"] == "verified", \
            f"checking the stored key did not succeed: {result!r}"
        recorded = assistant_setup.read_verify_state()
        assert recorded["ok"] is True and recorded["at"], \
            f"checking the stored key was not recorded: {recorded!r}"

        with _rejects():
            failed = assistant_setup.verify(_admin(opts))
        assert failed["ok"] is False and failed["code"] == "invalid_key", \
            f"a rejected stored key was not reported: {failed!r}"
        assert failed["message"] == assistant_setup.VERIFY_MESSAGES["invalid_key"], \
            "the outcome message left the fixed vocabulary"
    finally:
        _wipe()


@th.django_unit_test("enable and disable both reach the cache the resolver reads first")
def test_enable_disable_round_trip(opts):
    from mojo.apps.account.services import assistant_setup
    from mojo.helpers.settings import settings

    _wipe()
    try:
        with _accepts():
            assistant_setup.save(_admin(opts), enabled=True, model="", api_key=STORED_KEY)
        assert settings.get("LLM_ADMIN_ENABLED", False, kind="bool") is True, \
            "enabling the Assistant did not read back as True"

        with _accepts():
            assistant_setup.save(_admin(opts), enabled=False, model="")
        # The regression: a queryset .update() passes an ORM read and leaves the
        # Redis value Setting.resolve consults FIRST saying "true", so the
        # disable silently does not take effect.
        assert settings.get("LLM_ADMIN_ENABLED", False, kind="bool") is False, \
            "disabling the Assistant did not reach the settings cache"
        assert assistant_setup.is_ready() is False, \
            "a disabled Assistant still reports itself ready"

        with _accepts():
            assistant_setup.save(_admin(opts), enabled=True, model="claude-sonnet-5")
        assert settings.get("LLM_ADMIN_MODEL", None) == "claude-sonnet-5", \
            "the model pin did not round-trip through the settings chain"
        with _accepts():
            state = assistant_setup.save(_admin(opts), enabled=True, model="")
        assert settings.get("LLM_ADMIN_MODEL", None) in (None, ""), \
            "choosing Automatic did not remove the model pin"
        assert state["model"]["source"] == "automatic", \
            f"an unpinned model is not reported as automatic: {state['model']!r}"
    finally:
        _wipe()


@th.django_unit_test("a static emergency stop is displayed but never persisted by Save")
def test_static_stop_does_not_flip_database_stop(opts):
    from mojo.apps.account.models import Setting
    from mojo.apps.account.services import assistant_setup, llm_safety

    _wipe()
    try:
        assistant_setup.save(
            _admin(opts), enabled=False, model="", emergency_stop=False)
        with mock.patch.object(llm_safety, "emergency_stop_static", return_value=True):
            state = assistant_setup.state()
            assert state["emergency_stop"] is True \
                and state["emergency_stop_static"] is True \
                and state["emergency_stop_database"] is False, \
                f"effective/static/database stop halves were conflated: {state}"
            assistant_setup.save(_admin(opts), enabled=True, model="")
        Setting.objects.get(key="LLM_EMERGENCY_STOP", group=None)
        assert llm_safety.emergency_stop_database() is False, \
            "an unrelated Save under a static stop persisted database=true"
    finally:
        _wipe()


@th.django_unit_test("input validation is structural and bounded")
def test_input_validation(opts):
    from mojo import errors as merrors
    from mojo.apps.account.services import assistant_setup

    assert assistant_setup.normalize_model("claude-sonnet-5") == "claude-sonnet-5", \
        "a valid model identifier was rejected"
    assert assistant_setup.normalize_model("") == "" and \
        assistant_setup.normalize_model(None) == "", \
        "Automatic is not expressible"
    for bad in ("Claude Sonnet", "../etc/passwd", "a" * 200, {"id": "x"}):
        with th.assert_raises(merrors.ValueException):
            assistant_setup.normalize_model(bad)
    with th.assert_raises(merrors.ValueException):
        assistant_setup.save(_admin(opts), enabled=True, model="", api_key="x" * 5000)


@th.django_unit_test("nothing below a live literal superuser writes Assistant settings")
def test_writer_requires_a_literal_superuser(opts):
    from mojo import errors as merrors
    from mojo.apps.account.models import User
    from mojo.apps.account.services import assistant_setup

    regular = User.objects.get(pk=opts.assistant_regular_id)
    with th.assert_raises(merrors.PermissionDeniedException):
        assistant_setup.save(regular, enabled=True, model="")
    with th.assert_raises(merrors.PermissionDeniedException):
        assistant_setup.verify(regular)
    with th.assert_raises(merrors.PermissionDeniedException):
        assistant_setup.save(None, enabled=True, model="")


@th.django_unit_test("the fallback key is protected from the generic settings API")
def test_generic_settings_api_refuses_the_fallback(opts):
    from mojo.apps.account.services import admin_settings

    assert admin_settings.is_catalog_protected("LLM_HANDLER_API_KEY"), \
        "the deployment fallback is not catalog-protected, so a database row " \
        "written through /api/settings would outrank the file"
    assert opts.client.login(ADMIN_EMAIL, ADMIN_PASSWORD), "assistant admin login failed"
    response = opts.client.post(
        "/api/settings", json={"key": "LLM_HANDLER_API_KEY", "value": "sk-evil"})
    assert response.status_code == 403, \
        f"the generic settings API wrote a protected assistant key: {response.status_code} {response.body}"


@th.django_unit_test("a stale session gets 440 and writes nothing")
def test_stale_session_is_refused(opts):
    from mojo.apps.account.models import Setting, User

    _wipe()
    try:
        admin = User.objects.get(pk=opts.assistant_admin_id)
        from mojo.apps.account.utils.jwtoken import JWToken
        stale = JWToken(admin.get_auth_key()).create_access_token(
            uid=admin.pk, auth_time=int(time.time()) - 4000)
        opts.client.access_token = stale
        opts.client.is_authenticated = True
        opts.client.bearer = "bearer"
        origin = opts.client.host.rstrip("/")
        response = opts.client.post(
            "/api/account/admin/assistant",
            json={"action": "save", "enabled": True, "model": ""},
            headers={"Origin": origin})
        opts.client.logout()
        assert response.status_code == 440, \
            f"a stale token was accepted by the Assistant writer: {response.status_code} {response.body}"
        assert not Setting.objects.filter(key="LLM_ADMIN_ENABLED").exists(), \
            "a 440'd request still wrote the feature flag"
    finally:
        _wipe()


@th.django_unit_test("a non-superuser is refused at the endpoint, not just the service")
def test_endpoint_refuses_below_owner(opts):
    _wipe()
    try:
        assert opts.client.login(REGULAR_EMAIL, REGULAR_PASSWORD), \
            "assistant regular login failed"
        origin = opts.client.host.rstrip("/")
        read = opts.client.get("/api/account/admin/assistant")
        assert read.status_code in (403, 404), \
            f"a manage_settings holder read the Assistant key state: {read.status_code} {read.body}"
        write = opts.client.post(
            "/api/account/admin/assistant",
            json={"action": "save", "enabled": True, "model": ""},
            headers={"Origin": origin})
        opts.client.logout()
        assert write.status_code in (403, 404, 440), \
            f"a manage_settings holder wrote Assistant settings: {write.status_code} {write.body}"
    finally:
        _wipe()


@th.django_unit_test("the platform LLM key is settable, encrypted, honoured by every reader, and clearable")
def test_platform_key_is_settable_from_admin(opts):
    from mojo.apps.account.models import Setting
    from mojo.apps.account.services import assistant_setup
    from mojo.apps.incident import cronjobs as incident_cron
    from mojo.apps.incident.models import event as incident_event
    from mojo.helpers import llm
    from mojo.helpers.settings import settings

    _wipe()
    try:
        before = assistant_setup.state()
        assert before["handler_key"]["source"] == "none", \
            f"a wiped installation still reports a platform key: {before['handler_key']!r}"
        assert incident_cron._llm_triage_enabled() is False, \
            "incident triage believes a platform key exists before one is stored"

        with _accepts():
            state = assistant_setup.save(
                _admin(opts), enabled=True, model="", handler_api_key=PLATFORM_KEY,
                autonomous_triage=True)

        row = Setting.objects.get(key="LLM_HANDLER_API_KEY", group=None)
        assert row.is_secret is True, "the platform key row is not marked secret"
        assert row.value == "", f"the plaintext platform key was stored in `value`: {row.value!r}"
        assert row.get_value() == PLATFORM_KEY, "the stored platform key does not decrypt back"
        assert set(state["handler_key"]) == {"configured", "hint", "source"}, \
            f"the platform key state carries more than presence, hint and provenance: {state['handler_key']!r}"
        assert state["handler_key"]["source"] == "admin" and \
            state["handler_key"]["hint"] == PLATFORM_KEY[-4:], \
            f"a stored platform key is not reported as Admin-owned: {state['handler_key']!r}"
        # With no Assistant key of its own, the Assistant resolves through the
        # platform key — and says so.
        assert state["key"]["source"] == "fallback" and state["key"]["hint"] == PLATFORM_KEY[-4:], \
            f"the Assistant does not resolve through the stored platform key: {state['key']!r}"
        assert PLATFORM_KEY not in ujson.dumps(state), \
            "the platform key appears somewhere in the setup state payload"
        assert state["handler_verify"]["ok"] is True, \
            f"storing a verified platform key did not record its verification: {state['handler_verify']!r}"

        # Every reader that used to freeze the deployment-file value at import
        # now sees the Admin-stored row.
        assert llm.get_api_key() == PLATFORM_KEY, "the LLM helper does not resolve the stored platform key"
        assert settings.get("LLM_HANDLER_API_KEY") == PLATFORM_KEY, \
            "settings.get does not resolve the stored platform key"
        assert incident_cron._llm_triage_enabled() is True, \
            "incident triage ignores a platform key stored from the Admin"
        assert incident_event._autonomous_llm_enabled() is True, \
            "the default LLM triage path ignores the authoritative owner activation"

        # Checking the stored platform key records against ITS record, not the
        # Assistant's.
        with _rejects():
            result = assistant_setup.verify(_admin(opts), target="handler")
        state = assistant_setup.state()
        assert result["code"] == "invalid_key" and state["handler_verify"]["code"] == "invalid_key", \
            f"a platform-key check was not recorded on the platform record: {state['handler_verify']!r}"
        assert state["verify"]["code"] != "invalid_key", \
            f"a platform-key check leaked into the Assistant key's record: {state['verify']!r}"

        with _accepts():
            state = assistant_setup.save(
                _admin(opts), enabled=True, model="", clear_handler_api_key=True)
        assert state["handler_key"]["source"] == "none" and state["key"]["source"] == "none", \
            f"clearing the platform key did not fall back honestly: {state['handler_key']!r} / {state['key']!r}"
        assert not Setting.objects.filter(
            key__in=["LLM_HANDLER_API_KEY", "LLM_HANDLER_VERIFY_STATE"]).exists(), \
            "clearing the platform key left rows behind"
        assert settings.get("LLM_HANDLER_API_KEY") is None, \
            "the cleared platform key is still served from the settings cache"
    finally:
        _wipe()


@th.django_unit_test("the platform key edit obeys the same refusals as the Assistant key")
def test_platform_key_edit_validation(opts):
    from mojo import errors as merrors
    from mojo.apps.account.models import Setting
    from mojo.apps.account.services import assistant_setup

    _wipe()
    try:
        with _rejects(), th.assert_raises(merrors.ValueException):
            assistant_setup.save(
                _admin(opts), enabled=True, model="", handler_api_key=PLATFORM_KEY)
        assert not Setting.objects.filter(key="LLM_HANDLER_API_KEY").exists(), \
            "a rejected platform key was stored anyway"
        with th.assert_raises(merrors.ValueException):
            assistant_setup.save(
                _admin(opts), enabled=True, model="",
                handler_api_key=PLATFORM_KEY, clear_handler_api_key=True)
        with th.assert_raises(merrors.ValueException):
            assistant_setup.verify(_admin(opts), target="everything")

        assert opts.client.login(ADMIN_EMAIL, ADMIN_PASSWORD), "assistant admin login failed"
        origin = opts.client.host.rstrip("/")
        response = opts.client.post(
            "/api/account/admin/assistant",
            json={"action": "save", "enabled": True, "model": "",
                  "handler_api_key": PLATFORM_KEY, "unexpected": 1},
            headers={"Origin": origin})
        opts.client.logout()
        assert response.status_code == 400, \
            f"a save with an unexpected field was not refused: {response.status_code} {response.body}"
        assert not Setting.objects.filter(key="LLM_HANDLER_API_KEY").exists(), \
            "a refused save still stored the platform key"
    finally:
        _wipe()


# ---------------------------------------------------------------------------
# Remote agent access (MCP)
# ---------------------------------------------------------------------------
#
# Local copies of the safe_fetch fakes (tests/test_helpers/safe_fetch.py): the
# self-check's transport and resolver are injected seams, so every branch is
# exercised without a socket and without patching anything shared.

def _response(status, headers=None, body=b""):
    """A bare requests.Response the helper can drive like a live one."""
    import requests
    from requests.structures import CaseInsensitiveDict

    resp = requests.Response()
    resp.status_code = status
    # Must be case-insensitive: Response.is_redirect tests `"location" in headers`
    resp.headers = CaseInsensitiveDict(headers or {})
    resp._content = body
    resp._content_consumed = True
    resp.encoding = "utf-8"
    return resp


class _Transport:
    """Maps URL -> response or exception, and records every get() call."""

    def __init__(self, routes=None, default=None):
        self.routes = routes or {}
        self.default = default
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        answer = self.routes.get(url, self.default)
        assert answer is not None, f"transport asked for an unscripted URL: {url}"
        if isinstance(answer, Exception):
            raise answer
        return answer

    @property
    def urls(self):
        return [url for url, _ in self.calls]


class _ExplodingTransport:
    def get(self, url, **kwargs):
        raise AssertionError(f"transport must not be called, but was asked for {url}")


def _prm_body(resource=RESOURCE):
    return ujson.dumps({
        "resource": resource,
        "authorization_servers": [BASE + "/api/account/oauth"],
        "scopes_supported": ["mcp"],
        "bearer_methods_supported": ["header"],
    }).encode()


def _rpc(msg_id, method):
    return {"jsonrpc": "2.0", "id": msg_id, "method": method}


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _make_grant(user, client, resource=RESOURCE, scopes=None):
    """A live grant plus the raw credential pair a client would hold."""
    from mojo.apps.account.services.oauth_server import tokens

    grant = tokens.create_grant(
        user, client, scopes or ["mcp"], resource, AUTH_TIME)
    return grant, tokens.issue_tokens(grant)


@th.django_unit_test("the remote agent access switch round-trips and refuses non-booleans")
def test_mcp_switch_round_trip(opts):
    from mojo import errors as merrors
    from mojo.apps.account.models import Setting
    from mojo.apps.account.services import assistant_setup
    from mojo.helpers.settings import settings

    _wipe()
    try:
        assistant_setup.save(_admin(opts), enabled=False, model="", mcp_enabled=True)
        assert settings.get("ASSISTANT_MCP_ENABLED", False, kind="bool") is True, \
            "switching remote agent access on did not reach the settings cache"
        assert Setting.objects.filter(
            key="ASSISTANT_MCP_ENABLED", group=None).exists(), \
            "the remote agent access switch left no global row"
        state = assistant_setup.state()
        assert state["mcp"]["enabled"] is True, \
            f"the setup state does not report the switch: {state['mcp']!r}"
        # Honest capability: on, but this process has no public address, so no
        # client could actually find the door.
        assert assistant_setup.mcp_ready() is False, \
            "mcp_ready() claimed a reachable door with no public address"

        # Omitting the field leaves the stored value alone — the api-key rule.
        assistant_setup.save(_admin(opts), enabled=False, model="")
        assert settings.get("ASSISTANT_MCP_ENABLED", False, kind="bool") is True, \
            "a save that omitted mcp_enabled switched remote access off"

        # And so does an explicit JSON null, over the wire.
        assert opts.client.login(ADMIN_EMAIL, ADMIN_PASSWORD), \
            "assistant admin login failed"
        origin = opts.client.host.rstrip("/")
        response = opts.client.post(
            "/api/account/admin/assistant",
            json={"action": "save", "enabled": False, "model": "",
                  "mcp_enabled": None},
            headers={"Origin": origin})
        opts.client.logout()
        assert response.status_code == 200, \
            f"a null mcp_enabled was refused: {response.status_code} {response.body}"
        assert settings.get("ASSISTANT_MCP_ENABLED", False, kind="bool") is True, \
            "a null mcp_enabled switched remote access off"

        # Turning it off drops the cached discovery verdict with it.
        from mojo.helpers.redis import get_connection
        get_connection().setex(DISCOVERY_CACHE_KEY, 60, ujson.dumps(
            {"ok": True, "code": "ok", "detail": "cached",
             "checked_at": "2026-08-22T09:00:00+00:00", "resource": RESOURCE}))
        assistant_setup.save(_admin(opts), enabled=False, model="", mcp_enabled=False)
        assert settings.get("ASSISTANT_MCP_ENABLED", False, kind="bool") is False, \
            "switching remote agent access off did not reach the settings cache"
        assert get_connection().get(DISCOVERY_CACHE_KEY) is None, \
            "writing the switch left yesterday's discovery verdict in place"

        for bad in ("yes", 1, 0, [], {}):
            with th.assert_raises(merrors.ValueException):
                assistant_setup.save(
                    _admin(opts), enabled=False, model="", mcp_enabled=bad)
        assert settings.get("ASSISTANT_MCP_ENABLED", False, kind="bool") is False, \
            "a refused mcp_enabled still moved the switch"

        # --- the audit names the direction, and does not collapse ----------
        # report_event_suppressed dedupes on (category, key) for an hour, so a
        # bare "mcp_enabled" would turn on -> off -> on into ONE event that
        # cannot say which way the door moved.
        from mojo.apps import incident
        from mojo.apps.logit.models import Log

        Log.objects.filter(model_name="account.User",
                           model_id=opts.assistant_admin_id,
                           kind="assistant:mcp_switch").delete()
        filed = []

        def _record(details, key=None, **kwargs):
            filed.append(key or "")
            return True

        with mock.patch.object(incident, "report_event_suppressed", _record):
            assistant_setup.save(
                _admin(opts), enabled=False, model="", mcp_enabled=True)
            assistant_setup.save(
                _admin(opts), enabled=False, model="", mcp_enabled=False)
        switched = [key for key in filed if "mcp_enabled" in key]
        assert len(set(switched)) == 2, \
            f"switching on and off share one suppression key, so an hour of " \
            f"flipping files a single ambiguous event: {switched!r}"
        assert any("mcp_enabled:on" in key for key in switched) and \
            any("mcp_enabled:off" in key for key in switched), \
            f"the audit key does not name the direction: {switched!r}"

        lines = list(Log.objects.filter(
            model_name="account.User", model_id=opts.assistant_admin_id,
            kind="assistant:mcp_switch").order_by("pk").values_list("log", flat=True))
        assert len(lines) == 2, \
            f"two switch writes did not leave two audit lines on the actor: {lines!r}"
        assert "switched on" in lines[0] and "switched off" in lines[1], \
            f"the switch audit lines do not name their direction: {lines!r}"
    finally:
        _wipe()
        from mojo.apps.logit.models import Log as _Log
        _Log.objects.filter(model_name="account.User",
                            model_id=opts.assistant_admin_id,
                            kind="assistant:mcp_switch").delete()


@th.django_unit_test("flipping the switch opens and closes the live MCP door")
def test_mcp_flip_gates_the_endpoint_live(opts):
    from mojo.apps.account.models import OAuthClient
    from mojo.apps.account.services import assistant_setup

    _wipe()
    _clear_base_url()
    opts.client.logout()
    try:
        with th.server_settings(BASE_URL=BASE):
            client = OAuthClient.objects.create(
                client_id=CLIENT_IDS[0], kind="dcr",
                client_name="assistant setup flip client",
                redirect_uris=["http://127.0.0.1:8500/cb"])
            _grant, pair = _make_grant(_admin(opts), client)
            token = pair["access_token"]

            # --- switched off ------------------------------------------------
            resp = opts.client.post(MCP_PATH, _rpc(1, "ping"))
            assert resp.status_code == 404, \
                f"a disabled door must be indistinguishable from an unknown " \
                f"route, got {resp.status_code} {resp.body}"
            # A presented credential is refused by the auth chokepoint before
            # any view runs: validate_access refuses a disabled resource.
            resp = opts.client.post(MCP_PATH, _rpc(1, "ping"), headers=_auth(token))
            assert resp.status_code == 401, \
                f"a token for a disabled resource was not refused: " \
                f"{resp.status_code} {resp.body}"

            # --- switched on, no restart -------------------------------------
            assistant_setup.save(
                _admin(opts), enabled=False, model="", mcp_enabled=True)
            resp = opts.client.post(MCP_PATH, _rpc(1, "ping"), headers=_auth(token))
            assert resp.status_code == 200, \
                f"the same token was refused after the switch was flipped on: " \
                f"{resp.status_code} {resp.body}"
            assert "result" in resp.response and resp.response.get("id") == 1, \
                f"the live door did not answer the JSON-RPC ping: {resp.response!r}"

            # --- switched off again ------------------------------------------
            assistant_setup.save(
                _admin(opts), enabled=False, model="", mcp_enabled=False)
            resp = opts.client.post(MCP_PATH, _rpc(1, "ping"), headers=_auth(token))
            assert resp.status_code == 401, \
                f"a token still opened the door after it was switched off: " \
                f"{resp.status_code} {resp.body}"
            resp = opts.client.post(MCP_PATH, _rpc(1, "ping"))
            assert resp.status_code == 404, \
                f"the switched-off door stopped answering 404: " \
                f"{resp.status_code} {resp.body}"
    finally:
        _wipe()
        _clear_base_url()


@th.django_unit_test("disconnecting one agent, or all of them, kills their credentials")
def test_revoke_one_and_all(opts):
    from mojo import errors as merrors
    from mojo.apps.account.models import OAuthClient, User
    from mojo.apps.account.services import assistant_setup
    from mojo.apps.account.services import oauth_server
    from mojo.apps.account.services.oauth_server import tokens

    _wipe()
    _clear_base_url()
    opts.client.logout()
    try:
        with th.server_settings(BASE_URL=BASE):
            admin = _admin(opts)
            agent = User.objects.get(pk=opts.assistant_agent_id)
            regular = User.objects.get(pk=opts.assistant_regular_id)
            client_a = OAuthClient.objects.create(
                client_id=CLIENT_IDS[0], kind="dcr", client_name="Agent A",
                redirect_uris=["http://127.0.0.1:8500/cb"])
            client_b = OAuthClient.objects.create(
                client_id=CLIENT_IDS[1], kind="dcr", client_name="Agent B",
                redirect_uris=["http://127.0.0.1:8500/cb"])
            grant_one, pair_one = _make_grant(admin, client_a)
            grant_two, pair_two = _make_grant(admin, client_b)
            grant_three, pair_three = _make_grant(agent, client_a)
            # A grant at a DIFFERENT registered resource. This surface owns
            # remote agent access, not every OAuth resource the installation
            # may protect, so it must neither list nor sweep this one.
            other_grant, _other_pair = _make_grant(
                admin, client_a, OTHER_RESOURCE)
            # A connection at the API ROOT, consented to for both kinds of
            # access. This surface owns it too — one switch, two doors.
            api_grant, api_pair = _make_grant(
                admin, client_a, API_ROOT_RESOURCE, scopes=["mcp", "api"])
            assistant_setup.save(
                admin, enabled=False, model="", mcp_enabled=True)

            listed = assistant_setup.state()["mcp"]
            assert listed["grant_count"] == 4, \
                f"the Admin does not list all four connections: {listed!r}"
            assert all(row["resource"] in (RESOURCE, API_ROOT_RESOURCE)
                       for row in listed["grants"]), \
                f"the connected-agents list is not scoped to the two resource " \
                f"paths: {[row['resource'] for row in listed['grants']]!r}"
            access = {row["id"]: row["access"] for row in listed["grants"]}
            assert access[api_grant.pk] == "both", \
                f"a grant carrying mcp and api must read as both: {access!r}"
            assert access[grant_one.pk] == "tools" \
                and access[grant_two.pk] == "tools", \
                f"a tool-door grant must read as tools: {access!r}"

            # The bound lives in SQL, and the count still sees past it.
            assert len(oauth_server.list_grants(
                resource_path=MCP_PATH, limit=2)) == 2, \
                "list_grants ignored its row bound"
            assert oauth_server.count_grants(resource_path=MCP_PATH) == 3, \
                "the grant count cannot see past the slice"
            assert oauth_server.count_grants(
                resource_path=[MCP_PATH, API_ROOT_PATH]) == 4, \
                "the count does not span both resources in one predicate"
            assert oauth_server.count_grants(resource_path=OTHER_PATH) == 1, \
                "the count is not scoped by resource path"

            # The full-API token is the person's session: it reaches an
            # ordinary REST endpoint no mcp-only token can.
            resp = opts.client.get(
                "/api/account/user/me", headers=_auth(api_pair["access_token"]))
            assert resp.status_code == 200, \
                f"a full-API grant could not reach an ordinary REST endpoint: " \
                f"{resp.status_code} {resp.body}"
            emails = {row["user"]["email"] for row in listed["grants"]}
            assert emails == {ADMIN_EMAIL, AGENT_EMAIL}, \
                f"the connected-agents list does not name both operators: {emails!r}"
            for row in listed["grants"]:
                for banned in ("access_jti", "refresh_hash", "prev_refresh_hash"):
                    assert banned not in row, \
                        f"a listed grant carries the credential column {banned}: {row!r}"

            for token, why in ((pair_one["access_token"], "the owner's first agent"),
                               (pair_two["access_token"], "the owner's second agent"),
                               (pair_three["access_token"], "the other operator's agent")):
                resp = opts.client.post(MCP_PATH, _rpc(1, "ping"), headers=_auth(token))
                assert resp.status_code == 200, \
                    f"{why} could not reach the live door: " \
                    f"{resp.status_code} {resp.body}"

            # --- one -----------------------------------------------------
            assert assistant_setup.revoke_grant(admin, grant_one.pk) == 1, \
                "disconnecting a live agent did not report one revocation"
            resp = opts.client.post(
                MCP_PATH, _rpc(1, "ping"), headers=_auth(pair_one["access_token"]))
            assert resp.status_code == 401, \
                f"a disconnected agent's access token still worked: " \
                f"{resp.status_code} {resp.body}"
            with th.assert_raises(tokens.TokenError):
                tokens.refresh_grant(pair_one["refresh_token"], client_a)
            resp = opts.client.post(
                MCP_PATH, _rpc(1, "ping"), headers=_auth(pair_two["access_token"]))
            assert resp.status_code == 200, \
                f"disconnecting one agent killed another: " \
                f"{resp.status_code} {resp.body}"

            assert assistant_setup.revoke_grant(admin, grant_one.pk) == 0, \
                "re-disconnecting a dead grant did not answer a quiet zero"
            assert assistant_setup.revoke_grant(admin, 10 ** 9) == 0, \
                "an unknown grant id did not answer a quiet zero"

            # --- the boundary --------------------------------------------
            with th.assert_raises(merrors.PermissionDeniedException):
                assistant_setup.revoke_grant(regular, grant_two.pk)
            with th.assert_raises(merrors.PermissionDeniedException):
                assistant_setup.revoke_all_grants(regular)
            for bad in (True, "1", 0, -1, 1.5, None):
                with th.assert_raises(merrors.ValueException):
                    assistant_setup.revoke_grant(admin, bad)

            # --- a key-backed session is not a person --------------------
            # Two defences, and both are asserted: the model refuses to link a
            # key to a superuser AT ALL, and the endpoint refuses the most
            # authority a key can carry regardless of whose identity it holds.
            from mojo.apps.account.models import ApiKey, Group

            ApiKey.objects.filter(name=KEY_NAME).delete()
            Group.objects.filter(name=KEY_GROUP).delete()
            key_group = Group.objects.create(name=KEY_GROUP, kind="organization")
            with th.assert_raises(merrors.PermissionDeniedException):
                ApiKey.create_for_group(
                    group=key_group, name=KEY_NAME,
                    permissions={"admin": True}, user=admin, override_user=True)
            assert not ApiKey.objects.filter(name=KEY_NAME).exists(), \
                "a refused superuser-linked API key was stored anyway"
            _api_key, key_token = ApiKey.create_for_group(
                group=key_group, name=KEY_NAME,
                permissions={"manage_settings": True, "admin": True})
            origin = opts.client.host.rstrip("/")
            before = assistant_setup.state()["mcp"]["grant_count"]
            for payload in ({"action": "revoke_grant", "grant_id": grant_three.pk},
                            {"action": "revoke_all_grants"}):
                resp = opts.client.post(
                    "/api/account/admin/assistant", json=payload,
                    headers={"Authorization": f"apikey {key_token}",
                             "Origin": origin})
                assert resp.status_code != 200, \
                    f"a key-backed session reached {payload['action']}: " \
                    f"{resp.status_code} {resp.body}"
            assert assistant_setup.state()["mcp"]["grant_count"] == before, \
                "a key-backed session disconnected an agent"

            # --- over the wire -------------------------------------------
            assert opts.client.login(ADMIN_EMAIL, ADMIN_PASSWORD), \
                "assistant admin login failed"
            origin = opts.client.host.rstrip("/")
            response = opts.client.post(
                "/api/account/admin/assistant",
                json={"action": "revoke_grant", "grant_id": grant_two.pk},
                headers={"Origin": origin})
            assert response.status_code == 200, \
                f"the owner could not disconnect an agent: " \
                f"{response.status_code} {response.body}"
            data = response.json["data"]
            assert data["revoked"] == 1 and data["state"]["mcp"]["grant_count"] == 2, \
                f"the revoke answer or its fresh state is wrong: {data!r}"
            response = opts.client.post(
                "/api/account/admin/assistant",
                json={"action": "revoke_all_grants"}, headers={"Origin": origin})
            opts.client.logout()
            assert response.status_code == 200, \
                f"the owner could not disconnect every agent: " \
                f"{response.status_code} {response.body}"
            assert response.json["data"]["revoked"] == 2, \
                f"disconnect-all did not report the remaining connections at " \
                f"BOTH resources: {response.json['data']!r}"

            assert oauth_server.list_grants(
                resource_path=[MCP_PATH, API_ROOT_PATH]) == [], \
                "a live grant survived disconnect-all"
            resp = opts.client.get(
                "/api/account/user/me", headers=_auth(api_pair["access_token"]))
            assert resp.status_code == 401, \
                f"a full-API token survived disconnect-all: " \
                f"{resp.status_code} {resp.body}"
            surviving = oauth_server.list_grants(resource_path=OTHER_PATH)
            assert len(surviving) == 1 and surviving[0]["id"] == other_grant.pk, \
                f"disconnect-all swept a grant at another resource path: " \
                f"{surviving!r}"
            for token in (pair_two["access_token"], pair_three["access_token"]):
                resp = opts.client.post(
                    MCP_PATH, _rpc(1, "ping"), headers=_auth(token))
                assert resp.status_code == 401, \
                    f"a token survived disconnect-all: " \
                    f"{resp.status_code} {resp.body}"
            assert grant_three.pk and grant_two.pk, "grants must have ids"
    finally:
        _wipe()
        _clear_base_url()


@th.django_unit_test("the discovery self-check tells the truth, caches it, and never runs on a page load")
def test_discovery_check_verdicts(opts):
    import requests

    from mojo.apps.account.services import assistant_setup
    from mojo.helpers.redis import get_connection

    _wipe()
    try:
        messages = assistant_setup.DISCOVERY_MESSAGES

        # --- switched off: a local verdict, never a request, never cached ---
        verdict = assistant_setup.check_discovery(
            origin=BASE, transport=_ExplodingTransport())
        assert verdict["code"] == "disabled" and \
            verdict["detail"] == messages["switched_off"], \
            f"a switched-off installation did not say so: {verdict!r}"
        assert assistant_setup.discovery_cached()["ok"] is None, \
            "a local verdict was cached"

        assistant_setup.save(_admin(opts), enabled=False, model="", mcp_enabled=True)

        # --- no public address ------------------------------------------
        verdict = assistant_setup.check_discovery(
            origin="", transport=_ExplodingTransport())
        assert verdict["code"] == "disabled" and \
            verdict["detail"] == messages["no_address"], \
            f"an installation with no public address did not say so: {verdict!r}"

        # --- reachable ---------------------------------------------------
        transport = _Transport({PRM_URL: _response(
            200, {"Content-Type": "application/json"}, _prm_body())})
        verdict = assistant_setup.check_discovery(
            origin=BASE, transport=transport,
            resolver=lambda host: ["10.0.0.5"])
        assert verdict["ok"] is True and verdict["code"] == "ok", \
            f"a served discovery document was not accepted: {verdict!r}"
        assert verdict["checked_at"], "an ok verdict carries no timestamp"
        assert transport.urls == [PRM_URL], \
            f"the self-check asked for something other than this installation's " \
            f"own PRM document: {transport.urls!r}"
        assert transport.calls[0][1].get("allow_redirects") is False, \
            f"the self-check followed redirects: {transport.calls[0][1]!r}"

        # The resolver said 10.0.0.5 and the fetch still happened: allow_hosts
        # exempts the INITIAL url, which is the split-horizon self-probe case.
        cached = assistant_setup.check_discovery(
            origin=BASE, transport=_ExplodingTransport())
        assert cached["ok"] is True and cached["code"] == "ok", \
            f"the 60s cache did not serve the network verdict: {cached!r}"

        # A verdict for a DIFFERENT address is never shown beside this one.
        other = assistant_setup.check_discovery(
            origin=OTHER_BASE,
            transport=_Transport({
                OTHER_BASE + PRM_PATH: _response(404, {}, b"nope")}))
        assert other["code"] == "unreachable" and "404" in other["detail"], \
            f"a stale cache was served for a different public address: {other!r}"

        # --- HTTP status --------------------------------------------------
        get_connection().delete(DISCOVERY_CACHE_KEY)
        verdict = assistant_setup.check_discovery(
            origin=BASE, transport=_Transport({PRM_URL: _response(404, {}, b"")}))
        assert verdict["code"] == "unreachable" and \
            verdict["detail"] == messages["status"].format(status=404), \
            f"a 404 front door was not reported as unforwarded: {verdict!r}"

        # --- a 200 that is not the document -------------------------------
        get_connection().delete(DISCOVERY_CACHE_KEY)
        verdict = assistant_setup.check_discovery(
            origin=BASE, transport=_Transport({PRM_URL: _response(
                200, {"Content-Type": "text/html"}, b"<!doctype html><title>app")}))
        assert verdict["detail"] == messages["wrong_document"], \
            f"an SPA index served with 200 read as success: {verdict!r}"

        get_connection().delete(DISCOVERY_CACHE_KEY)
        verdict = assistant_setup.check_discovery(
            origin=BASE, transport=_Transport({PRM_URL: _response(
                200, {"Content-Type": "application/json"},
                _prm_body(OTHER_BASE + MCP_PATH))}))
        assert verdict["detail"] == messages["wrong_document"], \
            f"another installation's document read as success: {verdict!r}"

        # --- redirects, whichever check the hop trips ----------------------
        for location, resolver in (
                (BASE + "/elsewhere", lambda host: ["93.184.216.34"]),
                ("https://10.0.0.9/", lambda host: ["93.184.216.34"])):
            get_connection().delete(DISCOVERY_CACHE_KEY)
            transport = _Transport({PRM_URL: _response(
                302, {"Location": location}, b"")})
            verdict = assistant_setup.check_discovery(
                origin=BASE, transport=transport, resolver=resolver)
            assert verdict["detail"] == messages["redirected"], \
                f"a 302 to {location} was not reported as redirected: {verdict!r}"
            assert len(transport.calls) == 1, \
                f"the self-check followed the hop to {location}: {transport.urls!r}"

        # --- the transport simply failing ----------------------------------
        get_connection().delete(DISCOVERY_CACHE_KEY)
        verdict = assistant_setup.check_discovery(
            origin=BASE, transport=_Transport(
                {PRM_URL: requests.exceptions.ConnectionError("down")}))
        assert verdict["code"] == "unreachable" and \
            verdict["detail"] == messages["fetch"], \
            f"an unreachable front door was not reported: {verdict!r}"
        # The fixed sentence is the point: safe_fetch's own failure strings name
        # the host they could not reach, and no detail may ever carry one.
        assert "{" not in messages["fetch"], \
            f"the fetch verdict still interpolates: {messages['fetch']!r}"
        assert "oauth.testit.example" not in verdict["detail"], \
            f"the fetch verdict named the host it could not reach: {verdict!r}"

        # --- a page load never probes --------------------------------------
        get_connection().delete(DISCOVERY_CACHE_KEY)

        def _never(*args, **kwargs):
            raise AssertionError("a plain state() read reached the network")

        with mock.patch.object(assistant_setup, "safe_fetch", _never):
            state = assistant_setup.state()
        assert state["mcp"]["discovery"] == assistant_setup.UNCHECKED, \
            f"a page load did not fall back to the unchecked record: " \
            f"{state['mcp']['discovery']!r}"

        # --- switching off drops the verdict and hides any stale one -------
        assistant_setup.check_discovery(origin=BASE, transport=_Transport(
            {PRM_URL: _response(200, {"Content-Type": "application/json"},
                                _prm_body())}))
        assert assistant_setup.discovery_cached()["ok"] is True, \
            "the network verdict was not cached"
        assistant_setup.save(_admin(opts), enabled=False, model="", mcp_enabled=False)
        assert get_connection().get(DISCOVERY_CACHE_KEY) is None, \
            "switching remote access off left its discovery verdict cached"
        assert assistant_setup.state()["mcp"]["discovery"] == assistant_setup.UNCHECKED, \
            "a switched-off installation still shows a discovery verdict"

        # --- over the wire, owner only -------------------------------------
        assert opts.client.login(ADMIN_EMAIL, ADMIN_PASSWORD), \
            "assistant admin login failed"
        response = opts.client.get("/api/account/admin/assistant?check=discovery")
        opts.client.logout()
        assert response.status_code == 200, \
            f"the owner could not run the self-check: " \
            f"{response.status_code} {response.body}"
        assert response.json["data"]["mcp"]["discovery"]["code"] == "disabled", \
            f"the self-check reported something other than switched off: " \
            f"{response.json['data']['mcp']['discovery']!r}"
    finally:
        _wipe()
