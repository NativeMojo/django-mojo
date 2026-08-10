"""People Admin backend and feature-boundary contracts."""

from pathlib import Path

from testit import helpers as th


ROOT = Path(__file__).resolve().parents[2]
PEOPLE = ROOT / "mojo/apps/account/admin_portal/assets/features/people"


@th.django_unit_test("People freezes Activity links and never recovers stored API-key tokens")
def test_people_activity_and_secret_contract(opts):
    page = (PEOPLE / "page.js").read_text()
    expected = (
        "'tab', 'start', 'size', 'sort', 'search', 'date', 'user', 'group', 'ip',\n"
        "  'hostname', 'incident', 'model', 'model_id',"
    )
    assert expected in page, "People must use the exact shared Activity query-key contract"
    assert "graph=token" not in page, \
        "the portal must never invoke the compatible backend API-key recovery graph"
    assert "data-one-time-secret" in page and "content.replaceChildren()" in page, \
        "creation, rotation, and temporary-password dialogs must scrub transient secrets"
    assert "account/admin/apikey/action" in page and "action: 'revoke'" in page, \
        "all People API-key lifecycle operations must use the fresh-auth action boundary"


@th.django_unit_test("People capabilities and permission bundles are server-owned and versioned")
def test_people_capability_and_bundle_contract(opts):
    bootstrap = (ROOT / "mojo/apps/account/rest/admin_portal.py").read_text()
    feature = (ROOT / "mojo/apps/account/services/admin_features/people.py").read_text()
    bundles = (ROOT / "mojo/apps/account/services/admin_people.py").read_text()
    endpoint = (ROOT / "mojo/apps/account/rest/admin_people.py").read_text()

    for capability in ("manage_users", "manage_groups", "manage_api_keys",
                       "view_logins", "view_logs", "view_events",
                       "view_incidents", "view_tickets"):
        assert f'"{capability}"' in bootstrap and f'"{capability}"' in feature, \
            f"server bootstrap and People descriptor must both publish {capability}"
    for bundle in ("people", "platform", "network_hosting", "deployments",
                   "security_incidents", "logs_metrics", "system_administration"):
        assert f'"{bundle}"' in bundles, f"versioned permission map is missing {bundle}"
    assert "current = user.permissions" in bundles and "managed =" in bundles, \
        "bundle application must diff managed keys without replacing unknown permissions"
    assert endpoint.count("@md.requires_fresh_auth(seconds=600)") == 4, \
        "temporary/reset, bundle-write, and API-key actions need explicit 600s freshness"
    assert endpoint.count("@md.denies_key_backed_session()") == 5, \
        "every People custom endpoint must reject key-backed sessions"
    assert 'ApiKey.rest_check_permission_or_raise(request, "SAVE_PERMS", api_key)' in endpoint, \
        "API-key actions must use model security against the selected credential"


@th.django_unit_test("credential-bearing People routes suppress request and response bodies")
def test_people_sensitive_logging_contract(opts):
    helper = (ROOT / "mojo/helpers/request.py").read_text()
    middleware = (ROOT / "mojo/middleware/logging.py").read_text()
    sanitizer = (ROOT / "mojo/helpers/logit.py").read_text()
    for path in ("account/admin/user/password", "account/admin/apikey/action",
                 "group/apikey", "auth/"):
        assert path in helper, f"sensitive path classifier must include {path}"
    assert 'settings.get_static("MOJO_PREFIX"' in helper, \
        "sensitive route classification must follow a customized API prefix"
    assert middleware.count("request_helper.sensitive_body_label(request)") == 2, \
        "request and response logs must classify sensitivity independently of middleware order"
    for key in ("temporary_password", "forced_password_token", "rotated_token"):
        assert key in sanitizer, f"recursive logging sanitizer must redact {key}"
