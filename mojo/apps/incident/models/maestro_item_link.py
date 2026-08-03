from django.db import models
from django.db.models import Q

from mojo.models import MojoModel


class MaestroItemLink(models.Model, MojoModel):
    """A remote Maestro item associated with one local incident record.

    The Maestro connection itself is deployment configuration.  This table
    stores only the cardinal state: which remote item represents a Ticket or an
    Incident.  Exactly one local source is present on every row.
    """

    class Meta:
        ordering = ["-created"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(ticket__isnull=False, incident__isnull=True)
                    | Q(ticket__isnull=True, incident__isnull=False)
                ),
                name="incident_maestro_link_one_source",
            ),
            models.UniqueConstraint(
                fields=["ticket"],
                condition=Q(ticket__isnull=False),
                name="incident_maestro_link_ticket_unique",
            ),
            models.UniqueConstraint(
                fields=["incident"],
                condition=Q(incident__isnull=False),
                name="incident_maestro_link_incident_unique",
            ),
            models.UniqueConstraint(
                fields=["remote_integration_id", "remote_item_id"],
                name="incident_maestro_link_remote_unique",
            ),
        ]

    class RestMeta:
        VIEW_PERMS = ["view_security", "security"]
        SAVE_PERMS = ["manage_security", "security"]
        DELETE_PERMS = ["manage_security"]
        CAN_CREATE = False
        CAN_UPDATE = False
        CAN_DELETE = True
        GRAPHS = {
            "default": {
                "extra": ["source_kind", "source_id"],
            },
        }

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    ticket = models.ForeignKey(
        "incident.Ticket", related_name="maestro_links", blank=True, null=True,
        default=None, on_delete=models.PROTECT,
    )
    incident = models.ForeignKey(
        "incident.Incident", related_name="maestro_links", blank=True, null=True,
        default=None, on_delete=models.PROTECT,
    )

    remote_integration_id = models.CharField(max_length=100, db_index=True)
    remote_item_id = models.BigIntegerField(db_index=True)
    remote_board_id = models.BigIntegerField(blank=True, null=True, default=None, db_index=True)
    remote_url = models.CharField(max_length=500, blank=True, default="")
    last_synced = models.DateTimeField(blank=True, null=True, default=None)

    @property
    def source_kind(self):
        return "ticket" if self.ticket_id else "incident"

    @property
    def source_id(self):
        return self.ticket_id or self.incident_id

    @property
    def source(self):
        return self.ticket if self.ticket_id else self.incident

    def __str__(self):
        return (
            f"MaestroItemLink({self.source_kind}={self.source_id}, "
            f"integration={self.remote_integration_id}, item={self.remote_item_id})"
        )
