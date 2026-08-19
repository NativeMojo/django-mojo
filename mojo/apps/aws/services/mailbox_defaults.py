"""The one locked writer for mailbox default flags.

"Exactly one system default" and "at most one default per domain" used to be
enforced by two independent implementations — an unlocked pair of ``.update()``
calls in ``Mailbox.on_rest_saved`` and a separate locked pair inside
``aws_setup.configure_email``. Two writers, one invariant, no shared lock.

These two helpers are now the ONLY supported way to set a default mailbox.
Each takes a row lock on every current holder before collapsing it, inside one
transaction, so two concurrent claims serialize instead of leaving two
defaults behind. They touch nothing but the two boolean flags: no AWS calls,
no request objects, no side effects beyond the claim itself.
"""

from django.db import transaction


def claim_system_default(mailbox):
    """Make ``mailbox`` the single system-wide default, atomically."""
    from mojo.apps.aws.models import Mailbox
    with transaction.atomic():
        holders = list(Mailbox.objects.select_for_update().filter(
            is_system_default=True).exclude(pk=mailbox.pk).order_by("pk"))
        if holders:
            Mailbox.objects.filter(
                pk__in=[row.pk for row in holders]).update(is_system_default=False)
        Mailbox.objects.filter(pk=mailbox.pk).update(is_system_default=True)
    mailbox.is_system_default = True
    return mailbox


def claim_domain_default(mailbox):
    """Make ``mailbox`` its domain's single default, atomically."""
    from mojo.apps.aws.models import Mailbox
    with transaction.atomic():
        holders = list(Mailbox.objects.select_for_update().filter(
            domain=mailbox.domain_id, is_domain_default=True
        ).exclude(pk=mailbox.pk).order_by("pk"))
        if holders:
            Mailbox.objects.filter(
                pk__in=[row.pk for row in holders]).update(is_domain_default=False)
        Mailbox.objects.filter(pk=mailbox.pk).update(is_domain_default=True)
    mailbox.is_domain_default = True
    return mailbox
