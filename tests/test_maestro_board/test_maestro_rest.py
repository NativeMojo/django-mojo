"""REST/job/handler coverage for workspace-scoped Maestro reporting."""

from unittest import mock

from testit import helpers as th

PREFIX = "[maestro_rest]"
TEST_KEY = "rest" + "k" * 44
INTEGRATION = "integration-rest-project"
PWORD = "maestro##mojo77"


@th.django_unit_setup()
def setup_maestro_rest(opts):
    from mojo.apps.account.models import User
    from mojo.apps.incident.models import Incident, MaestroBoard, MaestroItemLink, Ticket
    from mojo.apps.jobs.models import Job

    MaestroItemLink.objects.all().delete()
    Ticket.objects.filter(title__startswith=PREFIX).delete()
    Incident.objects.filter(title__startswith=PREFIX).delete()
    MaestroBoard.objects.filter(name__startswith=PREFIX).delete()
    Job.objects.filter(func__startswith="mojo.apps.incident.asyncjobs.maestro").delete()

    admin = User.objects.filter(username="maestro_admin").last()
    if admin is None:
        admin = User(username="maestro_admin", email="maestro_admin@example.com")
        admin.save()
    admin.is_email_verified = True
    admin.save_password(PWORD)
    admin.remove_all_permissions()
    admin.add_permission("view_security")
    admin.add_permission("manage_security")
    opts.admin_name = "maestro_admin"


def _make_ticket(**kwargs):
    from mojo.apps.incident.models import Ticket
    defaults = dict(title=f"{PREFIX} ticket", description="rest test", status="open")
    defaults.update(kwargs)
    return Ticket.objects.create(**defaults)


def _make_incident(**kwargs):
    from mojo.apps.incident.models import Incident
    defaults = dict(title=f"{PREFIX} incident", details="rest details", category="test", status="open")
    defaults.update(kwargs)
    return Incident.objects.create(**defaults)


def _make_link(source, item_id=9000, board_id=3):
    from mojo.apps.incident.models import MaestroItemLink
    field = source._meta.model_name
    return MaestroItemLink.objects.create(
        **{field: source},
        remote_integration_id=INTEGRATION,
        remote_item_id=item_id,
        remote_board_id=board_id,
    )


def _clear_jobs():
    from mojo.apps.jobs.models import Job
    Job.objects.filter(func__startswith="mojo.apps.incident.asyncjobs.maestro").delete()


def _jobs(name):
    from mojo.apps.jobs.models import Job
    return list(Job.objects.filter(
        func=f"mojo.apps.incident.asyncjobs.{name}").order_by("created"))


def _server_config():
    return th.server_settings(
        MAESTRO_API_KEY=TEST_KEY,
        MAESTRO_API_URL="https://maestro.example.test",
        MAESTRO_CALLBACK_BASE="https://client.example.test",
    )


@th.django_unit_test()
def test_legacy_board_setup_is_read_only(opts):
    from mojo.apps.incident.models import MaestroBoard, MaestroBoardLink

    board = MaestroBoard.objects.create(name=f"{PREFIX} legacy", is_active=False)
    ticket = _make_ticket(title=f"{PREFIX} legacy link")
    link = MaestroBoardLink.objects.create(
        ticket=ticket, maestro_board=board, remote_item_id=42)
    assert opts.client.login(opts.admin_name, PWORD), "admin login failed"
    response = opts.client.get(f"/api/incident/maestro/board/{board.pk}")
    assert response.status_code == 200, f"legacy rows must remain readable: {response.status_code}: {response.body}"
    response = opts.client.post("/api/incident/maestro/board", json={"name": f"{PREFIX} new"})
    assert response.status_code in (400, 403, 405), f"legacy setup creation must be disabled: {response.status_code}"
    response = opts.client.post(f"/api/incident/maestro/board/{board.pk}", json={"name": "changed"})
    assert response.status_code in (400, 403, 405), f"legacy setup updates must be disabled: {response.status_code}"
    response = opts.client.delete(f"/api/incident/maestro/link/{link.pk}")
    assert response.status_code in (400, 403, 405), (
        f"legacy link deletion must be disabled: {response.status_code}")


@th.django_unit_test()
def test_push_to_maestro_default_and_explicit_board(opts):
    ticket = _make_ticket(title=f"{PREFIX} manual")
    assert opts.client.login(opts.admin_name, PWORD), "admin login failed"

    with _server_config():
        response = opts.client.post(
            f"/api/incident/ticket/{ticket.pk}", json={"push_to_maestro": True})
    assert response.status_code == 200, f"default push action failed: {response.status_code}: {response.body}"
    jobs = _jobs("maestro_push_source")
    assert len(jobs) == 1, f"default push must enqueue once: {[j.payload for j in jobs]}"
    assert jobs[0].payload == {"source_kind": "ticket", "source_id": ticket.pk}, (
        f"default push must omit board: {jobs[0].payload}")

    _clear_jobs()
    with _server_config():
        response = opts.client.post(
            f"/api/incident/ticket/{ticket.pk}", json={"push_to_maestro": 3})
    assert response.status_code == 200, f"explicit push failed: {response.status_code}: {response.body}"
    assert _jobs("maestro_push_source")[0].payload.get("board_id") == 3, (
        "push_to_maestro=3 must mean remote Maestro board 3")

    _clear_jobs()
    with _server_config():
        response = opts.client.post(
            f"/api/incident/ticket/{ticket.pk}", json={"push_to_maestro": False})
    assert response.status_code == 400, f"false must not become board 0/1: {response.status_code}: {response.body}"
    assert not _jobs("maestro_push_source"), "invalid selector must not enqueue"


@th.django_unit_test()
def test_missing_setting_rejects_manual_push_without_disclosing_secret(opts):
    ticket = _make_ticket(title=f"{PREFIX} missing config")
    assert opts.client.login(opts.admin_name, PWORD), "admin login failed"
    with th.server_settings(MAESTRO_API_KEY=""):
        response = opts.client.post(
            f"/api/incident/ticket/{ticket.pk}", json={"push_to_maestro": True})
    assert response.status_code == 400, f"missing config must fail clearly: {response.status_code}: {response.body}"
    rendered = str(response.response)
    assert "MAESTRO_API_KEY" in rendered, f"response must name missing setting: {rendered}"
    assert TEST_KEY not in rendered, "response must not disclose a credential"


@th.django_unit_test()
def test_linked_ticket_edit_and_note_enqueue(opts):
    ticket = _make_ticket(title=f"{PREFIX} linked ticket")
    link = _make_link(ticket)
    assert opts.client.login(opts.admin_name, PWORD), "admin login failed"

    response = opts.client.get(f"/api/incident/maestro/item-link/{link.pk}")
    assert response.status_code == 200, f"item link must be readable: {response.status_code}: {response.body}"
    assert response.response["data"]["source_kind"] == "ticket", (
        f"item link must identify its local source: {response.response}")

    _clear_jobs()
    response = opts.client.post(
        f"/api/incident/ticket/{ticket.pk}",
        json={"title": f"{PREFIX} edited", "priority": 8},
    )
    assert response.status_code == 200, f"ticket edit failed: {response.status_code}: {response.body}"
    jobs = _jobs("maestro_sync_change")
    assert len(jobs) == 1 and jobs[0].payload.get("link_id") == link.pk, (
        f"linked edit must enqueue one source sync: {[j.payload for j in jobs]}")
    assert set(jobs[0].payload.get("changed", [])) == {"title", "priority"}, (
        f"changed fields missing: {jobs[0].payload}")

    _clear_jobs()
    response = opts.client.post(
        "/api/incident/ticket/note", json={"parent": ticket.pk, "note": "local comment"})
    assert response.status_code == 200, f"ticket note failed: {response.status_code}: {response.body}"
    jobs = _jobs("maestro_push_note")
    assert len(jobs) == 1, f"linked note must enqueue once: {[j.payload for j in jobs]}"
    assert jobs[0].payload.get("note_kind") == "ticket", f"note kind wrong: {jobs[0].payload}"


@th.django_unit_test()
def test_linked_incident_edit_history_delete_and_merge(opts):
    from mojo.apps.incident.models import MaestroItemLink

    incident = _make_incident(title=f"{PREFIX} linked incident")
    link = _make_link(incident, item_id=9100)
    assert opts.client.login(opts.admin_name, PWORD), "admin login failed"

    _clear_jobs()
    response = opts.client.post(
        f"/api/incident/incident/{incident.pk}",
        json={"title": f"{PREFIX} incident edited", "priority": 7},
    )
    assert response.status_code == 200, f"incident edit failed: {response.status_code}: {response.body}"
    assert _jobs("maestro_sync_change"), "linked incident edit must enqueue a source sync"
    assert _jobs("maestro_push_note"), "incident history must mirror as Maestro comments"

    response = opts.client.delete(f"/api/incident/incident/{incident.pk}")
    assert response.status_code == 400, f"linked incident delete must be refused: {response.status_code}: {response.body}"
    assert type(incident).objects.filter(pk=incident.pk).exists(), "refused delete must preserve incident"

    source = _make_incident(title=f"{PREFIX} merge source")
    source_link = _make_link(source, item_id=9101)
    target = _make_incident(title=f"{PREFIX} merge target")
    result = target.on_action_merge([source.pk])
    assert result == {"status": True}, f"single-link merge must succeed: {result}"
    source_link.refresh_from_db()
    assert source_link.incident_id == target.pk, "sole source link must transfer to merge target"

    other = _make_incident(title=f"{PREFIX} merge conflict")
    _make_link(other, item_id=9102)
    try:
        target.on_action_merge([other.pk])
        assert False, "two linked incidents must not merge ambiguously"
    except Exception as err:
        assert "unlink duplicates" in str(err), f"wrong merge conflict: {err}"
    assert type(other).objects.filter(pk=other.pk).exists(), "failed merge must make no partial deletion"
    assert MaestroItemLink.objects.filter(incident=target).count() == 1, "target link must remain intact"


@th.django_unit_test()
def test_fixed_webhook_signed_ticket_and_incident(opts):
    from mojo.helpers.crypto.sign import generate_signature, get_signature_header
    from mojo.apps.incident.models import IncidentHistory, TicketNote

    ticket = _make_ticket(title=f"{PREFIX} webhook ticket")
    incident = _make_incident(title=f"{PREFIX} webhook incident")
    _make_link(ticket, item_id=9200)
    _make_link(incident, item_id=9201)
    header = get_signature_header()

    ticket_payload = {
        "integration_id": INTEGRATION,
        "event": "note.created",
        "item": {"id": 9200, "board": 3},
        "note": {"id": 2, "text": "ticket update", "author": "Alice"},
    }
    incident_payload = {
        "integration_id": INTEGRATION,
        "event": "note.created",
        "item": {"id": 9201, "board": 3},
        "note": {"id": 3, "text": "incident update", "author": "Bob"},
    }
    with _server_config():
        response = opts.client.post(
            "/api/incident/maestro/webhook", json=ticket_payload,
            headers={header: generate_signature(ticket_payload, TEST_KEY)},
        )
        response2 = opts.client.post(
            "/api/incident/maestro/webhook", json=incident_payload,
            headers={header: generate_signature(incident_payload, TEST_KEY)},
        )
    assert response.status_code == 200, f"signed ticket callback failed: {response.status_code}: {response.body}"
    assert response2.status_code == 200, f"signed incident callback failed: {response2.status_code}: {response2.body}"
    assert TicketNote.objects.filter(parent=ticket, metadata__remote_note_id=2).exists(), (
        "ticket callback must create TicketNote")
    assert IncidentHistory.objects.filter(parent=incident, metadata__remote_note_id=3).exists(), (
        "incident callback must create IncidentHistory")

    with _server_config():
        rejected = opts.client.post(
            "/api/incident/maestro/webhook", json=ticket_payload,
            headers={header: "0" * 64},
        )
    assert rejected.status_code == 401, f"bad signature must fail closed: {rejected.status_code}"


@th.django_unit_test()
def test_rule_handlers_use_remote_board_and_default(opts):
    from objict import objict
    from mojo.apps.incident.handlers.event_handlers import MaestroHandler, TicketHandler
    from mojo.apps.incident.models import Ticket
    from mojo.apps.incident.services import maestro_sync

    incident = _make_incident(title=f"{PREFIX} rule incident")
    event = objict(
        title=f"{PREFIX} event", details="rule details", level=4,
        incident=incident, incident_id=incident.pk, metadata={}, pk=70001,
    )
    _clear_jobs()
    with mock.patch.object(maestro_sync, "get_config", return_value=("https://maestro.test", TEST_KEY)):
        assert MaestroHandler(None, board="3").run(event) is True, "maestro:// handler must succeed"
    job = _jobs("maestro_push_source")[0]
    assert job.payload == {"source_kind": "incident", "source_id": incident.pk, "board_id": 3}, (
        f"maestro:// board must be remote id: {job.payload}")

    _clear_jobs()
    with mock.patch.object(maestro_sync, "get_config", return_value=("https://maestro.test", TEST_KEY)):
        handler = TicketHandler(None, maestro="1", title=f"{PREFIX} rule ticket")
        assert handler.run(event) is True, "ticket:// maestro=1 handler must succeed"
    ticket = Ticket.objects.get(title=f"{PREFIX} rule ticket")
    assert ticket.group_id == incident.group_id, "rule Ticket must inherit Incident group"
    job = _jobs("maestro_push_source")[0]
    assert job.payload == {"source_kind": "ticket", "source_id": ticket.pk}, (
        f"maestro=1 must request server default: {job.payload}")

    _clear_jobs()
    event2 = objict(
        title=f"{PREFIX} event 2", details="", level=2,
        incident=None, incident_id=None, metadata={}, pk=70002,
    )
    assert TicketHandler(None, title=f"{PREFIX} local ticket").run(event2) is True, (
        "plain ticket:// must remain local-only")
    assert not _jobs("maestro_push_source"), "plain ticket:// must not enqueue Maestro reporting"


@th.django_unit_test()
def test_ticket_handler_dedupe_is_group_scoped_and_reused_ticket_can_push(opts):
    from objict import objict
    from mojo.apps.account.models import Group
    from mojo.apps.incident.handlers.event_handlers import TicketHandler
    from mojo.apps.incident.models import RuleSet, Ticket, TicketNote
    from mojo.apps.incident.services import maestro_sync

    group_a, _created = Group.objects.get_or_create(
        name=f"{PREFIX} group A", defaults={"kind": "default"})
    group_b, _created = Group.objects.get_or_create(
        name=f"{PREFIX} group B", defaults={"kind": "default"})
    ruleset = RuleSet.objects.create(
        name=f"{PREFIX} group-scoped ticket", category="test")
    first_incident = _make_incident(
        title=f"{PREFIX} first grouped incident", group=group_a, rule_set=ruleset)
    recurring_incident = _make_incident(
        title=f"{PREFIX} recurring grouped incident", group=group_a, rule_set=ruleset)
    other_group_incident = _make_incident(
        title=f"{PREFIX} other grouped incident", group=group_b, rule_set=ruleset)

    def event_for(incident, pk):
        return objict(
            title=f"{PREFIX} grouped event", details="group scope", level=4,
            incident=incident, incident_id=incident.pk, metadata={}, pk=pk,
        )

    assert TicketHandler(None, title=f"{PREFIX} grouped ticket").run(
        event_for(first_incident, 71001)) is True
    first_ticket = Ticket.objects.get(incident=first_incident)
    assert first_ticket.group_id == group_a.pk, "new Ticket must inherit its Incident group"

    _clear_jobs()
    with mock.patch.object(
        maestro_sync, "get_config", return_value=("https://maestro.test", TEST_KEY)
    ):
        assert TicketHandler(None, maestro="1").run(
            event_for(recurring_incident, 71002)) is True
    assert Ticket.objects.filter(group=group_a, incident__rule_set=ruleset).count() == 1, (
        "same-group RuleSet recurrence must reuse the unresolved Ticket")
    assert TicketNote.objects.filter(
        parent=first_ticket, metadata__incident_id=recurring_incident.pk).exists(), (
        "Ticket reuse must record the recurring Incident")
    assert _jobs("maestro_push_source")[0].payload["source_id"] == first_ticket.pk, (
        "a Maestro-enabled recurrence must push the reused Ticket")

    assert TicketHandler(None, title=f"{PREFIX} other group ticket").run(
        event_for(other_group_incident, 71003)) is True
    other_ticket = Ticket.objects.get(incident=other_group_incident)
    assert other_ticket.group_id == group_b.pk and other_ticket.pk != first_ticket.pk, (
        "the same RuleSet in another group must create a separate Ticket")


@th.django_unit_test()
def test_direct_link_prevents_resolution_delete(opts):
    from mojo.apps.incident.models import RuleSet

    ruleset = RuleSet.objects.create(
        name=f"{PREFIX} retention rules", category="test",
        metadata={"delete_on_resolution": True},
    )
    incident = _make_incident(
        title=f"{PREFIX} retained", status="resolved", rule_set=ruleset)
    _make_link(incident, item_id=9300)
    assert incident.check_delete_on_resolution() is False, "direct Maestro link must preserve resolved Incident"
    assert type(incident).objects.filter(pk=incident.pk).exists(), "linked Incident must remain in database"
