from django.db import models

from mojo import errors as me
from mojo.models import MojoModel


class AcmeHubDelegation(models.Model, MojoModel):
    """Permanent tenant allocation inside the hub-owned ACME DNS zone.

    Rows are tombstones: a ``client_ref`` is never reusable for a different
    domain and a generated target is never handed to another onboarding.  The
    original project UUID survives group deletion so a deleted tenant cannot
    later reclaim an old reference through a newly-created Group row.
    """

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    owner_project_uuid = models.CharField(max_length=200, db_index=True)
    client_ref = models.CharField(max_length=128)
    domain_name = models.CharField(max_length=253, db_index=True)
    source_name = models.CharField(max_length=253, db_index=True)
    target_name = models.CharField(max_length=253, unique=True)

    group = models.ForeignKey(
        "account.Group", related_name="acme_hub_delegations",
        null=True, blank=True, default=None, on_delete=models.SET_NULL)

    allocation_zone_name = models.CharField(max_length=253)
    allocation_zone_id = models.CharField(max_length=128)

    class Meta:
        db_table = "dnsman_acme_hub_delegation"
        constraints = [
            models.UniqueConstraint(
                fields=["owner_project_uuid", "client_ref"],
                name="dnsman_acme_hub_owner_client_uniq"),
        ]
        indexes = [
            models.Index(fields=["owner_project_uuid", "domain_name"]),
        ]
        ordering = ["created"]

    class RestMeta:
        CAN_CREATE = False
        CAN_UPDATE = False
        CAN_DELETE = False
        VIEW_PERMS = ["view_dns", "manage_dns", "security"]
        SAVE_PERMS = ["manage_dns", "security"]
        DENY_AI = True
        SENSITIVE_FIELDS = ["allocation_zone_id"]

    _IMMUTABLE_FIELDS = (
        "owner_project_uuid", "client_ref", "domain_name", "source_name",
        "target_name", "group_id", "allocation_zone_name", "allocation_zone_id",
    )

    def save(self, *args, **kwargs):
        if self.pk:
            stored = type(self).objects.filter(pk=self.pk).values(
                *self._IMMUTABLE_FIELDS).first()
            if stored is not None:
                for field in self._IMMUTABLE_FIELDS:
                    if stored[field] != getattr(self, field):
                        raise me.ValueException("ACME hub allocations are immutable")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise me.ValueException("ACME hub allocations are permanent tombstones")

    def __str__(self):
        return f"{self.owner_project_uuid}:{self.client_ref} -> {self.target_name}"
