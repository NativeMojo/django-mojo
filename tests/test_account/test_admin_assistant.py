"""Static contracts for the built-in Admin Assistant.

Read-only: nothing here writes a Setting row, patches a shared production
object, or touches installation-wide configuration. The writer matrix (secret
storage, precedence, the enable/disable cache flip, the 440) lives in
tests/test_account_admin_extended_serial/test_assistant_setup.py.
"""

import re
from pathlib import Path

from testit import helpers as th

TESTIT_TIER = "admin"


ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "mojo/apps/account/admin_portal/assets"
PANEL = ASSETS / "assistant"

ASSISTANT_ASSETS = (
    "panel.js", "transport.js", "conversation.js", "blocks.js", "markdown.js",
    "plan.js", "approval.js", "setup.js", "assistant.css",
)


def _modules():
    return sorted(PANEL.glob("*.js"))


def _code(text):
    """The source with whole-line // comments dropped.

    A negative assertion has to be about what the module DOES; the comment
    explaining why it does not do a thing names that thing.
    """
    return "\n".join(line for line in text.splitlines()
                     if not line.strip().startswith("//"))


CONTRACT_ADMIN = "assistant-contract-admin@test.com"
CONTRACT_REGULAR = "assistant-contract-regular@test.com"
CONTRACT_PASSWORD = "Assistant_contract_99"


@th.django_unit_setup()
def setup_assistant_contract(opts):
    """Two test-owned identities. No Setting row and no shared object is touched."""
    from mojo.apps.account.models import User

    User.objects.filter(email__in=[CONTRACT_ADMIN, CONTRACT_REGULAR]).delete()
    admin = User.objects.create_user(
        username=CONTRACT_ADMIN, email=CONTRACT_ADMIN, password=CONTRACT_PASSWORD)
    admin.is_active = True
    admin.is_email_verified = True
    admin.requires_mfa = False
    admin.is_superuser = True
    admin.save()
    regular = User.objects.create_user(
        username=CONTRACT_REGULAR, email=CONTRACT_REGULAR,
        password=CONTRACT_PASSWORD)
    regular.is_active = True
    regular.is_email_verified = True
    regular.requires_mfa = False
    regular.permissions = {"manage_settings": True}
    regular.save()


# ---------------------------------------------------------------------------
# Bootstrap capability and feature descriptor
# ---------------------------------------------------------------------------

@th.django_unit_test("the Assistant namespace fails closed and follows authority alone")
def test_assistant_feature_provider_contract(opts):
    from mojo.apps.account.services import admin_features

    assert "assistant" in admin_features.FEATURE_NAMES, \
        "the assistant feature namespace is not in the fixed roster"

    # The provider must not read `request`: bootstrap_features(None, {}) is a
    # real call path (tests/test_account/test_admin_portal.py).
    closed = admin_features.bootstrap_features(None, {})["assistant"]
    assert closed["enabled"] is False and closed["capabilities"]["view"] is False, \
        f"an empty capability set produced an enabled Assistant: {closed!r}"

    granted = admin_features.bootstrap_features(None, {
        "assistant": True, "assistant_ready": True, "assistant_setup": True,
        "assistant_mcp": True,
    })["assistant"]
    assert granted["enabled"] is True and granted["capabilities"] == {
        "view": True, "ready": True, "setup": True, "mcp": True}, \
        f"a fully granted caller did not get the Assistant panel: {granted!r}"

    # `ready` is installation state, not authority. Folding it into `enabled`
    # would mount a panel whose every message the WebSocket handler refuses.
    ready_only = admin_features.bootstrap_features(None, {
        "assistant": False, "assistant_ready": True, "assistant_setup": True,
    })["assistant"]
    assert ready_only["enabled"] is False, \
        f"installation readiness enabled the panel without authority: {ready_only!r}"

    # `mcp` is the same kind of fact: remote agent access being reachable is
    # not a reason to mount a panel for a caller with no authority.
    mcp_only = admin_features.bootstrap_features(None, {
        "assistant": False, "assistant_mcp": True,
    })["assistant"]
    assert mcp_only["enabled"] is False and mcp_only["capabilities"]["mcp"] is True, \
        f"remote agent access enabled the panel without authority: {mcp_only!r}"


@th.django_unit_test("the Admin bootstrap gates the Assistant on view_admin alone")
def test_assistant_bootstrap_capabilities(opts):
    source = (ROOT / "mojo/apps/account/rest/admin_portal.py").read_text()
    assert '"assistant": has(["view_admin"])' in source, \
        "the Assistant launcher is not gated on view_admin alone — the " \
        "WebSocket handler admits nothing else"
    assert '"assistant_ready": assistant_setup.is_ready()' in source, \
        "readiness is not read from the setup service"
    assert '"assistant_setup": bool(request.user.is_superuser)' in source, \
        "the setup capability is not the literal-superuser predicate the writer enforces"
    assert '"assistant_mcp": assistant_setup.mcp_ready()' in source, \
        "remote agent access readiness is not read from the setup service"


# ---------------------------------------------------------------------------
# REST boundary
# ---------------------------------------------------------------------------

@th.django_unit_test("Assistant setup is owner-tier, fresh, human, and same-Origin")
def test_admin_assistant_rest_decorators(opts):
    from mojo.decorators.auth import SECURITY_REGISTRY
    from mojo.apps.account.rest import admin_assistant as views

    for func in (views.on_admin_assistant, views.on_admin_assistant_mutate):
        assert getattr(func, "_mojo_denies_key_backed_session", False), \
            f"{func.__name__} accepts a key-backed session"
        assert getattr(func, "_mojo_requires_perms", False), \
            f"{func.__name__} carries no permission requirement"
        entry = SECURITY_REGISTRY.get(f"{func.__module__}.{func.__name__}", {})
        assert entry.get("global_only") is True, \
            f"{func.__name__} does not require GLOBAL permissions: {entry!r}"

    mutate = views.on_admin_assistant_mutate
    assert getattr(mutate, "_mojo_requires_fresh_auth", False), \
        "the Assistant setup writer lacks recent interactive authentication"
    assert getattr(mutate, "_mojo_fresh_auth_seconds", None) == 600, \
        "the Assistant recent-authentication window is not 600 seconds"

    source = (ROOT / "mojo/apps/account/rest/admin_assistant.py").read_text()
    assert source.count("system_setup.require_request_admin(request)") == 2, \
        "both Assistant setup endpoints must prove an interactive live superuser"
    assert "system_setup.request_origin(request)" in source, \
        "the Assistant setup writer lacks the same-Origin gate"
    assert "request.POST" not in source and "request.GET" not in source, \
        "the Assistant setup endpoints read input from something other than request.DATA"


# ---------------------------------------------------------------------------
# Remote agent access (MCP)
# ---------------------------------------------------------------------------

@th.django_unit_test("the remote agent access state has a fixed, credential-free shape")
def test_mcp_state_shape(opts):
    from mojo.apps.account.services import assistant_setup

    state = assistant_setup.mcp_state()
    assert set(state) == {"enabled", "path", "url", "discovery_url", "discovery",
                          "grants", "grant_count"}, \
        f"the remote agent access state drifted: {sorted(state)}"
    assert set(state["discovery"]) == {"ok", "code", "detail", "checked_at"}, \
        f"the discovery record drifted — `resource` is bookkeeping and must not " \
        f"reach the wire: {state['discovery']!r}"
    assert state["path"] == "/api/assistant/mcp", \
        f"the connect path is not the registered resource path: {state['path']!r}"
    assert isinstance(state["enabled"], bool), \
        f"the switch is not reported as a boolean: {state['enabled']!r}"
    assert len(state["grants"]) <= assistant_setup.MAX_GRANT_ROWS, \
        f"the grant list is not bounded: {len(state['grants'])} rows"
    if state["grant_count"] <= assistant_setup.MAX_GRANT_ROWS:
        assert state["grant_count"] == len(state["grants"]), \
            f"the grant count disagrees with the listed rows: " \
            f"{state['grant_count']} vs {len(state['grants'])}"

    # A plain read must never reach the network, and Redis being unavailable is
    # simply "not checked yet".
    cached = assistant_setup.discovery_cached()
    assert set(cached) == {"ok", "code", "detail", "checked_at"}, \
        f"the cached discovery record drifted: {cached!r}"
    assert isinstance(assistant_setup.mcp_ready(), bool), \
        "mcp_ready() is not a boolean the bootstrap can publish"


@th.django_unit_test("the MCP contract refuses every malformed body and every non-owner")
def test_mcp_rest_contract(opts):
    from mojo.apps.account.models import Setting

    assert opts.client.login(CONTRACT_ADMIN, CONTRACT_PASSWORD), \
        "the contract superuser could not sign in"
    origin = opts.client.host.rstrip("/")
    # Relative, never absolute: a sibling serial module legitimately owns a row
    # for this key, and "no row exists anywhere" would flake on its state.
    before = Setting.objects.filter(key="ASSISTANT_MCP_ENABLED").count()
    refused = (
        # A present non-boolean is a 400, never coerced.
        ({"action": "save", "enabled": False, "model": "", "mcp_enabled": "yes"},
         "a string mcp_enabled"),
        ({"action": "revoke_grant"}, "a revoke with no grant_id"),
        ({"action": "revoke_grant", "grant_id": "1"}, "a string grant_id"),
        ({"action": "revoke_grant", "grant_id": 1, "extra": 1},
         "a revoke carrying an extra field"),
        ({"action": "revoke_all_grants", "extra": 1},
         "a revoke-all carrying an extra field"),
        ({"action": "nope"}, "an unknown action"),
    )
    for payload, why in refused:
        response = opts.client.post(
            "/api/account/admin/assistant", json=payload,
            headers={"Origin": origin})
        assert response.status_code == 400, \
            f"{why} was not refused: {response.status_code} {response.body}"
    opts.client.logout()
    assert Setting.objects.filter(key="ASSISTANT_MCP_ENABLED").count() == before, \
        "a refused body still wrote the remote agent access switch"

    assert opts.client.login(CONTRACT_REGULAR, CONTRACT_PASSWORD), \
        "the contract manage_settings holder could not sign in"
    origin = opts.client.host.rstrip("/")
    probe = opts.client.get("/api/account/admin/assistant?check=discovery")
    assert probe.status_code in (403, 404), \
        f"a manage_settings holder ran the discovery self-check: " \
        f"{probe.status_code} {probe.body}"
    for payload in ({"action": "revoke_grant", "grant_id": 1},
                    {"action": "revoke_all_grants"}):
        response = opts.client.post(
            "/api/account/admin/assistant", json=payload,
            headers={"Origin": origin})
        assert response.status_code in (403, 404, 440), \
            f"a manage_settings holder reached {payload['action']}: " \
            f"{response.status_code} {response.body}"
    opts.client.logout()


# ---------------------------------------------------------------------------
# Key protection
# ---------------------------------------------------------------------------

@th.django_unit_test("every Assistant key is catalog-protected from the generic writers")
def test_assistant_keys_are_catalog_protected(opts):
    from mojo import errors as merrors
    from mojo.apps.account.models import Setting
    from mojo.apps.account.services import admin_settings

    for key in ("LLM_ADMIN_ENABLED", "LLM_ADMIN_API_KEY", "LLM_ADMIN_MODEL",
                "LLM_ADMIN_VERIFY_STATE", "LLM_HANDLER_API_KEY",
                "LLM_HANDLER_VERIFY_STATE", "ASSISTANT_MCP_ENABLED"):
        assert admin_settings.is_catalog_protected(key), \
            f"{key} can still be written through the generic settings API"

    # The remote-agent door's switch is settable from the Admin, so its one
    # dedicated writer needs the escape — and nothing else may write it: a
    # global row outranks the deployment file on every node.
    assert "ASSISTANT_MCP_ENABLED" in admin_settings.ASSISTANT_WRITABLE_KEYS, \
        "the MCP switch has no owner editor, so the Admin cannot store it"
    assert "ASSISTANT_MCP_ENABLED" in admin_settings.PROTECTED_WRITER_KEYS, \
        "the MCP switch has no dedicated-writer escape, so its own writer is blocked"
    with th.assert_raises(merrors.PermissionDeniedException):
        Setting.set("ASSISTANT_MCP_ENABLED", True)

    # The one dedicated writer names the key it is saving, and the row must
    # carry that same key — so a writer cannot smuggle a different one past it.
    assert admin_settings.ASSISTANT_WRITABLE_KEYS <= admin_settings.PROTECTED_WRITER_KEYS, \
        "the assistant keys have no dedicated-writer escape, so their own writer is blocked"
    assert "GEOIP_API_KEY_MOJO" in admin_settings.PROTECTED_WRITER_KEYS, \
        "the pre-existing provider-setup escape was dropped"
    # The platform key is settable from the Admin, so its one dedicated writer
    # must have the escape too — and nothing else may write it.
    assert "LLM_HANDLER_API_KEY" in admin_settings.PROTECTED_WRITER_KEYS, \
        "the platform LLM key has no dedicated-writer escape, so the Admin cannot store it"
    assert "LLM_HANDLER_VERIFY_STATE" in admin_settings.PROTECTED_WRITER_KEYS, \
        "the platform key's verification record has no dedicated writer"
    with th.assert_raises(merrors.PermissionDeniedException):
        Setting.set("LLM_HANDLER_API_KEY", "sk-should-never-store-either")

    with th.assert_raises(merrors.PermissionDeniedException):
        Setting.set("LLM_ADMIN_API_KEY", "sk-should-never-store")

    model = (ROOT / "mojo/apps/account/models/setting.py").read_text()
    assert "protected_writer != self.key" in model, \
        "the protected-writer escape no longer requires the writer to name this row's key"


@th.django_unit_test("the Assistant setup body never reaches the generic request logs")
def test_assistant_request_redaction(opts):
    from unittest import mock

    from mojo.helpers import request as request_helpers

    # The save and verify bodies carry an Anthropic API key. Without a label
    # here, LOGIT_DB_ALL / LOGIT_FILE_ALL write the raw body into the logit.Log
    # table -- readable at manage_logs / view_logs / security / admin, well below
    # the superuser tier that is allowed to set the key -- and into requests.log.
    request = mock.Mock(path="/api/account/admin/assistant", method="POST")
    assert request_helpers.sensitive_body_label(request) == "assistant_setup", \
        "the Assistant setup body can enter generic request logs in plaintext"
    trailing = mock.Mock(path="/api/account/admin/assistant/", method="POST")
    assert request_helpers.sensitive_body_label(trailing) == "assistant_setup", \
        "a trailing slash escapes the sensitive-body classification"


@th.django_unit_test("the picker never goes empty when the model catalogue is unavailable")
def test_model_choices_fallback(opts):
    from mojo.helpers import llm

    # A locally injected loader: nothing shared is patched, so this is safe in
    # the parallel default tier.
    empty = llm.model_choices(loader=lambda: None)
    assert len(empty) == 3, f"an unavailable catalogue produced {empty!r}"
    assert {row["id"] for row in empty} == set(llm._FALLBACKS.values()), \
        f"the fallback picker does not offer the three resolution aliases: {empty!r}"

    broken = llm.model_choices(loader=lambda: (_ for _ in ()).throw(RuntimeError("down")))
    assert len(broken) == 3, f"a raising loader did not fall back: {broken!r}"

    live = llm.model_choices(loader=lambda: [
        {"id": "claude-sonnet-5", "display_name": "Claude Sonnet 5",
         "created_at": "2026-01-01T00:00:00Z"},
        {"id": "claude-opus-4-8", "created_at": "2025-01-01T00:00:00Z"},
        {"id": "text-embedding-3", "created_at": "2026-06-01T00:00:00Z"},
    ])
    assert [row["id"] for row in live] == ["claude-sonnet-5", "claude-opus-4-8"], \
        f"the picker did not rank by recency or dropped a family filter: {live!r}"
    assert live[0]["label"] == "Claude Sonnet 5" and live[1]["label"] == "claude-opus-4-8", \
        f"a missing display_name did not fall back to the id: {live!r}"


# ---------------------------------------------------------------------------
# Packaging
# ---------------------------------------------------------------------------

@th.django_unit_test("the Assistant ships as a shell slot, not a navigation lane")
def test_assistant_assets_are_declared(opts):
    from mojo.apps.account.services import admin_assets

    assets = admin_assets.load_manifest()
    for name in ASSISTANT_ASSETS:
        assert f"assets/assistant/{name}" in assets, \
            f"assets/assistant/{name} is not a declared package asset — it would 404"

    registry = (ASSETS / "features/registry.js").read_text()
    assert "assistant" not in registry, \
        "the Assistant was added to the navigation registry — it is a shell slot " \
        "with no route and no lane"
    assert "[dashboard, webapps, advanced, people, activity, platform, settings, sms, email]" in registry, \
        "the approved navigation lane order changed"
    assert "assistant" not in admin_assets.FEATURES, \
        "the Assistant was added to the feature asset roster — those are lane directories"

    index = (ROOT / "mojo/apps/account/admin_portal/index.html").read_text()
    assert '<link rel="stylesheet" href="assets/assistant/assistant.css">' in index, \
        "the Assistant stylesheet has no load path"

    app = (ASSETS / "app.js").read_text()
    assert "context.features?.assistant?.enabled === true" in app, \
        "the shell mounts the Assistant without checking its feature namespace"
    assert "disposeAssistant" in app, \
        "the panel's disposer shares the page disposer, which every navigation nulls"


# ---------------------------------------------------------------------------
# Static JavaScript contracts
# ---------------------------------------------------------------------------

@th.django_unit_test("no Assistant module hands model output to innerHTML")
def test_assistant_modules_never_use_innerhtml(opts):
    modules = _modules()
    assert len(modules) == 8, \
        f"the Assistant module walk found {len(modules)} files under {PANEL}"
    for path in modules:
        assert "innerHTML" not in _code(path.read_text()), \
            f"{path.name} writes innerHTML — block and markdown content is model output"


@th.django_unit_test("Assistant controls route through the one shared action helper")
def test_assistant_modules_follow_the_responsiveness_contract(opts):
    banned = ("onclick: async", "onchange: async", "onsubmit: async") + tuple(
        f"addEventListener('{event}', async"
        for event in ("click", "change", "submit", "keydown", "input"))
    for path in _modules():
        text = path.read_text()
        code = _code(text)
        for pattern in banned:
            assert pattern not in code, \
                f"{path.name} attaches a raw async handler ({pattern})"
        assert "function runAction" not in code, \
            f"{path.name} defines its own runAction instead of using the shared helper"
        if "runAction(" in code:
            assert re.search(r"import \{[^}]*\brunAction\b[^}]*\} from '[^']*actions\.js'", text), \
                f"{path.name} calls runAction() without importing it from components/actions.js"


@th.django_unit_test("the transport owns one turn, one outcome, and no stop button")
def test_assistant_transport_contract(opts):
    transport = (PANEL / "transport.js").read_text()
    code = _code(transport)

    assert "if (pendingTurn) throw new Error" in code, \
        "the transport does not refuse a second turn while one is pending"
    assert "pendingTurn = null" in code and "assistant_response" in code \
        and "assistant_error" in code, \
        "the terminal event pair does not resolve the pending turn"
    assert "BACKOFF_MS = [1000, 2000, 4000, 8000, 16000, 30000]" in code, \
        "the reconnect backoff ladder changed"
    assert "RATE_LIMITED_CODE = 4429" in code and "code === RATE_LIMITED_CODE" in code, \
        "a 4429 close is not treated as a deliberate rejection"
    assert "PING_MS = 12000" in code and "action: 'ping'" in code, \
        "the keep-alive is gone — the server closes an idle socket after 30 seconds"
    assert "MISSED_PONG_LIMIT" in code, \
        "missing pongs no longer trigger a reconnect"
    assert "TURN_WATCHDOG_MS = 240000" in code, \
        "the silent-turn watchdog changed"
    assert not re.search(r"cancel", code, re.IGNORECASE), \
        "the transport gained a cancel path — the server exposes no way to abort " \
        "a turn, so no control here may claim to"
    assert "APPROVAL_TIMEOUT_MS = 60000" in code and "approval_timeout" in code, \
        "an approval decision waits forever: a socket that drops between send " \
        "and result would leave both card controls disabled for good"
    assert "clearTimeout(entry.timer)" in code, \
        "the bounded approval wait is never cleared, so a settled decision leaks its timer"
    assert "isOwned(requestId)" in code, \
        "inbound events are not filtered by a request_id this transport minted; " \
        "send_event_to_user fans out to every socket the user holds"


@th.django_unit_test("markdown renders no links, images, or raw HTML")
def test_assistant_markdown_contract(opts):
    markdown = _code((PANEL / "markdown.js").read_text())
    for banned in ("createElement('a')", "createElement('img')", 'createElement("a")',
                   "setAttribute('href'", "setAttribute('src'"):
        assert banned not in markdown, \
            f"markdown.js builds {banned} — assistant prose must not become a click target"
    assert "MAX_INPUT = 100000" in markdown and "MAX_INLINE_LINE = 4000" in markdown, \
        "the markdown input bounds are gone"
    assert "textContent" in markdown and "createElement" in markdown, \
        "markdown.js no longer builds nodes explicitly"


@th.django_unit_test("a model-emitted file URL never becomes a link to another host")
def test_assistant_file_block_is_same_origin_only(opts):
    blocks = _code((PANEL / "blocks.js").read_text())

    # `file` is in the server's VALID_BLOCK_TYPES and _validate_block only
    # checks that filename and url are truthy, so the model can emit any URL it
    # read out of a tool result. In a superuser console that must not be a
    # click target.
    assert "url.origin !== location.origin" in blocks, \
        "renderFile no longer restricts the anchor to this installation's own origin"
    assert "url.protocol !== 'https:'" in blocks and "url.username || url.password" in blocks, \
        "the file URL check dropped its scheme or embedded-credential guard"
    anchor = blocks.split("function renderFile", 1)[1].split("function renderContext", 1)[0]
    assert anchor.count("h('a'") == 1, \
        f"renderFile builds more than one anchor: {anchor.count(chr(104) + chr(40) + chr(39) + 'a' + chr(39))}"
    assert "rel: 'noopener noreferrer'" in anchor and "referrerpolicy: 'no-referrer'" in anchor, \
        "the same-origin anchor lost its rel/referrerpolicy hardening"
    # The foreign branch is text: the hostname is named, and no anchor is built
    # anywhere the safeDownload check returned null.
    assert "url ? h('a'" in anchor and "h('span', {text: `Download link (copy it by hand)" in anchor, \
        "a foreign-host file URL no longer degrades to copyable text"
    assert "foreignHost" in blocks, \
        "the inert branch no longer names the destination host"


@th.django_unit_test("the panel is a docked region, a narrow dialog, and never on top")
def test_assistant_panel_contract(opts):
    panel = (PANEL / "panel.js").read_text()
    styles = (PANEL / "assistant.css").read_text()

    assert "'role', 'complementary'" in panel, \
        "the docked panel is not a complementary region"
    assert "'aria-modal', 'true'" in panel and "'role', 'dialog'" in panel, \
        "the narrow sheet is not a modal dialog"
    assert "'aria-live': 'off'" in panel, \
        "the panel does not silence #app's polite live region — a screen reader " \
        "would narrate every streamed token"
    assert "matchMedia(DOCKED_QUERY)" in panel and "(min-width: 1101px)" in panel, \
        "the docked/sheet mode switch is gone"
    assert "mojo-admin-assistant-open" in panel, \
        "the open/closed state is no longer remembered"
    assert "sessionStorage" in panel and "conversation" not in panel.split("sessionStorage")[1][:200], \
        "something other than a single boolean is being persisted in the browser"

    assert "grid-column: 3" in styles, \
        "the panel does not claim the third grid column explicitly — the sidebar " \
        "is position:fixed, so auto-placement lands it in column 1"
    assert "#app.assistant-open { grid-template-columns: 228px minmax(0, 1fr) 380px; }" in styles, \
        "the docked layout no longer widens the shell grid"
    assert "z-index: 60;" in styles, \
        "the panel z-index left the band below the busy (90) and modal (100/110/120) scrims"
    assert "@media (prefers-reduced-motion: reduce)" in styles, \
        "the typing indicator ignores prefers-reduced-motion"


@th.django_unit_test("approvals are decided in one module and framed in one transport")
def test_assistant_approval_seam(opts):
    approval = (PANEL / "approval.js").read_text()
    blocks = _code((PANEL / "blocks.js").read_text())
    transport = _code((PANEL / "transport.js").read_text())

    # The socket frame is built by the ONE socket owner (that is the whole
    # point of a single correlation owner); the DECISION lives in approval.js
    # and nowhere else.
    for module in _modules():
        code = _code(module.read_text())
        if module.name == "transport.js":
            continue
        assert "type: 'assistant_approval'" not in code and "type: 'assistant_action'" not in code, \
            f"{module.name} frames an approval message itself instead of going " \
            f"through the single socket owner"
    assert "type: 'assistant_approval'" in transport and "type: 'assistant_action'" in transport, \
        "the socket owner no longer frames the approval and quick-reply messages"

    assert "renderApprovalBlock" in blocks and "renderActionBlock" in blocks, \
        "blocks.js no longer delegates the approval and quick-reply cards"
    assert "'/api/assistant/action'" in approval, \
        "approval.js cannot resolve a fresh-auth card over REST"
    assert "mojo-admin:fresh-auth" in approval, \
        "the WebSocket reauth_required handoff does not raise the shell's step-up"
    assert "reauth_required" in approval, \
        "approval.js does not handle the step-up refusal"
    assert "state === LIVE" in approval or "block?.state === LIVE" in approval, \
        "the card is actionable in states other than pending"
    for module in _modules():
        if module.name == "approval.js":
            continue
        assert "/api/assistant/action" not in _code(module.read_text()), \
            f"{module.name} resolves approvals itself — that is approval.js's job"
