from django.db import models

from mojo.models import MojoModel


class MojoSecDeployment(models.Model, MojoModel):
    """Operator/driver-side pre-registration of one deployment identity.

    Registered before a deploy by the party driving it — never by the node,
    whose journal has no network channel and whose assertions are root-trust
    by definition. When an enrollment sets ``require_registered_deployments``,
    trusted-FIM routing only honors annotations whose deployment_id has a
    live registration here; everything else falls to the untrusted
    immediate-Event path.
    """

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    installation_key = models.ForeignKey(
        "account.ApiKey", related_name="mojosec_deployments",
        on_delete=models.PROTECT)
    deployment_id = models.CharField(max_length=128, db_index=True)
    registered_by = models.ForeignKey(
        "account.User", null=True, blank=True, default=None,
        related_name="+", on_delete=models.SET_NULL)
    expires_at = models.DateTimeField(db_index=True)
    note = models.CharField(max_length=256, blank=True, default="")

    class Meta:
        ordering = ["-created", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=("installation_key", "deployment_id"),
                name="incident_msd_key_deploy_uniq"),
        ]

    class RestMeta:
        VIEW_PERMS = ["view_security", "manage_security", "security"]
        SAVE_PERMS = ["manage_security", "security"]
        CAN_CREATE = False
        CAN_UPDATE = False
        CAN_DELETE = False
        DENY_AI = True
        GRAPHS = {
            "default": {
                "fields": [
                    "id", "created", "modified", "deployment_id",
                    "expires_at", "note",
                ],
            },
        }
