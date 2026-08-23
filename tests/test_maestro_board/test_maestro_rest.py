"""REST/job/handler coverage for workspace-scoped Maestro reporting."""

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
    # The Maestro config these tests need is baked into the generated test
    # project (bin/create_testproject; MAESTRO_API_KEY == TEST_KEY), so there is
    # nothing to reload — no server_settings() freeze of the parallel workers
    # (maestro #2791). Kept as a no-op context so the call sites read unchanged.
    import contextlib
    return contextlib.nullcontext()


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


# test_missing_setting_rejects_manual_push_without_disclosing_secret moved to
# tests/test_maestro_board_extended_serial/missing_config.py (maestro #2791):
# with MAESTRO_API_KEY now baked into the test project, proving the
# missing-config path requires UNSETTING it, which is a server reload — legal
# only in the serial sibling.


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


# test_rule_handlers_use_remote_board_and_default and
# test_ticket_handler_dedupe_is_group_scoped_and_reused_ticket_can_push moved
# to tests/test_maestro_board_extended_serial/test_maestro_rest.py — they
# mock.patch the shared maestro_sync module in-process (maestro item #1839).


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
