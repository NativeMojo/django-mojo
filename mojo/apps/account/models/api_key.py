import hashlib
import json
from django.db import models
from mojo.models import MojoModel
from mojo.models.secrets import MojoSecrets
from mojo.helpers import crypto, dates


class ApiKey(MojoSecrets, MojoModel):
    """
    A group-scoped API key for programmatic access.

    Keys authenticate via:  Authorization: apikey <token>

    The raw token is generated on creation, returned once in the REST response
    or via create_for_group(), and never stored — only its SHA-256 hash is kept
    in token_hash for fast indexed lookup.

    Permissions are explicit (JSON dict, same shape as GroupMember.permissions).
    System-level permissions (sys.*) are always denied regardless of what is in
    the permissions field.

    Rate limit overrides per endpoint are stored in limits:
        {"assess": {"limit": 500, "window": 60}}   # window in minutes
    """
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    group = models.ForeignKey(
        "account.Group", related_name="api_keys", on_delete=models.CASCADE)
    name = models.CharField(max_length=200)
    is_active = models.BooleanField(default=True, db_index=True)

    token_hash = models.CharField(max_length=64, db_index=True, unique=True, null=True, default=None)

    permissions = models.JSONField(default=dict, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    limits = models.JSONField(default=dict, blank=True)

    last_used = models.DateTimeField(null=True, default=None)
    expires_at = models.DateTimeField(null=True, default=None, blank=True)

    class Meta:
        ordering = ["-created"]

    class RestMeta:
        VIEW_PERMS = ["manage_group", "manage_groups"]
        SAVE_PERMS = ["manage_group", "manage_groups"]
        CAN_DELETE = True
        GRAPHS = {
            "default": {
                "fields": [
                    "id", "created", "modified", "name",
                    "is_active", "permissions", "limits",
                    "last_used", "expires_at", "metadata"
                ],
                "extra": [("get_token", "token")],
                "graphs": {
                    "group": "basic",
                }
            }
        }

    @property
    def display_name(self):
        return self.name

    @property
    def email(self):
        return f"{self.name}@apikey"

    def __str__(self):
        return f"{self.name}@{self.group}"

    def _get_permissions_dict(self):
        """Return permissions as a dict, handling string values from REST input."""
        perms = self.permissions
        if isinstance(perms, str):
            try:
                perms = json.loads(perms)
            except (json.JSONDecodeError, ValueError):
                return {}
        if not isinstance(perms, dict):
            return {}
        return perms

    def has_permission(self, perm_key):
        """
        Check if this API key grants the given permission.

        Mirrors GroupMember.has_permission — sys.* permissions escalate to the
        user's system-level permissions in GroupMember, but API keys have no
        backing user so sys.* is always denied.

        - sys.* always returns False (no system-level escalation)
        - "all" returns True
        - Supports list/set for OR logic
        - Otherwise checks self.permissions dict
        """
        if isinstance(perm_key, (list, set)):
            return any(self.has_permission(p) for p in perm_key)
        if isinstance(perm_key, str) and perm_key.startswith("sys."):
            return False
        if perm_key == "all":
            return True
        return bool(self._get_permissions_dict().get(perm_key, False))

    def is_group_allowed(self, group):
        """
        Returns True if the given group is this key's own group or a descendant.
        Used by the dispatcher to validate group= request param for API key requests.
        """
        if group is None:
            return False
        if group.pk == self.group.pk:
            return True
        return group.is_child_of(self.group)

    def get_groups(self, is_active=True, include_children=True):
        """
        Returns a QuerySet of groups accessible to this API key.

        An API key is scoped to its own group and, when include_children is True,
        all descendant groups. The is_active argument is accepted for interface
        compatibility with User.get_groups() but has no effect — API key group
        access is determined solely by the group hierarchy, not member activity.

        Args:
            is_active: Accepted for interface compatibility. Not used.
            include_children: Include descendant groups (default True).

        Returns:
            QuerySet of Group objects.
        """
        from mojo.apps.account.models import Group

        if not include_children:
            return Group.objects.filter(pk=self.group_id)

        all_ids = set([self.group_id])
        all_ids.update(self.group._get_all_child_ids())
        return Group.objects.filter(id__in=all_ids)

    def get_groups_with_permission(self, perms, is_active=True):
        """
        Returns a QuerySet of groups accessible to this API key where the key
        has the specified permission(s).

        If the API key has the permission, returns the same result as get_groups().
        If not, returns an empty QuerySet.

        Args:
            perms: Permission key (str) or list of permission keys to check (OR logic).
            is_active: Accepted for interface compatibility. Not used.

        Returns:
            QuerySet of Group objects.
        """
        from mojo.apps.account.models import Group

        if not self.has_permission(perms):
            return Group.objects.none()
        return self.get_groups()

    def generate_token(self):
        """
        Generate a new raw token, store its hash, and return the raw token.
        The raw token is never stored and cannot be recovered after this call.
        """
        token = crypto.random_string(48, allow_special=False)
        self.token_hash = hashlib.sha256(token.encode()).hexdigest()
        self.set_secret("token", token)
        return token

    @classmethod
    def create_for_group(cls, group, name, permissions=None, limits=None):
        """
        Create a new ApiKey for a group programmatically.

        Returns (api_key, raw_token). The raw_token must be stored by the caller
        — it cannot be recovered after this call.

        Args:
            group:       account.Group instance
            name:        Human-readable label (e.g. "Mobile App v2")
            permissions: Dict of {perm_key: True/False}
            limits:      Dict of {endpoint_key: {limit, window}} (window in minutes)
        """
        api_key = cls(
            group=group,
            name=name,
            permissions=permissions or {},
            limits=limits or {},
        )
        token = api_key.generate_token()
        api_key.save()
        return api_key, token

    @classmethod
    def validate_token(cls, token, request):
        """
        Validate an API key token and populate request.group and request.api_key.

        Called by AuthenticationMiddleware for 'Authorization: apikey <token>'.

        Returns (ApiKeyUser, None) on success or (None, error_string) on failure.
        """
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        try:
            api_key = cls.objects.select_related("group").get(token_hash=token_hash)
        except cls.DoesNotExist:
            return None, "Invalid API key"

        if not api_key.is_active:
            return None, "API key is inactive"

        if api_key.expires_at and dates.utcnow() > api_key.expires_at:
            return None, "API key has expired"

        request.group = api_key.group
        request.api_key = api_key

        try:
            cls.objects.filter(pk=api_key.pk).update(last_used=dates.utcnow())
        except Exception:
            pass

        api_key.is_authenticated = True
        api_key.username = f"apikey:{api_key.id}"

        return api_key, None

    def get_token(self):
        """Returns the raw token from encrypted storage."""
        return self.get_secret("token")

    def on_rest_created(self):
        """Generate token, store hash for lookup, store raw token encrypted."""
        self._raw_token = self.generate_token()
        self.save()
