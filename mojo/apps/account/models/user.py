from django.contrib.auth.models import AbstractBaseUser, BaseUserManager
from django.db import models
from mojo.models import MojoModel, MojoSecrets
from mojo.helpers.settings import settings
from mojo import errors as merrors
from mojo.helpers import dates
from mojo.apps.account.utils.jwtoken import JWToken
from mojo.apps import metrics
from .device import UserDevice
import uuid

SYS_USER_PERMS_PROTECTION = {
    "manage_users": "manage_users",
    "manage_groups": "manage_users",
    "view_logs": "manage_users",
    "view_incidents": "manage_users",
    "view_admin": "manage_users",
    "view_taskqueue": "manage_users",
    "view_global": "manage_users",
    "manage_notifications": "manage_users",
    "manage_files": "manage_users",
    "force_single_session": "manage_users",
    "file_vault": "manage_users",
    "manage_aws": "manage_users"
}

USER_PERMS_PROTECTION = settings.get("USER_PERMS_PROTECTION", {})
USER_PERMS_PROTECTION.update(SYS_USER_PERMS_PROTECTION)

USER_LAST_ACTIVITY_FREQ = settings.get("USER_LAST_ACTIVITY_FREQ", 300)
METRICS_TIMEZONE = settings.get("METRICS_TIMEZONE", "America/Los_Angeles")
METRICS_TRACK_USER_ACTIVITY = settings.get("METRICS_TRACK_USER_ACTIVITY", False)

class CustomUserManager(BaseUserManager):
    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError("The Email field must be set")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        return self.create_user(email, password, **extra_fields)

    def get_by_natural_key(self, username):
        """Required for Django authentication"""
        return self.get(**{self.model.USERNAME_FIELD: username})

class User(MojoSecrets, AbstractBaseUser, MojoModel):
    """
    Full custom user model.
    """
    created = models.DateTimeField(auto_now_add=True, editable=False)
    modified = models.DateTimeField(auto_now_add=True, editable=True)
    last_activity = models.DateTimeField(default=None, null=True, db_index=True)

    uuid = models.UUIDField(default=uuid.uuid4, editable=False, db_index=True)
    username = models.TextField(unique=True)
    email = models.EmailField(unique=True)
    phone_number = models.CharField(max_length=32, blank=True, null=True, default=None)
    is_active = models.BooleanField(default=True, db_index=True)
    display_name = models.CharField(max_length=80, blank=True, null=True, default=None)

    # Organization relationship for push config resolution
    org = models.ForeignKey("account.Group", on_delete=models.SET_NULL,
                           null=True, blank=True, related_name="org_users",
                           help_text="Default organization for this user")
    # key used for sessions and general authentication algs
    auth_key = models.TextField(null=True, default=None)
    onetime_code = models.TextField(null=True, default=None)
    # JSON-based permissions field
    permissions = models.JSONField(default=dict, blank=True)
    # JSON-based metadata field
    metadata = models.JSONField(default=dict, blank=True)

    # required default fields
    first_name = models.CharField(max_length=80, default="")
    last_name = models.CharField(max_length=80, default="")
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)  # Required for admin access
    is_superuser = models.BooleanField(default=False)
    date_joined = models.DateTimeField(auto_now_add=True)

    is_email_verified = models.BooleanField(default=False)
    is_phone_verified = models.BooleanField(default=False)

    avatar = models.ForeignKey('fileman.File', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+')

    USERNAME_FIELD = 'username'
    objects = CustomUserManager()

    class RestMeta:
        LOG_CHANGES = True
        POST_SAVE_ACTIONS = ['send_invite']
        NO_SHOW_FIELDS = ["password", "auth_key", "onetime_code"]
        SEARCH_FIELDS = ["username", "email", "display_name"]
        VIEW_PERMS = ["view_users", "manage_users", "owner"]
        SAVE_PERMS = ["manage_users", "owner"]
        OWNER_FIELD = "self"
        LIST_DEFAULT_FILTERS = {
            "is_active": True
        }
        UNIQUE_LOOKUP = ["username", "email"]
        GRAPHS = {
            "basic": {
                "fields": [
                    'id',
                    'display_name',
                    'username',
                    'last_login',
                    'last_activity',
                    'is_active'
                ],
                "graphs": {
                    "avatar": "basic"
                }
            },
            "default": {
                "fields": [
                    'id',
                    'display_name',
                    'username',
                    'email',
                    'phone_number',
                    'last_login',
                    'last_activity',
                    'permissions',
                    'metadata',
                    'is_active'
                ],
                "graphs": {
                    "avatar": "basic",
                    "org": "basic"
                }
            },
            "full": {
                "graphs": {
                    "avatar": "basic"
                }
            }
        }

    def __str__(self):
        return self.email

    def is_request_user(self, request=None):
        if request is None:
            request = self.active_request
        if request is None:
            return False
        return request.user.id == self.id

    def touch(self):
        # can't subtract offset-naive and offset-aware datetimes
        if self.last_activity is None or dates.has_time_elsapsed(self.last_activity, seconds=USER_LAST_ACTIVITY_FREQ):
            if self.last_activity and not dates.is_today(self.last_activity, METRICS_TIMEZONE):
                metrics.record("user_activity_day", category="user", min_granularity="days")
            self.last_activity = dates.utcnow()
            self.atomic_save()
        if METRICS_TRACK_USER_ACTIVITY:
            metrics.record(f"user_activity:{self.pk}", category="user", min_granularity="minutes")

    def track(self):
        self.touch()
        req = self.active_request
        if req:
            req.device = UserDevice.track(request=req, user=self)

    def get_groups(self, is_active=True):
        """
        Returns a QuerySet of all groups the user is a member of.

        Args:
            is_active: Filter by active members (default True). Set to None to get all.

        Returns:
            QuerySet of Group objects
        """
        from mojo.apps.account.models import Group
        queryset = Group.objects.filter(members__user=self)
        if is_active is not None:
            queryset = queryset.filter(members__is_active=is_active)
        return queryset.distinct()

    def get_group_ids(self, is_active=True):
        """
        Returns a list of group IDs the user is a member of.

        Args:
            is_active: Filter by active members (default True). Set to None to get all.

        Returns:
            List of group IDs
        """
        queryset = self.members.all()
        if is_active is not None:
            queryset = queryset.filter(is_active=is_active)
        return list(queryset.values_list('group_id', flat=True))

    def get_groups_with_permission(self, perms, is_active=True):
        """
        Returns a list of groups where the user has the specified permission(s).
        Checks both user-level permissions and group member permissions.
        Includes child groups where user has parent membership with permissions.

        Args:
            perms: Permission key (string) or list of permission keys to check (OR logic)
            is_active: Filter by active members (default True). Set to None to get all.

        Returns:
            QuerySet of Group objects where the user has the specified permission(s)
        """
        from mojo.apps.account.models import Group

        # First check if user has system-level permissions
        if self.has_permission(perms):
            # User has system-level permission, return all groups they're a member of
            return self.get_groups(is_active=is_active)

        # Get all groups where user is directly a member with permissions
        group_ids = set()
        members_queryset = self.members.select_related('group')
        if is_active is not None:
            members_queryset = members_queryset.filter(is_active=is_active)

        # Collect groups where user has direct membership with required permissions
        parent_group_ids = []
        for member in members_queryset:
            if member.has_permission(perms):
                group_ids.add(member.group_id)
                parent_group_ids.append(member.group_id)

        # Bulk fetch all child groups for parents with permissions (optimized)
        if parent_group_ids:
            parent_groups = Group.objects.filter(id__in=parent_group_ids)
            # Collect all child IDs from each parent in one go
            for parent_group in parent_groups:
                child_ids = parent_group._get_all_child_ids()
                group_ids.update(child_ids)

        return Group.objects.filter(id__in=list(group_ids))

    def get_auth_key(self):
        if self.auth_key is None:
            self.auth_key = uuid.uuid4().hex
            self.atomic_save()
        return self.auth_key

    def set_username(self, value):
        if not isinstance(value, str):
            raise ValueError("Username must be a string")
        self.username = value

    def set_permissions(self, value):
        if not isinstance(value, dict):
            return
        for key in value:
            if key in USER_PERMS_PROTECTION:
                if not self.active_user.has_permission(USER_PERMS_PROTECTION[key]):
                    raise merrors.PermissionDeniedException()
            elif not self.active_user.has_permission("manage_users"):
                raise merrors.PermissionDeniedException()
            if bool(value[key]):
                self.add_permission(key, commit=False)
            else:
                self.remove_permission(key, commit=False)

    def has_module_perms(self, app_label):
        """Check if user has any permissions in a given app."""
        return True  # Or customize based on your `permissions` JSON

    def has_permission(self, perm_key):
        """Check if user has a specific permission in JSON field."""
        if isinstance(perm_key, (list, set)):
            for pk in perm_key:
                if self.has_permission(pk):
                    return True
            return False
        if perm_key == "all":
            return True
        return self.permissions.get(perm_key, False)

    def add_permission(self, perm_key, value=True, commit=True):
        """Dynamically add a permission."""
        changed = False
        if isinstance(perm_key, (list, set)):
            for pk in perm_key:
                if self.permissions.get(pk) != value:
                    self.permissions[pk] = value
                    changed = True
        else:
            if self.permissions.get(perm_key) != value:
                self.permissions[perm_key] = value
                changed = True
        if changed:
            self.log(f"Added permission {perm_key}", "permission:added")
        if commit and changed:
            self.save()

    def remove_permission(self, perm_key, commit=True):
        """Remove a permission."""
        changed = False
        if isinstance(perm_key, (list, set)):
            for pk in perm_key:
                if pk in self.permissions:
                    del self.permissions[pk]
                    changed = True
        else:
            if perm_key in self.permissions:
                del self.permissions[perm_key]
                changed = True
        if changed:
            self.log(f"Removed permission {perm_key}", "permission:removed")
        if commit and changed:
            self.save()

    def remove_all_permissions(self):
        self.permissions = {}
        self.save()

    def save_password(self, value):
        self.set_password(value)
        self.save()

    def validate_email(self):
        import re
        if not self.email:
            raise merrors.ValueException("Email is required")
        if not re.match(r"[^@]+@[^@]+\.[^@]+", str(self.email)):
            raise merrors.ValueException("Invalid email format")
        return True

    def validate_username(self):
        if not self.username:
            raise merrors.ValueException("Username is required")
        if len(str(self.username)) <= 2:
            raise merrors.ValueException("Username must be more than 2 characters")
        # Check for special characters (only allow alphanumeric, underscore, dot, and @)
        import re
        if not re.match(r'^[a-zA-Z0-9_.@]+$', str(self.username)):
            raise merrors.ValueException("Username can only contain letters, numbers, underscores, dots, and @")
        # If username contains @, it must match the email field
        if '@' in str(self.username) and str(self.username) != str(self.email):
            raise merrors.ValueException("Username containing @ must match the email address")
        return True

    def set_new_password(self, new_password, old_password = None):
        if self.active_request:
            old_password = self.active_request.DATA.get("current_password", None)
            if not old_password and not self.active_request.user.has_permission("manage_users"):
                raise merrors.ValueException("You must provide your current password")
        if old_password and not self.check_password(old_password):
            self.report_incident(f"{self.username} entered an invalid password", "invalid_password")
            raise merrors.ValueException("Incorrect current password")
        strength_score = 0
        # Length contributes to strength (longer is better)
        if len(new_password) >= 12:
            strength_score += 2
        elif len(new_password) >= 10:
            strength_score += 1

        # Check for mixed case
        has_upper = any(c.isupper() for c in new_password)
        has_lower = any(c.islower() for c in new_password)
        if has_upper and has_lower:
            strength_score += 1

        # Check for numbers
        has_numbers = any(c.isdigit() for c in new_password)
        if has_numbers:
            strength_score += 1

        # Check for special characters
        import re
        has_special = bool(re.search(r'[!@#$%^&*(),.?":{}|<>]', new_password))
        if has_special:
            strength_score += 1

        # Require minimum strength score
        if strength_score < 2:
            raise merrors.ValueException("Password is too weak. Use a longer password or include a mix of uppercase, lowercase, numbers, and special characters")

        self.set_password(new_password)
        self._set_field_change("new_password", "*", "*********")

    def can_change_password(self):
        if self.pk == self.active_user.pk:
            return True
        if self.active_user.is_superuser:
            return True
        if self.active_user.has_permission(["manage_users"]):
            return True
        return False

    def generate_username_from_email(self):
        """Generate a username from email, falling back to email if username exists."""
        if not self.email:
            raise merrors.ValueException("Email is required to generate username")

        # Try using the part before @ as username
        potential_username = self.email.split("@")[0].lower()

        # Check if this username already exists
        qset = User.objects.filter(username=potential_username)
        if self.pk is not None:
            qset = qset.exclude(pk=self.pk)

        # If username doesn't exist, use it
        if not qset.exists():
            return potential_username

        # Fall back to using the full email as username
        return self.email.lower()

    def generate_display_name(self):
        """Generate a display name from email, falling back to email if username exists."""
        # Try using the part before @ as display name
        # generate display name from usernames like "bob.smith", "bob_smith", "bob.smith@example.com"
        # Extract the base part (before @ if email format)
        base_username = self.username.split("@")[0] if "@" in self.username else self.username
        # Replace underscores and dots with spaces, then title case
        return base_username.replace("_", " ").replace(".", " ").title()

    def on_rest_pre_save(self, changed_fields, created):
        creds_changed = False
        if "email" in changed_fields:
            creds_changed = True
            self.validate_email()
            self.email = self.email.lower()
            if not self.username:
                self.username = self.generate_username_from_email()
            elif "@" in self.username and self.username != self.email:
                self.username = self.email
            qset = User.objects.filter(email=self.email)
            if self.pk is not None:
                qset = qset.exclude(pk=self.pk)
            if qset.exists():
                raise merrors.ValueException("Email already exists")
        if "username" in changed_fields:
            creds_changed = True
            self.validate_username()
            self.username = self.username.lower()
            qset = User.objects.filter(username=self.username)
            if self.pk is not None:
                qset = qset.exclude(pk=self.pk)
            if qset.exists():
                raise merrors.ValueException("Username already exists")
        if not self.display_name:
            self.display_name = self.generate_display_name()
        if self.pk is not None:
            self._handle_existing_user_pre_save(creds_changed, changed_fields)

    def _handle_existing_user_pre_save(self, creds_changed, changed_fields):
        # only super user can change email or username
        if creds_changed and not self.active_user.is_superuser:
            raise merrors.PermissionDeniedException("You are not allowed to change email or username")
        if "password" in changed_fields:
            raise merrors.PermissionDeniedException("You are not allowed to change password")
        if "new_password" in changed_fields:
            if not self.can_change_password():
                raise merrors.PermissionDeniedException("You are not allowed to change password")
            self.debug("CHANGING PASSWORD")
            self.log("****", kind="password:changed")
        if "email" in changed_fields:
            self.log(kind="email:changed", log=f"{changed_fields['email']} to {self.email}")
        if "username" in changed_fields:
            self.log(kind="username:changed", log=f"{changed_fields['username']} to {self.username}")
        if "is_active" in changed_fields:
            if not self.is_active:
                metrics.record("user_deactivated", category="user", min_granularity="hours")
            metrics.set_value("total_users", User.objects.filter(is_active=True).count(), account="global")

    def check_edit_permission(self, perms, request):
        if "owner" in perms and self.is_request_user():
            return True
        return request.user.has_permission(perms)

    def on_action_send_invite(self, value):
        self.send_invite()

    def push_notification(self, title=None, body=None, data=None,
                          category="general", action_url=None):
        """
        Send push notification to all user's active devices.
        Simple - just loops through devices and calls device.send().

        Args:
            title: Notification title (optional for silent notifications)
            body: Notification body (optional for silent notifications)
            data: Custom data payload dict
            category: Notification category
            action_url: URL to open when notification is tapped

        Returns:
            List of NotificationDelivery objects
        """
        devices = self.registered_devices.filter(is_active=True, push_enabled=True)

        deliveries = []
        for device in devices:
            delivery = device.send(
                title=title,
                body=body,
                data=data,
                category=category,
                action_url=action_url
            )
            if delivery:
                deliveries.append(delivery)

        return deliveries

    def send_invite(self, **kwargs):
        from mojo.apps.account.utils import tokens

        context = kwargs.copy()
        context.update({
            "user": self.to_dict("basic"),
            "token": tokens.generate_token(self)
        })

        self.send_template_email(
            template_name="invite",
            context=context
            )

    def send_email(
        self,
        subject=None,
        body_text=None,
        body_html=None,
        cc=None,
        bcc=None,
        reply_to=None,
        **kwargs
    ):
        """Send email to this user using mailbox determined by user's org domain or system default

        Args:
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

        # Try to get mailbox from org domain
        if self.org and hasattr(self.org, 'metadata'):
            domain = self.org.metadata.get("domain")
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
            to=self.email,
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
        template_name,
        context=None,
        cc=None,
        bcc=None,
        reply_to=None,
        template_prefix=None,
        **kwargs
    ):
        """Send template email to this user using mailbox determined by user's org domain or system default

        Args:
            template_name: Name of the EmailTemplate in database
            context: Template context variables (user will be added automatically)
            cc, bcc, reply_to: Optional addressing
            **kwargs: Additional arguments passed to mailbox.send_template_email()

        Returns:
            SentMessage instance

        Raises:
            ValueError: If no mailbox can be found or template not found
        """
        from mojo.apps.aws.models import Mailbox, EmailTemplate

        mailbox = None

        # Try to get mailbox from org domain
        if self.org and hasattr(self.org, 'metadata'):
            domain = self.org.metadata.get("domain")
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

        if template_prefix is None and self.org:
            template_prefix = self.org.get_metadata_value("email_template")
        if template_prefix:
            new_template_name = f"{template_prefix}_{template_name}"
            if EmailTemplate.objects.filter(name=new_template_name).exists():
                template_name = new_template_name

        # Add user to context if not already present
        if context is None:
            context = {}
        if 'user' not in context:
            context['user'] = self.to_dict("basic")

        return mailbox.send_template_email(
            to=self.email,
            template_name=template_name,
            context=context,
            cc=cc,
            bcc=bcc,
            reply_to=reply_to,
            allow_unverified=True,
            **kwargs
        )

    def on_realtime_connected(self):
        # should always self.refresh_from_db()
        meta = self.metadata or {}
        meta["realtime_connected"] = True
        try:
            meta["realtime_connected_at"] = dates.utcnow().isoformat()
        except Exception:
            # Fallback without timestamp if serialization fails
            meta["realtime_connected_at"] = None
        self.metadata = meta
        self.save(update_fields=["metadata"])

    def on_realtime_message(self, data):
        # Simple test handler logic for unit tests
        # Supports:
        # - echo: returns payload back
        # - set_meta: sets a metadata key/value and returns ack
        mtype = None
        if isinstance(data, dict):
            mtype = data.get("message_type") or data.get("type")

        if mtype == "echo":
            payload = data.get("payload") if isinstance(data, dict) else None
            return {"response": {
                "type": "echo",
                "user_id": self.id,
                "payload": payload
            }}

        if mtype == "set_meta" and isinstance(data, dict):
            key = data.get("key")
            value = data.get("value")
            if key:
                meta = self.metadata or {}
                meta[str(key)] = value
                self.metadata = meta
                self.save(update_fields=["metadata"])
                return {"response": {"type": "ack", "key": key, "value": value}}


        # Default ack for unrecognized messages
        return {"response": {"type": "ack"}}

    def on_realtime_disconnected(self):
        meta = self.metadata or {}
        meta["realtime_connected"] = False
        try:
            meta["realtime_disconnected_at"] = dates.utcnow().isoformat()
        except Exception:
            meta["realtime_disconnected_at"] = None
        self.metadata = meta
        self.save(update_fields=["metadata"])

    def on_realtime_can_subscribe(self, topic):
        if topic.startswith("group:"):
            from .group import Group
            if self.has_permission(["view_groups", "manage_groups"]):
                return True
            group = Group.objects.filter(pk=int(topic.split(":")[1])).last()
            if group is None:
                return False
            return group.get_member_for_user(self) is not None
        if topic == f"user:{self.id}":
            return True
        if topic == "general_announcements":
            return True
        return False

    @classmethod
    def validate_jwt(cls, token, request=None):
        token_manager = JWToken()
        jwt_data = token_manager.decode(token, validate=False)
        if jwt_data.uid is None:
            return None, "Invalid token data"
        user = User.objects.filter(id=jwt_data.uid).last()
        if user is None:
            return None, "Invalid token user"
        token_manager.key = user.auth_key
        if not token_manager.is_token_valid(token):
            if token_manager.is_expired:
                return user, "Token expired"
            return user, "Token has invalid signature"
        user.track()
        return user, None
