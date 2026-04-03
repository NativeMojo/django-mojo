"""
Tests for incident pruning job.

Covers: prune_incidents deletes old resolved/closed/ignored incidents,
respects do_not_delete, and skips active statuses.
"""
from testit import helpers as th

CATEGORY = "test_prune_inc"


@th.django_unit_setup()
def setup_prune_incidents(opts):
    from mojo.apps.incident.models import Incident
    from django.utils import timezone
    from datetime import timedelta

    # Clean up from previous runs
    Incident.objects.filter(category__startswith=CATEGORY).delete()

    old = timezone.now() - timedelta(days=120)

    # Old resolved incident — should be pruned
    opts.old_resolved = Incident.objects.create(
        category=CATEGORY, title="Old resolved", status="resolved", metadata={})
    Incident.objects.filter(pk=opts.old_resolved.pk).update(created=old)

    # Old closed incident — should be pruned
    opts.old_closed = Incident.objects.create(
        category=CATEGORY, title="Old closed", status="closed", metadata={})
    Incident.objects.filter(pk=opts.old_closed.pk).update(created=old)

    # Old ignored incident — should be pruned
    opts.old_ignored = Incident.objects.create(
        category=CATEGORY, title="Old ignored", status="ignored", metadata={})
    Incident.objects.filter(pk=opts.old_ignored.pk).update(created=old)

    # Old resolved with do_not_delete — should NOT be pruned
    opts.old_protected = Incident.objects.create(
        category=CATEGORY, title="Old protected", status="resolved",
        metadata={"do_not_delete": True})
    Incident.objects.filter(pk=opts.old_protected.pk).update(created=old)

    # Old open incident — should NOT be pruned (active status)
    opts.old_open = Incident.objects.create(
        category=CATEGORY, title="Old open", status="open", metadata={})
    Incident.objects.filter(pk=opts.old_open.pk).update(created=old)

    # Old investigating incident — should NOT be pruned
    opts.old_investigating = Incident.objects.create(
        category=CATEGORY, title="Old investigating", status="investigating", metadata={})
    Incident.objects.filter(pk=opts.old_investigating.pk).update(created=old)

    # Recent resolved incident — should NOT be pruned (too new)
    opts.recent_resolved = Incident.objects.create(
        category=CATEGORY, title="Recent resolved", status="resolved", metadata={})


@th.django_unit_test()
def test_prune_deletes_old_terminal(opts):
    """prune_incidents deletes old resolved, closed, and ignored incidents."""
    from mojo.apps.incident.models import Incident
    from mojo.apps.incident.asyncjobs import prune_incidents
    from objict import objict

    job = objict(logs=[])
    job.add_log = lambda msg: job.logs.append(msg)

    prune_incidents(job)

    assert not Incident.objects.filter(pk=opts.old_resolved.pk).exists(), \
        "Old resolved incident should be pruned"
    assert not Incident.objects.filter(pk=opts.old_closed.pk).exists(), \
        "Old closed incident should be pruned"
    assert not Incident.objects.filter(pk=opts.old_ignored.pk).exists(), \
        "Old ignored incident should be pruned"
    assert len(job.logs) >= 1, "Job should log pruning activity"
    assert "Pruned" in job.logs[0], f"Expected pruning log, got: {job.logs[0]}"


@th.django_unit_test()
def test_prune_skips_do_not_delete(opts):
    """prune_incidents skips incidents with do_not_delete=True."""
    from mojo.apps.incident.models import Incident

    assert Incident.objects.filter(pk=opts.old_protected.pk).exists(), \
        "Protected incident should survive pruning"


@th.django_unit_test()
def test_prune_skips_active_statuses(opts):
    """prune_incidents skips open and investigating incidents regardless of age."""
    from mojo.apps.incident.models import Incident

    assert Incident.objects.filter(pk=opts.old_open.pk).exists(), \
        "Old open incident should survive pruning"
    assert Incident.objects.filter(pk=opts.old_investigating.pk).exists(), \
        "Old investigating incident should survive pruning"


@th.django_unit_test()
def test_prune_skips_recent(opts):
    """prune_incidents skips recent incidents even if resolved."""
    from mojo.apps.incident.models import Incident

    assert Incident.objects.filter(pk=opts.recent_resolved.pk).exists(), \
        "Recent resolved incident should survive pruning"
