from django.db import models
from mojo.models import MojoModel
from mojo import errors as merrors
from mojo.helpers.settings import settings
from mojo.helpers import dates

MEMBER_PERMS_PROTECTION = settings.get("MEMBER_PERMS_PROTECTION", {})
USER_LAST_ACTIVITY_FREQ = settings.get("USER_LAST_ACTIVITY_FREQ", 300)
METRICS_TIMEZONE = settings.get("METRICS_TIMEZONE", "America/Los_Angeles")
METRICS_TRACK_USER_ACTIVITY = settings.get("METRICS_TRACK_USER_ACTIVITY", False)

class GroupMember(models.Model, MojoModel):
    """
    A member of a group
    """
    created = models.DateTimeField(auto_now_add=True, editable=False)
    modified = models.DateTimeField(auto_now=True, db_index=True)
    last_activity = models.DateTimeField(default=None, null=True, db_index=True)

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
        VIEW_PERMS = ["view_members", "view_groups", "manage_groups", "manage_group"]
        SAVE_PERMS = ["manage_groups", "manage_group"]
        SEARCH_FIELDS = ["user__username", "user__email", "user__display_name"]
        POST_SAVE_ACTIONS = ['resend_invite']
        CREATED_BY_OWNER_FIELD = 'created_by'  # we do this to protect user
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
                    "user": "default",
                    "group": "basic"
                }
            }
        }

    def __str__(self):
        return f"{self.user.username}@{self.group.name}"

    @property
    def username(self):
        return self.user.username

    @property
    def display_name(self):
        return self.user.display_name

    @property
    def email(self):
        return self.user.email

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
        """
        Check if user has a specific permission—supports system-level permissions via 'sys.' prefix.
        If perm_key starts with 'sys.', only the user-level permission is checked.
        Otherwise, checks group-member-level permission as before.
        """
        # Support lists and sets for "OR" logic
        if isinstance(perm_key, (list, set)):
            for pk in perm_key:
                if self.has_permission(pk):
                    return True
            return False

        # System-level: only check user permission
        SYS_PREFIX = "sys."
        if isinstance(perm_key, str) and perm_key.startswith(SYS_PREFIX):
            bare_perm = perm_key[len(SYS_PREFIX):]
            return self.user.has_permission(bare_perm)

        if perm_key == "all":
            return True
        return self.permissions.get(perm_key, False)

    def add_permission(self, perm_key, value=True):
        """Dynamically add a permission."""
        if isinstance(perm_key, (list, set)):
            for pk in perm_key:
                self.add_permission(pk, value)
        else:
            self.permissions[perm_key] = value
        self.save()

    def remove_permission(self, perm_key):
        """Remove a permission."""
        if perm_key in self.permissions:
            del self.permissions[perm_key]
            self.save()

    def touch(self):
        from mojo.apps import metrics
        # can't subtract offset-naive and offset-aware datetimes
        if self.last_activity is None or dates.has_time_elsapsed(self.last_activity, seconds=USER_LAST_ACTIVITY_FREQ):
            if self.last_activity and not dates.is_today(self.last_activity, METRICS_TIMEZONE):
                metrics.record(
                    "member_activity_day",
                    min_granularity="days",
                    account=f"group-{self.group.pk}"
                )
            self.last_activity = dates.utcnow()
            self.save(update_fields=['last_activity'])
        if METRICS_TRACK_USER_ACTIVITY:
            metrics.record(f"member_activity:{self.pk}", category="member", min_granularity="minutes")

    def on_action_resend_invite(self, value):
        # Implement resend invite logic here
        self.send_invite()
        return {'status': True }

    def send_invite(self, context=None):
        if context is None:
            context = {}
        context['group'] = self.group.to_dict("basic")
        email_template = "group_invite"
        template_prefix = self.group.get_metadata_value('email_template')
        self.user.send_template_email(
            email_template, context=context,
            template_prefix=template_prefix)
        return {'status': True }
