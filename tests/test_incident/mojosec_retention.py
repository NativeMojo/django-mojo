import datetime
import io
import json

from django.core.management import call_command
from django.core.management.base import CommandError

from testit import helpers as th


TITLE = "mojosec-retention-test"
SENSOR = "mojosec-retention-sensor"


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _clear():
    from mojo.apps.incident.models import Event, Incident, MojoSecReceipt

    MojoSecReceipt.objects.filter(sensor_id=SENSOR).delete()
    Event.objects.filter(title__startswith=TITLE).delete()
    Incident.objects.filter(category=TITLE).delete()


def _event(age_days, category="mojosec.web.probe", scope="mojosec",
           model_name="mojosec_sensor", incident=None):
    from mojo.apps.incident.models import Event

    event = Event.objects.create(
        scope=scope, category=category, level=8, title=f"{TITLE} {category}",
        model_name=model_name, incident=incident,
        metadata={"mojosec": {"sensor_id": SENSOR}})
    Event.objects.filter(pk=event.pk).update(
        created=_now() - datetime.timedelta(days=age_days))
    return event


def _receipt(opts, event, publish_state="published", handler_state="none"):
    from mojo.apps.incident.models import MojoSecReceipt

    return MojoSecReceipt.objects.create(
        api_key_id=opts.retention_key_id,
        event=event,
        sensor_id=SENSOR,
        wire_event_id=f"retention-{event.pk}",
        payload_digest="b" * 64,
        publish_state=publish_state,
        handler_state=handler_state,
        replay_features={"event": {"id": f"retention-{event.pk}"}},
    )


def _remaining(ids):
    from mojo.apps.incident.models import Event

    return set(Event.objects.filter(pk__in=ids).values_list("pk", flat=True))


def _dry_run(**options):
    output = io.StringIO()
    call_command("prune_mojosec_events", stdout=output, **options)
    return json.loads(output.getvalue().strip())


class _Job:
    def __init__(self):
        self.logs = []

    def add_log(self, message):
        self.logs.append(message)


@th.django_unit_setup()
def setup_mojosec_retention(opts):
    from mojo.apps.account.models import ApiKey, Group

    _clear()
    ApiKey.objects.filter(name="mojosec_retention_key").delete()
    Group.objects.filter(name="mojosec_retention_group").delete()
    group = Group.objects.create(name="mojosec_retention_group", kind="organization")
    key, unused_token = ApiKey.create_for_group(
        group, "mojosec_retention_key", permissions={"mojosec_ingest": True})
    opts.retention_key_id = key.pk


@th.django_unit_test("retention deletes aged finished and orphaned Events, keeps the rest")
def test_retention_keep_and_delete_rules(opts):
    from mojo.apps.incident.models import Incident
    from mojo.apps.incident.services import mojosec_retention

    _clear()
    orphan = _event(60)
    deleted = [orphan.pk]
    for handler_state in ("none", "dispatched", "dead"):
        event = _event(60, category="mojosec.fim.change")
        _receipt(opts, event, handler_state=handler_state)
        deleted.append(event.pk)

    incident = Incident.objects.create(category=TITLE, scope="mojosec", title="held")
    kept = {"incident": _event(60, incident=incident).pk}
    for name, publish_state, handler_state in (
            ("publish_pending", "pending", "none"),
            ("publish_dead", "dead", "none"),
            ("handler_pending", "published", "pending"),
            ("handler_queued", "published", "queued"),
            ("handler_failed", "published", "failed")):
        event = _event(60)
        _receipt(opts, event, publish_state=publish_state, handler_state=handler_state)
        kept[name] = event.pk
    kept["case_promotion"] = _event(60, model_name="mojosec_case").pk
    kept["other_scope"] = _event(60, scope="global").pk
    kept["other_model"] = _event(60, model_name="other_sensor").pk
    kept["recent"] = _event(10).pk

    count = mojosec_retention.prune_projected_events(now=_now())

    th.assert_true(count >= len(deleted), f"deleted {count}, expected at least {len(deleted)}")
    th.assert_eq(_remaining(deleted), set(), "an aged finished or orphaned Event survived")
    survivors = _remaining(kept.values())
    missing = sorted(name for name, pk in kept.items() if pk not in survivors)
    th.assert_eq(missing, [], "retention deleted an Event it must keep")
    _clear()


@th.django_unit_test("dry run deletes nothing and reports counts, range and kept reasons")
def test_retention_dry_run_reports_without_deleting(opts):
    from mojo.apps.incident.models import Incident

    _clear()
    # Older than any other row in this package, so the window holds only these.
    probes = [_event(500).pk for unused in range(3)]
    fim = _event(500, category="mojosec.fim.change").pk
    incident = Incident.objects.create(category=TITLE, scope="mojosec", title="held")
    held = _event(500, incident=incident).pk
    pending = _event(500)
    _receipt(opts, pending, publish_state="pending")

    result = _dry_run(older_than_days=400)

    th.assert_eq((result["schema"], result["version"], result["mode"]),
                 ("mojosec.event-prune", 1, "dry-run"), "wrong report header")
    th.assert_eq(result["would_delete"], 4, "would_delete is wrong")
    th.assert_eq(result["by_category"],
                 {"mojosec.web.probe": 3, "mojosec.fim.change": 1},
                 "per-category counts are wrong")
    th.assert_eq(result["kept"], {"has_incident": 1, "receipt_not_finished": 1},
                 "kept counts by reason are wrong")
    th.assert_eq((result["range"]["first_id"], result["range"]["last_id"]),
                 (min(probes + [fim]), max(probes + [fim])), "id range is wrong")
    th.assert_true(result["range"]["first_created"] is not None, "date range missing")
    th.assert_eq(len(_remaining(probes + [fim, held, pending.pk])), 6,
                 "a dry run deleted rows")
    _clear()


@th.django_unit_test("apply needs --max-events and the age floor is seven days")
def test_retention_command_refusals(opts):
    with th.assert_raises(CommandError):
        call_command("prune_mojosec_events", apply=True, stdout=io.StringIO())
    with th.assert_raises(CommandError):
        call_command("prune_mojosec_events", older_than_days=6, stdout=io.StringIO())
    with th.assert_raises(CommandError):
        call_command("prune_mojosec_events", apply=True, max_events=0,
                     stdout=io.StringIO())
    result = _dry_run(older_than_days=7)
    th.assert_eq(result["older_than_days"], 7, "seven days was refused")
    th.assert_eq(_dry_run()["older_than_days"], 45,
                 "the default is not the receipt retention")


@th.django_unit_test("apply deletes at most --max-events")
def test_retention_apply_honours_max_events(opts):
    from mojo.apps.incident.services import mojosec_retention

    # The daily job's switch is off here, so this also proves --apply is not gated.
    th.assert_true(mojosec_retention.deletion_enabled() is False,
                   "the test project turns MOJOSEC_EVENT_PRUNE_ENABLED on")
    _clear()
    ids = [_event(500).pk for unused in range(5)]

    output = io.StringIO()
    call_command("prune_mojosec_events", older_than_days=400, apply=True,
                 max_events=3, stdout=output)
    result = json.loads(output.getvalue().strip())

    th.assert_eq((result["mode"], result["deleted"]), ("apply", 3), "apply ignored the ceiling")
    th.assert_eq(len(_remaining(ids)), 2, "apply deleted past --max-events")
    _clear()


@th.django_unit_test("the job stops at its batch ceiling and logs that rows remain")
def test_retention_batch_ceiling(opts):
    from mojo.apps.incident.services import mojosec_retention

    _clear()
    ids = [_event(500).pk for unused in range(5)]
    job = _Job()

    count = mojosec_retention._prune_projected_events(
        job=job, days=400, batch=2, max_batches=2)

    th.assert_eq(count, 4, "the batch ceiling was not applied")
    th.assert_eq(len(_remaining(ids)), 1, "wrong rows remain after the ceiling")
    th.assert_true(job.logs and "more remain" in job.logs[-1],
                   f"the job log did not say rows remain: {job.logs}")
    _clear()


@th.django_unit_test("the daily job deletes nothing while the switch is off")
def test_daily_prune_off_by_default_deletes_nothing(opts):
    from mojo.apps.incident.models import Incident
    from mojo.apps.incident.services import mojosec_retention

    th.assert_true(mojosec_retention.deletion_enabled() is False,
                   "MOJOSEC_EVENT_PRUNE_ENABLED is not off by default")
    _clear()
    ids = [_event(500).pk for unused in range(3)]
    ids.append(_event(500, category="mojosec.fim.change").pk)
    incident = Incident.objects.create(category=TITLE, scope="mojosec", title="held")
    ids.append(_event(500, incident=incident).pk)
    job = _Job()

    count = mojosec_retention.daily_prune(job=job, days=400)

    th.assert_eq(count, 0, "the daily job deleted while the switch is off")
    th.assert_eq(_remaining(ids), set(ids), "rows were deleted while the switch is off")
    th.assert_true(job.logs and "deleted nothing" in job.logs[-1]
                   and "prune_mojosec_events" in job.logs[-1],
                   f"the job log did not say deletion is off: {job.logs}")
    _clear()


@th.django_unit_test("the daily job deletes as before once the switch is on")
def test_daily_prune_on_deletes_as_before(opts):
    from mojo.apps.incident.services import mojosec_retention

    _clear()
    ids = [_event(500).pk for unused in range(3)]
    job = _Job()

    count = mojosec_retention.daily_prune(job=job, days=400, enabled=True)

    th.assert_eq(count, 3, "the switched-on job did not delete the aged rows")
    th.assert_eq(_remaining(ids), set(), "aged rows remain with the switch on")
    th.assert_true(job.logs and job.logs[-1].startswith("Pruned 3 "),
                   f"the job log changed: {job.logs}")
    _clear()


@th.django_unit_test("a job queued under the old path also obeys the switch")
def test_old_job_path_obeys_the_switch(opts):
    from mojo.apps.jobs.job_engine import load_job_function

    old_entry = load_job_function(
        "mojo.apps.incident.services.mojosec_retention.prune_projected_events")

    _clear()
    ids = [_event(500).pk for unused in range(3)]
    job = _Job()
    count = old_entry(job, days=400)
    th.assert_eq(count, 0, "a job queued under the old path deleted while the switch is off")
    th.assert_eq(_remaining(ids), set(ids), "the old job path deleted rows while the switch is off")
    th.assert_true(job.logs and "deleted nothing" in job.logs[-1],
                   f"the old job path did not log that deletion is off: {job.logs}")

    job = _Job()
    count = old_entry(job, days=400, enabled=True)
    th.assert_eq(count, 3, "the old job path did not delete with the switch on")
    th.assert_eq(_remaining(ids), set(), "aged rows remain on the old job path with the switch on")
    th.assert_true(job.logs and job.logs[-1].startswith("Pruned 3 "),
                   f"the old job path's log changed: {job.logs}")
    _clear()


@th.django_unit_test("the 08:35 cron publishes the gated entry")
def test_cron_publishes_the_gated_entry(opts):
    from mojo.apps.incident import cronjobs
    from mojo.apps.incident.services import mojosec_retention
    from mojo.apps.jobs.job_engine import load_job_function

    th.assert_eq(cronjobs.MOJOSEC_EVENT_PRUNE_JOB,
                 "mojo.apps.incident.services.mojosec_retention.daily_prune",
                 "the cron does not publish the gated entry")
    th.assert_true(
        load_job_function(cronjobs.MOJOSEC_EVENT_PRUNE_JOB) is mojosec_retention.daily_prune,
        "the cron's job path does not load daily_prune")


@th.django_unit_test("sixty days at the production mix leave only the retention window")
def test_retention_sixty_day_scaled_mix(opts):
    from mojo.apps.incident.models import Event, MojoSecReceipt
    from mojo.apps.incident.services import mojosec, mojosec_retention

    _clear()
    now = _now()
    # August 2026: 55,638 of 55,815 receipts were web.probe or fim.change.
    # Ten rows a day, one in ten days an ssh login.
    rows = []
    for day in range(60):
        for slot in range(10):
            if slot == 0 and day % 10 == 0:
                category = "mojosec.auth.ssh_login"
            else:
                category = ("mojosec.web.probe", "mojosec.fim.change")[slot % 2]
            rows.append((day, Event(
                scope="mojosec", category=category, level=8,
                title=f"{TITLE} {category}", model_name="mojosec_sensor",
                metadata={"mojosec": {"sensor_id": SENSOR}})))
    events = Event.objects.bulk_create([event for unused, event in rows])
    receipts = []
    for (day, unused), event in zip(rows, events):
        created = now - datetime.timedelta(days=day, hours=12)
        Event.objects.filter(pk=event.pk).update(created=created)
        receipts.append(MojoSecReceipt(
            api_key_id=opts.retention_key_id, event=event, sensor_id=SENSOR,
            wire_event_id=f"retention-{event.pk}", payload_digest="c" * 64,
            publish_state="published", handler_state="none", published_at=created,
            replay_features={"event": {"id": f"retention-{event.pk}"}}))
    MojoSecReceipt.objects.bulk_create(receipts)
    ids = [event.pk for event in events]

    mojosec.prune_receipts(now=now)
    orphaned = Event.objects.filter(
        pk__in=ids, mojosec_receipts__isnull=True).count()
    th.assert_eq(orphaned, 150, "the receipt prune should leave 150 aged Events without a receipt")

    mojosec_retention.prune_projected_events(now=now)

    cutoff = now - datetime.timedelta(days=45)
    remaining = Event.objects.filter(pk__in=ids)
    th.assert_eq(remaining.count(), 450, "more or less than the 45-day window remains")
    th.assert_eq(remaining.filter(created__lt=cutoff).count(), 0,
                 "an Event older than the retention remains")
    th.assert_eq(remaining.filter(mojosec_receipts__isnull=True).count(), 0,
                 "an Event whose receipt is gone remains")
    _clear()


def _locks_or_writes(sql):
    return sql.startswith(("UPDATE ", "DELETE ")) or (
        sql.startswith("SELECT ") and " FOR UPDATE" in sql)


@th.django_unit_test("an Incident linked by another connection after the candidate read is kept")
def test_retention_keeps_event_linked_during_prune(opts):
    import threading
    from django.db import connection
    from mojo.apps.incident.models import Event, Incident
    from mojo.apps.incident.services import mojosec_retention

    _clear()
    event = _event(500)
    incident = Incident.objects.create(category=TITLE, scope="mojosec", title="linked late")
    state = {}

    def link():
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '2s'")
            Event.objects.get(pk=event.pk).link_to_incident(
                Incident.objects.get(pk=incident.pk))
            state["linked"] = True
        except Exception as error:
            state["error"] = repr(error)
        finally:
            connection.close()

    def interleave(execute, sql, params, many, context):
        # The candidate ids are already read. Commit the link on another
        # connection just before the prune first locks or writes a row.
        if "linked" not in state and "error" not in state and _locks_or_writes(sql):
            writer = threading.Thread(target=link)
            writer.start()
            writer.join(10)
        return execute(sql, params, many, context)

    with connection.execute_wrapper(interleave):
        count = mojosec_retention.prune_projected_events(days=400)

    th.assert_eq(state, {"linked": True}, "the second connection did not commit its link")
    th.assert_eq(count, 0, "the prune counted the Event linked during it")
    survivor = Event.objects.filter(pk=event.pk).first()
    th.assert_true(survivor is not None, "the prune deleted an Event an Incident now holds")
    th.assert_eq(survivor.incident_id, incident.pk, "the Incident link was lost")
    _clear()


@th.django_unit_test("a row another connection has locked is skipped without waiting")
def test_retention_skips_locked_row(opts):
    import threading
    from django.db import connection, transaction
    from mojo.apps.incident.models import Event
    from mojo.apps.incident.services import mojosec_retention

    _clear()
    locked = _event(500)
    others = [_event(500).pk for unused in range(2)]
    holding, release = threading.Event(), threading.Event()

    def hold():
        try:
            with transaction.atomic():
                list(Event.objects.select_for_update().filter(pk=locked.pk))
                holding.set()
                release.wait(20)
        finally:
            connection.close()

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        th.assert_true(holding.wait(10), "the second connection did not lock the row")
        with connection.cursor() as cursor:
            # A prune that waits on the lock fails here instead of hanging.
            cursor.execute("SET lock_timeout = '3s'")
        try:
            count = mojosec_retention.prune_projected_events(days=400)
        finally:
            with connection.cursor() as cursor:
                cursor.execute("RESET lock_timeout")
    finally:
        release.set()
        holder.join(20)

    th.assert_eq(count, 2, "the prune did not delete exactly the unlocked rows")
    th.assert_eq(_remaining(others), set(), "an unlocked aged row survived")
    th.assert_eq(_remaining([locked.pk]), {locked.pk}, "the locked row was deleted")
    _clear()
