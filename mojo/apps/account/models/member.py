from django.db import models
from mojo.models import MojoModel
from mojo import errors as merrors
from mojo.helpers.settings import settings

MEMBER_PERMS_PROTECTION = settings.get("MEMBER_PERMS_PROTECTION", {})


class GroupMember(models.Model, MojoModel):
    """
    A member of a group
    """
    created = models.DateTimeField(auto_now_add=True, editable=False)
    modified = models.DateTimeField(auto_now=True, db_index=True)
    user = models.ForeignKey(
        "account.User",related_name="members",
        on_delete=models.CASCADE)
    group = models.ForeignKey(
        "account.Group", related_name="members",
        on_delete=models.CASCADE)
    is_active = models.BooleanField(default=True, db_index=True)
    # JSON-based permissions field
    permissions = models.JSONField(default=dict, blank=True)
    # JSON-based metadata field
    metadata = models.JSONField(default=dict, blank=True)

    class RestMeta:
        VIEW_PERMS = ["view_groups", "manage_groups"]
        SAVE_PERMS = ["manage_groups"]
        LIST_DEFAULT_FILTERS = {
            "is_active": True
        }
        GRAPHS = {
            "default": {
                "fields": [
                    'id',
                    'created',
                    'modified',
                    'is_active',
                    'permissions',
                    'metadata'
                ],
                "graphs": {
                    "user": "basic",
                    "group": "basic"
                }
            }
        }

    def __str__(self):
        return f"{self.user.username}@{self.group.name}"

    def can_change_permission(self, perm, value, request):
        if request.user.has_permission(["manage_groups", "manage_users"]):
            return True
        req_member = self.group.get_member_for_user(request.user)
        if req_member is not None:
            if perm in MEMBER_PERMS_PROTECTION:
                return req_member.has_permission(MEMBER_PERMS_PROTECTION[perm])
            return req_member.has_permission(["manage_group", "manage_members"])
        return False

    def set_permissions(self, value):
            if not isinstance(value, dict):
                return
            for perm, perm_value in value.items():
                if not self.can_change_permission(perm, perm_value, self.active_request):
                    raise merrors.PermissionDeniedException()
                if bool(perm_value):
                    self.add_permission(perm)
                else:
                    self.remove_permission(perm)

    def has_permission(self, perm_key):
        """Check if user has a specific permission in JSON field."""
        if isinstance(perm_key, list):
            for pk in perm_key:
                if self.has_permission(pk):
                    return True
            return False
        if perm_key == "all":
            return True
        return self.permissions.get(perm_key, False)

    def add_permission(self, perm_key, value=True):
        """Dynamically add a permission."""
        self.permissions[perm_key] = value
        self.save()

    def remove_permission(self, perm_key):
        """Remove a permission."""
        if perm_key in self.permissions:
            del self.permissions[perm_key]
            self.save()
