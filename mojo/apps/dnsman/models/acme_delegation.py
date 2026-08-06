import uuid

from django.db import models

from mojo import errors as me
from mojo.models import MojoModel


STATE_PENDING = "pending"
STATE_VERIFIED = "verified"
STATE_BROKEN = "broken"
STATE_RETIRED = "retired"


class AcmeDelegation(models.Model, MojoModel):
    """Tenant-side permanent allocation for delegated ACME DNS-01.

    The group, user and Domain relations are conveniences.  The snapshots and
    allocation identifiers are the durable ownership record and survive
    deletion of any related row.  Allocated rows are tombstones and cannot be
    deleted or repointed.
    """

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    domain = models.ForeignKey(
        "dnsman.Domain", related_name="acme_delegations",
        null=True, blank=True, default=None, on_delete=models.SET_NULL)
    group = models.ForeignKey(
        "account.Group", related_name="acme_delegations",
        null=True, blank=True, default=None, on_delete=models.SET_NULL)
    user = models.ForeignKey(
        "account.User", related_name="acme_delegations",
        null=True, blank=True, default=None, on_delete=models.SET_NULL)

    tenant_uuid = models.CharField(max_length=200, db_index=True)
    tenant_name = models.CharField(max_length=200)
    user_email = models.CharField(max_length=254, null=True, blank=True, default=None)
    domain_name = models.CharField(max_length=253, db_index=True)

    client_ref = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    source_name = models.CharField(max_length=253, null=True, blank=True, default=None)
    target_name = models.CharField(
        max_length=253, null=True, blank=True, default=None, unique=True)

    state = models.CharField(max_length=24, default=STATE_PENDING, db_index=True)
    verified_at = models.DateTimeField(null=True, blank=True, default=None)
    retired_at = models.DateTimeField(null=True, blank=True, default=None)
    last_error_code = models.CharField(max_length=64, null=True, blank=True, default=None)

    # A publish timeout is ambiguous: the hub may have committed the TXT set.
    # This intent is saved before publish and cleared only after withdrawal.
    cleanup_challenge_ref = models.CharField(
        max_length=128, null=True, blank=True, default=None)
    cleanup_requested_at = models.DateTimeField(null=True, blank=True, default=None)

    class Meta:
        db_table = "dnsman_acme_delegation"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant_uuid", "domain_name"],
                condition=~models.Q(state=STATE_RETIRED),
                name="dnsman_acme_tenant_domain_live_uniq"),
        ]
        indexes = [
            models.Index(fields=["tenant_uuid", "state"]),
            models.Index(fields=["domain", "state"]),
        ]
        ordering = ["-created"]

    class RestMeta:
        CAN_CREATE = False
        CAN_UPDATE = False
        CAN_DELETE = False
        GROUP_FIELD = "group"
        VIEW_PERMS = ["view_dns", "manage_dns", "security"]
        SAVE_PERMS = ["manage_dns", "security"]
        DENY_AI = True
        SENSITIVE_FIELDS = [
            "client_ref", "tenant_uuid", "tenant_name", "user_email",
            "cleanup_challenge_ref", "cleanup_requested_at",
        ]

    _IMMUTABLE_AFTER_ALLOCATION = (
        "client_ref", "tenant_uuid", "tenant_name", "user_email",
        "domain_name", "domain_id", "group_id", "user_id", "source_name", "target_name",
    )

    def save(self, *args, **kwargs):
        if self.pk:
            stored = type(self).objects.filter(pk=self.pk).values(
                *self._IMMUTABLE_AFTER_ALLOCATION).first()
            if stored is not None:
                allocated = bool(stored["source_name"] or stored["target_name"])
                for field in self._IMMUTABLE_AFTER_ALLOCATION:
                    # source/target are written once, after client_ref was
                    # already committed and the hub allocation returns.
                    if field in ("source_name", "target_name") and not allocated:
                        continue
                    # An external name has no globally unique Domain until
                    # authoritative proof succeeds.  Bind that relation once.
                    if field == "domain_id" and stored[field] is None:
                        continue
                    if stored[field] != getattr(self, field):
                        raise me.ValueException("ACME delegations are immutable tombstones")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise me.ValueException("ACME delegations are permanent tombstones")

    @property
    def is_sticky(self):
        return self.verified_at is not None

    def __str__(self):
        return f"{self.domain_name} [{self.state}]"
