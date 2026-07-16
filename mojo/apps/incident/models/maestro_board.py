from django.db import models
from mojo.models import MojoModel, MojoSecrets
from mojo.helpers import crypto


def generate_callback_token():
    return crypto.random_string(48, allow_special=False)


class MaestroBoard(MojoSecrets, MojoModel):
    """
    A registered link to a remote maestro board (the client half of maestro's
    board link API — DM-040).

    An admin pastes a maestro link URL (https://<host>/api/boards/link/<key>);
    the save parses it, stores the raw key in mojo_secrets, and registers
    synchronously against maestro's link/register endpoint — a bad paste never
    persists. The cached name/schema come from the register response.

    status_map (optional) maps ticket status onto a board category column:
        {"column": "<column slug>", "map": {"<ticket status>": "<option value>"}}
    Ticket status only syncs (either direction) when this is configured.
    """
    class Meta:
        ordering = ['-created']

    class RestMeta:
        VIEW_PERMS = ["manage_security", "security"]
        SAVE_PERMS = ["manage_security", "security"]
        DELETE_PERMS = ["manage_security"]
        CAN_DELETE = True
        SEARCH_FIELDS = ["name"]
        JSON_REPLACE_FIELDS = ["status_map", "metadata"]
        # POST {"refresh_schema": 1} re-registers and refreshes the cached schema.
        # POST {"paste_url": "https://..."} routes through set_paste_url().
        POST_SAVE_ACTIONS = ["refresh_schema"]
        GRAPHS = {
            "basic": {
                "fields": ["id", "name", "remote_board_id", "sync_notes", "is_active"]
            },
            "default": {
                "exclude": ["mojo_secrets"],  # Never expose the link key
                "graphs": {
                    "group": "basic"
                }
            },
        }

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    group = models.ForeignKey("account.Group", blank=True, null=True, default=None, related_name="+", on_delete=models.SET_NULL)

    name = models.CharField(max_length=200, blank=True, default="")
    # API base parsed from the pasted link URL, e.g. https://maestromojo.com
    api_url = models.CharField(max_length=255, blank=True, default="")
    remote_board_id = models.IntegerField(blank=True, null=True, default=None)
    # Cached register response: {"label": ..., "columns": [...]}
    schema = models.JSONField(default=dict, blank=True)
    status_map = models.JSONField(blank=True, null=True, default=None)
    sync_notes = models.BooleanField(default=True)
    # Unguessable path segment of this project's webhook callback URL —
    # generated before registration so the callback can be submitted with
    # the register call (the row has no pk yet at that point).
    callback_token = models.CharField(max_length=64, unique=True, db_index=True, default=generate_callback_token)
    is_active = models.BooleanField(default=True, db_index=True)
    metadata = models.JSONField(default=dict, blank=True)

    # link_key — the raw maestro link key, stored in mojo_secrets

    def set_paste_url(self, value):
        from mojo.apps.incident.services import maestro_sync
        api_url, key = maestro_sync.parse_paste_url(value)
        self.api_url = api_url
        self.set_secret("link_key", key)
        self.__needs_register__ = True

    def on_rest_pre_save(self, changed_fields, created):
        if created or getattr(self, "__needs_register__", False):
            from mojo.apps.incident.services import maestro_sync
            maestro_sync.register(self)
            self.__needs_register__ = False

    def on_action_refresh_schema(self, value):
        from mojo.apps.incident.services import maestro_sync
        maestro_sync.register(self)
        self.save(update_fields=["name", "remote_board_id", "schema", "modified"])
        return {
            "status": True,
            "data": {
                "name": self.name,
                "remote_board_id": self.remote_board_id,
                "schema": self.schema,
            },
        }

    def __str__(self):
        return f"MaestroBoard({self.pk}, {self.name or self.api_url})"
