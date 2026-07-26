"""Maestro item 419 — a member-level manage_security grant in group A must not
reach group B's ticket.

VERDICT: this ALREADY fails closed. The item was filed as a verify-first
suspicion and the suspicion does not hold — but the reason is indirect, so this
module pins it rather than closing on an argument alone.

The chain, traced during scoping:

1. mojo/models/rest.py — create runs rest_check_permission_or_raise with NO
   instance, so SAVE_PERMS is satisfied against the CALLER's own group A. (This
   is where the original suspicion stopped, and it is correct as far as it goes.)
2. But `parent` is a relation field, so on_rest_save_field routes it to
   on_rest_save_related_field, whose scalar-id branch calls
   Ticket.rest_check_permission(request, "VIEW_PERMS", related_instance) — the
   FK-attach VIEW gate.
3. Ticket has no check_view_permission, no GROUP_FIELD, and no
   on_rest_related_save, so _evaluate_permission REBINDS request.group to the
   ticket's own group B and evaluates B.user_has_permission(M, view_security)
   -> False.
4. The denial is a SILENT SKIP (audit event + return), so `parent` is never
   set. TicketNote.parent is NOT NULL, so the save fails before on_rest_saved
   runs — dispatch_action is never reached.

Two conditions are therefore load-bearing, and each is pinned differently:
  (a) TicketNote.parent staying NOT NULL — caught only STRUCTURALLY. If it
      became nullable the note would simply save with a NULL parent and the
      behavioral assertion would still pass.
  (b) TicketNote not gaining a NO_FK_VIEW_CHECK_FIELDS entry for `parent` —
      caught behaviorally.

Note the current rejection surfaces as a 500 IntegrityError rather than a clean
403. Making it a clean rejection would mean changing the FK-denial path for
EVERY model, so it is deliberately out of scope; the assertions below check
"not 200" so they survive either outcome.

This module is opt-in (requires_extra: slow) — it does NOT run in the default
suite. Verify with:
    bin/run_tests --agent --extra slow -t test_incident.test_ticketnote_cross_tenant
"""
import uuid as _uuid
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

PASSWORD = "i419##Member99"


def _mk_user(email):
    from mojo.apps.account.models import User
    u = User.objects.create_user(username=email, email=email, password=PASSWORD)
    u.is_active = True
    u.is_email_verified = True
    u.requires_mfa = False
    u.save()
    # TRAP: Group.user_has_permission short-circuits on a USER-LEVEL grant
    # before it ever checks membership. A stray global perm would make this
    # whole module pass vacuously.
    u.remove_all_permissions()
    u.save()
    return u


@th.django_unit_setup()
def setup_ticketnote_cross_tenant(opts):
    from mojo.apps.account.models import User, Group, GroupMember
    from mojo.apps.incident.models import Ticket

    tag = _uuid.uuid4().hex[:8]
    User.objects.filter(email__startswith="i419_").delete()
    Group.objects.filter(name__startswith="i419_").delete()
    Ticket.objects.filter(title__startswith="i419_").delete()

    # TRAP: A and B must be SIBLINGS with parent=None. get_member_for_user
    # walks the ancestor chain, so nesting them would grant access legitimately
    # and the pin would prove nothing.
    group_a = Group.objects.create(name=f"i419_a_{tag}", kind="organization", parent=None)
    group_b = Group.objects.create(name=f"i419_b_{tag}", kind="organization", parent=None)
    opts.group_a_id = group_a.pk
    opts.group_b_id = group_b.pk

    member = _mk_user(f"i419_member_{tag}@example.com")
    opts.member_email = member.email
    opts.member_id = member.pk

    # Member-level manage_security in A ONLY — the grant the suspicion is about.
    ms = GroupMember(user=member, group=group_a)
    ms.save()
    ms.add_permission("manage_security")
    ms.add_permission("view_security")

    ticket_b = Ticket.objects.create(
        title=f"i419_victim_{tag}", description="other tenant's ticket",
        status="open", category="llm_review", group=group_b)
    opts.ticket_b_id = ticket_b.pk

    ticket_a = Ticket.objects.create(
        title=f"i419_own_{tag}", description="caller's own ticket",
        status="open", category="llm_review", group=group_a)
    opts.ticket_a_id = ticket_a.pk


def _login(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1", key="login")
    ok = opts.client.login(opts.member_email, PASSWORD)
    assert ok, f"login failed for {opts.member_email}: {opts.client.last_response.body}"


@th.django_unit_test("cross-tenant: a plain note on another tenant's ticket is rejected")
def test_plain_note_on_other_tenant_ticket_rejected(opts):
    from mojo.apps.incident.models import TicketNote

    before = TicketNote.objects.filter(parent_id=opts.ticket_b_id).count()
    _login(opts)
    # TRAP: pass the caller's group in the QUERY STRING. In the body it would
    # also become a savable field on the note; with no group context at all the
    # caller is denied at the CREATE gate and the FK gate is never exercised,
    # so the test would pass for the wrong reason.
    resp = opts.client.post(
        f"/api/incident/ticket/note?group={opts.group_a_id}",
        {"parent": opts.ticket_b_id, "note": "i419 cross-tenant attempt"})
    opts.client.logout()

    assert_true(resp.status_code != 200,
                f"a member of group A must not create a note on group B's ticket, "
                f"got {resp.status_code}: {resp.body}")
    after = TicketNote.objects.filter(parent_id=opts.ticket_b_id).count()
    assert_eq(after, before,
              f"no note may be attached to the other tenant's ticket, count went {before} -> {after}")


@th.django_unit_test("cross-tenant: an action_response on another tenant's ticket never dispatches")
def test_action_response_on_other_tenant_ticket_rejected(opts):
    from mojo.apps.incident.models import Ticket, TicketNote

    before = TicketNote.objects.filter(parent_id=opts.ticket_b_id).count()
    _login(opts)
    resp = opts.client.post(
        f"/api/incident/ticket/note?group={opts.group_a_id}",
        {
            "parent": opts.ticket_b_id,
            "note": "approved",
            "metadata": {
                "action_response": {
                    "handler": "incident.rule_approval",
                    "action": "approve",
                    "context": {"target": {"model": "incident.RuleSet", "pk": 1}},
                }
            },
        })
    opts.client.logout()

    assert_true(resp.status_code != 200,
                f"a structured action_response must not be accepted on another tenant's "
                f"ticket, got {resp.status_code}: {resp.body}")
    after = TicketNote.objects.filter(parent_id=opts.ticket_b_id).count()
    assert_eq(after, before,
              f"no note may be attached, so dispatch_action can never run; "
              f"count went {before} -> {after}")
    ticket_b = Ticket.objects.get(pk=opts.ticket_b_id)
    assert_eq(ticket_b.status, "open",
              f"the other tenant's ticket must not be resolved/closed by the attempt, "
              f"got status={ticket_b.status!r}")


@th.django_unit_test("control: the same member CAN note their own tenant's ticket")
def test_same_tenant_note_allowed(opts):
    from mojo.apps.incident.models import TicketNote

    _login(opts)
    resp = opts.client.post(
        f"/api/incident/ticket/note?group={opts.group_a_id}",
        {"parent": opts.ticket_a_id, "note": "i419 same-tenant control"})
    opts.client.logout()

    assert_eq(resp.status_code, 200,
              f"the member's manage_security grant in group A must still allow noting "
              f"group A's own ticket, got {resp.status_code}: {resp.body}")
    assert_true(TicketNote.objects.filter(parent_id=opts.ticket_a_id).exists(),
                "the same-tenant note should have been created — if this fails the "
                "cross-tenant assertions above prove nothing")


@th.django_unit_test("structural: the two conditions the cross-tenant denial relies on")
def test_denial_preconditions_hold(opts):
    """A nullable `parent` or a NO_FK_VIEW_CHECK_FIELDS entry would silently
    reopen the hole — the first is invisible to the behavioral tests above,
    because the note would just save with a NULL parent."""
    from mojo.apps.incident.models import TicketNote

    field = TicketNote._meta.get_field("parent")
    assert_true(not field.null,
                "TicketNote.parent must stay NOT NULL — it is what turns the silent "
                "FK-attach denial into a hard failure instead of a note with no parent")

    skipped = getattr(TicketNote.RestMeta, "NO_FK_VIEW_CHECK_FIELDS", ())
    assert_true("parent" not in skipped,
                "TicketNote must not exempt `parent` from the FK-attach VIEW check — "
                "that check is the entire cross-tenant gate")
