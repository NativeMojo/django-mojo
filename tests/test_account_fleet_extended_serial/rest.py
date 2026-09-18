"""Interactive REST authority and non-secret serving proof contracts."""

import inspect
import socket
from types import SimpleNamespace
from unittest import mock

from django.core import signing
from django.test import RequestFactory
from testit import helpers as th


@th.django_unit_test("fleet REST mutations require fresh non-key authentication")
def test_fleet_route_auth_metadata(opts):
    from mojo.apps.account.rest import admin_fleet
    from mojo.apps.account.services import fresh_auth
    from mojo import errors as me

    mutate = admin_fleet.on_admin_fleet_mutate
    assert mutate._mojo_denies_key_backed_session is True, 'Fleet configuration contract failed: mutate._mojo_denies_key_backed_session is True'
    assert mutate._mojo_requires_fresh_auth is True, 'Fleet configuration contract failed: mutate._mojo_requires_fresh_auth is True'
    assert mutate._mojo_fresh_auth_seconds == 600, 'Fleet configuration contract failed: mutate._mojo_fresh_auth_seconds == 600'
    for view in (admin_fleet.on_admin_fleet, admin_fleet.on_admin_fleet_history,
                 admin_fleet.on_admin_fleet_operation):
        assert view._mojo_denies_key_backed_session is True, 'Fleet configuration contract failed: view._mojo_denies_key_backed_session is True'
    request = SimpleNamespace(bearer="bearer", api_key=None, group_token=None,
                              user_api_key=None, auth_token=None)
    with mock.patch.object(fresh_auth, "resolve_window", return_value=600):
        with th.assert_raises(me.ReauthRequiredException):
            fresh_auth.require_fresh(request, seconds=600)


@th.django_unit_test("fleet actor requires a live literal superuser and same origin")
def test_fleet_actor_gate(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.rest import admin_fleet
    from mojo import errors as me

    username = "fleet-rest-authority-test"
    User.objects.filter(username=username).delete()
    actor = User.objects.create(username=username, is_active=True, is_superuser=True)
    request = RequestFactory().post("/api/account/admin/fleet", secure=True,
                                    HTTP_HOST="admin.example.com",
                                    HTTP_ORIGIN="https://admin.example.com")
    request.get_host = lambda: "admin.example.com"
    request.user = actor
    request.bearer = "bearer"
    try:
        assert admin_fleet._actor(request, write=True).pk == actor.pk, 'Fleet configuration contract failed: admin_fleet._actor(request, write=True).pk == actor.pk'
        request.META["HTTP_ORIGIN"] = "https://attacker.example.com"
        with th.assert_raises(me.PermissionDeniedException):
            admin_fleet._actor(request, write=True)
        request.META["HTTP_ORIGIN"] = "https://admin.example.com"
        request.api_key = object()
        with th.assert_raises(me.PermissionDeniedException):
            admin_fleet._actor(request, write=True)
        request.api_key = None
        request.bearer = "session"
        with th.assert_raises(me.PermissionDeniedException):
            admin_fleet._actor(request, write=True)
        request.bearer = "bearer"
        User.objects.filter(pk=actor.pk).update(is_superuser=False, permissions={"admin": True})
        with th.assert_raises(me.PermissionDeniedException):
            admin_fleet._actor(request, write=True)
        User.objects.filter(pk=actor.pk).update(is_superuser=True, is_active=False)
        with th.assert_raises(me.PermissionDeniedException):
            admin_fleet._actor(request, write=True)
    finally:
        User.objects.filter(pk=actor.pk).delete()


@th.django_unit_test("fleet proof refuses unsigned wrong-host and expired challenges")
def test_fleet_proof_challenge(opts):
    from mojo.apps.account.rest import admin_fleet
    from mojo import errors as me

    view = inspect.unwrap(admin_fleet.on_fleet_serving_proof)
    challenge = {"revision": "a" * 32, "nonce": "b" * 32,
                 "node": socket.gethostname().lower()}
    request = SimpleNamespace(META={})
    invalid = ["", "unsigned", "x" * 2049,
               signing.dumps(dict(challenge, node="wrong-host"), salt="mojo.fleet.config.proof"),
               signing.dumps(dict(challenge, nonce="bad"), salt="mojo.fleet.config.proof"),
               signing.dumps(dict(challenge, extra=True), salt="mojo.fleet.config.proof")]
    for token in invalid:
        request.META["HTTP_X_MOJO_FLEET_PROOF"] = token
        with th.assert_raises(me.PermissionDeniedException):
            view(request)
    request.META["HTTP_X_MOJO_FLEET_PROOF"] = signing.dumps(challenge, salt="mojo.fleet.config.proof")
    with mock.patch("django.core.signing.time.time", return_value=10**12):
        with th.assert_raises(me.PermissionDeniedException):
            view(request)


@th.django_unit_test("fleet proof reports loaded revision and fresh dependency health without secrets")
def test_fleet_proof_result(opts):
    from mojo.apps.account.rest import admin_fleet
    from mojo.apps.account.services import admin_platform
    from mojo.helpers.request import sensitive_body_label, API_ROOT

    revision, nonce = "a" * 32, "b" * 32
    token = signing.dumps({"revision": revision, "nonce": nonce,
                           "node": socket.gethostname().lower()}, salt="mojo.fleet.config.proof")
    request = SimpleNamespace(META={"HTTP_X_MOJO_FLEET_PROOF": token})
    view = inspect.unwrap(admin_fleet.on_fleet_serving_proof)
    with mock.patch.object(admin_platform, "_database", return_value={"reachable": True}), \
            mock.patch.object(admin_platform, "_redis", return_value={"reachable": True}), \
            mock.patch.object(admin_fleet.settings, "get_static", return_value=revision):
        result = view(request)
        assert result["loaded_revision"] == revision and result["nonce"] == nonce, 'Fleet configuration contract failed: result["loaded_revision"] == revision and result["nonce"] == nonce'
        assert result["healthy"] is True and result["pid"] > 0, 'Fleet configuration contract failed: result["healthy"] is True and result["pid"] > 0'
        assert set(result) == {"loaded_revision", "nonce", "healthy", "pid", "node"}, 'Fleet configuration contract failed: set(result) == {"loaded_revision", "nonce", "healthy", "pid", "node"}'
    with mock.patch.object(admin_platform, "_database", side_effect=RuntimeError("secret-value")):
        result = view(request)
        assert result["healthy"] is False and "secret-value" not in str(result), 'Fleet configuration contract failed: result["healthy"] is False and "secret-value" not in str(result)'
    for path in ("", "/history", "/proof", "/operation/" + "c" * 32):
        sensitive = SimpleNamespace(path=API_ROOT + "/account/admin/fleet" + path, method="POST")
        assert sensitive_body_label(sensitive), "fleet requests must redact their entire body"


@th.django_unit_test("fleet serving proof works through real HTTP routing without interactive authentication")
def test_fleet_proof_http_dispatch(opts):
    from django.conf import settings as django_settings
    from mojo.helpers.settings import settings
    from mojo.helpers.request import API_ROOT

    opts.client.logout()
    nonce = "e" * 32
    hostname = socket.gethostname().lower()
    token = signing.dumps({"revision": "a" * 32, "nonce": nonce, "node": hostname},
                          salt="mojo.fleet.config.proof")
    endpoint = API_ROOT + "/account/admin/fleet/proof"
    refused = opts.client.get(endpoint)
    assert refused.status_code == 403, \
        "The actual proof route must reject an unsigned anonymous request with HTTP 403"
    response = opts.client.get(endpoint, headers={"X-Mojo-Fleet-Proof": token})
    assert response.status_code == 200, \
        "A valid same-installation signed challenge must reach the proof route without a user session"
    envelope = response.json
    assert isinstance(envelope, dict) and isinstance(envelope.get("data"), dict), \
        "HTTP proof must use the data-object response envelope consumed by the node"
    proof = envelope["data"]
    assert set(proof) == {"loaded_revision", "pid", "node", "nonce", "healthy"}, \
        "The real proof response must expose only the five non-secret evidence fields"
    assert proof["loaded_revision"] == settings.get_static("MOJO_FLEET_CONFIG_REVISION", None), \
        "The HTTP proof must report the serving project's actual loaded revision"
    assert type(proof["pid"]) is int and proof["pid"] > 0, \
        "The proof must identify an actual serving process"
    assert proof["node"] == hostname and proof["nonce"] == nonce, \
        "The dispatched proof must echo the signed node and request nonce"
    assert type(proof["healthy"]) is bool, \
        "Dependency health must remain a boolean through HTTP serialization"
    assert str(django_settings.SECRET_KEY) not in str(envelope), \
        "Serving proof must never expose the installation signing secret"
    refused_write = opts.client.post(API_ROOT + "/account/admin/fleet", json={
        "action": "apply", "expected_revision": "a" * 32})
    assert refused_write.status_code in (401, 403), \
        "Anonymous HTTP clients must not enter the fleet mutation workflow"
