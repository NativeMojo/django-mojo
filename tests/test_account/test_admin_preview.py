"""Live-target Admin preview and browser feedback contracts."""

import socket
import sys
from email.message import Message
from pathlib import Path
from unittest import mock

from testit import helpers as th


ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "mojo/apps/account/admin_portal/assets"


def _server():
    bin_root = str(ROOT / "bin")
    if bin_root not in sys.path:
        sys.path.insert(0, bin_root)
    from admin_preview_support import server
    return server


@th.django_unit_test("live preview accepts only one public HTTPS hostname origin")
def test_live_upstream_origin_contract(opts):
    server = _server()
    assert server.parse_upstream("https://api.mojoverify.com") == {
        "origin": "https://api.mojoverify.com", "hostname": "api.mojoverify.com",
        "port": 443,
    }, "a canonical public HTTPS origin was not accepted"
    refused = (
        "http://api.mojoverify.com", "https://user:pass@api.mojoverify.com",
        "https://api.mojoverify.com/path", "https://127.0.0.1",
        "https://[::ffff:127.0.0.1]", "https://localhost",
    )
    for value in refused:
        with th.assert_raises(server.PreviewProxyError):
            server.parse_upstream(value)


@th.django_unit_test("live preview rejects a DNS set containing any non-public answer")
def test_live_upstream_dns_contract(opts):
    server = _server()
    answers = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443)),
    ]
    with mock.patch.object(server.socket, "getaddrinfo", return_value=answers):
        with th.assert_raises(server.PreviewProxyError):
            server.resolve_public_addresses("api.mojoverify.com")
    public = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
    with mock.patch.object(server.socket, "getaddrinfo", return_value=public):
        assert server.resolve_public_addresses("api.mojoverify.com") == ["93.184.216.34"], \
            "a fully public DNS answer set was not accepted"


@th.django_unit_test("live preview rewrites only redirects back to its fixed upstream")
def test_live_location_rewrite_contract(opts):
    server = _server()
    local = "http://localhost:8766"
    upstream = "https://api.mojoverify.com"
    assert server.rewrite_location(
        "https://api.mojoverify.com/auth?step=1", upstream, local,
    ) == "http://localhost:8766/auth?step=1", "same-upstream navigation did not stay inside the preview"
    external = "https://accounts.google.com/o/oauth2/auth"
    assert server.rewrite_location(external, upstream, local) == external, \
        "an external OAuth redirect was incorrectly rewritten through localhost"


@th.django_unit_test("live preview has a pinned request gate and no browser cookie relay")
def test_live_proxy_source_contract(opts):
    source = (ROOT / "bin/admin_preview_support/server.py").read_text()
    for value in (
            "preview_sessions", "PREVIEW_SESSION_CAP", "Sec-Fetch-Site",
            "Preview mutations require a same-origin",
            "resolve_public_addresses", "PinnedHTTPSConnection", "server_hostname=self.host",
            "getpeername", "MAX_REQUEST_BODY", "MAX_RESPONSE_BODY", "response.getheaders()"):
        assert value in source, f"live preview omitted the {value} boundary"
    assert 'response_headers["Set-Cookie"]' not in source, \
        "an upstream Set-Cookie can escape into the localhost browser namespace"
    assert "--upstream" in source and "Password authentication is supported" in source, \
        "the live QA command or its authentication boundary is missing"


@th.django_unit_test("live preview route allowlist is canonical and boundary-aware")
def test_live_proxy_route_gate(opts):
    server = _server()
    handler = object.__new__(server.PreviewHandler)
    for path in ("/auth", "/auth/step", "/register", "/passkey/start", "/api/user", "/static/app.js"):
        assert handler._proxy_allowed(path), f"required live-preview route was refused: {path}"
    for path in ("/authentication", "/registering", "/passkeys", "/api/%2e%2e/auth",
                 "/auth/../api", "//api/user", "/api\\user"):
        assert not handler._proxy_allowed(path), f"non-canonical live-preview route was accepted: {path}"


@th.django_unit_test("live preview sessions and upstream cookies stay browser-isolated")
def test_live_proxy_session_cookie_isolation(opts):
    server = _server()
    server.PreviewHandler.preview_port = 8766
    server.PreviewHandler.upstream = {
        "origin": "https://api.mojoverify.com",
        "hostname": "api.mojoverify.com",
        "port": 443,
    }
    now = server.time.time()
    server.PreviewHandler.preview_sessions = {
        "browser-a": {"seen": now, "cookies": {}},
        "browser-b": {"seen": now, "cookies": {}},
    }

    handler = object.__new__(server.PreviewHandler)
    handler.command = "GET"
    handler.path = "/auth/continue"
    handler.headers = Message()
    handler.headers["Host"] = "localhost:8766"
    handler.headers["Cookie"] = f"{server.PREVIEW_COOKIE}=browser-a"
    handler.headers["Sec-Fetch-Site"] = "same-origin"
    handler._proxy_gate()
    assert handler.preview_session_token == "browser-a", \
        "the validated browser session was not bound to its request"

    response = mock.Mock()
    response.getheaders.return_value = [
        ("Set-Cookie", "session=upstream-a; Path=/auth; Max-Age=3600; Secure; HttpOnly"),
    ]
    handler._remember_upstream_cookies(response)
    assert handler._cookies_for_request("/auth/step") == {"session": "upstream-a"}, \
        "a matching scoped upstream cookie was not retained server-side"
    assert handler._cookies_for_request("/api/user") == {}, \
        "an upstream cookie escaped its declared Path scope"

    other = object.__new__(server.PreviewHandler)
    other.preview_session_token = "browser-b"
    assert other._cookies_for_request("/auth/step") == {}, \
        "one preview browser received another browser's upstream session cookie"


@th.django_unit_test("live preview suggests its validated upstream as Setup BASE_URL")
def test_live_preview_base_url_suggestion(opts):
    server = _server()
    server.PreviewHandler.preview_port = 8766
    server.PreviewHandler.upstream = {
        "origin": "https://api.mojoverify.com",
        "hostname": "api.mojoverify.com",
        "port": 443,
    }
    server.PreviewHandler.preview_sessions = {
        "browser-a": {"seen": server.time.time(), "cookies": {}},
    }
    handler = object.__new__(server.PreviewHandler)
    handler.command = "GET"
    handler.path = "/__preview__/context"
    handler.headers = Message()
    handler.headers["Host"] = "localhost:8766"
    handler.headers["Cookie"] = f"{server.PREVIEW_COOKIE}=browser-a"
    handler.headers["Sec-Fetch-Site"] = "same-origin"
    handler._send = mock.Mock()

    handler._serve_preview_context()

    handler._send.assert_called_once_with({
        "schema_version": 1,
        "suggested_base_url": "https://api.mojoverify.com",
    })
    platform = (ASSETS / "features/platform/page.js").read_text()
    assert "/__preview__/context" in platform and "suggestedBaseUrl" in platform, \
        "System Setup does not discover the live preview's validated API origin"
    assert "suggestions[name]" in platform, \
        "the detected public API origin is not offered in the Setup choice field"


@th.django_unit_test("Admin 440 and busy states are explicit and finally-safe")
def test_admin_feedback_contract(opts):
    core = (ASSETS / "core.js").read_text()
    app = (ASSETS / "app.js").read_text()
    overlays = (ASSETS / "components/overlays.js").read_text()
    views = (ASSETS / "components/views.js").read_text()
    platform = (ASSETS / "features/platform/page.js").read_text()
    styles = (ASSETS / "admin.css").read_text()
    assert "location.assign(`/auth?redirect=${next}" not in core, \
        "HTTP 440 still looks like an automatic logout"
    assert "FreshAuthRequired" in core and "mojo-admin:fresh-auth" in core, \
        "HTTP 440 is not a typed browser event"
    assert "Your session is still active" in app and "autocomplete: 'current-password'" in app, \
        "the Admin recent-authentication modal does not collect the current password"
    assert "window.MojoAuth.login(context.user.username, password.value)" in app, \
        "the Admin recent-authentication modal does not mint a fresh JWT in place"
    assert "force_reauth=1" not in app and "location.assign(`/auth?redirect=" not in app, \
        "Admin recent authentication still leaves the portal for Bouncer"
    assert "await requestFreshAuth(error)" in core and \
        "return requestPayload(path, options, retry, false)" in core, \
        "a successful Admin recent-authentication modal does not retry the blocked request once"
    assert "const BUSY = new Map()" in overlays and "clearBusy" in overlays, \
        "busy ownership is not tokenized and globally releasable"
    assert "finally" in platform and "busy.close()" in platform and "activeAction" in platform, \
        "Setup actions do not unconditionally release a single-flight busy lease"
    assert "apiOnce('/api/account/admin/setup/create'" in platform and "options.active_fix" in platform \
        and "/api/account/admin/setup/detail?operation=" in platform, \
        "an ambiguous Setup create cannot reconcile without replaying the mutation"
    assert "skeletonState" in views and "prefers-reduced-motion:reduce" in styles, \
        "professional loading feedback or reduced-motion support is missing"


@th.django_unit_test("preview exposes deterministic Setup delay, failure, 440, and ambiguity")
def test_setup_preview_states(opts):
    source = (ROOT / "bin/admin_preview_support/server.py").read_text()
    for state in ("delay", "error", "fresh", "ambiguous"):
        assert f'"{state}"' in source, f"preview cannot render the {state} Setup state"
    assert "Deterministic lost Setup response" in source and "Deterministic Setup failure" in source, \
        "preview failure states do not have stable non-secret messages"


@th.django_unit_test("preview covers independent WebApp health and secure posture")
def test_platform_preview_truth_axes(opts):
    platform = (ROOT / "bin/admin_preview_support/features/platform.py").read_text()
    setup = (ROOT / "bin/admin_preview_support/server.py").read_text()
    for value in ("current_health", "configured_origins", "not_started", "deployment_keys"):
        assert value in platform, f"preview omitted the WebApp {value} axis"
    for value in ("https_redirect", "csrf_cookie_secure", "hsts", '"disabled"'):
        assert value in platform, f"preview omitted the secure-posture {value} state"
    assert 'check("django.base_url", "fail"' in setup, \
        "preview cannot render the check-specific missing BASE_URL repair"
    assert 'check("django.local_request", "pass"' in setup and "operatorChecks" in \
        (ASSETS / "features/platform/page.js").read_text(), \
        "preview cannot exercise compatibility filtering for legacy local-listener noise"

    # The fleet fixture must be the exact projection _fleet() returns: a runner
    # proves itself with a heartbeat, never with a synthesized alive flag.
    from urllib.parse import urlparse

    class Handler:
        pass

    provider = _server().platform
    provider.reset(Handler, {"setup_choice": lambda: None})
    _, payload = provider.get(
        Handler, urlparse("/api/account/admin/platform?sections=fleet"))
    runners = payload["sections"]["fleet"]["data"]["runners"]
    assert runners and all(
        set(row) == {"runner", "channels", "last_heartbeat"} for row in runners), \
        f"the fleet fixture does not match what _fleet() returns: {runners!r}"
    assert '"alive"' not in platform, \
        "the fleet fixture still invents an alive flag the collector never sends"


@th.django_unit_test("preview renders the jobs and sanity Dashboard sources in every state")
def test_dashboard_preview_jobs_and_sanity_states(opts):
    from urllib.parse import urlparse

    server = _server()
    source = (ROOT / "bin/admin_preview_support/server.py").read_text()
    gallery = (ROOT / "bin/admin_preview_support/gallery.py").read_text()
    provider = server.dashboard

    for state in ("jobs_stalled", "sanity_failed"):
        assert state in source, f"the preview cannot select the {state} scenario"
    assert '"setup_attention": True' in gallery, \
        "the deterministic bootstrap never badges System Setup"
    assert server.bootstrap([])["features"]["platform"]["capabilities"][
        "setup_attention"] is True, \
        "the preview bootstrap does not publish the Setup attention flag"

    class Handler:
        pass

    states = ("healthy", "degraded", "down", "jobs_stalled", "sanity_failed",
              "denied", "unknown")
    for state in states:
        provider.reset(Handler, {}, dashboard_state=state)
        status, body = provider.get(Handler, urlparse("/api/account/admin/dashboard"))
        assert status == 200, f"{state}: the dashboard fixture answered {status}"
        for name in ("jobs", "sanity"):
            assert name in body["sources"], \
                f"{state}: the {name} source is missing from the fixture"

    provider.reset(Handler, {}, dashboard_state="jobs_stalled")
    _, stalled = provider.get(Handler, urlparse("/api/account/admin/dashboard"))
    jobs = stalled["sources"]["jobs"]
    assert jobs["status"] == "degraded" and jobs["data"]["scheduler_active"] is False, \
        f"the stalled scenario does not stall the scheduler: {jobs!r}"
    assert jobs["data"]["failed_recent"] == 4, \
        f"the stalled scenario has no failures inside the window: {jobs!r}"
    assert stalled["availability"]["state"] == "ok", \
        "a stalled queue reddened the preview headline — jobs is not an outage"

    provider.reset(Handler, {}, dashboard_state="sanity_failed")
    _, failed = provider.get(Handler, urlparse("/api/account/admin/dashboard"))
    checks = failed["sources"]["sanity"]["data"]["checks"]
    failing = [row["name"] for row in checks if not row["ok"]]
    assert failing == ["migrations"], \
        f"the sanity scenario does not fail exactly one named check: {checks!r}"
    assert failed["availability"]["state"] == "ok", \
        "a failing node check reddened the preview headline"

    # The healthy fixture must carry a large all-time ledger beside an empty
    # window, or the row's "never colour from the ledger" rule is untested.
    provider.reset(Handler, {}, dashboard_state="healthy")
    _, healthy = provider.get(Handler, urlparse("/api/account/admin/dashboard"))
    data = healthy["sources"]["jobs"]["data"]
    assert data["jobs"]["failed"] > 100 and data["failed_recent"] == 0, \
        f"the healthy jobs fixture cannot prove the ledger is ignored: {data!r}"


@th.django_unit_test("preview renders every CloudWatch metrics state deterministically")
def test_metrics_preview_states(opts):
    from urllib.parse import urlparse

    server = _server()
    source = (ROOT / "bin/admin_preview_support/server.py").read_text()
    gallery = (ROOT / "bin/admin_preview_support/gallery.py").read_text()
    provider = server.platform

    assert "--metrics-state" in source and "metrics_state=args.metrics_state" in source, \
        "the preview cannot select a metrics scenario"
    assert "manage_aws" in gallery and "metrics_state" in gallery, \
        "the deterministic bootstrap omits the AWS grant or the metrics scenario"
    assert server.bootstrap([])["features"]["platform"]["capabilities"]["metrics"] is True, \
        "the preview bootstrap never enables the Metrics lane"

    expected = {
        "live": (True, None, 2, 1),
        "empty": (True, None, 2, 1),
        "unconfigured": (False, "credentials_unavailable", 0, 0),
        "denied": (False, "denied", 0, 0),
        "partial": (True, None, 2, 0),
    }
    for state, (available, reason, ec2_count, rds_count) in expected.items():
        class Handler:
            pass

        provider.reset(Handler, {"setup_choice": lambda: None}, metrics_state=state)
        status, resources = provider.get(
            Handler, urlparse("/api/aws/cloudwatch/resources"))
        assert status == 200, f"{state}: resources answered {status}"
        assert resources["available"] is available, \
            f"{state}: expected available={available}, got {resources['available']}"
        assert resources["reason"] == reason, \
            f"{state}: expected reason={reason!r}, got {resources['reason']!r}"
        assert len(resources["ec2"]) == ec2_count and len(resources["rds"]) == rds_count, \
            f"{state}: expected {ec2_count} EC2 / {rds_count} RDS, got {resources}"

        # Providers answer with the body the browser sees after api() unwraps
        # the envelope, so the availability flags sit at this level.
        _, data = provider.get(Handler, urlparse(
            "/api/aws/cloudwatch/fetch?account=ec2&category=cpu&granularity=hours"))
        assert data["available"] is available, \
            f"{state}: fetch availability disagrees with the resource listing"
        if available:
            assert len(data["labels"]) == 24, \
                f"{state}: hourly fetch did not produce 24 buckets: {len(data['labels'])}"
            values = list(data["data"].values())
            assert values and all(len(row) == 24 for row in values), \
                f"{state}: a series does not line up with its labels: {data}"
            has_signal = any(any(point for point in row) for row in values)
            assert has_signal is (state != "empty"), \
                f"{state}: the all-zero fixture and the live fixture are indistinguishable"

    # Partial means one service is out, not the whole page.
    class Partial:
        pass

    provider.reset(Partial, {"setup_choice": lambda: None}, metrics_state="partial")
    _, resources = provider.get(Partial, urlparse("/api/aws/cloudwatch/resources"))
    assert resources["degraded"] == {"rds": "service_error"}, \
        f"the partial fixture does not name the single failed service: {resources['degraded']}"
    _, rds = provider.get(Partial, urlparse(
        "/api/aws/cloudwatch/fetch?account=rds&category=cpu&granularity=hours"))
    assert rds["available"] is False and rds["reason"] == "service_error", \
        f"the degraded service still charts: {rds}"


@th.django_unit_test("preview recovers committed new-group response loss after reload")
def test_webapp_new_group_preview_contract(opts):
    from urllib.parse import urlparse

    server = _server()

    class Handler:
        groups = [dict(row) for row in server.GROUPS]

    fixtures = {"webapps": server.WEBAPPS,
                "webapp_onboarding": server.webapp_onboarding_operation}
    server.webapps.reset(Handler, fixtures, onboarding_state="new_group")
    first = Handler()
    payload = {
        "operation_id": "00000000-0000-4000-8000-000000001920",
        "group_intent": "new", "display_name": "Recovered Portal",
        "slug": "recovered-portal", "environment": "production",
    }

    status, lost = server.webapps.post(
        first, "/api/edge/webapp/onboarding/create", payload)
    reloaded = Handler()
    detail_status, operation = server.webapps.get(
        reloaded, urlparse(
            "/api/edge/webapp/onboarding/detail?operation=" +
            payload["operation_id"]))
    replay_status, replay = server.webapps.post(
        reloaded, "/api/edge/webapp/onboarding/create", dict(payload))

    assert status == 503 and "committed" in lost["error"], \
        "preview did not lose the response after committing the operation"
    assert detail_status == 200 and operation["operation_id"] == payload["operation_id"], \
        "reload could not mount the authoritative committed operation"
    assert replay_status == 200 and replay["created"] is False and \
        replay["operation"] is operation, \
        "exact replay did not reconcile the committed receipt"
    assert sum(row.get("id") == 109 for row in Handler.groups) == 1 and \
        sum(row.get("id") == 142 for row in Handler.webapps) == 1, \
        "committed-loss recovery duplicated its Group or WebApp"

    bootstrap = server.bootstrap(Handler.groups)
    assert bootstrap["user"]["username"] == "ian@example.com", \
        "preview bootstrap omitted the username used by inline recent authentication"
    assert bootstrap["edge"] == {
        "available": True, "http_enabled": True,
        "dnsman_issuance": "dns-01"}, \
        "preview bootstrap omitted the Vhosts certificate-serving posture"
    assert all(row.get("can_manage_dns") is True
               for row in bootstrap["webapp_groups"]), \
        "preview bootstrap omitted per-group DNS management authority"


@th.django_unit_test("preview covers Settings provenance, duplicate, delay, error, and 440")
def test_settings_preview_states(opts):
    server = (ROOT / "bin/admin_preview_support/server.py").read_text()
    provider = (ROOT / "bin/admin_preview_support/features/settings.py").read_text()
    gallery = (ROOT / "bin/admin_preview_support/gallery.py").read_text()
    from urllib.parse import urlparse

    for state in ("normal", "duplicate", "invalid", "provider_failed", "unset",
                  "restricted", "delay", "error", "fresh"):
        assert f'"{state}"' in server or f'"{state}"' in provider, \
            f"preview cannot render the {state} Settings state"
    for source in ("database", "deployment", "duplicate_override", "invalid"):
        assert source in provider, f"preview omitted Settings provenance {source}"
    assert "settings" in gallery and "settings_owner_edit" in gallery, \
        "the deterministic bootstrap fixed roster omitted Settings"
    assert "payload.get(\"value\")" in provider and "handler.setting_entries" in provider, \
        "Settings preview cannot render set/clear outcomes"
    assert 'path == "/api/account/admin/settings"' in server and 'key == "value"' in server, \
        "Settings preview events can retain submitted values"

    feature = _server().settings
    for state in ("normal", "provider_failed", "unset", "restricted"):
        class Handler:
            pass

        feature.reset(Handler, {}, settings_state=state)
        code, report = feature.get(Handler, urlparse("/api/account/admin/settings"))
        assert code == 200, f"{state}: the Settings fixture answered {code}"
        setup = report.get("provider_setup")
        if state == "restricted":
            # The only way to see the non-superuser fallback where the GeoIP
            # descriptors render as their own rows.
            assert setup is None, \
                f"{state}: a non-superuser fixture still returned provider status"
            continue
        assert setup is not None, f"{state}: the fixture emitted no provider status"
        hint = setup["geoip"]["GEOIP_API_KEY_MOJO_HINT"]
        assert len(hint) == (0 if state == "unset" else 4), \
            f"{state}: the GeoIP key hint is not four characters: {hint!r}"
        assert setup["geoip_providers"] and "verify_state" in setup, \
            f"{state}: the fixture omitted the provider picker or verification"
        assert "api_key_hint" in setup["sms"], \
            f"{state}: the SMS section lost its key hint"
        assert "GEOIP_API_KEY_MOJO" not in setup["geoip"], \
            f"{state}: the fixture returned a full GeoIP key"
    class Failed:
        pass

    feature.reset(Failed, {}, settings_state="provider_failed")
    _, failing = feature.get(Failed, urlparse("/api/account/admin/settings"))
    geoip = failing["provider_setup"]["verify_state"]["geoip"]
    assert geoip["ok"] is False and "mojoverify.com" in geoip["message"], \
        f"the failing fixture does not diagnose a host: {geoip!r}"
    status, answer = feature.post(Failed, "/api/account/admin/settings", {
        "action": "test_providers", "topic": "sms",
        "providers": {"sms": {"remote_url": "https://sms.example.com"}}})
    assert status == 200 and answer["results"]["sms"]["success"] is True, \
        f"a failing GeoIP fixture also failed the unrelated SMS topic: {answer!r}"
    status, refused = feature.post(Failed, "/api/account/admin/settings", {
        "action": "test_providers", "topic": "sms",
        "providers": {"sms": {}, "geoip": {}}})
    assert status == 400, \
        f"the fixture accepted a payload carrying the untouched topic: {refused!r}"


@th.django_unit_test("preview renders every merged Deployments state deterministically")
def test_deployments_preview_states(opts):
    from urllib.parse import urlparse

    server = _server()
    source = (ROOT / "bin/admin_preview_support/server.py").read_text()
    gallery = (ROOT / "bin/admin_preview_support/gallery.py").read_text()

    assert "--deployments-state" in source and \
        "deployments_state=args.deployments_state" in source, \
        "the preview cannot select a deployments scenario"
    assert "deployments_state" in gallery, \
        "gallery reset does not thread the deployments scenario to providers"

    fixtures = {"webapps": server.WEBAPPS,
                "webapp_onboarding": server.webapp_onboarding_operation,
                "setup_choice": lambda: None}
    # (API attempt status or None, app rows, rows with no address)
    expected = {
        "mixed": ("partial", 2, 1),
        "converged": ("converged", 2, 0),
        "failed": ("failed", 2, 0),
        "empty": (None, 0, 0),
    }
    for state, (api_status, app_count, missing) in expected.items():
        class Handler:
            pass

        server.webapps.reset(Handler, fixtures, deployments_state=state)
        server.platform.reset(Handler, fixtures, deployments_state=state)

        code, body = server.webapps.get(
            Handler, urlparse("/api/edge/webapp/summaries"))
        assert code == 200, f"{state}: summaries answered {code}"
        assert body["schema_version"] == 1 and body["limit"] == 50, \
            f"{state}: the summaries envelope drifted: {body.keys()}"
        assert len(body["items"]) == app_count, \
            f"{state}: expected {app_count} app rows, got {len(body['items'])}"
        assert sum(1 for row in body["items"] if row["address"] is None) == missing, \
            f"{state}: expected {missing} no-address rows"

        code, report = server.platform.get(Handler, urlparse(
            "/api/account/admin/platform?sections=deployments,api"))
        assert code == 200, f"{state}: platform overview answered {code}"
        attempts = report["sections"]["deployments"]["data"]["items"]
        if api_status is None:
            assert attempts == [], f"{state}: expected no attempts, got {attempts}"
        else:
            assert attempts[0]["status"] == api_status, \
                f"{state}: expected API status {api_status}, got {attempts[0]['status']}"

    # The mixed state proves row truncation with a real 40-char release id,
    # and the failed state pairs a failed deploy with an expired certificate.
    class Mixed:
        pass

    server.webapps.reset(Mixed, fixtures, deployments_state="mixed")
    body = server.webapps.get(Mixed, urlparse("/api/edge/webapp/summaries"))[1]
    green = body["items"][0]
    assert len(green["current_release"]["version"]) == 40, \
        "the mixed fixture cannot prove the 40-char id is demoted on the row"
    assert green["address"]["certificate"]["status"] == "active", \
        "the mixed fixture lost its valid-certificate state"

    class Failed:
        pass

    server.webapps.reset(Failed, fixtures, deployments_state="failed")
    body = server.webapps.get(Failed, urlparse("/api/edge/webapp/summaries"))[1]
    broken = body["items"][0]
    assert broken["latest_deployment"]["status"] == "failed", \
        "the failed fixture has no failed latest deployment"
    assert broken["address"]["certificate"]["not_after"] < "2026-08-18", \
        "the failed fixture's certificate is not expired"

    # The per-app drill-in summary carries the additive certificate fact.
    class Drill:
        pass

    server.webapps.reset(Drill, fixtures)
    summary = server.webapps.get(Drill, urlparse("/api/edge/webapp/summary"))[1]
    assert summary["address"]["certificate"]["status"] == "active", \
        "the drill-in summary fixture omitted address.certificate"


@th.django_unit_test("preview renders every Maintenance state deterministically")
def test_maintenance_preview_states(opts):
    from urllib.parse import urlparse

    server = _server()
    source = (ROOT / "bin/admin_preview_support/server.py").read_text()
    gallery = (ROOT / "bin/admin_preview_support/gallery.py").read_text()
    provider = server.maintenance

    assert "--maintenance-state" in source and "maintenance_state=args.maintenance_state" in source, \
        "the preview cannot select a maintenance scenario"
    assert "RESET_ONLY" in gallery and "maintenance" in gallery, \
        "the maintenance provider is never reset between scenarios"
    # It serves pages but publishes no lane; the platform mirror owns the lane.
    assert "maintenance" not in server.bootstrap([])["features"], \
        "the preview bootstrap invented a feature the portal registry has never heard of"
    assert server.bootstrap([])["features"]["platform"]["capabilities"]["maintenance"] is True, \
        "the preview bootstrap never enables the Maintenance lane"

    # (status, finding count, warning count, scheduled, can_update, blocked)
    expected = {
        "findings": ("ok", 3, 0, True, True, None),
        "denied": ("ok", 2, 1, True, True, None),
        "in_flight": ("ok", 3, 0, True, True, None),
        "unavailable": ("unavailable", 0, 0, True, True, None),
        "framework_pinned": ("ok", 0, 0, True, True, None),
        "framework_none": ("ok", 0, 0, True, False, "no_converged_deployment"),
        "clear": ("ok", 0, 0, False, False, "update_unavailable"),
    }
    for state, (status, findings, warnings, scheduled, can_update, blocked) in expected.items():
        class Handler:
            pass

        provider.reset(Handler, {}, maintenance_state=state)
        code, report = provider.get(Handler, urlparse("/api/aws/maintenance/versions"))
        assert code == 200, f"{state}: versions answered {code}"
        assert report["status"] == status, \
            f"{state}: expected status={status}, got {report['status']}"
        assert len(report["findings"]) == findings, \
            f"{state}: expected {findings} findings, got {len(report['findings'])}"
        assert len(report["warnings"]) == warnings, \
            f"{state}: expected {warnings} warnings, got {report['warnings']}"
        assert report["scheduled"] is scheduled, \
            f"{state}: expected scheduled={scheduled}, got {report['scheduled']}"

        code, overview = provider.get(
            Handler, urlparse("/api/account/admin/platform/framework"))
        assert code == 200, f"{state}: framework answered {code}"
        assert overview["can_update"] is can_update, \
            f"{state}: expected can_update={can_update}, got {overview['can_update']}"
        assert overview["blocked_reason"] == blocked, \
            f"{state}: expected blocked={blocked!r}, got {overview['blocked_reason']!r}"

    # A denied IAM read must name the exact action an operator has to grant.
    class Denied:
        pass

    provider.reset(Denied, {}, maintenance_state="denied")
    _, report = provider.get(Denied, urlparse("/api/aws/maintenance/versions"))
    assert report["warnings"][0]["iam_action"] == "elasticache:DescribeCacheClusters", \
        f"the denied fixture does not name the missing IAM action: {report['warnings']}"
    assert not any(row["kind"] == "elasticache" for row in report["findings"]), \
        "a denied describe still produced cache findings, hiding the denial"

    # An applied upgrade must be visibly in flight, then settle honestly. The
    # 'stalled' state is the one that proves a settled resource on its OLD
    # version is reported as a failure rather than a success.
    for state, upgraded in (("findings", True), ("stalled", False)):
        class Applying:
            pass

        provider.reset(Applying, {}, maintenance_state=state)
        code, body = provider.post(Applying, "/api/aws/maintenance/apply", {
            "kind": "rds-instance", "resource": "mojo-prod-postgres",
            "target_version": "16.4", "confirm_resource": "mojo-prod-postgres",
            "apply_immediately": False})
        assert code == 200 and body["requested"] is True, \
            f"{state}: the apply fixture did not accept the request: {body}"
        path = "/api/aws/maintenance/status?kind=rds-instance&resource=mojo-prod-postgres&target_version=16.4"
        first = provider.get(Applying, urlparse(path))[1]
        assert first["settled"] is False and first["pending_version"] == "16.4", \
            f"{state}: the first poll does not show work in flight: {first}"
        provider.get(Applying, urlparse(path))
        final = provider.get(Applying, urlparse(path))[1]
        assert final["settled"] is True, f"{state}: the upgrade never settled: {final}"
        assert final["upgraded"] is upgraded, \
            f"{state}: expected upgraded={upgraded}, got {final}"


@th.django_unit_test("the preview can demonstrate an externally-managed installation")
def test_preview_infrastructure_mode(opts):
    from urllib.parse import urlparse

    server = _server()
    source = (ROOT / "bin/admin_preview_support/server.py").read_text()

    assert "--infrastructure-mode" in source \
        and "infrastructure_mode=args.infrastructure_mode" in source, \
        "the preview cannot select an infrastructure mode"

    # The default must keep every existing caller — the tests included — on the
    # payload they had.
    default = server.bootstrap([])
    assert default["infrastructure"] == {"mode": "managed", "managed": True}, \
        f"the default preview installation is not managed: {default['infrastructure']}"
    assert default["capabilities"]["infrastructure_managed"] is True, \
        "the default preview bootstrap does not publish the managed capability"
    for name in ("platform", "webapps"):
        assert default["features"][name]["capabilities"]["infrastructure_managed"] is True, \
            f"the default preview {name} lane does not mirror the managed flag"

    external = server.bootstrap([], infrastructure_mode="external")
    assert external["infrastructure"] == {"mode": "external", "managed": False}, \
        f"the preview cannot publish external mode: {external['infrastructure']}"
    assert external["capabilities"]["infrastructure_managed"] is False, \
        "the external preview bootstrap still claims a portal-managed installation"
    for name in ("platform", "webapps"):
        assert external["features"][name]["capabilities"]["infrastructure_managed"] is False, \
            f"the external preview {name} lane still claims managed"
    assert external["features"]["platform"]["enabled"] is True, \
        "external mode closed the Platform lane instead of only disabling controls"

    # The framework fixture must show what production shows, or the preview is
    # demonstrating a screen that cannot happen.
    class Handler:
        pass

    server.maintenance.reset(Handler, {}, maintenance_state="findings")
    Handler.infrastructure_mode = "external"
    code, overview = server.maintenance.get(
        Handler, urlparse("/api/account/admin/platform/framework"))
    assert code == 200, f"the framework fixture answered {code} in external mode"
    assert overview["can_update"] is False, \
        "the external preview still offers a framework update"
    assert overview["blocked_reason"] == "infrastructure_external", \
        f"the external preview names the wrong block: {overview['blocked_reason']!r}"
    assert overview["installed"] and overview["latest"], \
        f"the external preview hid the version facts: {overview}"


@th.django_unit_test("preview renders every Text messages provider state deterministically")
def test_sms_preview_states(opts):
    from urllib.parse import urlparse

    gallery = (ROOT / "bin/admin_preview_support/gallery.py").read_text()
    assert "sms_state" in gallery, \
        "gallery reset does not thread the SMS scenario to providers"
    assert "messaging_sms" in gallery and "messaging_sms_system_write" in gallery, \
        "the deterministic bootstrap fixed roster omitted the SMS capabilities"

    feature = _server().sms
    for state in ("configured", "unset", "verify_failed", "test_mode",
                  "not_installed"):
        class Handler:
            pass

        feature.reset(Handler, {}, sms_state=state)
        code, report = feature.get(
            Handler, urlparse("/api/account/admin/messaging-sms/summary"))
        assert code == 200, f"{state}: the SMS fixture answered {code}"
        if state == "not_installed":
            assert report["installed"] is False, \
                f"{state}: the fixture claims phonehub is installed"
            continue
        assert report["installed"] is True, \
            f"{state}: the fixture claims phonehub is missing"
        if state == "unset":
            assert report["system"] is None, \
                f"{state}: an unset fixture still carries a system config"
            continue
        system = report["system"]
        assert system and set(system["secrets"].values()) <= {True, False}, \
            f"{state}: secret presence must be booleans only: {system!r}"
        assert "api_key" not in system, \
            f"{state}: the fixture leaked a secret value: {system!r}"
        assert report["verify_state"]["ok"] is (state != "verify_failed"), \
            f"{state}: verification outcome does not match the scenario"

    class TestMode:
        pass

    feature.reset(TestMode, {}, sms_state="test_mode")
    status, result = feature.post(
        TestMode, "/api/account/admin/messaging-sms",
        {"action": "test_connection"})
    assert status == 200 and result["state"] == "test_mode", \
        f"test_mode must stay a distinct third state, got {result!r}"
    assert "not contacted" in result["message"], \
        f"test_mode result does not say the provider was skipped: {result!r}"

    class Failing:
        pass

    feature.reset(Failing, {}, sms_state="verify_failed")
    status, refused = feature.post(
        Failing, "/api/account/admin/messaging-sms",
        {"action": "save", "provider": "mojo",
         "remote_url": "https://sms.example.com"})
    assert status == 400, \
        f"a failing verification fixture accepted a save: {refused!r}"
    status, sent = feature.post(
        Failing, "/api/account/admin/messaging-sms",
        {"action": "send_test", "to_number": "+15550001111"})
    assert status == 200 and sent["test_number"] is True, \
        f"a +1555 recipient is not reported as a test number: {sent!r}"


@th.django_unit_test("preview serves the Email feature and its mock routes deterministically")
def test_email_preview_states(opts):
    from urllib.parse import urlparse

    server = _server()
    bootstrap = server.bootstrap([])
    assert bootstrap["capabilities"].get("email") is True, \
        "the deterministic bootstrap fixed roster omits the email capability"
    email = bootstrap["features"].get("email")
    assert email and email["enabled"] is True \
        and email["capabilities"] == {"view": True, "manage": True}, \
        f"the preview bootstrap does not publish the Email feature: {email!r}"

    feature = server.email

    class Handler:
        pass

    feature.reset(Handler, {})
    code, payload = feature.get(Handler, urlparse("/api/aws/email/summary"))
    assert code == 200, f"the email summary fixture answered {code}"
    report = payload["data"]
    names = {row["name"] for row in report["domains"]}
    assert {"mojo.example", "sandbox.example", "inbound.example"} <= names, \
        f"the fixture lost a documented domain scenario: {names}"
    sandbox = next(row for row in report["domains"]
                   if row["name"] == "sandbox.example")
    assert sandbox["can_send"] is False, \
        "the sandbox-only scenario must not read as sendable"
    inbound = next(row for row in report["domains"]
                   if row["name"] == "inbound.example")
    assert inbound["can_send"] is True and inbound["can_recv"] is False, \
        "the half-configured receiving scenario changed shape"

    code, payload = feature.get(Handler, urlparse("/api/aws/email/domain/2/audit"))
    assert code == 200 and payload["data"]["audit_pass"] is False, \
        "the sandbox domain's audit fixture no longer reports its finding"
    assert payload["data"]["recommendations"], \
        "the failing audit fixture carries no plain-words recommendation"

    for from_email, expected in (
            ("noreply@mojo.example", "outbound_not_allowed"),
            ("ghost@mojo.example", "mailbox_not_found"),
            ("test@sandbox.example", "domain_not_verified"),
            ("", "invalid_request")):
        code, payload = feature.post(Handler, "/api/aws/email/test", {
            "from_email": from_email, "to": "you@example.org", "subject": "s"})
        assert code == 200, f"{expected}: the test-send fixture answered {code}"
        assert payload["data"]["sent"] is False \
            and payload["data"]["error_code"] == expected, \
            f"{expected}: wrong structured error: {payload['data']!r}"

    code, payload = feature.post(Handler, "/api/aws/email/test", {
        "from_email": "support@mojo.example", "to": "fail@example.org",
        "subject": "s"})
    assert code == 200 and payload["data"]["status"] == "failed", \
        f"the SES-refusal scenario changed shape: {payload['data']!r}"

    code, payload = feature.post(Handler, "/api/aws/email/mailbox-default",
                                 {"mailbox": 13, "scope": "system"})
    assert code == 200 and payload["data"]["is_system_default"] is True, \
        f"the mailbox-default fixture refused a valid claim: {payload!r}"


@th.django_unit_test("preview serves every Assistant setup state and never a key value")
def test_assistant_preview_states(opts):
    from urllib.parse import urlparse

    server = _server()
    bootstrap = server.bootstrap([])
    for capability in ("assistant", "assistant_ready", "assistant_setup",
                       "assistant_mcp"):
        assert bootstrap["capabilities"].get(capability) is True, \
            f"the deterministic bootstrap fixed roster omits {capability}"
    assistant = bootstrap["features"].get("assistant")
    assert assistant and assistant["enabled"] is True, \
        f"the preview bootstrap does not publish the Assistant namespace: {assistant!r}"
    assert set(assistant["capabilities"]) == {"view", "ready", "setup", "mcp"}, \
        f"the Assistant preview namespace drifted: {assistant!r}"

    feature = server.assistant
    for state in ("configured", "unset", "fallback", "verify_failed", "disabled"):
        class Handler:
            pass

        feature.reset(Handler, {}, assistant_state=state)
        code, report = feature.get(Handler, urlparse("/api/account/admin/assistant"))
        assert code == 200, f"{state}: the Assistant fixture answered {code}"
        assert set(report["key"]) == {"configured", "hint", "source"}, \
            f"{state}: the fixture key state carries more than presence and provenance"
        assert len(report["key"]["hint"]) in (0, 4), \
            f"{state}: the fixture hint is not four characters — a leak could ship " \
            f"looking correct: {report['key']!r}"
        assert "api_key" not in report and "value" not in report["key"], \
            f"{state}: the Assistant fixture emitted a key value: {report!r}"
        assert report["enabled"] is (state != "disabled"), \
            f"{state}: the enabled flag does not follow the scenario"
    assert report["key"]["source"] == "admin", \
        "the disabled scenario lost its stored-credential provenance"

    class Failing:
        pass

    feature.reset(Failing, {}, assistant_state="verify_failed")
    status, refused = feature.post(
        Failing, "/api/account/admin/assistant",
        {"action": "save", "enabled": True, "model": "", "api_key": "sk-nope"})
    assert status == 400, \
        f"a failing verification fixture accepted a credential save: {refused!r}"


@th.django_unit_test("preview serves every remote agent access state and never a token")
def test_assistant_mcp_preview_states(opts):
    from urllib.parse import urlparse

    server = _server()
    feature = server.assistant
    hashish = ("access_jti", "refresh_hash", "prev_refresh_hash", "token",
               "refresh_token", "access_token")

    for scenario in ("off", "reachable", "unreachable", "connected"):
        class Handler:
            pass

        feature.reset(Handler, {}, assistant_mcp_state=scenario)
        code, report = feature.get(
            Handler, urlparse("/api/account/admin/assistant"))
        assert code == 200, f"{scenario}: the Assistant fixture answered {code}"
        mcp = report["mcp"]
        assert set(mcp) == {"enabled", "path", "url", "discovery_url", "discovery",
                            "grants", "grant_count"}, \
            f"{scenario}: the fixture remote-access state drifted: {sorted(mcp)}"
        assert set(mcp["discovery"]) == {"ok", "code", "detail", "checked_at"}, \
            f"{scenario}: the fixture discovery record drifted: {mcp['discovery']!r}"
        assert mcp["enabled"] is (scenario != "off"), \
            f"{scenario}: the switch does not follow the scenario: {mcp!r}"
        assert mcp["grant_count"] == len(mcp["grants"]), \
            f"{scenario}: the fixture grant count disagrees with its rows: {mcp!r}"
        for grant in mcp["grants"]:
            for banned in hashish:
                assert banned not in grant, \
                    f"{scenario}: the fixture grant emitted {banned} — a real " \
                    f"leak could ship looking correct: {grant!r}"

        # A plain read never re-checks; the explicit control does, visibly.
        _code, rechecked = feature.get(
            Handler, urlparse("/api/account/admin/assistant?check=discovery"))
        assert rechecked["mcp"]["discovery"]["checked_at"] == \
            feature.MCP_RECHECKED_AT, \
            f"{scenario}: ?check=discovery did not re-stamp the verdict: " \
            f"{rechecked['mcp']['discovery']!r}"
        assert mcp["discovery"]["checked_at"] != feature.MCP_RECHECKED_AT, \
            f"{scenario}: a plain read already carried the re-checked stamp"

    class Connected:
        pass

    feature.reset(Connected, {}, assistant_mcp_state="connected")
    _code, report = feature.get(Connected, urlparse("/api/account/admin/assistant"))
    assert report["mcp"]["grant_count"] == 2, \
        f"the connected scenario does not list two agents: {report['mcp']!r}"
    first = report["mcp"]["grants"][0]["id"]

    status, answer = feature.post(
        Connected, "/api/account/admin/assistant",
        {"action": "revoke_grant", "grant_id": first})
    assert status == 200 and answer["revoked"] == 1, \
        f"disconnecting one agent was not honoured: {status} {answer!r}"
    assert answer["state"]["mcp"]["grant_count"] == 1, \
        f"the disconnected agent is still listed: {answer['state']['mcp']!r}"
    status, answer = feature.post(
        Connected, "/api/account/admin/assistant",
        {"action": "revoke_grant", "grant_id": first})
    assert status == 200 and answer["revoked"] == 0, \
        f"re-disconnecting a gone agent did not answer a quiet zero: {answer!r}"

    feature.reset(Connected, {}, assistant_mcp_state="connected")
    status, answer = feature.post(
        Connected, "/api/account/admin/assistant", {"action": "revoke_all_grants"})
    assert status == 200 and answer["revoked"] == 2, \
        f"disconnecting all agents did not answer the count: {status} {answer!r}"
    assert answer["state"]["mcp"]["grants"] == [], \
        f"disconnecting all left rows behind: {answer['state']['mcp']!r}"

    feature.reset(Connected, {}, assistant_mcp_state="connected")
    status, answer = feature.post(
        Connected, "/api/account/admin/assistant",
        {"action": "save", "enabled": True, "model": "", "mcp_enabled": False})
    assert status == 200 and answer["state"]["mcp"]["enabled"] is False, \
        f"a save that switched remote access off was not honoured: {answer!r}"
    status, answer = feature.post(
        Connected, "/api/account/admin/assistant",
        {"action": "save", "enabled": True, "model": ""})
    assert status == 200 and answer["state"]["mcp"]["enabled"] is False, \
        f"a save omitting mcp_enabled changed the switch: {answer['state']['mcp']!r}"
