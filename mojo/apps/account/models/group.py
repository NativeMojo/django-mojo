from django.db import models
from mojo.models import MojoModel, MojoSecrets
from mojo.helpers import dates
from mojo.apps import metrics
from mojo.helpers.settings import settings

GROUP_LAST_ACTIVITY_FREQ = settings.get("GROUP_LAST_ACTIVITY_FREQ", 300)
METRICS_TIMEZONE = settings.get("METRICS_TIMEZONE", "America/Los_Angeles")


class Group(MojoSecrets, MojoModel):
    """
    Group model.
    """
    created = models.DateTimeField(auto_now_add=True, editable=False)
    modified = models.DateTimeField(auto_now=True, db_index=True)
    last_activity = models.DateTimeField(default=None, null=True, db_index=True)

    name = models.CharField(max_length=200)
    uuid = models.CharField(max_length=200, null=True, default=None, db_index=True)
    is_active = models.BooleanField(default=True, db_index=True)
    kind = models.CharField(max_length=80, default="group", db_index=True)

    parent = models.ForeignKey("account.Group", null=True, related_name="groups",
        default=None, on_delete=models.CASCADE)

    # JSON-based metadata field
    metadata = models.JSONField(default=dict, blank=True)

    class RestMeta:
        SEARCH_FIELDS = ["name"]
        VIEW_PERMS = ["view_groups", "manage_groups"]
        SAVE_PERMS = ["manage_groups"]
        LIST_DEFAULT_FILTERS = {
            "is_active": True
        }
        GRAPHS = {
            "basic": {
                "fields": [
                    'id',
                    'name',
                    'created',
                    'modified',
                    'last_activity',
                    'is_active',
                    'kind',
                ]
            },
            "default": {
                "fields": [
                    'id',
                    'name',
                    'created',
                    'modified',
                    'last_activity',
                    'is_active',
                    'kind',
                    'parent',
                    'metadata'
                ],
                "graphs": {
                    "parent": "basic"
                }
            },

        }

    @property
    def timezone(self):
        return self.metadata.get("timezone", "America/Los_Angeles")

    def get_local_day(self, dt_utc=None):
        return dates.get_local_day(self.timezone, dt_utc)

    def get_local_time(self, dt_utc):
        return dates.get_local_time(self.timezone, dt_utc)

    def __str__(self):
        return str(self.name)

    def has_permission(self, user):
        from mojo.account.models.member import GroupMember
        return GroupMember.objects.filter(user=user).last()

    def member_has_permission(self, user, perms, check_user=True):
        if check_user and user.has_permission(perms):
            return True
        ms = self.has_permission(user)
        if ms is not None:
            return ms.has_permission(perms)
        return False

    def touch(self):
        # can't subtract offset-naive and offset-aware datetimes
        if self.last_activity and not dates.is_today(self.last_activity, METRICS_TIMEZONE):
            metrics.record("group_activity_day", category="group", min_granularity="days")
        if self.last_activity is None or dates.has_time_elsapsed(self.last_activity, seconds=GROUP_LAST_ACTIVITY_FREQ):
            self.last_activity = dates.utcnow()
            self.atomic_save()

    def get_metadata(self):
        # converts our local metadata into an objict
        self.metadata = self.jsonfield_as_objict("metadata")
        return self.metadata

    def get_member_for_user(self, user):
        return self.members.filter(user=user).last()

    @classmethod
    def on_rest_handle_list(cls, request):
        if cls.rest_check_permission(request, "VIEW_PERMS"):
            return cls.on_rest_list(request)
        if getattr(request.user, 'members') is not None:
            group_ids = request.user.members.values_list('group__id', flat=True)
            return cls.on_rest_list(request, cls.objects.filter(id__in=group_ids))
        return cls.on_rest_list(request, cls.objects.none())
