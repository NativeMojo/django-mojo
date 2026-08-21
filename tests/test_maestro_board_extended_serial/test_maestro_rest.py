"""Rule-handler Maestro tests moved from
tests/test_maestro_board/test_maestro_rest.py — they mock.patch the shared
mojo.apps.incident.services.maestro_sync module around direct in-process
handler calls, which is unsafe under the parallel default tier (maestro item
#1839). Runs opt-in (`extended`) and serial.
"""

from unittest import mock

from testit import helpers as th

PREFIX = "[maestro_rest]"
TEST_KEY = "rest" + "k" * 44


@th.django_unit_setup()
def setup_maestro_rest_serial(opts):
    from mojo.apps.incident.models import Incident, MaestroItemLink, Ticket
    from mojo.apps.jobs.models import Job

    MaestroItemLink.objects.all().delete()
    Ticket.objects.filter(title__startswith=PREFIX).delete()
    Incident.objects.filter(title__startswith=PREFIX).delete()
    Job.objects.filter(func__startswith="mojo.apps.incident.asyncjobs.maestro").delete()


def _make_incident(**kwargs):
    from mojo.apps.incident.models import Incident
    defaults = dict(title=f"{PREFIX} incident", details="rest details", category="test", status="open")
    defaults.update(kwargs)
    return Incident.objects.create(**defaults)


def _clear_jobs():
    from mojo.apps.jobs.models import Job
    Job.objects.filter(func__startswith="mojo.apps.incident.asyncjobs.maestro").delete()


def _jobs(name):
    from mojo.apps.jobs.models import Job
    return list(Job.objects.filter(
        func=f"mojo.apps.incident.asyncjobs.{name}").order_by("created"))


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
