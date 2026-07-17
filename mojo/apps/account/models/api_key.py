import hashlib
import json
from django.db import models
from mojo.models import MojoModel
from mojo.models.secrets import MojoSecrets
from mojo.helpers import crypto, dates
from mojo.helpers.perms import implied_perms
from mojo.helpers.settings import settings
from mojo import errors as merrors


def _apikey_perms_protection():
    # kind="dict" so a DB-backed Setting (stored as a JSON string) parses into a
    # dict — otherwise `perm in <str>` would silently degrade to substring matching.
    return settings.get("APIKEY_PERMS_PROTECTION", {}, kind="dict") or {}


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
        VIEW_PERMS = ["manage_group", "manage_groups", "groups"]
        SAVE_PERMS = ["manage_group", "manage_groups", "groups"]
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
            },
            # Safe self-introspection graph for the `group/apikey/me` whoami
            # endpoint. Deliberately omits the `token` extra — the caller
            # already holds the token; echoing it back is a needless exposure.
            "me": {
                "fields": [
                    "id", "created", "name", "is_active",
                    "permissions", "limits", "last_used", "expires_at"
                ],
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

    @property
    def is_superuser(self):
        return False

    @property
    def org(self):
        return self.group

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
        if perm_key in ["all", "authenticated", "member"]:
            return True
        # Bare domain terms ("groups") satisfy their view_/manage_ forms —
        # one-directional; see mojo.helpers.perms.
        perms = self._get_permissions_dict()
        return any(bool(perms.get(pk, False)) for pk in implied_perms(perm_key))

    def can_change_permission(self, perm, value, request):
        """Whether `request.user` may assign `perm` to this key.

        Mirrors GroupMember.can_change_permission: a global manage_groups/
        manage_users holder may assign anything; otherwise the requester must be
        a member of this key's group and hold either the perm required by
        APIKEY_PERMS_PROTECTION (if the perm is listed) or a key-management perm.
        Prevents a group admin from self-minting a key with arbitrary powerful
        permissions.
        """
        user = getattr(request, "user", None)
        if user is None:
            return False
        if user.has_permission(["manage_groups", "manage_users"]):
            return True
        # On REST create the group FK is auto-stamped AFTER the field loop, so
        # self.group may still be None while set_permissions runs — fall back to
        # the request's group (set by the dispatcher from the group param).
        group = self.group if self.group_id else getattr(request, "group", None)
        if group is None:
            return False
        req_member = group.get_member_for_user(user, check_parents=True)
        if req_member is not None:
            protection = _apikey_perms_protection()
            if perm in protection:
                return req_member.has_permission(protection[perm])
            return req_member.has_permission(
                ["manage_group", "manage_members", "manage_users", "manage_groups"])
        return False

    def set_permissions(self, value):
        """REST setter for `permissions` — gates each key through
        can_change_permission so a group admin cannot assign perms they aren't
        entitled to grant. (create_for_group assigns `permissions` directly and
        is not affected — it is a trusted internal call.)

        Accepts only a real JSON object; any other shape — including a
        JSON-encoded string — is rejected with a 400."""
        if not isinstance(value, dict):
            raise merrors.ValueException("permissions must be a JSON object")
        request = self.active_request
        for perm, perm_value in value.items():
            if not self.can_change_permission(perm, perm_value, request):
                raise merrors.PermissionDeniedException()
            if not isinstance(self.permissions, dict):
                self.permissions = {}
            if bool(perm_value):
                self.permissions[perm] = perm_value
            else:
                self.permissions.pop(perm, None)

    def is_group_allowed(self, group):
        """
        Returns True if the given group is EFFECTIVELY ACTIVE (it and every
        ancestor — DM-048) and is this key's own group or a descendant. An
        inactive group is never allowed (ITEM-037), and deactivating an
        ancestor darkens the whole subtree — an active child under an inactive
        parent no longer passes (the old per-group carve-out was overturned by
        DM-048). Used by the dispatcher to validate the group= request param
        and by Group.check_view_permission / check_edit_permission (whose
        instance hooks run before the model-security is_active gate — without
        this, a suspended tenant's key could still read/write its own Group
        row, including flipping is_active back).
        """
        if group is None or not group.is_effectively_active():
            return False
        if group.pk == self.group.pk:
            return True
        return group.is_child_of(self.group)

    def get_groups(self, is_active=True, include_children=True):
        """
        Returns a QuerySet of EFFECTIVELY ACTIVE groups accessible to this API key.

        An API key is scoped to its own group and, when include_children is True,
        all descendant groups. Inactive groups are ALWAYS excluded (ITEM-037),
        and DM-048 extends the exclusion to the whole chain: a group whose
        ancestor is deactivated is effectively inactive too (the old "active
        child under an inactive parent stays reachable" carve-out was
        overturned). Deactivating suspends access at request time — nothing is
        mutated, so reactivating an ancestor instantly restores the subtree.
        This is the derivation the RestMeta list fallback uses
        (mojo/models/rest.py on_rest_handle_list), so an inactive tenant's rows
        never leak there. The `is_active` argument is accepted for interface
        compatibility with User.get_groups() (which filters *member* activity,
        N/A for keys) and does not change this group-level active filter.

        Args:
            is_active: Accepted for interface compatibility. Not used — group
                       active-state is always enforced.
            include_children: Include descendant groups (default True).

        Returns:
            QuerySet of effectively active Group objects.
        """
        from mojo.apps.account.models import Group

        # DM-048: the key's own group carries the whole ancestor burden — if
        # its chain is dark, every descendant is dark too (one bounded walk,
        # not one per group).
        if not self.group.is_effectively_active():
            return Group.objects.none()
        if not include_children:
            return Group.objects.filter(pk=self.group_id, is_active=True)
        # Descendants: with the own group's chain verified above, a descendant
        # is effectively active iff it is reachable from the key's group
        # through own-flag-active nodes. ONE query for the subtree's
        # (id, parent_id) pairs, then an in-memory walk — no per-group
        # ancestor queries (N+1 guard).
        all_ids = set([self.group_id])
        all_ids.update(self.group._get_all_child_ids())
        children_of = {}
        for gid, pid in Group.objects.filter(
                id__in=all_ids, is_active=True).values_list("id", "parent_id"):
            children_of.setdefault(pid, []).append(gid)
        kept_ids = []
        seen = set()
        stack = [self.group_id]
        while stack:
            gid = stack.pop()
            if gid in seen:
                continue
            seen.add(gid)
            kept_ids.append(gid)
            stack.extend(children_of.get(gid, []))
        return Group.objects.filter(id__in=kept_ids)

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

        # Group context is granted only for an EFFECTIVELY ACTIVE group — the
        # group AND every ancestor (DM-048). Deactivating a tenant (or any of
        # its ancestors) instantly suspends its keys; reactivating restores
        # them (no key mutation). An effectively-inactive group leaves
        # request.group None so group-scoped model security fails closed via
        # the groupless-deny branch (mojo/models/rest.py), matching ITEM-025's
        # active-only contract. NOT a hard reject: the federation path
        # (requires_global_perms, allow_api_keys) ignores request.group, so
        # rejecting the token would over-suspend legitimate fleet-peer keys.
        # (The group FK is non-nullable and select_related-loaded above — there
        # is no null-group key variant to guard for.)
        request.group = api_key.group if api_key.group.is_effectively_active() else None
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
        self.log(f"API Key '{self.name}' created", "api_key:generated")

    def rotate_token(self):
        """Rotate this key's secret in place: same id / name / permissions /
        limits, a brand-new token.

        ``generate_token`` overwrites ``token_hash`` and the encrypted secret,
        so the previous token stops authenticating the instant this saves
        (``validate_token`` looks up by hash). The new raw token is returned and
        must be persisted by the caller — like creation, it cannot be recovered
        after the next rotation. No new row, so existing references stay valid.
        """
        token = self.generate_token()
        self.save()
        self.log(f"API Key '{self.name}' rotated", "api_key:rotated")
        return token
