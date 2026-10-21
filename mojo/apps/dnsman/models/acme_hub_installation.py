"""Installation authority; deliberately has no tenant or REST lifetime."""
import uuid

from django.db import models
from django.db.models import Q

from mojo import errors as me
from mojo.models import MojoModel


class AcmeHubInstallation(models.Model, MojoModel):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, editable=False, db_index=True)
    installation_uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    feature_epoch = models.PositiveIntegerField(default=1, editable=False)
    mode = models.CharField(max_length=16, default="compatibility", editable=False)
    activation_uuid = models.UUIDField(null=True, default=None, editable=False)
    deployment_uuid = models.UUIDField(null=True, default=None, editable=False)
    app_sha = models.CharField(max_length=40, default="", editable=False)
    minimum_release = models.CharField(max_length=64, default="", editable=False)
    source_sha = models.CharField(max_length=40, default="", editable=False)
    wheel_sha256 = models.CharField(max_length=64, default="", editable=False)
    manifest_sha256 = models.CharField(max_length=64, default="", editable=False)
    roster_sha256 = models.CharField(max_length=64, default="", editable=False)
    attestation_sha256 = models.CharField(max_length=64, default="", editable=False)
    activated_at = models.DateTimeField(null=True, default=None, editable=False)

    class Meta:
        db_table = "dnsman_acme_hub_installation"
        constraints = [
            models.CheckConstraint(condition=Q(id=1), name="dnsman_hub_installation_singleton"),
            models.CheckConstraint(
                condition=(Q(mode="compatibility", activation_uuid__isnull=True,
                             deployment_uuid__isnull=True, activated_at__isnull=True,
                             app_sha="", minimum_release="", source_sha="", wheel_sha256="",
                             manifest_sha256="", roster_sha256="", attestation_sha256="") |
                           (Q(mode="enabled", activation_uuid__isnull=False,
                              deployment_uuid__isnull=False, activated_at__isnull=False) &
                            ~Q(app_sha="") & ~Q(minimum_release="") & ~Q(source_sha="") &
                            ~Q(wheel_sha256="") & ~Q(manifest_sha256="") &
                            ~Q(roster_sha256="") & ~Q(attestation_sha256=""))),
                name="dnsman_hub_installation_state"),
        ]

    class RestMeta:
        CAN_CREATE = False
        CAN_UPDATE = False
        CAN_DELETE = False
        VIEW_PERMS = ["security"]
        SAVE_PERMS = ["security"]
        DENY_AI = True
        GRAPHS = {}

    def save(self, *args, **kwargs):
        raise me.PermissionDeniedException("ACME installation state has a dedicated operator writer")

    def delete(self, *args, **kwargs):
        raise me.PermissionDeniedException("ACME installation state cannot be deleted")
