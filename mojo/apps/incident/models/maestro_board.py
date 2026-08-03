from django.db import models
from mojo.models import MojoModel, MojoSecrets
from mojo.helpers import crypto


def generate_callback_token():
    return crypto.random_string(48, allow_special=False)


class MaestroBoard(MojoSecrets, MojoModel):
    """
    Legacy board-scoped Maestro configuration.

    Retained read-only so deployments can inspect pre-workspace-integration
    credentials and links. Active reporting uses MAESTRO_API_KEY in django.conf
    plus MaestroItemLink; no new runtime path should create or update this row.
    """
    class Meta:
        ordering = ['-created']

    class RestMeta:
        SENSITIVE_FIELDS = ['callback_token']
        VIEW_PERMS = ["manage_security", "security"]
        SAVE_PERMS = ["manage_security", "security"]
        DELETE_PERMS = ["manage_security"]
        # Legacy board-scoped credentials are retained for inspection only.
        # Active reporting is configured once per deployment in django.conf.
        CAN_CREATE = False
        CAN_UPDATE = False
        CAN_DELETE = False
        SEARCH_FIELDS = ["name"]
        JSON_REPLACE_FIELDS = ["status_map", "metadata"]
        # POST {"refresh_schema": 1} re-registers and refreshes the cached schema.
        # POST {"paste_url": "https://..."} routes through set_paste_url().
        POST_SAVE_ACTIONS = []
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
        from mojo.errors import ValueException
        raise ValueException(
            "MaestroBoard setup is legacy; configure MAESTRO_API_KEY in django.conf", 400)

    def on_rest_pre_save(self, changed_fields, created):
        from mojo.errors import ValueException
        raise ValueException(
            "MaestroBoard setup is read-only; configure MAESTRO_API_KEY in django.conf", 400)

    def on_action_refresh_schema(self, value):
        from mojo.errors import ValueException
        raise ValueException("legacy MaestroBoard schema refresh is unavailable", 400)

    def __str__(self):
        return f"MaestroBoard({self.pk}, {self.name or self.api_url})"
