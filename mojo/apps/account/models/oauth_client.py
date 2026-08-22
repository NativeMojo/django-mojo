from django.db import models

from mojo.models import MojoModel


class OAuthClient(models.Model, MojoModel):
    """
    A client of this installation's OAuth 2.1 authorization server.

    Two kinds, both public (no client secret is ever issued):

      - ``dcr``  — RFC 7591 Dynamic Client Registration. ``client_id`` is a
        random hex string this server minted.
      - ``cimd`` — Client ID Metadata Document. ``client_id`` IS the https URL
        of the client's published metadata document; the row is a cache of
        what that document said, refreshed on resolve.

    Rows are managed entirely by ``services/oauth_server`` — there is no REST
    CRUD endpoint. The RestMeta exists so ``to_dict`` graphs are defined for
    the Admin surface and so the Assistant is denied the model outright.

    Deactivation (``is_active = False``) is the kill switch: it is checked
    before any metadata fetch or write, so re-resolving a deactivated CIMD
    client can never silently re-activate it.
    """
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    client_id = models.CharField(max_length=512, unique=True, db_index=True)
    kind = models.CharField(max_length=16, default="dcr")
    client_name = models.CharField(max_length=200, default="", blank=True)
    redirect_uris = models.JSONField(default=list, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    last_used = models.DateTimeField(null=True, default=None)

    class Meta:
        ordering = ["-created"]

    class RestMeta:
        VIEW_PERMS = ["manage_users", "users"]
        SAVE_PERMS = ["manage_users", "users"]
        CAN_CREATE = False
        CAN_UPDATE = False
        CAN_DELETE = False
        DENY_AI = True
        GRAPHS = {
            "default": {
                "fields": [
                    "id", "client_id", "kind", "client_name", "redirect_uris",
                    "is_active", "last_used", "created",
                ],
            },
        }

    def __str__(self):
        return f"{self.client_name or self.client_id} ({self.kind})"
