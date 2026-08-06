from django.db import models

from mojo.models import MojoModel


STATE_PENDING = "pending"
STATE_ACTIVE = "active"
STATE_WITHDRAWN = "withdrawn"
STATE_EXPIRED = "expired"


class AcmeHubChallengeLease(models.Model, MojoModel):
    """Durable desired TXT values for one downstream ACME challenge."""

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    allocation = models.ForeignKey(
        "dnsman.AcmeHubDelegation", related_name="challenge_leases",
        on_delete=models.PROTECT)
    challenge_ref = models.CharField(max_length=128)
    record_values = models.JSONField(default=list)
    state = models.CharField(max_length=24, default=STATE_PENDING, db_index=True)
    expires_at = models.DateTimeField(db_index=True)
    reconciled_at = models.DateTimeField(null=True, blank=True, default=None, db_index=True)
    last_error = models.TextField(null=True, blank=True, default=None)

    class Meta:
        db_table = "dnsman_acme_hub_challenge_lease"
        constraints = [
            models.UniqueConstraint(
                fields=["allocation", "challenge_ref"],
                name="dnsman_acme_hub_alloc_challenge_uniq"),
        ]
        indexes = [
            models.Index(fields=["allocation", "state"]),
            models.Index(fields=["state", "expires_at"]),
            models.Index(fields=["allocation", "reconciled_at"]),
        ]
        ordering = ["created"]

    class RestMeta:
        CAN_CREATE = False
        CAN_UPDATE = False
        CAN_DELETE = False
        VIEW_PERMS = ["view_dns", "manage_dns", "security"]
        SAVE_PERMS = ["manage_dns", "security"]
        DENY_AI = True
        SENSITIVE_FIELDS = ["record_values"]

    def __str__(self):
        return f"{self.allocation_id}:{self.challenge_ref} [{self.state}]"
