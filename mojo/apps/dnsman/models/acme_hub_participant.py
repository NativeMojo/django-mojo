import uuid

from django.db import models

from mojo import errors as me
from mojo.models import MojoModel


class AcmeHubParticipant(models.Model, MojoModel):
    """Permanent project-local reference and explicit certificate issuance grant."""

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)
    allocation = models.ForeignKey(
        "dnsman.AcmeHubDelegation", related_name="participants", on_delete=models.PROTECT)
    participant_ref = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    consumer_project_uuid = models.CharField(max_length=200, db_index=True)
    client_ref = models.CharField(max_length=128)
    group = models.ForeignKey(
        "account.Group", related_name="acme_hub_participants", null=True,
        blank=True, default=None, on_delete=models.SET_NULL)
    role = models.CharField(max_length=16, choices=[("owner", "owner"), ("trusted", "trusted")])
    revoked_at = models.DateTimeField(null=True, blank=True, default=None, db_index=True)

    class Meta:
        db_table = "dnsman_acme_hub_participant"
        constraints = [models.UniqueConstraint(
            fields=["consumer_project_uuid", "client_ref"],
            name="dnsman_acme_hub_consumer_client_uniq")]
        ordering = ["pk"]

    class RestMeta:
        CAN_CREATE = False
        CAN_UPDATE = False
        CAN_DELETE = False
        VIEW_PERMS = ["view_dns", "manage_dns", "security"]
        SAVE_PERMS = ["manage_dns", "security"]
        DENY_AI = True
        SENSITIVE_FIELDS = ["consumer_project_uuid", "client_ref", "group", "participant_ref"]
        GRAPHS = {"default": {"fields": ["id", "created", "modified", "role", "revoked_at"]}}

    _IMMUTABLE_FIELDS = (
        "allocation_id", "participant_ref", "consumer_project_uuid", "client_ref", "group_id", "role")

    def save(self, *args, **kwargs):
        if self.pk:
            stored = type(self).objects.filter(pk=self.pk).values(
                *self._IMMUTABLE_FIELDS, "revoked_at").first()
            if stored:
                if any(stored[field] != getattr(self, field) for field in self._IMMUTABLE_FIELDS):
                    raise me.ValueException("ACME hub participants are immutable")
                if stored["revoked_at"] is not None and self.revoked_at != stored["revoked_at"]:
                    raise me.ValueException("ACME hub revocation is permanent")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise me.ValueException("ACME hub participants are permanent tombstones")
