import uuid

from django.db import models
from django.db.models import Q

from mojo.models import MojoModel


STATE_RESERVED = "reserved"
STATE_CLEANUP_PENDING = "cleanup_pending"
STATE_RELEASED = "released"
LIVE_STATES = (STATE_RESERVED, STATE_CLEANUP_PENDING)


class DnsRecordReservation(models.Model, MojoModel):
    """Durable exclusive ownership of one provider DNS record set.

    ACME stores its complete TXT intent before touching the provider. Human
    DNS writes lock this row and fail closed while that intent is live, so an
    apex/wildcard challenge cannot be replaced halfway through validation.
    """

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    domain = models.ForeignKey(
        "dnsman.Domain", related_name="record_reservations",
        on_delete=models.CASCADE)
    certificate = models.ForeignKey(
        "dnsman.Certificate", related_name="record_reservations",
        null=True, blank=True, default=None, on_delete=models.SET_NULL)
    owner_ref = models.UUIDField(default=uuid.uuid4, editable=False, db_index=True)
    record_type = models.CharField(max_length=16)
    record_name = models.CharField(max_length=253)
    record_values = models.JSONField(default=list, blank=True)
    state = models.CharField(max_length=24, default=STATE_RESERVED, db_index=True)
    mutation_attempted = models.BooleanField(default=False)
    mutation_proven = models.BooleanField(default=False)
    last_error = models.TextField(null=True, blank=True, default=None)

    class Meta:
        db_table = "dnsman_record_reservation"
        ordering = ["record_name", "record_type"]
        indexes = [
            models.Index(fields=["domain", "state"]),
            models.Index(fields=["certificate", "state"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["domain", "record_type", "record_name"],
                condition=Q(state__in=LIVE_STATES),
                name="dnsman_record_reservation_live_uniq"),
        ]

    class RestMeta:
        CAN_CREATE = False
        CAN_DELETE = False
        GROUP_FIELD = "domain__group"
        VIEW_PERMS = ["view_dns", "manage_dns", "security"]
        SAVE_PERMS = ["security"]
        NO_SAVE_FIELDS = [
            "id", "pk", "created", "domain", "certificate", "owner_ref",
            "record_type", "record_name", "record_values", "state",
            "mutation_attempted", "mutation_proven", "last_error",
        ]
        GRAPHS = {
            "default": {"fields": [
                "id", "created", "modified", "record_type", "record_name",
                "state", "mutation_attempted", "mutation_proven", "last_error",
            ], "graphs": {"domain": "basic", "certificate": "basic"}},
        }

    def __str__(self):
        return f"{self.record_name} {self.record_type} [{self.state}]"
