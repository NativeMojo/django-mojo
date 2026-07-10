from django.db import models
from mojo.models import MojoModel, MojoSecrets
from mojo.helpers import crypto, dates, logit
from mojo.apps import metrics
from mojo.helpers.settings import settings
from objict import objict
import uuid


WEBHOOK_SECRET_KEY = "webhook_secret"
WEBHOOK_SECRET_PREFIX = "wsec_"
WEBHOOK_SECRET_TOKEN_LEN = 48


GROUP_LAST_ACTIVITY_FREQ = settings.get_static("GROUP_LAST_ACTIVITY_FREQ", 300)
METRICS_TIMEZONE = settings.get_static("METRICS_TIMEZONE", "America/Los_Angeles")
MOJO_REST_LIST_PERM_DENY = settings.get_static("MOJO_REST_LIST_PERM_DENY", True)


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

    # Custom domain for white-label auth pages (e.g. auth.operator.com)
    auth_domain = models.CharField(max_length=255, null=True, default=None,
        unique=True, db_index=True, blank=True)

    # JSON-based metadata field
    metadata = models.JSONField(default=dict, blank=True)

    avatar = models.ForeignKey('fileman.File', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+')

    class RestMeta:
        LOG_CHANGES = True
        SEARCH_FIELDS = ["name"]
        VIEW_PERMS = ["view_groups", "manage_groups", "manage_group", "groups"]
        SAVE_PERMS = ["manage_groups", "manage_group", "groups"]
        PROTECTED_JSON_PERMS = ["admin_compliance", "admin_verify"]
        NO_SAVE_FIELDS = ["id", "pk", "created"]
        POST_SAVE_ACTIONS = ['realtime_message', 'disable', 'reactivate']
        LIST_DEFAULT_FILTERS = {
            "is_active": True
        }
        GRAPHS = {
            "simple": {
                "extra": ["timezone", "short_name", "thumbnail"],
                "fields": [
                    'uuid',
                    'id',
                    'name',
                    'created',
                    'modified',
                    'is_active',
                    'parent',
                    'kind',
                ]
            },
            "basic": {
                "fields": [
                    'id',
                    "uuid",
                    'name',
                    'created',
                    'modified',
                    'last_activity',
                    'is_active',
                    'kind',
                ],
                "graphs": {
                    "avatar": "basic"
                }
            },
            "default": {
                "extra": ["member_count"],
                "fields": [
                    'id',
                    "uuid",
                    'name',
                    'created',
                    'modified',
                    'last_activity',
                    'is_active',
                    'kind',
                    'parent',
                    'auth_domain',
                    'metadata'
                ],
                "graphs": {
                    "avatar": "basic",
                    "parent": "basic"
                }
            },

        }
        FORMATS = {
            "csv": [
                "id",
                "uuid",
                "name",
                "created",
                "modified",
                "last_activity",
                "is_active",
                "kind",
                "parent.id",
                "parent.name",
                ("metadata.timezone", "timezone")
            ]
        }

    @property
    def timezone(self):
        return self.metadata.get("timezone", "America/Los_Angeles")

    @property
    def short_name(self):
        return self.metadata.get("short_name", "")

    @property
    def thumbnail(self):
        return None

    @property
    def member_count(self):
        """Count of active direct members (does not include descendant groups)."""
        return self.members.filter(is_active=True).count()

    def get_uuid(self):
        if not self.uuid:
            self.uuid = uuid.uuid4().hex
            self.save(update_fields=["uuid"])
        return self.uuid

    # ------------------------------------------------------------------
    # Webhook signing secret (per-Group HMAC key for outbound webhooks)
    # ------------------------------------------------------------------

    @staticmethod
    def _mint_webhook_secret_value():
        return WEBHOOK_SECRET_PREFIX + crypto.random_string(
            WEBHOOK_SECRET_TOKEN_LEN, allow_special=False
        )

    def get_webhook_secret_info(self, auto_create=False):
        """Return the webhook-secret record as an objict with keys:
        value, created_at, last_rotated_at. Returns None if no secret exists
        and auto_create=False (the safe default). Pass auto_create=True only
        on emit-side paths that intentionally provision on first use.
        """
        record = self.get_secret(WEBHOOK_SECRET_KEY)
        if record:
            return objict.from_dict(record) if not isinstance(record, objict) else record
        if not auto_create:
            return None
        now = dates.utcnow().isoformat()
        record = objict(
            value=self._mint_webhook_secret_value(),
            created_at=now,
            last_rotated_at=now,
        )
        self.set_secret(WEBHOOK_SECRET_KEY, dict(record))
        self.save()
        return record

    def get_webhook_secret(self, auto_create=False):
        """Return the raw `wsec_…` secret string, or None if not set.

        Default `auto_create=False` keeps verify paths safe — a tampered
        request can never trigger a fresh mint. Pass `auto_create=True` from
        emit paths (`sign_for_group`, the REST endpoint).
        """
        info = self.get_webhook_secret_info(auto_create=auto_create)
        return info.value if info else None

    def rotate_webhook_secret(self):
        """Generate a new secret value, preserve created_at, update
        last_rotated_at. Returns the new info record (objict).
        """
        existing = self.get_secret(WEBHOOK_SECRET_KEY) or {}
        now = dates.utcnow().isoformat()
        created_at = existing.get("created_at") or now
        record = objict(
            value=self._mint_webhook_secret_value(),
            created_at=created_at,
            last_rotated_at=now,
        )
        self.set_secret(WEBHOOK_SECRET_KEY, dict(record))
        self.save()
        return record

    def get_local_day(self, dt_utc=None):
        return dates.get_local_day(self.timezone, dt_utc)

    def get_local_time(self, dt_utc):
        return dates.get_local_time(self.timezone, dt_utc)

    def __str__(self):
        return str(self.name)

    def user_has_permission(self, user, perms, check_user=True):
        if check_user and user.has_permission(perms):
            return True
        if not hasattr(user, "is_request_user"):
            return False
        ms = self.get_member_for_user(user, check_parents=True)
        if ms is not None:
            return ms.has_permission(perms)
        return False

    @classmethod
    def get_active(cls, pk):
        """Resolve a client-supplied numeric group id to an ACTIVE group, else None.

        The one resolver for ids arriving from request params (dispatcher
        `group=` and the requires_perms/requires_group_perms fallbacks).
        An inactive id resolves exactly like a nonexistent one — no touch,
        no error, no existence oracle — matching the dispatcher's group_uuid
        branch contract."""
        return cls.objects.filter(pk=pk, is_active=True).first()

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

    def get_protected_metadata(self, key, default=None):
        meta = self.metadata or {}
        protected = meta.get("protected") or {}
        return protected.get(key, default)

    def set_protected_metadata(self, key, value):
        from objict import objict
        meta = self.get_metadata()
        if not isinstance(meta.get("protected"), objict):
            meta.protected = objict.fromdict(meta.get("protected") or {})
        meta.protected[key] = value
        self.save(update_fields=["metadata", "modified"])

    def add_member(self, user):
        member, created = self.members.get_or_create(user=user)
        return member

    def get_member_for_user(self, user, check_parents=False, is_active=True, max_depth=8):
        """
        Get the member object for a user, optionally checking parent chain if not found.

        Args:
            user: User object to find membership for
            check_parents: If True, check parent groups if not found in current group
            is_active: Filter by active members (default True)
            max_depth: Maximum depth to check in parent chain (default 8)

        Returns:
            GroupMember object if found, None otherwise
        """
        # A GroupMember always links an account.User. A non-User identity — e.g.
        # an ApiKey authenticating the request (request.user can be a bare ApiKey)
        # — has no member row, and `.members.filter(user=<ApiKey>)` makes Django
        # raise 'Must be "User" instance.'. Treat it as "no membership" so every
        # caller (guarded or not) degrades to deny/None instead of crashing.
        if not hasattr(user, "is_request_user"):
            return None
        # First check direct membership
        queryset = self.members.filter(user=user)
        if is_active:
            queryset = queryset.filter(is_active=True)
        member = queryset.last()

        if member is not None or not check_parents:
            return member

        # Walk up the parent chain with depth protection
        current = self.parent
        depth = 0

        while current is not None and depth < max_depth:
            queryset = current.members.filter(user=user)
            if is_active:
                queryset = queryset.filter(is_active=True)
            member = queryset.last()

            if member is not None:
                return member

            current = current.parent
            depth += 1

        return None

    def get_children(self, is_active=True, kind=None):
        """
        Returns a QuerySet of all direct and indirect children of this group.
        """
        child_ids = self._get_all_child_ids()
        queryset = Group.objects.filter(id__in=child_ids)

        if is_active is not None:
            queryset = queryset.filter(is_active=is_active)
        if kind:
            queryset = queryset.filter(kind=kind)

        return queryset

    def _get_all_child_ids(self, collected_ids=None):
        """
        Recursively collects the IDs of all children.
        """
        if collected_ids is None:
            collected_ids = set()

        # Note: self.groups is the related_name from the parent ForeignKey
        children = self.groups.all()
        for child in children:
            if child.id not in collected_ids:
                collected_ids.add(child.id)
                child._get_all_child_ids(collected_ids)
        return list(collected_ids)

    def get_parents(self, is_active=True, kind=None):
        """
        Returns a QuerySet of all parents (ancestors) of this group.
        """
        parent_ids = []
        current = self.parent
        while current:
            parent_ids.append(current.id)
            current = current.parent

        queryset = Group.objects.filter(id__in=parent_ids)

        if is_active is not None:
            queryset = queryset.filter(is_active=is_active)
        if kind:
            queryset = queryset.filter(kind=kind)

        return queryset

    @property
    def top_most_parent(self):
        """
        Finds the top-most parent (root ancestor) of this group.
        Returns self if the group has no parent.
        """
        return self.get_top_most_parent()

    def get_top_most_parent(self, kind=None):
        current = self
        while current.parent:
            current = current.parent
            if current.kind == kind:
                return current
        return current

    def is_child_of(self, parent_group):
        """
        Checks if this group is a descendant of the given parent_group.
        """
        current = self.parent
        while current:
            if current.id == parent_group.id:
                return True
            current = current.parent
        return False

    def is_parent_of(self, child_group):
        """
        Checks if this group is an ancestor of the given child_group.
        """
        return child_group.is_child_of(self)

    def get_metadata_value(self, key):
        current = self
        while current:
            if key in current.metadata:
                return current.metadata[key]
            current = current.parent
        return None

    def invite(self, email, context=None):
        """
        Invites a user to join the group.
        """
        from mojo.apps.account.models import User
        email = email.strip().lower()
        user = User.objects.filter(email=email).last()
        ms = None
        if user:
            ms = self.add_member(user)
        elif not user:
            user = User(is_active=True, email=email)
            user.org = self.top_most_parent
            user.on_rest_pre_save(dict(email=None), True)
            user.save()
            # this is important to invite the user to the group
            user.send_invite(group=self)
            ms = self.add_member(user)
            return ms
        try:
            ms.send_invite(context=context)
        except Exception as e:
            logit.error(f"Error sending email: {e}")
        return ms

    def push_notification(self, title=None, body=None, data=None, **kwargs):
        from mojo.apps.account.services.push import send_direct_notification
        for member in self.members.filter(is_active=True).select_related('user'):
            send_direct_notification(member.user, title=title, body=body, data=data, **kwargs)

    def send_email(
        self,
        to,
        subject=None,
        body_text=None,
        body_html=None,
        cc=None,
        bcc=None,
        reply_to=None,
        **kwargs
    ):
        """Send email using mailbox determined by group's domain or system default

        Args:
            to: One or more recipient addresses
            subject: Email subject
            body_text: Optional plain text body
            body_html: Optional HTML body
            cc, bcc, reply_to: Optional addressing
            **kwargs: Additional arguments passed to mailbox.send_email()

        Returns:
            SentMessage instance

        Raises:
            ValueError: If no mailbox can be found
        """
        from mojo.apps.aws.models import Mailbox

        mailbox = None
        domain = None

        # Try to get domain from this group's metadata
        if self.metadata:
            domain = self.metadata.get("domain")

        # If no domain, check top_most_parent's metadata
        if not domain and self.top_most_parent != self:
            parent_metadata = self.top_most_parent.metadata
            if parent_metadata:
                domain = parent_metadata.get("domain")

        # Try to get mailbox from domain
        if domain:
            # Try domain default first
            mailbox = Mailbox.get_domain_default(domain)
            if not mailbox:
                # Try any mailbox from that domain
                mailbox = Mailbox.objects.filter(
                    domain__name__iexact=domain,
                    allow_outbound=True
                ).first()

        # Fall back to system default
        if not mailbox:
            mailbox = Mailbox.get_system_default()

        if not mailbox:
            raise ValueError("No mailbox available for sending email. Please configure a system default mailbox.")

        return mailbox.send_email(
            to=to,
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            cc=cc,
            bcc=bcc,
            reply_to=reply_to,
            **kwargs
        )

    def send_template_email(
        self,
        to,
        template_name,
        context=None,
        cc=None,
        bcc=None,
        reply_to=None,
        **kwargs
    ):
        """Send template email using mailbox determined by group's domain or system default

        Args:
            to: One or more recipient addresses
            template_name: Name of the EmailTemplate in database
            context: Template context variables (group will be added automatically)
            cc, bcc, reply_to: Optional addressing
            **kwargs: Additional arguments passed to mailbox.send_template_email()

        Returns:
            SentMessage instance

        Raises:
            ValueError: If no mailbox can be found or template not found
        """
        from mojo.apps.aws.models import Mailbox

        mailbox = None
        domain = None

        # Try to get domain from this group's metadata
        if self.metadata:
            domain = self.metadata.get("domain")

        # If no domain, check top_most_parent's metadata
        if not domain and self.top_most_parent != self:
            parent_metadata = self.top_most_parent.metadata
            if parent_metadata:
                domain = parent_metadata.get("domain")

        # Try to get mailbox from domain
        if domain:
            # Try domain default first
            mailbox = Mailbox.get_domain_default(domain)
            if not mailbox:
                # Try any mailbox from that domain
                mailbox = Mailbox.objects.filter(
                    domain__name__iexact=domain,
                    allow_outbound=True
                ).first()

        # Fall back to system default
        if not mailbox:
            mailbox = Mailbox.get_system_default()

        if not mailbox:
            raise ValueError("No mailbox available for sending email. Please configure a system default mailbox.")

        # Add group to context if not already present
        if context is None:
            context = {}
        if 'group' not in context:
            context['group'] = self

        return mailbox.send_template_email(
            to=to,
            template_name=template_name,
            context=context,
            cc=cc,
            bcc=bcc,
            reply_to=reply_to,
            **kwargs
        )

    def check_view_permission(self, perms, request):
        # READ gate (GET, and the FK attach-by-pk VIEW check). Writes route to
        # check_edit_permission instead — this hook's any-member fallthrough
        # (basic-graph downgrade at the end) is a read affordance and must never
        # authorize a save. A group-scoped ApiKey may read its own group (and
        # descendants) ONLY if it also holds the perm; the same confine-AND-perm
        # bar is enforced for writes in check_edit_permission and in the
        # group-scoped ApiKey branch of MojoModel._evaluate_permission.
        api_key = getattr(request, "api_key", None)
        if api_key is not None:
            return api_key.is_group_allowed(self) and api_key.has_permission(perms)
        # check if the user is a member of the group
        if request.user.has_permission(perms):
            return True
        ms = self.get_member_for_user(request.user, check_parents=True)
        if ms is None:
            return False
        if ms.has_permission(["view_group", "manage_group"]):
            return True
        # we still allow the user to view the group if they are a member
        # but we limit the fields they can see
        request.DATA.set("graph", "basic")
        return True

    def check_edit_permission(self, perms, request):
        # WRITE gate for POST/PUT/DELETE on /api/group/<pk> and its
        # POST_SAVE_ACTIONS (the save handler is the only permission gate in
        # front of on_action_*). Unlike check_view_permission there is NO
        # any-member fallthrough and no graph downgrade: a write demands an
        # actual SAVE_PERMS grant. ApiKey: confined to its own group tree AND
        # holds the perm (same bar as check_view_permission and
        # _evaluate_permission). Users: global grant OR member-level grant, via
        # user_has_permission (global-or-member, parent-aware).
        api_key = getattr(request, "api_key", None)
        if api_key is not None:
            return api_key.is_group_allowed(self) and api_key.has_permission(perms)
        return self.user_has_permission(request.user, perms)

    def on_action_realtime_message(self, value):
        # send a realtime message to the group
        from mojo.apps import realtime
        if not isinstance(value, dict):
            return {"status": False, "error": "Invalid message"}
        if "topic" in value:
            topic = value["topic"]
            if not topic.startswith(f"group:{self.id}:"):
                return {"status": False, "error": "Invalid topic for this group"}
            realtime.publish_topic(topic, value.get("message"))
        return {"status": True}

    def on_action_disable(self, value):
        from mojo import errors as merrors
        from mojo.apps.account.services import disable as disable_service
        if not isinstance(value, dict):
            value = {}
        if not self.active_user.has_permission("manage_groups"):
            raise merrors.PermissionDeniedException("manage_groups required to disable a group")
        reason = value.get("reason")
        if reason not in disable_service.GROUP_REST_REASONS:
            allowed = ", ".join(sorted(disable_service.GROUP_REST_REASONS))
            raise merrors.ValueException(f"reason must be one of: {allowed}")
        disable_service.disable_entity(
            self,
            reason=reason,
            note=value.get("note"),
            by_user=self.active_user,
            request=self.active_request,
        )

    def on_action_reactivate(self, value):
        from mojo import errors as merrors
        from mojo.apps.account.services import disable as disable_service
        if not isinstance(value, dict):
            value = {}
        if not self.active_user.has_permission("manage_groups"):
            raise merrors.PermissionDeniedException("manage_groups required to reactivate a group")
        disable_service.reactivate_entity(
            self,
            note=value.get("note"),
            by_user=self.active_user,
            request=self.active_request,
        )

    def on_rest_pre_save(self, changed_fields, created):
        # Reject an invalid auth config at write time so a bad
        # metadata.auth_config surfaces as a 400 here, not a render error later.
        auth_cfg = (self.metadata or {}).get("auth_config")
        if auth_cfg is not None:
            from mojo.apps.account.services.auth_config import validate_auth_config
            validate_auth_config(auth_cfg)
        # Same for geofence rules — a typo'd rule must 400 here, not surface
        # as a request-time rule_invalid deny. Validation runs on the merged
        # metadata, so partial REST updates are checked post-merge.
        gf_rule = (self.metadata or {}).get("geofence")
        if gf_rule is not None:
            from mojo import errors as merrors
            from mojo.apps.account.services.geofence.dsl import validate_rule
            try:
                validate_rule(gf_rule)
            except ValueError as exc:
                raise merrors.ValueException(f"Invalid geofence rule: {exc}")
        # geofence_strict is the per-group strict-posture override: tri-state
        # (absent/null = inherit the global GEOFENCE_STRICT_POSTURE). Anything
        # non-boolean must 400 here, not silently coerce at decision time.
        gf_strict = (self.metadata or {}).get("geofence_strict")
        if gf_strict is not None and not isinstance(gf_strict, bool):
            from mojo import errors as merrors
            raise merrors.ValueException(
                "metadata.geofence_strict must be a boolean (true/false) "
                "or null to inherit the global posture")
        # Changing the override is a compliance-posture change: it requires the
        # same GLOBAL trust level as the global switch (a tenant admin who can
        # merely edit the group must not opt their group out of a
        # platform-mandated strict posture). Detected by DB comparison — REST
        # JSONField merges don't register in changed_fields.
        old_strict = None
        if not created and self.pk:
            old_md = type(self).objects.filter(
                pk=self.pk).values_list("metadata", flat=True).first()
            old_strict = (old_md or {}).get("geofence_strict")
        if gf_strict != old_strict:
            from mojo import errors as merrors
            actor = getattr(self, "active_user", None)
            if actor is None or not actor.has_permission(
                    ["manage_geofence", "security"]):
                raise merrors.PermissionDeniedException(
                    "Changing metadata.geofence_strict requires the global "
                    "manage_geofence (or security) permission")
            # emit the config-change evidence after the save lands
            self._geofence_strict_change = (old_strict, gf_strict)

    def on_rest_created(self):
        metrics.set_value("total_groups", Group.objects.filter(is_active=True).count(), account="global")

    def on_rest_saved(self, changed_fields, created):
        if "is_active" in changed_fields:
            metrics.set_value("total_groups", Group.objects.filter(is_active=True).count(), account="global")
        if "auth_domain" in changed_fields or "is_active" in changed_fields:
            self._invalidate_auth_domain_cache(changed_fields)
        # JSONField merges don't register in changed_fields, so invalidate this
        # group's cached geofence decisions on every update — a rule edit must
        # not serve stale allows for up to GEOFENCE_CACHE_TTL. Bounded scan.
        if not created:
            self._invalidate_geofence_decisions()
        # A strict-posture flip is compliance evidence (change detected and
        # stashed in on_rest_pre_save).
        change = getattr(self, "_geofence_strict_change", None)
        if change is not None:
            self._geofence_strict_change = None
            try:
                from mojo.apps.account.services.geofence import evidence
                evidence.report_config_change(
                    f"group:{self.pk}", old=change[0], new=change[1],
                    request=getattr(self, "active_request", None),
                    user=getattr(self, "active_user", None))
            except Exception:
                pass

    def _invalidate_geofence_decisions(self):
        try:
            from mojo.apps.account.services.geofence import cache as gf_cache
            gf_cache.invalidate_group(self.pk)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Auth domain cache (white-label auth pages)
    # ------------------------------------------------------------------

    AUTH_DOMAIN_CACHE_PREFIX = "auth_domain:"

    def _invalidate_auth_domain_cache(self, changed_fields):
        """Clear stale auth_domain→group_id cache entries on save."""
        try:
            from mojo.helpers.redis import get_connection
            r = get_connection()
            if not r:
                return
            # If auth_domain changed, clear the old hostname entry
            if "auth_domain" in changed_fields:
                # Try to find old value — tracker may not have it, so clear broadly
                # by scanning for any cache entry pointing to this group
                for key in r.scan_iter(f"{self.AUTH_DOMAIN_CACHE_PREFIX}*"):
                    cached = r.get(key)
                    if cached and int(cached) == self.pk:
                        r.delete(key)
            # Set the new entry if group is active and has auth_domain
            if self.is_active and self.auth_domain:
                r.set(f"{self.AUTH_DOMAIN_CACHE_PREFIX}{self.auth_domain}", self.pk, ex=86400)
            elif self.auth_domain:
                # Group deactivated — clear the cache entry
                r.delete(f"{self.AUTH_DOMAIN_CACHE_PREFIX}{self.auth_domain}")
        except Exception:
            pass

    @classmethod
    def resolve_by_auth_domain(cls, hostname):
        """Look up active group by auth_domain. Cached in Redis."""
        if not hostname:
            return None
        try:
            from mojo.helpers.redis import get_connection
            r = get_connection()
            if r:
                cached = r.get(f"{cls.AUTH_DOMAIN_CACHE_PREFIX}{hostname}")
                if cached is not None:
                    if cached == b'0':
                        return None  # negative cache — hostname not mapped
                    group_id = int(cached)
                    return cls.objects.filter(pk=group_id, is_active=True).first()
        except Exception:
            pass
        # DB lookup
        group = cls.objects.filter(auth_domain=hostname, is_active=True).first()
        try:
            from mojo.helpers.redis import get_connection
            r = get_connection()
            if r:
                if group:
                    r.set(f"{cls.AUTH_DOMAIN_CACHE_PREFIX}{hostname}", group.pk, ex=86400)
                else:
                    # Negative cache — avoid repeated DB misses for unknown hostnames
                    r.set(f"{cls.AUTH_DOMAIN_CACHE_PREFIX}{hostname}", 0, ex=3600)
        except Exception:
            pass
        return group

    @classmethod
    def on_rest_handle_list(cls, request):
        if cls.rest_check_permission(request, "VIEW_PERMS"):
            return cls.on_rest_list(request)

        # Check if user has group-level permissions (includes parent chain and children)
        if request.user.is_authenticated:
            perms = cls.get_rest_meta_prop("VIEW_PERMS", [])
            groups_with_perms = request.user.get_groups_with_permission(perms)

            # Also include all groups where user is a member (even without specific perms)
            # This matches the behavior of check_view_permission which allows members to view
            all_user_groups = request.user.get_groups(is_active=True)

            # Combine both querysets
            combined_ids = set(groups_with_perms.values_list('id', flat=True)) | set(all_user_groups.values_list('id', flat=True))

            if combined_ids:
                return cls.on_rest_list(request, cls.objects.filter(id__in=combined_ids))
            else:
                # Authenticated user with no groups - return empty list (not 403)
                return cls.on_rest_list(request, cls.objects.none())

        from mojo.errors import PermissionDeniedException
        view_perms = cls.get_rest_meta_prop("VIEW_PERMS", [])

        if not request.user.is_authenticated:
            raise PermissionDeniedException(
                reason=f"GET permission denied: {cls.__name__}",
                status=401,
                code=401,
                model_name=cls.__name__,
                perms=list(view_perms) if view_perms else None,
                permission_keys="VIEW_PERMS",
                branch="list_unauthenticated_group",
                event_type="unauthenticated",
            )

        if MOJO_REST_LIST_PERM_DENY:
            raise PermissionDeniedException(
                reason=f"GET permission denied: {cls.__name__}",
                status=403,
                code=403,
                model_name=cls.__name__,
                perms=list(view_perms) if view_perms else None,
                permission_keys="VIEW_PERMS",
                branch="list_perm_deny_group",
                event_type="user_permission_denied",
            )
        return cls.on_rest_list(request, cls.objects.none())
