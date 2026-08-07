"""Focused REST contracts for scoped TicketNote and IncidentHistory media."""

from testit import helpers as th
from testit.helpers import assert_eq, assert_true


USERNAME = "i1487_operator"
OTHER_USERNAME = "i1487_other"
PASSWORD = "i1487##Media99"
MEDIA_ERROR = "Media must reference an active, completed File in the record scope"
REFERENCE_FIELDS = ["category", "content_type", "filename", "id"]


def _file(opts, filename, **overrides):
    from mojo.apps.fileman.models import File

    values = {
        "filename": filename,
        "content_type": "image/png",
        "category": "image",
        "file_size": 32,
        "upload_status": File.COMPLETED,
        "is_active": True,
        "file_manager_id": opts.manager_a_id,
        "user_id": opts.operator_id,
        "group_id": opts.group_a_id,
    }
    values.update(overrides)
    return File.objects.create(**values)


def _login(opts, username=USERNAME):
    from mojo.decorators.limits import clear_rate_limits

    clear_rate_limits(ip="127.0.0.1", key="login")
    ok = opts.client.login(username, PASSWORD)
    assert_true(ok, f"login must succeed for {username}: {opts.client.last_response.body}")


@th.django_unit_setup()
def setup_record_media_relations(opts):
    from mojo.apps.account.models import Group, GroupMember, User
    from mojo.apps.fileman.models import File, FileManager
    from mojo.apps.incident.models import Incident, IncidentHistory, Ticket, TicketNote

    TicketNote.objects.filter(parent__title__startswith="i1487_").delete()
    IncidentHistory.objects.filter(parent__title__startswith="i1487_").delete()
    Ticket.objects.filter(title__startswith="i1487_").delete()
    Incident.objects.filter(title__startswith="i1487_").delete()
    File.objects.filter(filename__startswith="i1487_").delete()
    FileManager.objects.filter(name__startswith="i1487_").delete()
    GroupMember.objects.filter(group__name__startswith="i1487_").delete()
    Group.objects.filter(name__startswith="i1487_").delete()
    User.objects.filter(username__in=[USERNAME, OTHER_USERNAME]).delete()

    operator = User.objects.create_user(
        username=USERNAME, email=f"{USERNAME}@example.com", password=PASSWORD)
    operator.is_active = True
    operator.is_email_verified = True
    operator.requires_mfa = False
    operator.save()
    operator.remove_all_permissions()

    other = User.objects.create_user(
        username=OTHER_USERNAME, email=f"{OTHER_USERNAME}@example.com", password=PASSWORD)
    other.is_active = True
    other.is_email_verified = True
    other.requires_mfa = False
    other.save()
    other.remove_all_permissions()

    group_a = Group.objects.create(name="i1487_group_a", kind="organization", parent=None)
    group_b = Group.objects.create(name="i1487_group_b", kind="organization", parent=None)
    for group in (group_a, group_b):
        member = GroupMember.objects.create(user=operator, group=group)
        member.add_permission("manage_security")
        member.add_permission("view_security")

    manager_a = FileManager.objects.create(
        name="i1487_manager_a", backend_type="file", backend_url="file://",
        is_active=True, group=group_a)
    manager_b = FileManager.objects.create(
        name="i1487_manager_b", backend_type="file", backend_url="file://",
        is_active=True, group=group_b)
    manager_inactive = FileManager.objects.create(
        name="i1487_manager_inactive", backend_type="file", backend_url="file://",
        is_active=False, group=group_a)

    ticket = Ticket.objects.create(
        title="i1487_ticket", description="attachment contract", group=group_a)
    incident = Incident.objects.create(
        title="i1487_incident", details="attachment contract",
        category="i1487_test", group=group_a)

    opts.operator_id = operator.pk
    opts.other_id = other.pk
    opts.group_a_id = group_a.pk
    opts.group_b_id = group_b.pk
    opts.manager_a_id = manager_a.pk
    opts.manager_b_id = manager_b.pk
    opts.manager_inactive_id = manager_inactive.pk
    opts.ticket_id = ticket.pk
    opts.incident_id = incident.pk


@th.django_unit_test("record media: TicketNote derives group and emits the safe File reference")
def test_ticket_note_media_happy_path(opts):
    from mojo.apps.incident.models import TicketNote

    media = _file(opts, "i1487_ticket_happy.png")
    _login(opts)
    resp = opts.client.post(
        f"/api/incident/ticket/note?group={opts.group_a_id}",
        {
            "media": media.pk,
            "parent": opts.ticket_id,
            "group": opts.group_b_id,
            "note": "i1487 preserve this note text",
        })
    opts.client.logout()

    assert_eq(resp.status_code, 200, f"valid ticket media must attach: {resp.response}")
    note = TicketNote.objects.get(pk=resp.response.data.id)
    assert_eq(note.group_id, opts.group_a_id,
              "TicketNote group must be derived from its parent before the first save")
    assert_eq(note.note, "i1487 preserve this note text",
              "adding media must preserve the required note text")
    assert_eq(note.media_id, media.pk, "the validated File must be attached")
    assert_eq(sorted(resp.response.data.media.keys()), REFERENCE_FIELDS,
              "TicketNote media must expose only the exact File reference graph")
    assert_eq(resp.response.data.media.id, media.pk,
              "the reference projection must identify the attached File")


@th.django_unit_test("record media: omitted media is preserved and explicit null clears it")
def test_ticket_note_media_only_validates_when_supplied(opts):
    from mojo.apps.incident.models import TicketNote

    media = _file(opts, "i1487_ticket_update.png")
    note = TicketNote.objects.create(
        parent_id=opts.ticket_id, group_id=opts.group_a_id,
        note="i1487 original", media=media)
    media.is_active = False
    media.save(update_fields=["is_active"])

    _login(opts)
    omitted = opts.client.post(
        f"/api/incident/ticket/note/{note.pk}?group={opts.group_a_id}",
        {"note": "i1487 media omitted"})
    cleared = opts.client.post(
        f"/api/incident/ticket/note/{note.pk}?group={opts.group_a_id}",
        {"media": None})
    opts.client.logout()

    assert_eq(omitted.status_code, 200,
              f"omitting media must not revalidate an existing relation: {omitted.response}")
    assert_eq(omitted.response.data.media.id, media.pk,
              "omitting media must preserve the existing relation")
    assert_eq(cleared.status_code, 200,
              f"explicit null must clear without validating the old File: {cleared.response}")
    note.refresh_from_db()
    assert_true(note.media_id is None, "explicit null must clear TicketNote.media")


@th.django_unit_test("record media: TicketNote rejects every invalid lifecycle and scope candidate")
def test_ticket_note_media_policy_failures(opts):
    from mojo.apps.fileman.models import File
    from mojo.apps.incident.models import TicketNote

    candidates = [
        _file(opts, "i1487_uploading.png", upload_status=File.UPLOADING),
        _file(opts, "i1487_inactive.png", is_active=False),
        _file(opts, "i1487_inactive_manager.png", file_manager_id=opts.manager_inactive_id),
        _file(
            opts, "i1487_wrong_file_group.png", group_id=opts.group_b_id,
            file_manager_id=opts.manager_b_id),
        _file(opts, "i1487_wrong_manager_group.png", file_manager_id=opts.manager_b_id),
    ]
    _login(opts)
    for candidate in candidates:
        before = TicketNote.objects.filter(parent_id=opts.ticket_id).count()
        resp = opts.client.post(
            f"/api/incident/ticket/note?group={opts.group_a_id}",
            {"parent": opts.ticket_id, "note": "i1487 rejected", "media": candidate.pk})
        assert_eq(resp.status_code, 400,
                  f"invalid media candidate {candidate.pk} must return 400: {resp.response}")
        assert_eq(resp.response.error, MEDIA_ERROR,
                  "all lifecycle and record-scope failures must be non-oracular")
        assert_eq(TicketNote.objects.filter(parent_id=opts.ticket_id).count(), before,
                  f"candidate {candidate.pk} must not create a TicketNote")
    opts.client.logout()


@th.django_unit_test("record media: hidden, missing, and deleted Files remain uniformly unavailable")
def test_ticket_note_media_file_visibility_failures(opts):
    from mojo.apps.incident.models import TicketNote

    hidden = _file(opts, "i1487_hidden.png", user_id=opts.other_id)
    deleted = _file(opts, "i1487_deleted.png")
    deleted_id = deleted.pk
    deleted.delete()
    before = TicketNote.objects.filter(parent_id=opts.ticket_id).count()

    _login(opts)
    responses = [
        opts.client.post(
            f"/api/incident/ticket/note?group={opts.group_a_id}",
            {"parent": opts.ticket_id, "note": "i1487 hidden", "media": hidden.pk}),
        opts.client.post(
            f"/api/incident/ticket/note?group={opts.group_a_id}",
            {"parent": opts.ticket_id, "note": "i1487 missing", "media": 2147483647}),
        opts.client.post(
            f"/api/incident/ticket/note?group={opts.group_a_id}",
            {"parent": opts.ticket_id, "note": "i1487 deleted", "media": deleted_id}),
    ]
    opts.client.logout()

    for resp in responses:
        assert_eq(resp.status_code, 403, f"unavailable File must return 403: {resp.response}")
        assert_eq(resp.response.error, "File unavailable",
                  "hidden, missing, and deleted File ids must share one response")
    assert_eq(TicketNote.objects.filter(parent_id=opts.ticket_id).count(), before,
              "unavailable File ids must not create notes")


@th.django_unit_test("record media: IncidentHistory validates parent scope without rewriting provenance")
def test_incident_history_media_and_provenance(opts):
    from mojo.apps.incident.models import IncidentHistory

    media = _file(opts, "i1487_history_happy.png")
    _login(opts)
    resp = opts.client.post(
        f"/api/incident/incident/history?group={opts.group_a_id}",
        {
            "parent": opts.incident_id,
            "group": opts.group_b_id,
            "kind": "evidence",
            "note": "i1487 history evidence",
            "media": media.pk,
        })
    opts.client.logout()

    assert_eq(resp.status_code, 200, f"valid incident media must attach: {resp.response}")
    history = IncidentHistory.objects.get(pk=resp.response.data.id)
    assert_eq(history.group_id, opts.group_b_id,
              "IncidentHistory group is provenance and must not be rewritten from its parent")
    assert_eq(history.media_id, media.pk, "the validated history File must be attached")
    assert_eq(sorted(resp.response.data.media.keys()), REFERENCE_FIELDS,
              "IncidentHistory media must expose only the exact File reference graph")


@th.django_unit_test("record media: IncidentHistory rejects cross-scope media without a row")
def test_incident_history_media_scope_failure(opts):
    from mojo.apps.incident.models import IncidentHistory

    media = _file(
        opts, "i1487_history_wrong_scope.png", group_id=opts.group_b_id,
        file_manager_id=opts.manager_b_id)
    before = IncidentHistory.objects.filter(parent_id=opts.incident_id).count()
    _login(opts)
    resp = opts.client.post(
        f"/api/incident/incident/history?group={opts.group_a_id}",
        {
            "parent": opts.incident_id,
            "kind": "evidence",
            "note": "i1487 rejected history evidence",
            "media": media.pk,
        })
    opts.client.logout()

    assert_eq(resp.status_code, 400,
              f"cross-scope incident media must return 400: {resp.response}")
    assert_eq(resp.response.error, MEDIA_ERROR,
              "history scope failure must use the bounded media response")
    assert_eq(IncidentHistory.objects.filter(parent_id=opts.incident_id).count(), before,
              "rejected history media must not create an audit row")


@th.django_unit_test("record media: File deletion preserves IncidentHistory provenance")
def test_incident_history_survives_file_deletion(opts):
    from mojo.apps.incident.models import Incident, IncidentHistory

    media = _file(opts, "i1487_history_deleted_later.png")
    history = IncidentHistory.objects.create(
        parent=Incident.objects.get(pk=opts.incident_id), group_id=opts.group_b_id,
        kind="evidence", note="i1487 durable provenance", media=media)
    history_id = history.pk
    media.delete()

    history = IncidentHistory.objects.get(pk=history_id)
    assert_true(history.media_id is None,
                "deleting a File must clear media rather than deleting IncidentHistory")
    assert_eq(history.note, "i1487 durable provenance",
              "File deletion must preserve the audit note")
    assert_eq(history.group_id, opts.group_b_id,
              "File deletion must preserve the audit group provenance")


@th.django_unit_setup()
def cleanup_record_media_relations(opts):
    from mojo.apps.account.models import Group, GroupMember, User
    from mojo.apps.fileman.models import File, FileManager
    from mojo.apps.incident.models import Incident, IncidentHistory, Ticket, TicketNote

    TicketNote.objects.filter(parent_id=opts.ticket_id).delete()
    IncidentHistory.objects.filter(parent_id=opts.incident_id).delete()
    Ticket.objects.filter(pk=opts.ticket_id).delete()
    Incident.objects.filter(pk=opts.incident_id).delete()
    File.objects.filter(filename__startswith="i1487_").delete()
    FileManager.objects.filter(name__startswith="i1487_").delete()
    GroupMember.objects.filter(group_id__in=[opts.group_a_id, opts.group_b_id]).delete()
    Group.objects.filter(pk__in=[opts.group_a_id, opts.group_b_id]).delete()
    User.objects.filter(pk__in=[opts.operator_id, opts.other_id]).delete()
