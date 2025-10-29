"""
Flow-style example that demonstrates:
- sharing expensive fixtures through opts
- guarding costly work with @requires_extra
- asserting on system behaviour rather than recreating business logic
"""

from testit import helpers as th
from testit.helpers import assert_eq


@th.django_unit_setup()
def setup_billing_account(opts):
    """Provision shared billing records once for this module."""
    from django.utils import timezone
    from billing.models import Account

    opts.now = timezone.now()
    opts.billing_account = Account.objects.create(
        name="Acme Pro",
        plan="pro",
        billing_email="billing@example.com",
        rebill_at=opts.now,
    )
    opts.rebill_endpoint = f"/api/billing/{opts.billing_account.id}/rebill"


@th.django_unit_test()
def test_rebill_is_deferred_without_flag(opts):
    """The rebill endpoint should refuse to run without an explicit override."""
    response = opts.client.post(opts.rebill_endpoint, json={})
    assert_eq(response.status_code, 409, "rebill should defer without explicit approval")


@th.requires_extra("run-billing-jobs")
@th.django_unit_test()
def test_rebill_runs_when_flagged(opts):
    """Real job enqueues only when --extra run-billing-jobs is present."""
    response = opts.client.post(
        opts.rebill_endpoint,
        json={"scheduled_for": opts.now.isoformat()},
    )
    assert_eq(response.status_code, 202, "rebill should enqueue when flagged")


@th.django_unit_test("highlight missing API")
def test_can_request_statement_export(opts):
    """
    If this test starts to implement business logic, pause and raise the design issue.
    The goal is to call the framework/API, not re-create its behaviour inside the test.
    """
    response = opts.client.post(f"/api/billing/{opts.billing_account.id}/export")
    assert_eq(
        response.status_code,
        200,
        "billing export endpoint should exist; raise a design issue if it does not",
    )
