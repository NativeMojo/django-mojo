"""The missing-Maestro-config rejection path (maestro #2791).

MAESTRO_API_KEY is baked into the generated test project so the parallel
workspace-push tests need no reload. Proving the *absence* path — a manual push
must fail clearly, name the missing setting, and never echo the credential —
therefore requires UNSETTING the key at the server, which is a reload. That is
legal only here, in the serial sibling. Moved from tests/test_maestro_board.
"""
from testit import helpers as th

PREFIX = "[maestro_missing]"
TEST_KEY = "rest" + "k" * 44          # the baked MAESTRO_API_KEY
PWORD = "maestro##mojo77"


@th.django_unit_setup()
@th.requires_app("mojo.apps.incident")
def setup_missing_config(opts):
    from mojo.apps.account.models import User
    from mojo.apps.incident.models import Incident, Ticket

    Ticket.objects.filter(title__startswith=PREFIX).delete()
    Incident.objects.filter(title__startswith=PREFIX).delete()

    admin = User.objects.filter(username="maestro_missing_admin").last()
    if admin is None:
        admin = User(username="maestro_missing_admin",
                     email="maestro_missing_admin@example.com")
        admin.save()
    admin.is_email_verified = True
    admin.save_password(PWORD)
    admin.remove_all_permissions()
    admin.add_permission("view_security")
    admin.add_permission("manage_security")
    opts.admin_name = "maestro_missing_admin"


@th.django_unit_test()
def test_missing_setting_rejects_manual_push_without_disclosing_secret(opts):
    from mojo.apps.incident.models import Ticket

    ticket = Ticket.objects.create(
        title=f"{PREFIX} missing config", description="rest test", status="open")
    assert opts.client.login(opts.admin_name, PWORD), "admin login failed"
    # UNSET the baked key to exercise the missing-config path — a reload.
    with th.server_settings(MAESTRO_API_KEY=""):
        response = opts.client.post(
            f"/api/incident/ticket/{ticket.pk}", json={"push_to_maestro": True})
    assert response.status_code == 400, \
        f"missing config must fail clearly: {response.status_code}: {response.body}"
    rendered = str(response.response)
    assert "MAESTRO_API_KEY" in rendered, f"response must name missing setting: {rendered}"
    assert TEST_KEY not in rendered, "response must not disclose a credential"
