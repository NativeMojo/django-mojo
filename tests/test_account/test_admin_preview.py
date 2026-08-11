"""Live-target Admin preview and browser feedback contracts."""

import socket
import sys
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
            "secrets.compare_digest", "Sec-Fetch-Site", "Preview mutations require a same-origin",
            "resolve_public_addresses", "PinnedHTTPSConnection", "server_hostname=self.host",
            "getpeername", "MAX_REQUEST_BODY", "MAX_RESPONSE_BODY", "response.getheaders()"):
        assert value in source, f"live preview omitted the {value} boundary"
    assert 'response_headers["Set-Cookie"]' not in source, \
        "an upstream Set-Cookie can escape into the localhost browser namespace"
    assert "--upstream" in source and "Password authentication is supported" in source, \
        "the live QA command or its authentication boundary is missing"


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
    assert "Your session is still active" in app and "force_reauth=1" in app, \
        "the explicit recent-authentication prompt is incomplete"
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
