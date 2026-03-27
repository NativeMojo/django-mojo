from django.contrib.auth.models import AbstractBaseUser, BaseUserManager
from django.db import models
from mojo.models import MojoModel, MojoSecrets, MojoAuthMixin
from mojo.helpers.settings import settings
from mojo import errors as merrors
from mojo.helpers import dates
from mojo.helpers import content_guard
from mojo.apps.account.utils.jwtoken import JWToken
from mojo.apps import metrics
from .device import UserDevice
from objict import objict
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

USER_PERMS_PROTECTION = settings.get_static("USER_PERMS_PROTECTION", {})
USER_PERMS_PROTECTION.update(SYS_USER_PERMS_PROTECTION)

USER_LAST_ACTIVITY_FREQ = settings.get_static("USER_LAST_ACTIVITY_FREQ", 300)

# Fields that only a superuser may write via REST, on both create and update paths.
#   is_email_verified / is_phone_verified  — set only by internal token flows
#   requires_mfa                           — disabling MFA is a security policy decision
#
# Note: auth_key and last_activity are NOT listed here because they are in
# NO_SAVE_FIELDS — the REST framework silently ignores them for everyone,
# which is a stronger guarantee than a permission check.
SUPERUSER_ONLY_FIELDS = frozenset((
    "is_email_verified", "is_phone_verified",
    "is_dob_verified",
    "requires_mfa",
))

# Fields that require manage_users permission (superusers implicitly qualify).
#   is_active       — activating/deactivating an account is an admin action
#   org / org_id    — org assignment controls token TTLs and push config;
#                     both names checked because Django FK fields appear in
#                     changed_fields as the attrib name ("org") when set via
#                     setattr, but REST may also pass "org_id" directly
MANAGE_USERS_ONLY_FIELDS = frozenset(("is_active", "org", "org_id"))
METRICS_TIMEZONE = settings.get_static("METRICS_TIMEZONE", "America/Los_Angeles")
METRICS_TRACK_USER_ACTIVITY = settings.get_static("METRICS_TRACK_USER_ACTIVITY", False)

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

class User(MojoSecrets, MojoAuthMixin, AbstractBaseUser, MojoModel):
    """
    Full custom user model.
    """
    created = models.DateTimeField(auto_now_add=True, editable=False)
    modified = models.DateTimeField(auto_now_add=True, editable=True)
    last_activity = models.DateTimeField(default=None, null=True, db_index=True)

    uuid = models.UUIDField(default=uuid.uuid4, editable=False, db_index=True)
    username = models.TextField(unique=True)
    email = models.EmailField(unique=True)
    phone_number = models.CharField(max_length=32, blank=True, null=True, default=None, unique=True)
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
    is_dob_verified = models.BooleanField(default=False)
    requires_mfa = models.BooleanField(default=False)

    dob = models.DateField(null=True, blank=True, default=None)

    avatar = models.ForeignKey('fileman.File', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+')

    USERNAME_FIELD = 'username'
    objects = CustomUserManager()

    class RestMeta:
        LOG_CHANGES = True
        POST_SAVE_ACTIONS = ['send_invite']
        NO_SHOW_FIELDS = ["password", "auth_key", "onetime_code"]
        # auth_key and last_activity must never be writable via REST by anyone.
        # auth_key is the JWT signing secret — writing it is a session-invalidation
        # attack vector. last_activity is a server-managed audit timestamp.
        # Superusers who need to rotate auth_key or correct last_activity should
        # do so via direct DB access or a dedicated management command.
        NO_SAVE_FIELDS = ["auth_key", "last_activity", "is_dob_verified"]
        SEARCH_FIELDS = ["username", "email", "display_name", "phone_number"]
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
                    'is_active',
                    "is_email_verified",
                    "is_phone_verified",
                    "is_dob_verified"
                ],
                "graphs": {
                    "avatar": "basic"
                }
            },
            "list": {
                "fields": [
                    'id',
                    "first_name",
                    "last_name",
                    'display_name',
                    'username',
                    'email',
                    'phone_number',
                    'last_login',
                    'last_activity',
                    'permissions',
                    'metadata',
                    'is_active',
                    "is_superuser",
                    "is_email_verified",
                    "is_phone_verified",
                    "is_dob_verified",
                    "is_online",
                    "dob"
                ],
                "graphs": {
                    "avatar": "basic",
                    "org": "basic"
                }
            },
            "default": {
                "fields": [
                    'id',
                    "first_name",
                    "last_name",
                    'display_name',
                    'username',
                    'email',
                    'phone_number',
                    'last_login',
                    'last_activity',
                    'permissions',
                    'metadata',
                    'is_active',
                    "is_superuser",
                    "is_email_verified",
                    "is_phone_verified",
                    "is_dob_verified",
                    "dob",
                    "has_passkey"
                ],
                "graphs": {
                    "avatar": "basic",
                    "org": "basic"
                }
            },
            "full": {
                "extra": [
                    "full_name", "has_passkey"
                ],
                "graphs": {
                    "avatar": "basic"
                }
            }
        }

    def __str__(self):
        return self.email

    def get_age(self):
        """
        Calculate current age in whole years from `dob`.
        Returns None if `dob` is not set.
        """
        if not self.dob:
            return None
        from mojo.helpers import dates
        today = dates.utcnow().date()
        born = self.dob
        return today.year - born.year - ((today.month, today.day) < (born.month, born.day))

    @property
    def has_passkey(self):
        return self.passkeys.filter(is_enabled=True).count() > 0

    @property
    def is_online(self):
        from mojo.apps import realtime
        return realtime.is_online("user", self.id)

    @property
    def full_name(self):
        """Return full name from first/last, falling back to display_name or generated display name."""
        name = " ".join(filter(None, [self.first_name, self.last_name])).strip()
        if name:
            return name
        if self.display_name:
            return self.display_name
        return self.generate_display_name()

    def init_metadata(self):
        # this will init metadata to an objict
        return self.jsonfield_as_objict("metadata")

    def get_protected_metadata(self, key, default=None):
        meta = self.metadata or {}
        protected = meta.get("protected") or {}
        return protected.get(key, default)

    def set_protected_metadata(self, key, value):
        meta = self.init_metadata()
        if not isinstance(meta.get("protected"), objict):
            meta.protected = objict.fromdict(meta.get("protected") or {})
        meta.protected[key] = value
        self.save(update_fields=["metadata", "modified"])

    def is_request_user(self, request=None):
        if request is None:
            request = self.active_request
        if request is None:
            return False
        if hasattr(request.user, "is_request_user"):
            return request.user.id == self.id
        return False

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

    def get_groups(self, is_active=True, include_children=True):
        """
        Returns a QuerySet of all groups the user is a member of.

        Args:
            is_active: Filter by active members (default True). Set to None to get all.
            include_children: Include child groups down the parent chain (default True).
                             Set to False to get only direct memberships.

        Returns:
            QuerySet of Group objects
        """
        from mojo.apps.account.models import Group

        # Get direct groups the user is a member of
        queryset = Group.objects.filter(members__user=self)
        if is_active is not None:
            queryset = queryset.filter(members__is_active=is_active)

        # If not including children, return direct memberships only
        if not include_children:
            return queryset.distinct()

        # Collect all group IDs including children
        direct_groups = queryset
        all_group_ids = set()
        for group in direct_groups:
            all_group_ids.add(group.id)
            # Add all child group IDs
            child_ids = group._get_all_child_ids()
            all_group_ids.update(child_ids)

        # Return queryset with all groups
        queryset = Group.objects.filter(id__in=all_group_ids)
        if is_active is not None:
            queryset = queryset.filter(is_active=is_active)

        return queryset.distinct()

    def get_group_ids(self, is_active=True, include_children=True):
        """
        Returns a list of group IDs the user is a member of.

        Args:
            is_active: Filter by active members (default True). Set to None to get all.
            include_children: Include child groups down the parent chain (default True).
                             Set to False to get only direct memberships.

        Returns:
            List of group IDs
        """
        from mojo.apps.account.models import Group

        # Get direct group memberships
        queryset = self.members.all()
        if is_active is not None:
            queryset = queryset.filter(is_active=is_active)
        direct_group_ids = list(queryset.values_list('group_id', flat=True))

        # If not including children, return direct memberships only
        if not include_children:
            return direct_group_ids

        # Collect all group IDs including children
        all_group_ids = set(direct_group_ids)
        direct_groups = Group.objects.filter(id__in=direct_group_ids)
        for group in direct_groups:
            # Add all child group IDs
            child_ids = group._get_all_child_ids()
            all_group_ids.update(child_ids)

        return list(all_group_ids)

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
        # auth_key stored as a dedicated field for backwards compatibility
        if self.auth_key is None:
            self.auth_key = uuid.uuid4().hex
            self.atomic_save()
        return self.auth_key

    def set_username(self, value):
        if not isinstance(value, str):
            raise ValueError("Username must be a string")
        self.username = value

    def set_is_superuser(self, value):
        if not self.active_user.is_superuser:
            raise merrors.PermissionDeniedException("Only a superuser can grant superuser status")
        self.is_superuser = bool(value)

    def set_is_staff(self, value):
        if not self.active_user.is_superuser:
            raise merrors.PermissionDeniedException("Only a superuser can grant staff status")
        self.is_staff = bool(value)

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

    def has_perm(self, perm_key):
        return self.has_permission(perm_key)

    def has_permission(self, perm_key):
        """Check if user has a specific permission in JSON field."""
        if self.is_superuser:
            return True
        if isinstance(perm_key, (list, set)):
            for pk in perm_key:
                if self.has_permission(pk):
                    return True
            return False
        if perm_key in ["all", "authenticated"]:
            return True
        return self.permissions.get(perm_key, False)

    def add_perm(self, perm_key, value=True, commit=True):
        self.add_permission(perm_key, value, commit)

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

    def remove_perm(self, perm_key, commit=True):
        self.remove_permission(perm_key, commit)

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

    def set_phone_number(self, value):
        if not value:
            self.phone_number = None
            return
        normalized = self.normalize_phone(value)
        if not normalized:
            from mojo import errors as merrors
            raise merrors.ValueException(f"Invalid phone number: {value}")
        self.phone_number = normalized

    def validate_email(self):
        import re
        if not self.email:
            raise merrors.ValueException("Email is required")
        if not re.match(r"[^@]+@[^@]+\.[^@]+", str(self.email)):
            raise merrors.ValueException("Invalid email format")
        return True

    def validate_name_fields(self, changed_fields, created):
        """Check display_name, first_name, and last_name for inappropriate content."""
        if self.active_request and self.active_request.user.is_superuser:
            return
        fields = {
            "display_name": self.display_name,
            "first_name": self.first_name,
            "last_name": self.last_name,
        }
        for field, value in fields.items():
            if not value:
                continue
            if not created and field not in changed_fields:
                continue
            result = content_guard.check_text(value, surface="name", policy={"text_block_threshold": 50})
            if result.decision == "block":
                label = field.replace("_", " ")
                raise merrors.ValueException(f"Invalid {label}: contains inappropriate content")

    def validate_username(self):
        if not self.username:
            raise merrors.ValueException("Username is required")
        if self.active_request and self.active_request.user.is_superuser:
            return True
        if len(str(self.username)) <= 2:
            raise merrors.ValueException("Username must be more than 2 characters")
        # Check for special characters (only allow alphanumeric, underscore, dot, and @)
        import re
        if not re.match(r'^[a-zA-Z0-9_.@]+$', str(self.username)):
            raise merrors.ValueException("Username can only contain letters, numbers, underscores, dots, and @")
        # If username contains @, it must match the email field
        if '@' in str(self.username) and str(self.username) != str(self.email):
            raise merrors.ValueException("Username containing @ must match the email address")
        # Only run content guard on non-email usernames (email usernames are validated above)
        if '@' not in str(self.username):
            result = content_guard.check_username(self.username, policy={"allow_dot_in_username": True})
            if result.decision == "block":
                raise merrors.ValueException("Username is not allowed")
        return True

    def check_password_strength(self, password):
        """Raise ValueException if password does not meet minimum strength requirements."""
        import re
        strength_score = 0
        if len(password) >= 12:
            strength_score += 2
        elif len(password) >= 10:
            strength_score += 1
        if any(c.isupper() for c in password) and any(c.islower() for c in password):
            strength_score += 1
        if any(c.isdigit() for c in password):
            strength_score += 1
        if re.search(r'[!@#$%^&*(),.?":{}|<>]', password):
            strength_score += 1
        if strength_score < 2:
            raise merrors.ValueException("Password is too weak. Use a longer password or include a mix of uppercase, lowercase, numbers, and special characters")

    def set_new_password(self, new_password, old_password=None):
        if self.active_request:
            old_password = self.active_request.DATA.get("current_password", None)
            if not old_password and not self.active_request.user.has_permission("manage_users"):
                raise merrors.ValueException("You must provide your current password")
        if old_password and not self.check_password(old_password):
            self.report_incident(f"{self.username} entered an invalid password", "invalid_password")
            raise merrors.ValueException("Incorrect current password")
        self.check_password_strength(new_password)
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

    def infer_names_from_email(self):
        """
        Best-effort attempt to extract first/last name from a business email like john.smith@company.com.
        Only runs if:
          - first_name and last_name are both currently empty
          - the local part contains exactly one dot (two parts)
          - the domain is not a known consumer provider
        """
        CONSUMER_DOMAINS = {
            "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
            "icloud.com", "me.com", "mac.com", "live.com", "msn.com",
            "aol.com", "protonmail.com", "pm.me"
        }
        if self.first_name or self.last_name:
            return
        if not self.email or "@" not in self.email:
            return
        local, domain = self.email.lower().split("@", 1)
        if domain in CONSUMER_DOMAINS:
            return
        parts = local.split(".")
        if len(parts) != 2:
            return
        first, last = parts
        # Sanity check — skip single-char or obviously non-name parts
        if len(first) < 2 or len(last) < 2:
            return
        self.first_name = first.capitalize()
        self.last_name = last.capitalize()

    def on_rest_created(self):
        self.infer_names_from_email()
        if self.first_name or self.last_name:
            User.objects.filter(pk=self.pk).update(first_name=self.first_name, last_name=self.last_name)
        metrics.set_value("total_users", User.objects.filter(is_active=True).count(), account="global")

    def on_rest_saved(self, changed_fields, created):
        if "is_active" in changed_fields:
            metrics.set_value("total_users", User.objects.filter(is_active=True).count(), account="global")

    def on_rest_pre_save(self, changed_fields, created):
        for _field in SUPERUSER_ONLY_FIELDS:
            if _field in changed_fields and not self.active_user.is_superuser:
                raise merrors.PermissionDeniedException(f"You are not allowed to change {_field}")
        for _field in MANAGE_USERS_ONLY_FIELDS:
            if _field in changed_fields and not self.active_user.has_permission("manage_users"):
                raise merrors.PermissionDeniedException(f"You are not allowed to change {_field}")
        if "dob" in changed_fields:
            self.is_dob_verified = False
        if "email" in changed_fields:
            self.validate_email()
            self.email = self.email.lower()
            self.is_email_verified = False
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
            self.validate_username()
            self.username = self.username.lower()
            qset = User.objects.filter(username=self.username)
            if self.pk is not None:
                qset = qset.exclude(pk=self.pk)
            if qset.exists():
                raise merrors.ValueException("Username already exists")
        if "phone_number" in changed_fields:
            if self.phone_number:
                normalized = self.normalize_phone(self.phone_number)
                if not normalized:
                    raise merrors.ValueException("Invalid phone number format")
                self.phone_number = normalized
                qset = User.objects.filter(phone_number=self.phone_number)
                if self.pk is not None:
                    qset = qset.exclude(pk=self.pk)
                if qset.exists():
                    raise merrors.ValueException("Phone number already in use")
            self.is_phone_verified = False
        if not self.display_name:
            self.display_name = self.generate_display_name()
        self.validate_name_fields(changed_fields, created)
        if self.pk is not None:
            self._handle_existing_user_pre_save(changed_fields)

    def _handle_existing_user_pre_save(self, changed_fields):
        creds_changed = "email" in changed_fields or "username" in changed_fields
        # only superuser can change email or username (guard already fired in on_rest_pre_save
        # for the REST path; this catches any direct programmatic call to this method)
        if creds_changed and not self.active_user.is_superuser:
            raise merrors.PermissionDeniedException("You are not allowed to change email or username")
        if "phone_number" in changed_fields and not self.active_user.is_superuser:
            old_phone = changed_fields.get("phone_number")
            # Block replacing an existing phone number directly — must use the phone change
            # flow (POST /api/auth/phone/change/request → confirm) so ownership of the new
            # number is verified via OTP before it is committed.
            # Clearing a phone number or setting one for the first time is always allowed.
            if old_phone and self.phone_number:
                raise merrors.PermissionDeniedException(
                    "Use the phone change flow to update an existing phone number")
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
        if "phone_number" in changed_fields:
            old_phone = changed_fields.get("phone_number")
            self.log(kind="phone:changed", log=f"{old_phone} to {self.phone_number}")
        if "is_active" in changed_fields:
            if not self.is_active:
                metrics.record("user_deactivated", category="user", min_granularity="hours")

    def check_edit_permission(self, perms, request):
        if "owner" in perms and self.is_request_user():
            return True
        return request.user.has_permission(perms)

    def on_action_send_invite(self, value):
        self.send_invite()

    def pii_anonymize(self):
        """
        Anonymize all personally identifiable information on this user row.

        Satisfies GDPR Article 17 (right to erasure) while preserving the row
        for FK integrity and audit trail. After this call the user is
        permanently deactivated and cannot log in.

        Returns a summary dict of what was deleted/cleared.
        """
        token = uuid.uuid4().hex[:12]
        summary = {"user_id": self.pk, "token": token}

        # ── 1. Anonymize core PII fields ─────────────────────────────────────
        self.username = f"deleted-{token}"
        self.email = f"deleted-{token}@deleted.local"
        self.phone_number = None
        self.display_name = None
        self.first_name = ""
        self.last_name = ""
        self.dob = None
        self.metadata = {}
        self.onetime_code = None
        self.avatar = None
        self.org = None
        # ── 2. Revoke access ──────────────────────────────────────────────────
        self.auth_key = uuid.uuid4().hex        # invalidates all active JWTs
        self.is_active = False
        self.is_staff = False
        self.is_superuser = False
        self.permissions = {}
        self.is_email_verified = False
        self.is_phone_verified = False
        self.is_dob_verified = False
        self.save(update_fields=[
            "username", "email", "phone_number", "display_name",
            "first_name", "last_name", "metadata", "onetime_code",
            "avatar", "org", "auth_key", "is_active", "is_staff",
            "is_superuser", "permissions", "is_email_verified",
            "is_phone_verified", "is_dob_verified", "dob", "modified",
        ])

        # ── 3. Wipe secrets (MojoSecrets encrypted JSON field) ────────────────
        try:
            self.mojo_secrets = {}
            self.save(update_fields=["mojo_secrets"])
        except Exception:
            pass

        # ── 4. Delete passkeys ────────────────────────────────────────────────
        try:
            from mojo.apps.account.models.passkey import PassKey
            n, _ = PassKey.objects.filter(user=self).delete()
            summary["deleted_passkeys"] = n
        except Exception:
            summary["deleted_passkeys"] = 0

        # ── 5. Delete push/notification devices ───────────────────────────────
        try:
            from mojo.apps.account.models.push.device import Device
            n, _ = Device.objects.filter(user=self).delete()
            summary["deleted_devices"] = n
        except Exception:
            summary["deleted_devices"] = 0

        # ── 6. Delete TOTP devices ────────────────────────────────────────────
        try:
            from mojo.apps.account.models.totp import TOTPDevice
            n, _ = TOTPDevice.objects.filter(user=self).delete()
            summary["deleted_totp"] = n
        except Exception:
            summary["deleted_totp"] = 0

        # ── 7. Delete inbox notifications (may contain PII in title/body) ─────
        try:
            from mojo.apps.account.models.notification import Notification
            n, _ = Notification.objects.filter(user=self).delete()
            summary["deleted_notifications"] = n
        except Exception:
            summary["deleted_notifications"] = 0

        # ── 8. Remove group memberships ───────────────────────────────────────
        try:
            from mojo.apps.account.models.member import GroupMember
            n, _ = GroupMember.objects.filter(user=self).delete()
            summary["deleted_memberships"] = n
        except Exception:
            summary["deleted_memberships"] = 0

        return summary

    def notify(self, title, body="", kind="general", data=None,
               action_url=None, expires_in=3600, push=True, ws=True):
        """
        Create an inbox notification and deliver via WebSocket + device push.
        Use this as the standard way to notify a user.
        """
        from mojo.apps.account.models.notification import Notification
        return Notification.send(
            title, body, user=self, kind=kind, data=data,
            action_url=action_url, expires_in=expires_in, push=push, ws=ws,
        )

    def push_notification(self, title=None, body=None, data=None,
                          category="general", action_url=None, kind=None):
        """
        Send push notification to all user's active devices.
        Simple - just loops through devices and calls device.send().

        Args:
            title: Notification title (optional for silent notifications)
            body: Notification body (optional for silent notifications)
            data: Custom data payload dict
            category: Notification category
            action_url: URL to open when notification is tapped
            kind: Optional notification kind for preference check.
                  When provided, the user's notification preferences are
                  checked before sending. Omit for system/transactional pushes.

        Returns:
            List of NotificationDelivery objects
        """
        if kind:
            from mojo.apps.account.services.notification_prefs import is_notification_allowed
            if not is_notification_allowed(self, kind, "push"):
                return []

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

    def send_invite(self, group=None, request=None, **kwargs):
        from mojo.apps.account.utils import tokens
        from mojo.apps.account.utils.webapp_url import build_token_url
        from mojo.apps.shortlink import maybe_shorten_url

        token = tokens.get_or_generate_invite_token(self)
        token_url = build_token_url("invite", token, request=request, user=self, group=group)
        token_url = maybe_shorten_url(token_url, source="invite", user=self, expire_days=7)

        context = {
            "user": self.to_dict("basic"),
            "token": token,
            "token_url": token_url,
        }
        for key, value in kwargs.items():
            if hasattr(value, 'to_dict'):
                context[key] = value.to_dict('basic')
            elif isinstance(value, (str, int, float)):
                context[key] = value

        self.send_template_email(
            template_name="invite",
            group=group,
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
        fail_silently=True,
        **kwargs
    ):
        """Send email to this user using mailbox determined by user's org domain or system default

        Args:
            subject: Email subject
            body_text: Optional plain text body
            body_html: Optional HTML body
            cc, bcc, reply_to: Optional addressing
            fail_silently: If True (default), log failures via incident reporting instead of raising
            **kwargs: Additional arguments passed to mailbox.send_email()

        Returns:
            SentMessage instance or None if fail_silently and no mailbox
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
            msg = "No mailbox available for sending email. Please configure a system default mailbox."
            if fail_silently:
                self.report_incident(msg, "email:no_mailbox", level=6)
                return None
            raise ValueError(msg)

        try:
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
        except Exception as e:
            if fail_silently:
                self.report_incident(f"email send failed: {e}", "email:send_failed", level=6)
                return None
            raise

    def send_template_email(
        self,
        template_name,
        context=None,
        cc=None,
        bcc=None,
        reply_to=None,
        template_prefix=None,
        fail_silently=True,
        group=None,
        kind=None,
        **kwargs
    ):
        """Send template email to this user using mailbox determined by user's org domain or system default

        Args:
            template_name: Name of the EmailTemplate in database
            context: Template context variables (user will be added automatically)
            cc, bcc, reply_to: Optional addressing
            fail_silently: If True (default), log failures via incident reporting instead of raising
            kind: Optional notification kind string (e.g. "marketing"). When provided,
                  the user's notification preferences are checked before sending.
                  System/transactional emails should NOT pass kind so they are never suppressed.
            **kwargs: Additional arguments passed to mailbox.send_template_email()

        Returns:
            SentMessage instance or None if fail_silently and send fails
        """
        # Check notification preferences if a kind is provided.
        # System/transactional emails (password reset, verification, etc.) do NOT
        # pass kind and are therefore never suppressed.
        if kind:
            from mojo.apps.account.services.notification_prefs import is_notification_allowed
            if not is_notification_allowed(self, kind, "email"):
                return None

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
            msg = "No mailbox available for sending email. Please configure a system default mailbox."
            if fail_silently:
                self.report_incident(msg, "email:no_mailbox", level=6)
                return None
            raise ValueError(msg)

        if group is not None:
            template_prefix = group.get_metadata_value("email_template")
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
        if group is not None:
            context["group"] = group.to_dict('basic')

        try:
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
        except Exception as e:
            if fail_silently:
                self.report_incident(f"template email send failed ({template_name}): {e}", "email:send_failed", level=6)
                return None
            raise

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


        # Chat message routing
        if mtype and mtype.startswith("chat_"):
            from mojo.apps.chat.handler import handle_chat_message
            result = handle_chat_message(self, data)
            if result:
                return {"response": result}
            return None

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
        if topic.startswith("chat:"):
            from mojo.apps.chat.models import ChatRoom, ChatMembership
            try:
                room_id = int(topic.split(":")[1])
            except (IndexError, ValueError):
                return False
            room = ChatRoom.objects.filter(pk=room_id).first()
            if not room:
                return False
            # Banned users cannot subscribe
            membership = ChatMembership.objects.filter(room=room, user=self).first()
            if membership and membership.status == "banned":
                return False
            # Group-linked room: check group permission
            if room.group:
                return room.group.user_has_permission(self, ["chat", "manage_chat"])
            # Non-group room: check membership exists and is active/muted
            return membership is not None and membership.status in ("active", "muted")
        if topic.startswith("group:"):
            from .group import Group
            if self.has_permission(["view_groups", "manage_groups"]):
                return True
            group = Group.objects.filter(pk=int(topic.split(":")[1])).last()
            if group is None:
                return False
            return group.get_member_for_user(self, check_parents=True) is not None
        if topic == f"user:{self.id}":
            return True
        if topic == "general_announcements":
            return True
        return False

    @classmethod
    def normalize_phone(cls, phone_number):
        from mojo.apps.phonehub.services.phonenumbers import normalize
        return normalize(phone_number)

    @classmethod
    def lookup_from_request(cls, request, phone_as_username=False):
        username = request.DATA.get("username", "").lower().strip()
        email = request.DATA.get("email", "").lower().strip()
        if not email and username and "@" in username:
            email = username
        phone_number = request.DATA.get("phone_number")
        if not phone_number and phone_as_username:
            phone_number = username
        return cls.lookup(username=username, email=email, phone_number=phone_number)

    @classmethod
    def lookup_from_request_with_source(cls, request, phone_as_username=False):
        username = request.DATA.get("username", "").lower().strip()
        email = request.DATA.get("email", "").lower().strip()
        phone_number = request.DATA.get("phone_number")

        if not email and username and "@" in username:
            email = username

        if phone_number:
            user = cls.lookup(phone_number=phone_number)
            if user:
                return user, "phone_number"
        if not phone_number and phone_as_username and username and "@" not in username:
            normalized = cls.normalize_phone(username)
            if normalized:
                user = cls.lookup(phone_number=normalized)
                if user:
                    return user, "phone_number"
        if email:
            user = cls.lookup(email=email)
            if user:
                return user, "email"
        if username:
            user = cls.lookup(username=username)
            if user:
                return user, "username"
        return None, None

    @classmethod
    def lookup(cls, username=None, email=None, phone_number=None):
        from django.db.models import Q
        if not username and not email and not phone_number:
            return None
        q = None
        if username:
            q = Q(username=username)
        if email:
            if q is None:
                q = Q(email=email)
            else:
                q |= Q(email=email)
        if phone_number:
            phone_number = cls.normalize_phone(phone_number)
        if phone_number:
            if q is None:
                q = Q(phone_number=phone_number)
            else:
                q |= Q(phone_number=phone_number)
        return User.objects.filter(q).first()

    @classmethod
    def validate_jwt(cls, token, request=None):
        from mojo.helpers import dates
        token_manager = JWToken()
        try:
            jwt_data = token_manager.decode(token, validate=False)
        except Exception:
            return None, "Invalid token"
        if jwt_data.uid is None:
            return None, "Invalid token data"

        if jwt_data.get("token_type") == "user_api_key":
            from mojo.apps.account.models.user_api_key import UserAPIKey
            jti = jwt_data.get("jti")
            if not jti:
                return None, "Invalid token: missing jti"
            key_record = UserAPIKey.objects.filter(jti=jti, is_active=True).select_related("user").first()
            if key_record is None:
                return None, "Invalid token: api key not found or revoked"
            if dates.utcnow() > key_record.expires:
                return None, "Token expired"
            token_manager.key = key_record.get_auth_key()
            if not token_manager.is_token_valid(token):
                return None, "Token has invalid signature"
            if key_record.allowed_ips and request and request.ip not in key_record.allowed_ips:
                return None, "Not allowed from this location"
            try:
                UserAPIKey.objects.filter(pk=key_record.pk).update(last_used=dates.utcnow())
            except Exception:
                pass
            return key_record.user, None

        user = User.objects.filter(id=jwt_data.uid).last()
        if user is None:
            return None, "Invalid token user"
        token_manager.key = user.auth_key
        if not token_manager.is_token_valid(token):
            if token_manager.is_expired:
                return user, "Token expired"
            return user, "Token has invalid signature"
        # verify ip address is allowed
        if isinstance(jwt_data.get("allowed_ips"), list):
            if request and request.ip not in jwt_data.allowed_ips:
                return user, "Not allowed from location"
        user.track()
        return user, None
