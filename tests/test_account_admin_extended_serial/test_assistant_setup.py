"""The Assistant setup writer: encryption, precedence, the cache flip, and 440.

Opt-in and serial: every test here writes installation-wide Setting rows and
patches the provider verification seam, which is unsafe under the parallel
default tier. The read-only decorator, catalog-protection and asset contracts
stay in tests/test_account/test_admin_assistant.py.
"""

import time
from unittest import mock

import ujson
from testit import helpers as th


ADMIN_EMAIL = "assistant-setup-admin@test.com"
ADMIN_PASSWORD = "Assistant_setup_Admin_99"
REGULAR_EMAIL = "assistant-setup-regular@test.com"
REGULAR_PASSWORD = "Assistant_setup_Regular_99"

KEYS = ("LLM_ADMIN_ENABLED", "LLM_ADMIN_API_KEY", "LLM_ADMIN_MODEL",
        "LLM_ADMIN_VERIFY_STATE", "LLM_HANDLER_API_KEY")

STORED_KEY = "sk-ant-assistant-setup-abcd1234"


def _wipe():
    """Delete every assistant row AND its Redis entry.

    Setting.resolve reads Redis first, so a row deleted through the queryset
    (which is how a protected key has to be removed) would otherwise stay live
    in the cache for the next test in this module.
    """
    from mojo.apps.account.models import Setting
    Setting.objects.filter(key__in=KEYS).delete()
    redis = Setting._redis()
    if redis:
        for key in KEYS:
            redis.hdel(Setting._redis_key(), key)


@th.django_unit_setup()
def setup_assistant_setup(opts):
    from mojo.apps.account.models import User

    _wipe()
    User.objects.filter(email__in=[ADMIN_EMAIL, REGULAR_EMAIL]).delete()
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
    opts.assistant_admin_id = admin.pk
    opts.assistant_regular_id = regular.pk


def _admin(opts):
    from mojo.apps.account.models import User
    return User.objects.get(pk=opts.assistant_admin_id)


def _accepts():
    """Patch the provider check so it accepts, without reaching Anthropic."""
    from mojo.helpers import llm
    return mock.patch.object(llm, "verify_api_key", lambda key=None: (True, None))


def _rejects():
    from mojo.helpers import llm
    return mock.patch.object(
        llm, "verify_api_key", lambda key=None: (False, "API key is invalid or expired."))


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
