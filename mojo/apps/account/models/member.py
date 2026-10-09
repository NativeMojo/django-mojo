import json
from django.db import models, router, transaction
from mojo.models import MojoModel
from mojo import errors as merrors
from mojo.helpers.settings import settings
from mojo.helpers import dates, logit
from mojo.helpers.perms import implied_perms


def _valid_protection_requirement(value):
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set)):
        return bool(value) and all(
            isinstance(item, str) and item.strip() for item in value)
    return False


def parse_member_perms_protection(value):
    """Return one MEMBER_PERMS_PROTECTION source as a dict, or None if malformed.

    None and a blank (empty or whitespace-only) string are "nothing configured"
    and return {}. Accepted: a dict, or a string holding a JSON object, whose
    keys are non-empty strings and whose values are a non-empty string or a
    non-empty list/tuple/set of non-empty strings. A tuple is returned as a
    list — has_permission reads only a list or set as "any of".
    """
    if value is None:
        return {}
    if isinstance(value, str):
        if not value.strip():
            return {}
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return None
    if not isinstance(value, dict):
        return None
    parsed = {}
    for perm, requirement in value.items():
        if not isinstance(perm, str) or not perm.strip():
            return None
        if not _valid_protection_requirement(requirement):
            return None
        parsed[perm] = list(requirement) if isinstance(requirement, tuple) else requirement
    return parsed


def resolve_member_perms_protection(file_value, db_value):
    """Merge the settings-file map over the platform-wide Setting row.

    The file is the floor: it wins for every permission it names, so a row can
    add protected permissions but never remove or loosen one. Returns None
    when either source is malformed — the caller refuses rather than reading
    an unreadable map as empty.
    """
    file_map = parse_member_perms_protection(file_value)
    db_map = parse_member_perms_protection(db_value)
    if file_map is None or db_map is None:
        return None
    return {**db_map, **file_map}


def _member_perms_protection():
    # Read the two sources separately — settings.get would return a DB row
    # WHOLESALE in place of the file value. No kind= on the file read, so a
    # wrong type is seen as malformed rather than coerced to {}.
    from mojo.apps.account.models.setting import Setting
    return resolve_member_perms_protection(
        settings.get_static("MEMBER_PERMS_PROTECTION", None),
        Setting.resolve("MEMBER_PERMS_PROTECTION"))


def _user_last_activity_freq():
    return settings.get("USER_LAST_ACTIVITY_FREQ", 300)


def _metrics_timezone():
    return settings.get("METRICS_TIMEZONE", "America/Los_Angeles")


def _metrics_track_user_activity():
    return settings.get("METRICS_TRACK_USER_ACTIVITY", False)

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
        VIEW_PERMS = ["view_members", "view_groups", "manage_groups", "manage_group", "groups"]
        SAVE_PERMS = ["manage_groups", "manage_group", "groups"]
        SEARCH_FIELDS = ["user__username", "user__email", "user__display_name"]
        POST_SAVE_ACTIONS = ['resend_invite']
        CREATED_BY_OWNER_FIELD = 'created_by'  # we do this to protect user
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

    def save(self, *args, **kwargs):
        """Commit the member and synchronous signal cleanup together."""
        # Match Django's write routing, including the legacy positional using
        # argument, and pin super().save to that same connection.
        using = kwargs.get("using") or (args[2] if len(args) > 2 else None)
        using = using or router.db_for_write(type(self), instance=self)
        if len(args) > 2:
            args = (*args[:2], using, *args[3:])
        else:
            kwargs["using"] = using
        # A permission removed or the member deactivated: the user's open
        # websockets re-check chat access once this commits (maestro #7498).
        # Registered after the row and its signal cleanup are written, so the
        # re-check reads all of it.
        from mojo.apps.realtime.access import (
            access_before, save_is_insert, save_removes_access)
        update_fields = kwargs.get("update_fields", args[3] if len(args) > 3 else None)
        force_insert = kwargs.get("force_insert", args[0] if args else False)
        # Only a certain insert is exempt. A row this instance never read, or
        # read inside a transaction since rolled back, announces: when
        # unsure, the sockets re-check.
        announce = not save_is_insert(self, force_insert) and save_removes_access(
            access_before(self), self, update_fields)
        with transaction.atomic(using=using):
            result = super().save(*args, **kwargs)
            self._remember_access(update_fields)
            if announce:
                self._announce_access_change(using)
            return result

    def delete(self, *args, **kwargs):
        user_id = self.user_id
        using = kwargs.get("using") or (args[0] if args else None)
        using = using or router.db_for_write(type(self), instance=self)
        result = super().delete(*args, **kwargs)
        self._announce_access_change(using, user_id)
        return result

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        instance._remember_access()
        return instance

    def refresh_from_db(self, *args, **kwargs):
        super().refresh_from_db(*args, **kwargs)
        self._remember_access(kwargs.get("fields", args[1] if len(args) > 1 else None))

    def _remember_access(self, fields=None):
        """Note what this instance last saw stored for the fields that grant
        access, so a save can tell a removal from a value it only carried.
        `fields` names the ones a partial save or refresh just synced."""
        from mojo.apps.realtime.access import remember_access
        remember_access(self, fields)

    def _announce_access_change(self, using, user_id=None):
        from mojo.apps.realtime import manager
        manager.publish_access_changed("user", user_id or self.user_id, using=using)

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
        # The global short-circuit is skipped for a session that ASSUMES a
        # member (an override ApiKey, or any GroupScopedToken): request.user is
        # a real User there and this reads their untenanted platform-wide dict,
        # which would let tenant-page JS driving a staff visitor's token assign
        # member permissions inside the gated group. Authority falls through to
        # the requester's own member row. A reference-mode or unlinked ApiKey
        # is unaffected — request.user IS the key, so this reads the key's own
        # group-bounded dict.
        from mojo.helpers.request import is_override_user_session
        if not is_override_user_session(request) and request.user.has_permission(
                ["manage_groups", "manage_users"]):
            return True
        req_member = self.group.get_member_for_user(request.user, check_parents=True)
        if req_member is not None:
            member_perms_protection = _member_perms_protection()
            if member_perms_protection is None:
                # Nobody knows which permissions were meant to be protected, so
                # refuse every member-level change. Global managers returned
                # above and can repair the setting.
                logit.error(
                    "MEMBER_PERMS_PROTECTION is malformed; refusing member-level "
                    "permission changes until it is fixed")
                return False
            if perm in member_perms_protection:
                return req_member.has_permission(member_perms_protection[perm])
            return req_member.has_permission(["manage_group", "manage_members", "manage_users", "manage_groups"])
        return False

    def set_permissions(self, value):
            if not isinstance(value, dict):
                return
            current = self.permissions if isinstance(self.permissions, dict) else {}
            for perm, perm_value in value.items():
                # A no-op needs no authority — same rule as ApiKey.set_permissions.
                # The admin UI submits the ENTIRE permission switch catalog on
                # every save, so a protected perm the admin never touched rides
                # along as False on writes that have nothing to do with it.
                # Gating that would 403 the whole save the moment
                # MEMBER_PERMS_PROTECTION is populated — turning an attempt to
                # HARDEN member permissions into an outage for every group admin.
                #
                # Read-only: never normalize the column before the gate, or an
                # all-no-op payload could wipe it unauthorized (the bug this
                # pattern already caused once on ApiKey).
                #
                # REVOKING a protected perm still requires the authority to
                # grant it, so this is not a downgrade loophole.
                stored = current.get(perm, False)
                if (stored == perm_value) if bool(perm_value) else not bool(stored):
                    continue
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

        Membership tiers: "member" is satisfied by ANY member row (the view
        tier). "full_member" is the write tier — a member row not marked
        guest. The marker is permissions["guest"] (truthy = guest), set and
        cleared via add_permission("guest") / remove_permission("guest")
        under the can_change_permission gate. "full_member" is derived from
        the marker alone — a stored permissions["full_member"] key is
        ignored on member rows. Marking guest does NOT strip or freeze the
        row's other grants: demoting a manager means removing their
        manage-level perms AND setting the marker.
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

        if perm_key in ["all", "authenticated", "member"]:
            return True
        # Derived from the guest marker only — must precede the stored-dict
        # lookup so a stored "full_member" key can never grant the write tier.
        if perm_key == "full_member":
            return not bool(self.permissions.get("guest", False))
        # Bare domain terms ("groups") satisfy their view_/manage_ forms —
        # one-directional; see mojo.helpers.perms.
        return any(bool(self.permissions.get(pk, False)) for pk in implied_perms(perm_key))

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
        if self.last_activity is None or dates.has_time_elsapsed(self.last_activity, seconds=_user_last_activity_freq()):
            if self.last_activity and not dates.is_today(self.last_activity, _metrics_timezone()):
                metrics.record(
                    "member_activity_day",
                    min_granularity="days",
                    account=f"group-{self.group.pk}"
                )
            self.last_activity = dates.utcnow()
            self.save(update_fields=['last_activity'])
        if _metrics_track_user_activity():
            metrics.record(f"member_activity:{self.pk}", category="member", min_granularity="minutes")

    def on_action_resend_invite(self, value):
        # Implement resend invite logic here
        self.send_invite()
        return {'status': True }

    def send_invite(self, context=None):
        # User has never logged in — send account-setup invite with token link.
        if self.user.last_login is None:
            self.user.send_invite(group=self.group)
            return {'status': True}
        if context is None:
            context = {}
        context['group'] = self.group.to_dict("basic")
        email_template = "group_invite"
        template_prefix = self.group.get_metadata_value('email_template')
        self.user.send_template_email(
            email_template, context=context,
            template_prefix=template_prefix)
        return {'status': True}
