import hashlib
import json
from django.db import models
from mojo.models import MojoModel
from mojo.models.secrets import MojoSecrets
from mojo.helpers import crypto, dates, logit
from mojo.helpers.perms import implied_perms
from mojo.helpers.settings import settings
from mojo import errors as merrors


# Framework-level protection floor: permissions that must never be grantable by
# an ordinary group admin, whatever a deployment configures.
#
# geoip_sync authorizes POST /api/system/geoip/sync, which writes GLOBAL threat
# intel consumed by every instance federating with this one. It is escalate-only
# and cannot touch per-fleet enforcement, but "raise suspicion fleet-wide" is
# still not a tenant-level power.
#
# The `sys.` prefix is load-bearing. Protection values resolve through
# GroupMember.has_permission, where a bare term reads the member's own group
# dict — so a domain term like "security" would still pass for any group admin
# holding it in their own tenant, which is exactly the hole being closed.
# `sys.geoip_sync` forces the check onto the granter's GLOBAL dict instead.
APIKEY_PERMS_PROTECTION_DEFAULTS = {
    "geoip_sync": "sys.geoip_sync",
}


def _apikey_perms_protection():
    # kind="dict" so a DB-backed Setting (stored as a JSON string) parses into a
    # dict — otherwise `perm in <str>` would silently degrade to substring matching.
    configured = settings.get("APIKEY_PERMS_PROTECTION", {}, kind="dict") or {}
    # MERGED, not defaulted. settings.get returns a configured value WHOLESALE —
    # the `{}` above is consulted only when the setting is absent entirely. So a
    # deployment that sets APIKEY_PERMS_PROTECTION to protect its own perms would
    # otherwise silently drop the floor along with it, which is the failure this
    # merge exists to prevent.
    #
    # Deployment wins per key: naming an entry explicitly overrides the floor
    # (including relaxing it — a deliberate, visible act, not an accident).
    return {**APIKEY_PERMS_PROTECTION_DEFAULTS, **configured}


class ApiKey(MojoSecrets, MojoModel):
    """
    A group-scoped API key for programmatic access.

    Keys authenticate via:  Authorization: apikey <token>

    The raw token is generated on creation and returned in the REST response
    or by create_for_group(). It is retained TWO ways:

      - token_hash — SHA-256 of the raw token; indexed and unique, used by
        validate_token() for the fast lookup.
      - mojo_secrets — the raw token itself, stored ENCRYPTED via MojoSecrets
        (AES-256-GCM, key derived with PBKDF2). get_token() decrypts and
        returns it. This is NOT a "shown once" credential.

    Read-back over REST is OPT-IN. No ordinary read carries the secret: the
    "default" graph (which lists fall back to) and the "me" graph both omit
    it. A caller who genuinely needs the live token asks for graph="token",
    which exports it through rest_get_token() and writes an
    `api_key:token_read` audit row. The opt-in is open to the same VIEW_PERMS
    holders as any other read (manage_group / manage_groups / groups) — what
    changed is that the credential no longer rides along on requests that
    never asked for it, not who may ask.

    Encryption caveat: MojoSecrets derives its key from
    `{created}{pk}{ClassName}` — every input is a plaintext column on this
    same row, with no server-side secret mixed in. It therefore protects
    against exfiltration of the mojo_secrets column alone, NOT against a row
    or full-table dump. Treat this table as holding live credentials.

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

    # The member this key acts as. Two modes, controlled by override_user:
    #   override_user=False (default) — REFERENCE ONLY. request.user stays the
    #     ApiKey and authorization is unchanged; the linked user is exposed as
    #     request.acting_user and is what lands in FK(User) attribution sites
    #     (see CREATED_BY_OWNER_FIELD handling in mojo/models/rest.py).
    #   override_user=True — the key ASSUMES the member: request.user is this
    #     User and permissions resolve through their GroupMember. Opt-in per
    #     key so every existing key keeps today's behavior bit-for-bit.
    # In BOTH modes the key's group remains the tenant boundary, and a
    # key-backed session can never mutate the member's credentials
    # (see is_key_backed_session in mojo/helpers/request.py).
    user = models.ForeignKey(
        "account.User", null=True, blank=True, default=None,
        on_delete=models.SET_NULL, related_name="api_keys")
    override_user = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created"]

    class RestMeta:
        VIEW_PERMS = ["manage_group", "manage_groups", "groups"]
        SAVE_PERMS = ["manage_group", "manage_groups", "groups"]
        CAN_DELETE = True
        # This model HAS a `user` field, but it means "the member this key acts
        # as" — not "who created this key". Without this opt-out,
        # on_rest_save's create branch (mojo/models/rest.py) would auto-stamp
        # the CREATING ADMIN into it on every REST create, silently linking
        # every new key to whoever made it.
        CREATED_BY_OWNER_FIELD = None
        SENSITIVE_FIELDS = ["token_hash"]
        # This table holds live credentials. The assistant's query_model takes a
        # caller-supplied graph and does not filter sensitive values out of
        # serialized output, so nothing else would stop it asking for the
        # "token" graph below.
        DENY_AI = True
        GRAPHS = {
            # NOTE: the raw token is deliberately ABSENT here. This is the graph
            # every ordinary read gets — including lists, which ask for "list",
            # find no such graph, and fall back to this one. Read-back is opt-in
            # via graph="token".
            "default": {
                "fields": [
                    "id", "created", "modified", "name",
                    "is_active", "permissions", "limits",
                    "last_used", "expires_at", "metadata", "override_user"
                ],
                "graphs": {
                    "group": "basic",
                    "user": "basic",
                }
            },
            # Opt-in credential read: same shape as "default" plus the live raw
            # token. Available to the same VIEW_PERMS holders — the point is
            # that the secret only ships when a caller asks for it by name, not
            # that fewer callers may ask. Every read here is audited; see
            # rest_get_token.
            "token": {
                "fields": [
                    "id", "created", "modified", "name",
                    "is_active", "permissions", "limits",
                    "last_used", "expires_at", "metadata", "override_user"
                ],
                "extra": [("rest_get_token", "token")],
                "graphs": {
                    "group": "basic",
                    "user": "basic",
                }
            },
            # Safe self-introspection graph for the `group/apikey/me` whoami
            # endpoint. Deliberately omits the `token` extra — the caller
            # already holds the token; echoing it back is a needless exposure.
            # `user` IS included: "who am I acting as" is the fact that lets a
            # consumer stop parsing the key's name to find out.
            "me": {
                "fields": [
                    "id", "created", "name", "is_active",
                    "permissions", "limits", "last_used", "expires_at",
                    "override_user"
                ],
                "graphs": {
                    "group": "basic",
                    "user": "basic",
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
        user's system-level permissions in GroupMember. Here they are ALWAYS
        denied, including when this key is linked to a member: a key may carry
        a member's identity (see the `user` field) but must never become a
        route to platform-level authority. This method evaluates the KEY's own
        permissions dict; an override_user key does not reach it at all,
        because request.user is the member and the normal GroupMember path
        runs instead — where sys.* is likewise gated on the member's own
        system permissions, not on the key.

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
        from mojo.helpers.request import (
            is_override_user_session, is_key_backed_session)
        user = getattr(request, "user", None)
        if user is None:
            return False
        # A key-backed session may NEVER grant a PROTECTED permission.
        #
        # Without this the floor is bypassable in two REST calls. The
        # short-circuit below reads `user.has_permission([...])`, and for a
        # non-override key session request.user IS the ApiKey — so it reads the
        # KEY's own group-bounded dict, and implied_perms expands
        # manage_groups -> groups. A group admin can therefore mint key A with
        # the unprotected `groups` perm (allowed: `groups` is in this model's
        # SAVE_PERMS), authenticate as A, and have A mint key B carrying
        # geoip_sync — the exact escalation the floor exists to stop.
        #
        # Same class as the block in _can_manage_acting_user: a confined
        # credential must not be able to mint a successor with authority it
        # does not itself legitimately hold. Scoped to PROTECTED perms so
        # ordinary key-provisions-key flows keep working.
        if is_key_backed_session(request) and perm in _apikey_perms_protection():
            return False
        # Skipped for a session that ASSUMES a member (override ApiKey /
        # GroupScopedToken) — see GroupMember.can_change_permission for the
        # reasoning. Reference-mode and unlinked keys read their own dict here
        # and are unaffected.
        if not is_override_user_session(request) and user.has_permission(
                ["manage_groups", "manage_users"]):
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
            # Read through the normalizer instead of resetting the column
            # first. `permissions` may legitimately hold a JSON STRING — that
            # shape is what _get_permissions_dict exists for, and has_permission
            # authorizes off it. Materializing `{}` before the gate would let an
            # all-no-op payload silently WIPE a stringy permissions column with
            # no authorization check at all, stripping (say) a federation key's
            # geoip_sync. Only assign back once a write is actually authorized.
            current = self._get_permissions_dict()
            # A no-op needs no authority.
            #
            # This loop gates every key in the incoming dict, and the admin UI
            # submits the ENTIRE switch catalog on every save (web-mojo's
            # FormView.getFormData re-collects every checkbox by name — even
            # disabled ones). So an untouched protected perm rides along on
            # writes that have nothing to do with it: renaming a key, flipping
            # is_active, creating any key at all. Gating those would 403 the
            # whole save with no indication why.
            #
            # Harmless while APIKEY_PERMS_PROTECTION was empty; a live floor
            # (see APIKEY_PERMS_PROTECTION_DEFAULTS) makes it reachable, so the
            # skip is required for the floor to be shippable at all.
            #
            # Only a genuine state change is gated — REVOKING a protected perm
            # still demands the authority to grant it, so this cannot become a
            # loophole for stripping a federation key's access.
            stored = current.get(perm, False)
            if (stored == perm_value) if bool(perm_value) else not bool(stored):
                continue
            if not self.can_change_permission(perm, perm_value, request):
                raise merrors.PermissionDeniedException()
            self.permissions = current
            if bool(perm_value):
                self.permissions[perm] = perm_value
            else:
                self.permissions.pop(perm, None)

    def _can_manage_acting_user(self, request):
        """Whether `request.user` may link/unlink this key's acting member.

        Same bar as can_change_permission's fallback: a global manage_groups/
        manage_users holder, or a member of THIS KEY's group holding a
        key-management perm. Deliberately NOT routed through
        APIKEY_PERMS_PROTECTION — that setting gates keys of the `permissions`
        dict and has no relationship to model fields.
        """
        if request is None:
            return False
        # A key-backed session may never establish or change an acting-as
        # link — not on itself, not on a sibling key it creates. Otherwise an
        # override key acting as a group admin could mint a successor key,
        # point it at any member, enable override on it, and read its raw token
        # out of the create response: a credential that outlives revocation of
        # the original, which is precisely what the credential rules exist to
        # prevent. Linking is an interactive administrative act.
        from mojo.helpers.request import is_key_backed_session
        if is_key_backed_session(request):
            return False
        user = getattr(request, "user", None)
        if user is None:
            return False
        if user.has_permission(["manage_groups", "manage_users"]):
            return True
        group = self._acting_group(request)
        if group is None:
            return False
        req_member = group.get_member_for_user(user, check_parents=True)
        if req_member is None:
            return False
        return req_member.has_permission(
            ["manage_group", "manage_members", "manage_users", "manage_groups"])

    def _acting_group(self, request):
        """This key's group, resolved explicitly.

        On REST create the group FK is auto-stamped AFTER the field loop, so
        self.group may still be None while set_user runs (same trap
        set_permissions documents). Fall back to the request's group. Returns
        None when neither is available — callers MUST fail closed, never
        default to "no group means no check".

        Resolving the group here rather than reading self.group at each call
        site also makes validation independent of the client's JSON key order:
        on_rest_save iterates data_dict as given, so {"group":G,"user":M} and
        {"user":M,"group":G} would otherwise validate against different groups.
        """
        if self.group_id:
            return self.group
        return getattr(request, "group", None)

    def validate_acting_user(self, user, request):
        """Gate for linking this key to a member. Raises on refusal.

        Two rules, in order:
          1. Never a superuser. A bright line with no legitimate use — a key is
             a bearer token in a config file, and platform-wide authority must
             not be reachable through one. This is a hard block, not a warning:
             an incident nobody reads is not a control.
          2. Only an ACTIVE member of this key's own group, check_parents=False.
             Delegation must not exceed the delegator. check_parents=True walks
             UP the tree, which would let an admin of a child group link a key
             to a more-privileged ancestor member — the exact escalation this
             rule exists to stop.
        """
        if user is None:
            return
        if getattr(user, "is_superuser", False):
            raise merrors.PermissionDeniedException()
        group = self._acting_group(request)
        if group is None:
            raise merrors.PermissionDeniedException()
        if group.get_member_for_user(user, check_parents=False) is None:
            raise merrors.PermissionDeniedException()

    def _report_acting_user_event(self, details, title, level):
        """Best-effort incident on a NOTEWORTHY link change.

        Fired on the LINK (a discrete admin action), never on use — a linked
        key serving 10k requests must not write 10k incidents. Never let a
        reporting failure break the write itself.
        """
        try:
            from mojo.apps.incident import reporter
            reporter.report_event(
                details, title=title, category="api_key_acting_user",
                level=level, request=self.active_request)
        except Exception:
            logit.exception("failed to report api_key acting-user event")

    def _elevated_perms_for(self, user):
        """Permission keys held by `user` that make a link worth recording."""
        if user is None:
            return []
        watched = ["manage_users", "manage_groups"]
        found = [p for p in watched if user.has_permission(p)]
        # sys.* is the system-level namespace (see has_permission above). A key
        # can never exercise it, but linking to someone who holds it is still
        # the kind of thing an operator should be able to find later.
        perms = getattr(user, "permissions", None)
        if isinstance(perms, dict):
            found += [p for p in perms if isinstance(p, str) and p.startswith("sys.")]
        return found

    def set_user(self, value):
        """REST setter for the acting member.

        Falsy clears the link (and override_user with it — see below).
        """
        from mojo.apps.account.models.user import User

        request = self.active_request
        if not self._can_manage_acting_user(request):
            raise merrors.PermissionDeniedException()

        if not value:
            self.user = None
            # An override_user=True key with no member is a meaningless state
            # that reads as "assume nobody" — clear both together so the row
            # can never sit in it.
            self.override_user = False
            return

        pk = value.get("id") if isinstance(value, dict) else value
        try:
            target = User.objects.get(pk=int(pk))
        except (User.DoesNotExist, TypeError, ValueError):
            raise merrors.ValueException("user must be a valid user id")

        self.validate_acting_user(target, request)
        self.user = target

        elevated = self._elevated_perms_for(target)
        if elevated:
            self._report_acting_user_event(
                f"ApiKey '{self.name}' linked to {target.username}, who holds "
                f"elevated permissions: {', '.join(sorted(elevated))}",
                "API key linked to an elevated member", 4)

    def set_override_user(self, value):
        """REST setter for override_user — the switch from reference to assume.

        Same requester bar as set_user. Always reported: this is the moment a
        key stops being a scoped credential and starts carrying a member's
        identity.
        """
        request = self.active_request
        if not self._can_manage_acting_user(request):
            raise merrors.PermissionDeniedException()

        enabled = bool(value)
        if enabled and self.user_id is None:
            raise merrors.ValueException(
                "override_user requires a linked user")

        was = self.override_user
        self.override_user = enabled
        if enabled and not was:
            self._report_acting_user_event(
                f"ApiKey '{self.name}' now assumes the identity of "
                f"{self.user.username} — permissions resolve through that "
                f"member, bounded by the key's group",
                "API key override_user enabled", 5)

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
        Generate a new raw token, persist it, and return it.

        Two writes happen: token_hash gets the SHA-256 (for validate_token's
        indexed lookup) and set_secret("token", ...) puts the RAW token into
        mojo_secrets, encrypted on save. The raw token stays recoverable
        afterwards via get_token(), and over REST via the opt-in "token" graph.

        What this call destroys is the PREVIOUS token — both the old hash and
        the old encrypted copy are overwritten, so any token issued before
        this call stops authenticating and cannot be recovered.
        """
        token = crypto.random_string(48, allow_special=False)
        self.token_hash = hashlib.sha256(token.encode()).hexdigest()
        self.set_secret("token", token)
        return token

    @classmethod
    def create_for_group(cls, group, name, permissions=None, limits=None,
                         user=None, override_user=False):
        """
        Create a new ApiKey for a group programmatically.

        Returns (api_key, raw_token). The caller does NOT have to persist
        raw_token in order to see it again — it is stored encrypted on the row
        and api_key.get_token() returns it (over REST, the opt-in "token"
        graph). It is still a live credential: hand it to the client over a
        secure channel and treat the stored copy accordingly.

        Args:
            group:       account.Group instance
            name:        Human-readable label (e.g. "Mobile App v2")
            permissions: Dict of {perm_key: True/False}
            limits:      Dict of {endpoint_key: {limit, window}} (window in minutes)
            user:        Optional account.User this key acts as
            override_user: When True the key ASSUMES that user's identity and
                         permissions; when False (default) the link is a
                         reference used only for attribution.
        """
        api_key = cls(
            group=group,
            name=name,
            permissions=permissions or {},
            limits=limits or {},
        )
        if user is not None:
            # Trusted internal path (no request), but the invariants are
            # properties of the DATA, not of who is writing it — a superuser or
            # non-member link is wrong however it is created.
            if getattr(user, "is_superuser", False):
                raise merrors.PermissionDeniedException()
            if group.get_member_for_user(user, check_parents=False) is None:
                raise merrors.PermissionDeniedException()
            api_key.user = user
            api_key.override_user = bool(override_user)
        elif override_user:
            raise merrors.ValueException("override_user requires a user")
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
            api_key = cls.objects.select_related("group", "user").get(token_hash=token_hash)
        except cls.DoesNotExist:
            return None, "Invalid API key"

        if not api_key.is_active:
            return None, "API key is inactive"

        if api_key.expires_at and dates.utcnow() > api_key.expires_at:
            return None, "API key has expired"

        # A linked member who has been deactivated takes their keys with them.
        # Reuses the EXISTING inactive string rather than a new one — a
        # distinct message would turn this into an account-state oracle for
        # anyone holding the token (same reasoning as User.validate_jwt).
        if api_key.user_id and not api_key.user.is_active:
            return None, "API key is inactive"

        # Re-assert the superuser bar at AUTH time, not only at link time.
        # validate_acting_user blocks a superuser target when the link is made,
        # but a member can be promoted afterwards — and User.has_permission
        # returns True for everything once is_superuser is set, so a key linked
        # before the promotion would silently become a superuser key.
        if api_key.user_id and api_key.user.is_superuser:
            logit.error(
                f"api key {api_key.pk} is linked to superuser "
                f"{api_key.user_id}; refusing to authenticate")
            return None, "API key is inactive"

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

        # Set in BOTH modes. This is the attribution identity — what lands in
        # FK(User) columns — and it is deliberately independent of whether the
        # key also assumes the member's authority. request.api_key stays set
        # either way and remains the machine-identity signal every guard keys
        # on (mojo/helpers/request.py is_key_backed_session).
        request.acting_user = api_key.user

        try:
            cls.objects.filter(pk=api_key.pk).update(last_used=dates.utcnow())
        except Exception:
            pass

        # NOTE for anyone fixing WebSocket api-key auth: it is dead today —
        # realtime/handler.py calls async_validate_bearer_token with
        # request=None, so the request.group write above raises and is
        # swallowed as "handler error". If you make it work, you MUST also put
        # the key on the scope, or an override key binds a real User with no
        # machine-identity marker anywhere and every guard below opens.
        if api_key.override_user and api_key.user_id:
            # The key ASSUMES the member: authorization now resolves through
            # their GroupMember. Bounded by the key's group — request.group is
            # already pinned above and mojo/models/rest.py refuses to rebind it
            # outside the key's tree for a key-backed session.
            return api_key.user, None

        api_key.is_authenticated = True
        api_key.username = f"apikey:{api_key.id}"

        return api_key, None

    def get_token(self):
        """Returns the raw token from encrypted storage."""
        return self.get_secret("token")

    def rest_get_token(self):
        """Graph export for the opt-in "token" graph — audited.

        get_token() stays quiet for server-side use; this is the REST boundary,
        where handing back a live credential is worth a trail. One Log row per
        serialized key, so a bulk read (graph=token on a list) is visible once
        per credential — that is the intent, and no ordinary traffic reaches
        this path.

        The audit guards itself: the serializer turns ANY exception raised by a
        graph extra into `"token": null` with a 200, which is indistinguishable
        from "this key has no token". A failing audit write must never be able
        to reach that.
        """
        try:
            self.log(f"API Key '{self.name}' token read", "api_key:token_read")
        except Exception:
            logit.exception("failed to audit api_key token read")
        return self.get_token()

    def on_rest_get(self, request, graph="default"):
        """A freshly created key still hands back its raw token.

        on_rest_handle_create responds through this method, and the token is no
        longer on the default graph. The creation echo must NOT be routed
        through a request-selected graph: Group.check_view_permission rewrites
        request.DATA["graph"] to "basic" for member-level callers, which would
        silently drop the token for exactly the callers least able to recover
        it. So the just-minted value is attached explicitly, the same way
        group/apikey/rotate does it.

        The hardcoded "default" is safe only because this model defines no
        "basic" or "list" graph, so the downgrade would land on "default"
        anyway. Adding either one means revisiting this line — otherwise the
        create echo starts bypassing a narrowing the caller was meant to get.
        """
        raw = getattr(self, "_raw_token", None)
        if raw is None:
            return super().on_rest_get(request, graph=graph)
        data = self.to_dict(graph="default")
        data["token"] = raw
        return dict(status=True, data=data, graph="default")

    def on_rest_created(self):
        """Generate token, store hash for lookup, store raw token encrypted."""
        self._raw_token = self.generate_token()
        self.save()
        self.log(f"API Key '{self.name}' created", "api_key:generated")

    def rotate_token(self):
        """Rotate this key's secret in place: same id / name / permissions /
        limits, a brand-new token.

        ``generate_token`` overwrites ``token_hash`` AND the encrypted secret,
        so the previous token stops authenticating the instant this saves
        (``validate_token`` looks up by hash) and is gone for good — its
        encrypted copy is replaced, not kept.

        The NEW token is not write-once: like any ApiKey token it remains
        readable through ``get_token()`` and, over REST, the opt-in "token"
        graph until the next rotation. It is returned here for convenience,
        not because this is the only chance to see it. No new row, so existing
        references stay valid.
        """
        token = self.generate_token()
        self.save()
        self.log(f"API Key '{self.name}' rotated", "api_key:rotated")
        return token
