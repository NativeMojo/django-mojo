"""Activity center permissions, bounded graphs, paging, and audit contracts."""

from pathlib import Path

from testit import helpers as th


ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "mojo/apps/account/admin_portal/assets/features/activity"
ADMIN = "admin_activity@test.com"
PASSWORD = "Admin_activity_pw_99"
PREFIX = "admin_activity_"


@th.django_unit_setup()
def setup_admin_activity(opts):
    from mojo.apps.account.models import Group, User
    from mojo.apps.incident.models import Event, Incident, Ticket
    from mojo.apps.logit.models import Log

    Ticket.objects.filter(category__startswith=PREFIX).delete()
    Event.objects.filter(category__startswith=PREFIX).delete()
    Incident.objects.filter(category__startswith=PREFIX).delete()
    Log.objects.filter(kind__startswith=PREFIX).delete()
    Group.objects.filter(name__startswith=PREFIX).delete()
    User.objects.filter(email=ADMIN).delete()
    user = User.objects.create_user(username=ADMIN, email=ADMIN, password=PASSWORD)
    user.is_active = True
    user.is_email_verified = True
    user.requires_mfa = False
    user.is_superuser = True
    user.save()
    group_a = Group.objects.create(name=f"{PREFIX}a", kind="organization")
    group_b = Group.objects.create(name=f"{PREFIX}b", kind="organization")
    incident = Incident.objects.create(
        category=f"{PREFIX}incident", title="Activity needle incident",
        details="operator evidence", priority=8, group=group_a,
        metadata={"client_secret": "never-render-raw"},
    )
    for index in range(30):
        Event.objects.create(
            category=f"{PREFIX}{'needle' if index == 17 else 'event'}",
            title=f"Activity event {index}", details="bounded search evidence",
            level=index % 10, group=group_a if index < 20 else group_b,
            incident=incident if index < 20 else None,
            metadata={"password": "never-render-raw"},
        )
    ticket = Ticket.objects.create(
        category=f"{PREFIX}ticket", title="Activity ticket", description="review",
        group=group_a, user=user, assignee=user, incident=incident,
        metadata={"api_token": "never-render-raw"},
    )
    Log.objects.create(
        kind=f"{PREFIX}needle", level="warning", path="/activity/test",
        gid=group_a.pk, uid=user.pk, username=ADMIN,
        payload='{"password":"never-render-raw"}', log="Activity search needle",
    )
    opts.group_a_id = group_a.pk
    opts.group_b_id = group_b.pk
    opts.incident_id = incident.pk
    opts.ticket_id = ticket.pk


@th.django_unit_test("Activity publishes independent log, security-read, and security-manage grants")
def test_activity_capability_split(opts):
    from objict import objict
    from mojo.apps.account.services.admin_features import activity

    class Identity:
        def __init__(self, grants): self.grants = set(grants)
        def has_permission(self, values):
            values = values if isinstance(values, (list, set)) else [values]
            return bool(self.grants.intersection(values))

    logs = activity.describe(objict(user=Identity({"view_logs"})), {})
    security = activity.describe(objict(user=Identity({"view_security"})), {})
    manager = activity.describe(objict(user=Identity({"manage_security"})), {})
    assert logs["capabilities"] == {"view_logs": True, "view_security": False, "manage_security": False}
    assert security["capabilities"] == {"view_logs": False, "view_security": True, "manage_security": False}
    assert manager["capabilities"] == {"view_logs": False, "view_security": True, "manage_security": True}


@th.django_unit_test("Activity graphs expose bounded scalar context without nested account or GeoIP graphs")
def test_activity_graph_contracts(opts):
    from mojo.apps.incident.models import Event, Incident, Ticket
    from mojo.apps.logit.models import Log

    event_graph = Event.RestMeta.GRAPHS["activity"]
    ticket_graph = Ticket.RestMeta.GRAPHS["activity"]
    assert "geo_ip" not in event_graph.get("graphs", {})
    assert set(event_graph["extra"]) == {"group_id", "incident_id"}
    assert not ticket_graph.get("graphs")
    assert {"user_id", "group_id", "assignee_id", "incident_id"}.issubset(ticket_graph["extra"])
    assert UserGraphNames.isdisjoint(ticket_graph.get("graphs", {}))
    assert Log.RestMeta.SEARCH_FIELDS == ["kind", "method", "path", "ip", "username", "log", "model_name"]
    for model in (Event, Incident, Ticket):
        assert "metadata" not in model.RestMeta.SEARCH_FIELDS


UserGraphNames = {"user", "group", "assignee"}


@th.django_unit_test("Activity REST paging, count, search, group filters, and scalar graphs stay authoritative")
def test_activity_rest_list_contract(opts):
    assert opts.client.login(ADMIN, PASSWORD), "admin login failed"
    response = opts.client.get("/api/incident/event", params={
        "graph": "activity", "category__startswith": PREFIX,
        "size": 7, "start": 7, "sort": "created",
    })
    assert response.status_code == 200, response.body
    envelope = response.json
    assert envelope.get("count") == 30
    assert envelope.get("start") == 7 and envelope.get("size") == 7
    assert len(envelope.get("data") or []) == 7
    assert all("geo_ip" not in row for row in envelope["data"])
    assert all(not any(key in row for key in UserGraphNames) for row in envelope["data"])

    searched = opts.client.get("/api/incident/event", params={
        "graph": "activity", "search": f"{PREFIX}needle", "size": 5,
    })
    searched_data = searched.json
    assert searched.status_code == 200 and searched_data.get("count") == 1
    grouped = opts.client.get("/api/incident/event", params={
        "graph": "activity", "category__startswith": PREFIX,
        "group": opts.group_b_id, "size": 50,
    })
    grouped_data = grouped.json
    assert grouped_data.get("count") == 10
    assert {row.get("group_id") for row in grouped_data.get("data", [])} == {opts.group_b_id}

    ticket = opts.client.get("/api/incident/ticket", params={
        "graph": "activity", "category__startswith": PREFIX,
    }).json
    row = ticket["data"][0]
    assert row["group_id"] == opts.group_a_id and row["incident_id"] == opts.incident_id
    assert "user" not in row and "group" not in row and "assignee" not in row


@th.django_unit_test("Incident and Ticket lifecycle writes retain model-owned audit trails")
def test_activity_lifecycle_uses_audited_public_saves(opts):
    from mojo.apps.incident.models import IncidentHistory, TicketNote

    assert opts.client.login(ADMIN, PASSWORD), "admin login failed"
    changed = opts.client.put(f"/api/incident/incident/{opts.incident_id}", json={"status": "open"})
    assert changed.status_code == 200, changed.body
    assert IncidentHistory.objects.filter(parent_id=opts.incident_id, kind="status_changed").exists()
    ticket = opts.client.put(f"/api/incident/ticket/{opts.ticket_id}", json={"status": "paused"})
    assert ticket.status_code == 200, ticket.body
    assert TicketNote.objects.filter(parent_id=opts.ticket_id, metadata__type="status_change").exists()


@th.django_unit_test("Activity browser owns frozen queries, masking, unavailable states, and no aggregate shortcut")
def test_activity_browser_contract(opts):
    page = (ASSETS / "page.js").read_text()
    feature = (ASSETS / "feature.js").read_text()
    manifest = (ASSETS / "manifest.json").read_text()
    preview = (ROOT / "bin/admin_preview_support/features/activity.py").read_text()
    server = (ROOT / "bin/admin_preview_support/server.py").read_text()
    assert "apiEnvelope" in page and "AbortController" in page
    assert "controller !== activeController" in page and "activeController.signal.aborted" in page
    assert "envelope.raw?.count" in page
    assert "QUERY_KEYS" in page and "Unsupported subject filter" in page
    assert "subjectFilters" in page and "without broadening the query" in page
    assert "Incomplete subject filters cannot be discarded" in page
    assert "subject_model is valid only with subject_type=model" in page
    assert "button.classList.toggle('active', active)" in page
    assert "SENSITIVE_KEY" in page and "[redacted]" in page and "depth >= 5" in page
    assert "envelope.count > 0 && state.start >= envelope.count" in page
    assert "Unavailable" in page and "String(envelope.count)" in page
    assert "/api/incident/stats" not in page
    assert "activityPage(ctx, signal)" in feature and '"page.js"' in manifest
    assert 'activity_state == "unavailable"' in preview and 'activity_state == "full"' in preview
    assert "_record_event(path, payload)" in server and "activity.put" in server


@th.django_unit_test("Ticket Activity sort columns are backed by the generated incident migration")
def test_activity_ticket_indexes(opts):
    from mojo.apps.incident.models import Ticket

    assert Ticket._meta.get_field("created").db_index is True
    assert Ticket._meta.get_field("modified").db_index is True
    migration = ROOT / "mojo/apps/incident/migrations/0038_alter_ticket_created_alter_ticket_modified.py"
    text = migration.read_text()
    assert "AlterField" in text and "db_index=True" in text
