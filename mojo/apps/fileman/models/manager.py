import os
import hashlib
import json
import re
from botocore.exceptions import ClientError
from django.db import models
from django.utils import timezone
from mojo.models import MojoModel, MojoSecrets
from urllib.parse import urlparse
from mojo.helpers.settings import settings
from mojo.helpers import logit
from mojo import errors as me
from mojo.helpers.aws.provider_call import safe_error_detail

# "not resolved yet" for the per-instance CDN cache; None is a real answer.
_CDN_UNRESOLVED = object()

# A storage purpose (`use`) goes into the storage path, so it must be a plain
# short name: no dot, slash, backslash, space or wildcard (#7981).
USE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
# Purposes server code owns; a request may not select them.
FILEMAN_RESERVED_USES = ("filevault",)
# How a lookup picks among managers for one scope and purpose: the default
# first, then the oldest (#8662).
SELECTION_ORDER = ("-is_default", "id")
DUPLICATE_USE_MESSAGE = "An active file manager already exists for this purpose"


class DuplicateUseError(me.ValueException):
    """A save would make a second active manager for one scope and purpose."""


class FileManager(MojoSecrets, MojoModel):
    """
    File manager configuration for different storage backends and upload strategies
    """

    class RestMeta:
        # FileManager supports user-, group-, and system-scoped managers, so the
        # owner must never be auto-stamped to the caller — an omitted user stays None.
        CREATED_BY_OWNER_FIELD = None
        CAN_CREATE = True
        CAN_DELETE = True
        # The encrypted column, and two names that would dispatch to the
        # two-argument set_secret/set_setting.
        NO_SAVE_FIELDS = ["mojo_secrets", "secret", "setting"]
        DEFAULT_SORT = "-id"
        POST_SAVE_ACTIONS = ["test_connection", "fix_cors", "clone", "check_cors",
                             "enable_cdn", "check_cdn", "disable_cdn"]
        VIEW_PERMS = ["view_fileman", "manage_files", "files"]
        SAVE_PERMS = ["manage_files", "files"]
        SEARCH_FIELDS = ["name", "backend_type", "description"]
        SEARCH_TERMS = [
            "name", "backend_type", "description",
            ("group", "group__name")]

        GRAPHS = {
            "default": {
                "extra": ["aws_region", "aws_key_masked", "aws_secret_masked", "allowed_origins",
                          "assume_role_arn", "has_external_id", "cdn", "credentials"],
                "fields": [
                    "created", "id", "name", "use", "backend_type", "backend_url",
                    "is_active", "is_default", "is_public"],
                "graphs": {
                    "user": "basic",
                    "group": "basic"
                }
            },
            "list": {
                "extra": ["aws_region", "aws_key_masked", "aws_secret_masked", "allowed_origins",
                          "assume_role_arn", "has_external_id"],
                "fields": ["created", "id", "name", "use", "backend_type",  "backend_url",
                    "is_active", "is_default", "is_public"],
                "graphs": {
                    "user": "basic",
                    "group": "basic"
                }
            },
            "basic": {
                "extra": ["aws_region", "aws_key_masked", "aws_secret_masked", "allowed_origins"],
                "fields": [
                    "id", "name", "use", "backend_type", "backend_url",
                    "is_active", "is_default", "is_public"
                ]
            },
            "upload_policy": {
                "fields": [
                    "id", "name", "use", "is_active", "max_file_size",
                    "allowed_extensions", "allowed_mime_types",
                    "supports_direct_upload",
                ]
            }
        }

    # Storage backend types
    FILE_SYSTEM = 'file'
    AWS_S3 = 's3'
    AZURE_BLOB = 'azure'
    GOOGLE_CLOUD = 'gcs'
    CUSTOM = 'custom'

    BACKEND_CHOICES = [
        (FILE_SYSTEM, 'File System'),
        (AWS_S3, 'AWS S3'),
        (AZURE_BLOB, 'Azure Blob Storage'),
        (GOOGLE_CLOUD, 'Google Cloud Storage'),
        (CUSTOM, 'Custom Backend'),
    ]

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True)

    group = models.ForeignKey(
        "account.Group",
        related_name="file_managers",
        null=True,
        blank=True,
        default=None,
        on_delete=models.CASCADE,
        help_text="Group that owns this file manager configuration"
    )

    user = models.ForeignKey(
        "account.User",
        related_name="file_managers",
        null=True,
        blank=True,
        default=None,
        on_delete=models.CASCADE,
        help_text="User that owns this file manager configuration"
    )

    name = models.CharField(
        max_length=255,
        db_index=True,
        help_text="Descriptive name for this file manager configuration"
    )

    description = models.TextField(
        blank=True,
        default="",
        help_text="Optional description of this file manager's purpose"
    )

    backend_type = models.CharField(
        max_length=32,
        choices=BACKEND_CHOICES,
        db_index=True,
        help_text="Type of storage backend (file, s3, azure, gcs, custom)"
    )

    backend_url = models.CharField(
        max_length=500,
        help_text="Base URL or connection string for the storage backend"
    )

    supports_direct_upload = models.BooleanField(
        default=False,
        help_text="Whether this backend supports direct upload (pre-signed URLs)"
    )

    max_file_size = models.BigIntegerField(
        default=1000 * 1024 * 1024,  # 100MB default
        help_text="Maximum file size in bytes (0 for unlimited)"
    )

    allowed_extensions = models.JSONField(
        default=list,
        blank=True,
        help_text="List of allowed file extensions (empty for all)"
    )

    allowed_mime_types = models.JSONField(
        default=list,
        blank=True,
        help_text="List of allowed MIME types (empty for all)"
    )

    is_active = models.BooleanField(
        default=True,
        help_text="Whether this file manager is active and can be used"
    )

    is_default = models.BooleanField(
        default=False,
        help_text="Whether this is the default file manager for the group or user"
    )

    is_public = models.BooleanField(
        default=True,
        help_text="Whether this allows public access to the files"
    )

    public_access_audit = models.JSONField(
        null=True,
        blank=True,
        default=None,
        editable=False,
        help_text="Internal versioned evidence for the effective public-access classification"
    )

    parent = models.ForeignKey(
        'self',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        help_text="Used if this file manager is a child of another file manager, and inherits settings from its parent"
    )

    use = models.CharField(
        max_length=64,
        db_index=True,
        blank=True,
        default="",
        help_text="Optional purpose key (e.g., 'invoices') to support multiple managers per group"
    )

    class Meta:
        unique_together = [
            ['group', 'name'],
        ]
        indexes = [
            models.Index(fields=['backend_type', 'is_active']),
            models.Index(fields=['group', 'use', 'is_default']),
            models.Index(fields=['user', 'is_default']),
            models.Index(fields=['group', 'backend_type']),
            models.Index(fields=['group', 'use', 'backend_type']),
        ]

    def __str__(self):
        group_name = self.group.name if self.group else "Global"
        return f"{self.name} ({self.get_backend_type_display()}) - {group_name}"

    def save(self, *args, **kwargs):
        """Override save to ensure name is set before saving"""
        if not self.name:
            self.name = self.generate_name()
        if self.pk and self._backend is not None:
            persisted = type(self).objects.filter(pk=self.pk).values_list(
                "backend_type", "backend_url"
            ).first()
            if persisted and persisted != (self.backend_type, self.backend_url):
                self._backend = None
        if getattr(self, "_secrets_changed", False):
            self._backend = None
        update_fields = kwargs.get("update_fields")
        if self._needs_use_lock(update_fields):
            from django.db import transaction
            # Lock the owning Group (or User) so two saves with different
            # names cannot both pass the check (pattern of aws_setup.py).
            with transaction.atomic():
                self._lock_and_check_use(update_fields)
                super().save(*args, **kwargs)
            return
        super().save(*args, **kwargs)

    def atomic_save(self):
        """The REST save: the duplicate check, the default switch and the row
        are one transaction, in the order owner lock, check, defaults, row.
        A refused save rolls the default switch back with it (#8662)."""
        from django.db import transaction
        with transaction.atomic():
            if self._needs_use_lock(None):
                self._lock_and_check_use(None)
            self._update_default()
            self.save()

    USE_KEY_FIELDS = ("use", "is_active", "group_id", "user_id")

    @staticmethod
    def _use_key(use, is_active, group_id, user_id):
        """The (group, user, use) an active purpose manager claims, else None.

        Purpose-less managers and the system scope (no user, no group) claim
        nothing: several purpose-less managers per group are normal, and the
        system default is checked by aws_check/aws_setup.
        """
        if not is_active or not use or (group_id is None and user_id is None):
            return None
        return (group_id, user_id, use)

    def _written_use_fields(self, update_fields):
        """The key fields this save writes: all of them on a create or a full
        save, else only those `update_fields` names or a deferred-field row
        loaded, as Django persists them."""
        if self.pk is None:
            return set(self.USE_KEY_FIELDS)
        if update_fields is not None:
            written = {self._meta.get_field(name).attname for name in update_fields}
        elif self.get_deferred_fields():
            written = {f.attname for f in self._meta.concrete_fields} - self.get_deferred_fields()
        else:
            return set(self.USE_KEY_FIELDS)
        return written.intersection(self.USE_KEY_FIELDS)

    def _stored_use_values(self, lock=False):
        if self.pk is None:
            return None
        rows = type(self).objects.filter(pk=self.pk)
        if lock:
            rows = rows.select_for_update()
        row = rows.values_list(*self.USE_KEY_FIELDS).first()
        return dict(zip(self.USE_KEY_FIELDS, row)) if row is not None else None

    def _saved_use_values(self, stored, update_fields):
        """The key fields as this save leaves them in the database: the
        in-memory value of each written field, the stored value of the rest."""
        values = {name: getattr(self, name) for name in self.USE_KEY_FIELDS}
        if stored is not None:
            written = self._written_use_fields(update_fields)
            for name in self.USE_KEY_FIELDS:
                if name not in written:
                    values[name] = stored[name]
        return values

    def _entered_use_key(self, stored, update_fields=None):
        """The key this save moves the row into, else None: a create, or a
        change of use, is_active, group or user. Editing an existing duplicate
        in place stays allowed."""
        key = self._use_key(**self._saved_use_values(stored, update_fields))
        if key is None:
            return None
        if stored is not None and self._use_key(**stored) == key:
            return None
        return key

    def _needs_use_lock(self, update_fields):
        """Whether this save must take the locked decision: it enters a key,
        or it writes a key field whose stored value it changes (the stored
        row read here is unlocked, so the locked read decides)."""
        if not self._written_use_fields(update_fields):
            return False
        stored = self._stored_use_values()
        if self._entered_use_key(stored, update_fields) is not None:
            return True
        return stored is not None and self._saved_use_values(stored, update_fields) != stored

    def _lock_and_check_use(self, update_fields):
        """Inside a transaction: lock the owner the row ends up under, re-read
        the stored row under that lock, and refuse when the key this save
        enters is held by another active row. Group (or User) before
        FileManager, the order every purpose lookup takes."""
        stored = self._stored_use_values()
        locked = []
        while True:
            values = self._saved_use_values(stored, update_fields)
            owner = (values["group_id"], values["user_id"])
            if owner in locked:
                break
            self._lock_use_owner(owner)
            locked.append(owner)
            stored = self._stored_use_values(lock=True)
        key = self._entered_use_key(stored, update_fields)
        if key is not None:
            self._require_unique_use(key)

    @staticmethod
    def _lock_use_owner(owner):
        group_id, user_id = owner
        if group_id is not None:
            from mojo.apps.account.models import Group
            Group.objects.select_for_update().filter(pk=group_id).values_list("pk", flat=True).first()
        elif user_id is not None:
            from mojo.apps.account.models import User
            User.objects.select_for_update().filter(pk=user_id).values_list("pk", flat=True).first()

    def _require_unique_use(self, key):
        group_id, user_id, use = key
        others = type(self).objects.filter(
            group_id=group_id, user_id=user_id, use=use, is_active=True)
        if self.pk is not None:
            others = others.exclude(pk=self.pk)
        if others.exists():
            raise DuplicateUseError(DUPLICATE_USE_MESSAGE)

    def set_secret(self, key, value):
        self._backend = None
        self._cdn_distribution_cache = _CDN_UNRESOLVED
        return super().set_secret(key, value)

    def set_secrets(self, value):
        self._backend = None
        self._cdn_distribution_cache = _CDN_UNRESOLVED
        return super().set_secrets(value)

    def clear_secrets(self):
        self._backend = None
        self._cdn_distribution_cache = _CDN_UNRESOLVED
        return super().clear_secrets()

    # --- CDN delivery (maestro #8250) ---

    _cdn_distribution_cache = _CDN_UNRESOLVED
    # Per-instance seam for the behavior sync's provisioner (clients, region):
    # tests set it on a row they own; production leaves it empty.
    _cdn_provision_kwargs = None

    @property
    def cdn_distribution(self):
        """The CdnDistribution this manager delivers through, or None.

        Resolved once per instance (the bucket's enabled row, unless this
        manager or its top parent sets `cdn = "off"`); reset whenever the
        settings store or the backend changes.
        """
        if self._cdn_distribution_cache is _CDN_UNRESOLVED:
            from mojo.apps.fileman.services import cdn
            self._cdn_distribution_cache = cdn.resolve_for_manager(self)
        return self._cdn_distribution_cache

    @property
    def uses_cdn(self):
        return self.cdn_distribution is not None

    @property
    def cdn(self):
        """Graph extra: what the portal shows. Never the key material."""
        distribution = self.cdn_distribution
        if distribution is None:
            return {"enabled": False}
        return {
            "enabled": True,
            "url": distribution.base_url,
            "bucket": distribution.bucket_name,
            "distribution_id": distribution.distribution_id,
            "source": distribution.source,
            "status": distribution.status,
            "signed": not self.is_public,
            "signing_key_id": distribution.signing_key.key_id if distribution.signing_key_id else None,
        }

    CDN_SETTING_KEYS = ("cdn", "cdn_url_expires_in")

    def _validate_cdn_settings(self):
        """The per-manager CDN settings are small and strict; S3 only."""
        from mojo.apps.fileman.services import cdn

        switch = self.get_secret("cdn")
        expires = self.get_secret("cdn_url_expires_in")
        if self.is_s3 and self._cdn_distribution_row() is not None:
            # On a CDN bucket the root path becomes a CloudFront path pattern:
            # a `*` or `?` in it would be read as a wildcard and could match
            # another manager's prefix.
            prefix = (self.root_path or "").strip("/")
            if prefix and not cdn.PREFIX_SAFE_RE.match(prefix):
                raise me.ValueException(
                    "on a bucket delivered through the CDN a file manager's path may only use "
                    "letters, digits, '/', '-', '_', '.', '~', '@', ':' and '+'")
        if switch in (None, "") and expires in (None, ""):
            return
        if not self.is_s3:
            raise me.ValueException(
                "cdn settings apply to S3 file managers only")
        try:
            if switch not in (None, ""):
                self.set_secret("cdn", cdn.validate_switch(switch))
            if expires not in (None, ""):
                self.set_secret("cdn_url_expires_in", cdn.validate_expires_in(expires))
        except ValueError as exc:
            raise me.ValueException(str(exc))

    def _cdn_distribution_row(self):
        """The bucket's CdnDistribution with a real distribution, enabled or
        not. Visibility and prefix changes must reach the edge whenever one
        exists: a disabled row or an opted-out manager still has its objects
        served by that distribution."""
        from mojo.apps.fileman.services import cdn_provision
        return cdn_provision.distribution_row_for(self)

    def _cdn_edge_change(self, created):
        """What a save changes at the edge: 'tighten', 'loosen' or None.

        A new private manager, a public→private flip and any move of the
        prefix tighten (the edge must protect the prefix before the row says
        it is private); a new public manager or a private→public flip loosens.
        """
        if not self.is_s3 or self._cdn_distribution_row() is None:
            return None
        if created:
            return "loosen" if self.is_public else "tighten"
        stored = FileManager.objects.filter(pk=self.pk).values("is_public", "backend_url").first()
        if stored is None:
            return None
        if stored["backend_url"] != self.backend_url:
            return "tighten"
        if bool(stored["is_public"]) == bool(self.is_public):
            return None
        return "loosen" if self.is_public else "tighten"

    def _tighten_cdn_before_save(self, created):
        """A change that must protect a prefix converges the cache behaviors
        BEFORE the row is written: if CloudFront refuses, the save fails and
        the edge stays as it was. Never a database that says private while
        the edge still serves the prefix unsigned."""
        if self._cdn_edge_change(created) != "tighten":
            return
        from mojo.apps.fileman.services import cdn_provision
        result = cdn_provision.sync_manager_behavior(self, **(self._cdn_provision_kwargs or {}))
        if result.get("blocking") or result.get("worst") == "MANUAL":
            codes = ", ".join(sorted({item["code"] for item in result.get("items", [])
                                      if item["status"] in ("BLIND", "BLOCKED", "MANUAL")}))
            raise me.ValueException(
                f"The CDN could not be switched to signed delivery for this manager, so the "
                f"change was not saved ({codes}). Run check_cdn for the exact fix.")

    def _loosen_cdn_after_save(self, changed_fields, created):
        """Private → public converges after the write; a failure is logged and
        shows in check_cdn as behaviors.drift, never a public row served signed."""
        if created and not self.is_public:
            return
        if not created and "is_public" not in (changed_fields or {}):
            return
        if not self.is_public or self._cdn_distribution_row() is None:
            return
        from mojo.apps.fileman.services import cdn_provision
        result = cdn_provision.sync_manager_behavior(self, **(self._cdn_provision_kwargs or {}))
        if result.get("blocking"):
            logit.warning("fileman", f"CDN behavior sync for manager {self.pk} did not converge; "
                                     f"run check_cdn: {result.get('worst')}")

    CDN_ADMIN_PERMS = ("manage_files", "files")

    def _is_cdn_admin(self):
        """A global file admin (`manage_files` / `files`, which a superuser
        implies) may run the CDN controls. SAVE_PERMS is satisfied by the
        group-level grant too, and that holder must not drive the
        deployment's AWS identity, so the check is the global one."""
        actor = self.active_user
        if actor is None:
            return False
        if getattr(actor, "is_superuser", False):
            return True
        check = getattr(actor, "has_permission", None)
        return bool(check and check(list(self.CDN_ADMIN_PERMS)))

    def _require_cdn_admin_for_settings(self, created):
        """Only a global file admin may change a manager's CDN settings over REST.

        The settings decide whether a manager's links are signed for and
        how long a bearer link lives. Same shape as
        `_require_superuser_for_platform_role`, with the file-admin gate.
        """
        if self._is_cdn_admin():
            return
        if not created:
            stored = FileManager.objects.filter(pk=self.pk).first()
            if stored is not None and all(
                    self.get_secret(key) == stored.get_secret(key)
                    for key in self.CDN_SETTING_KEYS):
                return
        elif all(self.get_secret(key) in (None, "") for key in self.CDN_SETTING_KEYS):
            return
        raise me.PermissionDeniedException(
            reason="Only file admins (manage_files) can change a FileManager's CDN settings",
            model_name="FileManager",
            event_type="user_permission_denied",
            branch="fileman_cdn_setting",
        )

    def get_setting(self, key, default=None):
        """Get a specific setting value"""
        value = self.get_secret(key, default)
        if value is None:
            value = self.primary_parent.get_secret(key, default)
        return value

    def set_setting(self, key, value):
        """Set a specific setting value"""
        self.set_secret(key, value)

    def set_settings(self, value):
        """Set a specific setting value"""
        self.set_secrets(value)

    def set_backend_url(self, url, *args):
        """Set the backend URL"""
        self._backend = None
        self.backend_url = os.path.join(url, *args)
        self.backend_type = self.backend_url.split(':')[0]

    def _update_default(self):
        # A default is per purpose: making an `invoices` manager the default
        # leaves the group's purpose-less default alone (#8662).
        if self.is_default:
            if self.pk is None:
                FileManager.objects.filter(
                    group=self.group,
                    user=self.user,
                    use=self.use,
                    is_default=True
                ).update(is_default=False)
            else:
                FileManager.objects.filter(
                    group=self.group,
                    user=self.user,
                    use=self.use,
                    is_default=True
                ).exclude(pk=self.pk).update(is_default=False)

    _backend = None

    @property
    def aws_key(self):
        """Raw access key for trusted backend use; never expose in a REST graph."""
        return self.get_secret('aws_key')

    @property
    def aws_secret(self):
        return self.get_secret('aws_secret')

    @property
    def aws_key_masked(self):
        key = self.get_secret('aws_key', '') or ''
        if len(key) > 4:
            return '*' * (len(key) - 4) + key[-4:]
        return '*' * len(key)

    @property
    def aws_secret_masked(self):
        secret = self.get_secret('aws_secret', '') or ''
        if len(secret) > 4:
            return '*' * (len(secret) - 4) + secret[-4:]
        return '*' * len(secret)

    @property
    def aws_region(self):
        return self.get_secret('aws_region')

    @property
    def is_verified(self):
        return self.status in ["verified", "ready"]

    def set_aws_key(self, key):
        self.set_secret('aws_key', key)

    def set_aws_secret(self, secret):
        self.set_secret('aws_secret', secret)

    def set_aws_region(self, secret):
        self.set_secret('aws_region', secret)

    @property
    def assume_role_arn(self):
        return self.get_secret('assume_role_arn')

    @property
    def has_external_id(self):
        """Only ever report presence.

        An external ID is short and exists precisely to be unguessable by
        whoever already knows the role ARN, so it gets no masked form —
        aws_secret_masked reveals both exact length and the trailing four
        characters, which would be most of a short secret.
        """
        return bool(self.get_secret('external_id'))

    @property
    def assume_role_duration(self):
        return self.get_secret('assume_role_duration')

    ROLE_SETTING_KEYS = ("assume_role_arn", "external_id", "role_session_name", "assume_role_duration")

    def set_assume_role_arn(self, value):
        self.set_secret('assume_role_arn', value)

    def set_external_id(self, value):
        self.set_secret('external_id', value)

    def set_role_session_name(self, value):
        self.set_secret('role_session_name', value)

    def set_assume_role_duration(self, value):
        self.set_secret('assume_role_duration', value)

    def uses_platform_credentials(self):
        """True when a role on this store would be assumed with the platform's AWS identity.

        Only the store's own secrets count, never a parent's: with no key
        the session falls through to the server's ambient identity. A key
        copied from the platform (the AWS_KEY setting, or a system-scoped
        store) is the platform's too — created and derived stores are seeded
        with it.
        """
        if self.user_id is None and self.group_id is None:
            return True
        key = self.get_secret('aws_key')
        if not key or not self.get_secret('aws_secret'):
            return True
        if key == settings.get("AWS_KEY", None):
            return True
        for manager in FileManager.objects.filter(user=None, group=None):
            if manager.get_secret('aws_key') == key:
                return True
        return False

    def _require_superuser_for_platform_role(self, created):
        """One rule for every route a role or key can arrive by.

        on_rest_save_field dispatches set_<key> for any key in the payload —
        the flat fields, `secrets` and `settings` all land in the same store —
        and SAVE_PERMS is the group-level "files"/"manage_files" permission. So
        this checks the store as the request leaves it: a non-superuser may not
        leave a role on a store that runs on platform credentials (confused
        deputy), whether they added the role or took the store's own key away.
        A store on its own key reaches only what that key already reaches.
        Direct ORM use (bootstrap, clone, get_for_*) does not come through here.
        """
        if not self.assume_role_arn:
            return
        actor = self.active_user
        if actor is not None and getattr(actor, "is_superuser", False):
            return
        if not self.uses_platform_credentials():
            return
        if not created:
            # An untouched role/key pair is not this request's doing: a file
            # admin can still edit the other settings of such a store.
            stored = FileManager.objects.filter(pk=self.pk).first()
            watched = (*self.ROLE_SETTING_KEYS, "aws_key", "aws_secret")
            if stored is not None and all(
                    self.get_secret(key) == stored.get_secret(key) for key in watched):
                return
        raise me.PermissionDeniedException(
            reason="Only superusers can set an AWS role on a FileManager that runs on platform credentials",
            model_name="FileManager",
            event_type="user_permission_denied",
            branch="fileman_assume_role_setting",
        )

    # --- storage containment (maestro #7990) ---
    # A manager reaches what its top parent's keys reach, at the location its
    # own backend_url and the top parent's settings name. A group-level file
    # grant may not aim borrowed keys (the platform's or another manager's)
    # outside the group's own folder.

    PARENT_WALK_LIMIT = 16
    STORAGE_WATCHED_FIELDS = ("backend_type", "backend_url", "parent_id", "group_id", "is_public")
    STORAGE_WATCHED_SETTINGS = ("base_path", "temp_upload_path", "endpoint_url", "aws_key", "aws_secret")

    def _has_platform_file_grant(self):
        """True for a superuser, or a user holding `manage_files`/`files` on the
        user record. A key that assumes a member carries only that member's
        group-level grant (rest.py), so it never counts here; a reference-mode
        key is not a User and gets its group-level answer too."""
        from mojo.helpers.request import is_override_user_session, is_request_user

        request = self.active_request
        actor = self.active_user
        if request is None or actor is None:
            return False
        if is_override_user_session(request) or not is_request_user(request):
            return False
        if getattr(actor, "is_superuser", False):
            return True
        return bool(actor.has_permission(list(self.CDN_ADMIN_PERMS)))

    def _top_parent(self):
        """The row whose keys and settings the backend uses (primary_parent),
        with a bounded walk: REST skips only a direct self-reference."""
        node = self
        seen = {id(self)} if self.pk is None else {self.pk}
        for _ in range(self.PARENT_WALK_LIMIT):
            parent = node.parent
            if parent is None:
                return node
            key = parent.pk if parent.pk is not None else id(parent)
            if key in seen:
                raise me.ValueException("File manager parents form a cycle")
            seen.add(key)
            node = parent
        raise me.ValueException("File manager parent chain is too deep")

    def _runs_on_own_key(self):
        """An S3 store that is its own top parent and holds its own key pair.
        A filesystem store never uses a key, so it is never "own key"."""
        if self._top_parent() is not self or not self.is_s3:
            return False
        if not self.get_secret("aws_key") or not self.get_secret("aws_secret"):
            return False
        return not self.uses_platform_credentials()

    def _location(self):
        """Where the backend would read and write, the way it reads it: the
        backend type and URL of this row, the settings of the top parent."""
        store = self._top_parent().secrets or {}
        purl = urlparse(self.backend_url or "")
        root = (purl.path or "").strip("/")
        if self.is_file_system:
            # The host part of a file:// URL is ignored by the backend.
            base = os.path.realpath(store.get("base_path") or "/tmp/fileman")
            return (self.FILE_SYSTEM, base, "", root)
        return (self.backend_type, purl.netloc, store.get("endpoint_url") or "", root)

    def _group_storage_roots(self):
        """The locations of the group's anchors: group managers with no user
        whose parent is system-scoped, the shape get_for_group provisions.
        Read from the database, so an unsaved edit cannot move them; never
        through group.get_uuid(), which writes the Group."""
        if self.group_id is None:
            return []
        anchors = FileManager.objects.filter(
            group_id=self.group_id, user=None,
            parent__isnull=False, parent__user=None, parent__group=None,
        ).select_related("parent")
        return [anchor._location() for anchor in anchors]

    @staticmethod
    def _location_within(location, root):
        if location[:3] != root[:3]:
            return False
        path, root_path = location[3], root[3]
        return not root_path or path == root_path or path.startswith(root_path + "/")

    def _storage_unchanged(self):
        stored = FileManager.objects.filter(pk=self.pk).first()
        if stored is None:
            return False
        if any(getattr(self, name) != getattr(stored, name) for name in self.STORAGE_WATCHED_FIELDS):
            return False
        return all(self.get_secret(key) == stored.get_secret(key)
                   for key in self.STORAGE_WATCHED_SETTINGS)

    def _refuse_storage(self, reason):
        raise me.PermissionDeniedException(
            reason=reason,
            model_name="FileManager",
            event_type="user_permission_denied",
            branch="fileman_storage_outside_group",
        )

    def _require_contained_storage(self, created):
        """A group-level grant may not aim borrowed keys outside the group's folder.

        Platform-wide file admins and superusers pass. Everyone else — a
        person, an API key or a group token, on the same grant — may save a
        manager that runs on its own S3 key anywhere, and one on the
        platform's or a same-group parent's keys only inside one of the
        group's anchors. Direct ORM use (bootstrap, clone, get_for_*) does
        not come through here.
        """
        request = self.active_request
        if request is None:
            # The middleware sets the active request on every HTTP call, so
            # none means server code calling the hook directly.
            return
        if self._has_platform_file_grant():
            return
        stored_group_id = None
        if not created:
            stored_group_id = FileManager.objects.filter(pk=self.pk).values_list(
                "group_id", flat=True).first()
        if created or stored_group_id != self.group_id:
            original_group = getattr(request, "group", None)
            try:
                allowed = self.rest_check_permission(request, "SAVE_PERMS", self)
            finally:
                request.group = original_group
            if not allowed:
                self._refuse_storage("A file manager can only be saved into a group you manage files in")
        if not created and self._storage_unchanged():
            return
        stored = None if created else FileManager.objects.filter(pk=self.pk).first()
        self._require_contained_reach(stored)
        if not created:
            # A descendant runs on this row's keys and settings, so an edit
            # here can move it too (review 113553).
            for node, before in self._storage_descendants():
                if self._reach_of(node) != self._reach_of(before):
                    node._require_contained_reach(before)

    def _require_contained_reach(self, before):
        """Where this row, as it now stands, may point. `before` is the stored
        copy (None when new): a value already stored is judged only when the
        save changes it."""
        path = urlparse(self.backend_url or "").path or ""
        segments = path.strip("/").split("/") if path.strip("/") else []
        if "\\" in path or any(seg in ("", ".", "..") for seg in segments):
            self._refuse_storage("A file manager's path may not contain empty, '.' or '..' parts")
        self._require_contained_temp_path(before)
        if self._runs_on_own_key():
            return
        top = self._top_parent()
        if top is not self:
            own_key_parent = (top.group_id == self.group_id and top.user_id is None
                              and top._runs_on_own_key())
            if own_key_parent and self.is_s3:
                # Only an S3 child uses the parent's key; any other backend
                # falls through to the folder check.
                return
            if not own_key_parent and (top.group_id is not None or top.user_id is not None):
                self._refuse_storage("A file manager can only borrow keys from its own group")
        location = self._location()
        if not any(self._location_within(location, root) for root in self._group_storage_roots()):
            self._refuse_storage(
                "A file manager on borrowed keys must stay inside its group's storage folder")

    def _effective_temp_path(self):
        """The temp_upload_path the filesystem backend reads (the top parent's),
        or None when unset and the backend's default applies."""
        if not self.is_file_system:
            return None
        return self._top_parent().get_secret("temp_upload_path") or None

    def _require_contained_temp_path(self, before):
        """The filesystem backend creates folders at temp_upload_path and prunes
        the old ones, so a changed value must sit in one of the group's folders."""
        effective = self._effective_temp_path()
        if not effective or effective == (before._effective_temp_path() if before else None):
            return
        path = os.path.realpath(effective)
        folders = [os.path.join(root[1], root[3]) for root in self._group_storage_roots()
                   if root[0] == self.FILE_SYSTEM]
        if not any(path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)
                   for folder in folders):
            self._refuse_storage(
                "A file manager's temp_upload_path must stay inside its group's storage folder")

    def _storage_descendants(self):
        """Every row under this one, each as (node, before): `node` reaches this
        unsaved row through its parent chain, `before` is a fresh copy whose
        chain reads the database. Bounded and cycle-safe like _top_parent."""
        out = []
        frontier = [self]
        seen = {self.pk}
        for _ in range(self.PARENT_WALK_LIMIT):
            by_pk = {node.pk: node for node in frontier}
            children = list(FileManager.objects.filter(parent_id__in=list(by_pk)).exclude(pk__in=seen))
            if not children:
                return out
            for child in children:
                seen.add(child.pk)
                child.parent = by_pk[child.parent_id]
                out.append((child, FileManager.objects.get(pk=child.pk)))
            frontier = children
        if FileManager.objects.filter(parent_id__in=[n.pk for n in frontier]).exclude(pk__in=seen).exists():
            raise me.ValueException("File manager children are nested too deep")
        return out

    @staticmethod
    def _reach_of(node):
        """What a save can change about a row's reach: whose keys (the label and
        the top parent that holds them, with its owner), where, and the temp
        folder its backend uses. A key rotation on the same top parent keeps
        the same answer."""
        try:
            top = node._top_parent()
            return (node.credentials, top.pk, top.group_id, top.user_id,
                    node._location(), node._effective_temp_path())
        except me.ValueException:
            return None

    @property
    def credentials(self):
        """Whose keys this manager runs on: "own", "parent" or "platform". Never key material."""
        try:
            top = self._top_parent()
        except me.ValueException:
            return "platform"
        if top is self:
            return "own" if self._runs_on_own_key() else "platform"
        return "parent" if top._runs_on_own_key() else "platform"

    def set_allowed_origins(self, origins):
        if isinstance(origins, str) and "," in origins:
            origins = [origin.strip() for origin in origins.split(',')]
        self.set_secret('allowed_origins', origins)

    @property
    def allowed_origins(self):
        return self.get_secret('allowed_origins')

    @property
    def backend(self):
        """Get the backend instance"""
        from mojo.apps.fileman import backends
        if not self._backend:
            self._backend = backends.get_backend(self)
        return self._backend

    @property
    def settings(self):
        return self.secrets

    @property
    def primary_settings(self):
        return self.primary_parent.secrets

    @property
    def primary_parent(self):
        parent = self
        while parent.parent:
            parent = parent.parent
        return parent

    @property
    def root_path(self):
        purl = urlparse(self.backend_url)
        return purl.path.lstrip('/')

    @property
    def root_location(self):
        purl = urlparse(self.backend_url)
        return purl.netloc

    @property
    def is_file_system(self):
        return self.backend_type == self.FILE_SYSTEM

    @property
    def is_s3(self):
        return self.backend_type == self.AWS_S3

    @property
    def is_azure(self):
        return self.backend_type == self.AZURE_BLOB

    @property
    def is_gcs(self):
        return self.backend_type == self.GOOGLE_CLOUD

    @property
    def is_custom(self):
        return self.backend_type == self.CUSTOM

    _UPLOAD_MIME_RE = re.compile(r"^[a-z0-9!#$&^_.+\-]+/[a-z0-9!#$&^_.+\-]+$")

    @classmethod
    def normalize_upload_filename(cls, filename):
        """Return the safe client display name used for storage naming."""
        if not isinstance(filename, str):
            raise me.ValueException("filename must be a string")
        # Browsers may submit either POSIX or Windows fake paths. Normalize both.
        filename = filename.replace("\\", "/").rsplit("/", 1)[-1]
        filename = "".join(ch for ch in filename if ord(ch) >= 32 and ch != "\x7f").strip()
        if not filename or filename in (".", ".."):
            raise me.ValueException("filename is required")
        if len(filename) > 255:
            raise me.ValueException("filename exceeds 255 characters")
        return filename

    @classmethod
    def normalize_upload_size(cls, file_size):
        if isinstance(file_size, bool) or not isinstance(file_size, int):
            raise me.ValueException("file_size must be a nonnegative integer")
        if file_size < 0:
            raise me.ValueException("file_size must be a nonnegative integer")
        return file_size

    @classmethod
    def normalize_upload_mime_type(cls, mime_type, allow_wildcard=False):
        if not isinstance(mime_type, str):
            raise me.ValueException("content_type must be a MIME type")
        mime_type = mime_type.split(";", 1)[0].strip().lower()
        if allow_wildcard and mime_type.endswith("/*"):
            prefix = mime_type[:-2]
            if prefix and cls._UPLOAD_MIME_RE.match(f"{prefix}/x"):
                return mime_type
        if not cls._UPLOAD_MIME_RE.match(mime_type):
            raise me.ValueException("content_type must be a valid MIME type")
        return mime_type

    def normalized_allowed_extensions(self):
        if not isinstance(self.allowed_extensions, list):
            raise me.ValueException("File manager extension policy is invalid")
        normalized = []
        for value in self.allowed_extensions:
            if not isinstance(value, str):
                raise me.ValueException("File manager extension policy is invalid")
            value = value.strip().lower().lstrip(".")
            if not value or "/" in value or "\\" in value:
                raise me.ValueException("File manager extension policy is invalid")
            normalized.append(value)
        return normalized

    def normalized_allowed_mime_types(self):
        if not isinstance(self.allowed_mime_types, list):
            raise me.ValueException("File manager MIME policy is invalid")
        return [self.normalize_upload_mime_type(value, allow_wildcard=True)
                for value in self.allowed_mime_types]

    def can_upload_file(self, filename, file_size=None):
        """Check normalized filename and declared size against this manager."""
        if not self.is_active:
            return False, "File manager is not active"
        try:
            filename = self.normalize_upload_filename(filename)
            if file_size is not None:
                file_size = self.normalize_upload_size(file_size)
            allowed_extensions = self.normalized_allowed_extensions()
        except me.ValueException as exc:
            return False, exc.reason
        if file_size is not None and self.max_file_size > 0 and file_size > self.max_file_size:
            return False, f"File size exceeds maximum of {self.max_file_size} bytes"
        if allowed_extensions:
            _, ext = os.path.splitext(filename.lower())
            if not ext:
                return False, "A file extension is required"
            if ext[1:] not in allowed_extensions:
                return False, f"File extension {ext} is not allowed"
        return True, "File can be uploaded"

    def can_upload_mime_type(self, mime_type):
        """Check exact and top-level wildcard MIME policy."""
        try:
            mime_type = self.normalize_upload_mime_type(mime_type)
            allowed = self.normalized_allowed_mime_types()
        except me.ValueException:
            return False
        if not allowed:
            return True
        major = mime_type.split("/", 1)[0] + "/*"
        return mime_type in allowed or major in allowed

    @classmethod
    def clean_use(cls, value, from_request=False):
        """A storage purpose as a plain short name, or "" for none.

        It goes into the storage path, so anything else is refused (400). A
        purpose server code owns is refused (403) when it comes from a request.
        """
        if value is None or value == "":
            return ""
        # fullmatch on the value as sent: `$` alone would let a final newline through.
        if not isinstance(value, str) or not USE_RE.fullmatch(value):
            raise me.ValueException("use must be 1-64 letters, digits, '_' or '-'")
        if from_request and value in FILEMAN_RESERVED_USES:
            raise me.PermissionDeniedException("File manager is unavailable")
        return value

    @staticmethod
    def _require_upload_session(request):
        from mojo.helpers.request import is_key_backed_session, is_request_user, restricted_identity

        actor = getattr(request, "user", None)
        if (actor is None or not is_request_user(request)
                or is_key_backed_session(request) or restricted_identity(request) is not None):
            raise me.PermissionDeniedException("A real user session is required for uploads")
        return actor

    def authorize_for_upload(self, request):
        """Fail closed unless the request may use this exact manager scope."""
        actor = self._require_upload_session(request)
        if not self.is_active:
            raise me.PermissionDeniedException("File manager is unavailable")
        if self.group_id and not self.group.is_effectively_active():
            raise me.PermissionDeniedException("File manager is unavailable")

        supplied_group = "group" in request.DATA
        supplied_use = "use" in request.DATA or "fileman_use" in request.DATA or "file_manager_use" in request.DATA
        if supplied_group and (request.group is None or self.group_id != request.group.id):
            raise me.PermissionDeniedException("File manager scope does not match the request")
        requested_use = self.clean_use(
            request.DATA.get(["use", "fileman_use", "file_manager_use"], ""), from_request=True)
        if supplied_use and self.use != requested_use:
            raise me.PermissionDeniedException("File manager purpose does not match the request")

        global_admin = actor.has_permission(["manage_files", "files"])
        if global_admin:
            return actor
        if self.user_id is None and self.group_id is None:
            raise me.PermissionDeniedException("Global file storage permission is required")
        if self.user_id is not None and self.user_id != actor.id:
            raise me.PermissionDeniedException("File manager is unavailable")
        if self.group_id is not None:
            if request.group is None or request.group.id != self.group_id:
                raise me.PermissionDeniedException("File manager scope does not match the request")
            if self.group.get_member_for_user(actor, check_parents=True) is None:
                raise me.PermissionDeniedException("Active group membership is required")
        return actor

    @classmethod
    def resolve_for_upload(cls, request):
        """Resolve selectors and apply the upload authorization truth table."""
        # Before resolution: a key-backed session must never cause storage to
        # be created on the upload route.
        cls._require_upload_session(request)
        if "user" in request.DATA or "user_id" in request.DATA:
            raise me.PermissionDeniedException("A user upload scope cannot be selected")
        fm_id = request.DATA.get(["fileman", "filemanager", "file_manager", "manager", "file_manager_id"])
        if fm_id:
            try:
                file_manager = cls.objects.select_related("user", "group").get(pk=int(fm_id))
            except (TypeError, ValueError, cls.DoesNotExist):
                raise me.PermissionDeniedException("File manager is unavailable")
        else:
            file_manager = cls.get_from_request(request)
        if file_manager is None:
            raise me.ValueException("No file manager found")
        file_manager.authorize_for_upload(request)
        return file_manager

    @classmethod
    def resolve_for_file_create(cls, request):
        """Resolve the manager for a File created over the general endpoint.

        User sessions get the upload rule unchanged. An ApiKey or group token,
        which that rule refuses, may use group storage only where the
        framework's own permission check lets it save that manager: its group
        or, for a key, a descendant, with `files` or `manage_files` there.
        """
        from mojo.helpers.request import restricted_identity

        if restricted_identity(request) is None:
            return cls.resolve_for_upload(request)
        fm_id = request.DATA.get(["fileman", "filemanager", "file_manager", "manager", "file_manager_id"])
        try:
            if fm_id:
                file_manager = cls.objects.select_related("user", "group").get(pk=int(fm_id))
            else:
                file_manager = cls.get_from_request(request)
        except (TypeError, ValueError, cls.DoesNotExist):
            raise me.PermissionDeniedException("File manager is unavailable")
        if file_manager is None or not file_manager.is_active:
            raise me.PermissionDeniedException("File manager is unavailable")
        if file_manager.group_id is None or file_manager.user_id is not None:
            raise me.PermissionDeniedException("File manager is unavailable")
        original_group = getattr(request, "group", None)
        try:
            allowed = cls.rest_check_permission(request, "SAVE_PERMS", file_manager)
        finally:
            request.group = original_group
        if not allowed:
            raise me.PermissionDeniedException("File manager is unavailable")
        return file_manager

    @classmethod
    def _authorize_named_manager(cls, request, fm_id):
        """The manager a request names by id, if the caller may use it.

        People get the upload rule (`authorize_for_upload`), except that a
        member naming a group manager without sending `group` is checked by
        active membership of that manager's group. An ApiKey or group token
        may name a group manager in a group it covers (`is_group_allowed`, the
        dispatcher's own bound — no permission grant, as for an unnamed
        manager), and a key acting as a member that member's own manager.
        """
        from mojo.helpers.request import (
            is_override_user_session, is_request_user, restricted_identity)

        unavailable = me.PermissionDeniedException("File manager is unavailable")
        try:
            manager = cls.objects.select_related("user", "group").get(pk=int(fm_id))
        except (TypeError, ValueError, cls.DoesNotExist):
            raise unavailable
        if not manager.is_active:
            raise unavailable
        if manager.group_id is not None and not manager.group.is_effectively_active():
            raise unavailable

        ident = restricted_identity(request)
        if ident is not None:
            if manager.group_id is not None and manager.user_id is None:
                if ident.is_group_allowed(manager.group):
                    return manager
            elif (manager.user_id is not None and manager.group_id is None
                  and is_override_user_session(request)
                  and manager.user_id == getattr(request.user, "id", None)):
                return manager
            raise unavailable

        actor = getattr(request, "user", None)
        if actor is None or not is_request_user(request):
            raise unavailable
        if "group" in request.DATA or manager.group_id is None or manager.user_id is not None:
            manager.authorize_for_upload(request)
            return manager
        requested_use = cls.clean_use(
            request.DATA.get(["use", "fileman_use", "file_manager_use"], ""), from_request=True)
        supplied_use = any(key in request.DATA for key in ("use", "fileman_use", "file_manager_use"))
        if supplied_use and manager.use != requested_use:
            raise me.PermissionDeniedException("File manager purpose does not match the request")
        if actor.has_permission(["manage_files", "files"]):
            return manager
        if manager.group.get_member_for_user(actor, check_parents=True) is None:
            raise me.PermissionDeniedException("Active group membership is required")
        return manager

    def on_rest_created(self):
        self._update_default()

    def on_rest_pre_save(self, changed_fields, created):
        if created and self.user is None and self.group is None:
            # A manager with no user and no group is system-scoped — it can
            # become the system default that get_for_user/get_for_group derive
            # every other manager from. Restrict REST creation to superusers;
            # direct ORM creation (bootstrap, get_for_*) bypasses this path.
            actor = self.active_user
            if actor is None or not getattr(actor, "is_superuser", False):
                raise me.PermissionDeniedException(
                    reason="Only superusers can create a system-scoped FileManager (user and group both unset)",
                    model_name="FileManager",
                    event_type="user_permission_denied",
                    branch="fileman_system_scope_create",
                )
        # Nothing here writes another row: the default switch runs inside
        # atomic_save, after the locked check. The key defaults below cannot
        # change the answer — a created store with no key of its own is on
        # platform credentials either way.
        if "use" in changed_fields or (created and self.use):
            self.use = self.clean_use(self.use)
        # Advisory, for the clear 400: atomic_save makes the locked decision.
        use_key = self._entered_use_key(self._stored_use_values())
        if use_key is not None:
            self._require_unique_use(use_key)
        self._require_superuser_for_platform_role(created)
        self._require_contained_storage(created)
        self._require_cdn_admin_for_settings(created)
        self._validate_cdn_settings()
        self._tighten_cdn_before_save(created)
        if not self.name:
            self.name = self.generate_name()
        if created:
            if not self.aws_region:
                self.set_aws_region(settings.get("AWS_REGION", "us-east-1"))
            if not self.aws_key:
                self.set_aws_key(settings.get("AWS_KEY", None))
            if not self.aws_secret:
                self.set_aws_secret(settings.get("AWS_SECRET", None))

    def on_rest_saved(self, changed_fields, created):
        self._update_default()
        if not self.name:
            self.name = self.generate_name()
        backend_changed = bool({"backend_type", "backend_url"}.intersection(changed_fields))
        policy_changed = "is_public" in changed_fields or created
        if backend_changed or created:
            self._backend = None
            self._cdn_distribution_cache = _CDN_UNRESOLVED
        if self.uses_cdn or (self.is_s3 and self._cdn_distribution_row() is not None):
            # With a CDN, `is_public` means "deliverable unsigned through the
            # CDN"; the bucket stays private behind OAC. No bucket-policy
            # write (Block Public Access refuses it anyway) and no S3 audit,
            # which would read the private bucket and rewrite is_public.
            self._loosen_cdn_after_save(changed_fields, created)
            return
        if policy_changed:
            if self.is_public:
                self.backend.make_path_public()
            else:
                self.backend.make_path_private()
        if self.is_s3 and (policy_changed or backend_changed):
            self.audit_is_public(force=True)

    def generate_name(self):
        use_part = f"{self.use} " if getattr(self, "use", "") else ""
        if self.user and self.group:
            return f"{self.user.username}@{self.group.name}'s {use_part}{self.backend_type} FileManager"
        elif self.user:
            return f"{self.user.username}'s {use_part}{self.backend_type} FileManager"
        elif self.group:
            return f"{self.group.name}'s {use_part}{self.backend_type} FileManager"
        return f"{use_part}{self.backend_type} FileManager"

    PUBLIC_ACCESS_AUDIT_VERSION = 1
    PUBLIC_ACCESS_SETTING_KEYS = (
        "aws_key",
        "aws_secret",
        "aws_region",
        "endpoint_url",
        "signature_version",
        "addressing_style",
        # The effective identity changes when the role does, so cached
        # public-access evidence must not survive a role change.
        "assume_role_arn",
        "external_id",
    )

    def public_access_config_fingerprint(self):
        """Hash effective storage inputs without persisting credential material."""
        payload = {
            "backend_type": self.backend_type,
            "backend_url": self.backend_url,
            "settings": {
                key: self.get_setting(key, None)
                for key in self.PUBLIC_ACCESS_SETTING_KEYS
            },
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _uses_automatic_public_access_audit(self):
        """Lazy upgrade repair is deliberately limited to personal S3 managers."""
        return bool(
            self.pk
            and self.is_active
            and self.is_s3
            and self.user_id is not None
            and self.group_id is None
        )

    def _public_access_audit_is_current(self, file_path=None):
        audit = self.public_access_audit
        if not isinstance(audit, dict):
            return False
        if audit.get("version") != self.PUBLIC_ACCESS_AUDIT_VERSION:
            return False
        if audit.get("config_fingerprint") != self.public_access_config_fingerprint():
            return False
        # A policy-only provisioning result is superseded once by evidence from
        # a real object. Thereafter normal URL reads remain network-free.
        if file_path and audit.get("method") != "object":
            return False
        return audit.get("status") in ("public", "private", "unknown")

    def audit_is_public(self, file_path=None, force=True, persist=True):
        """Inspect S3 access and return the safe effective public/private value.

        Conclusive results repair ``is_public``. An indeterminate result keeps
        the operator's stored value but is effectively private so callers use a
        presigned URL.
        """
        if not self.is_s3 or self.uses_cdn:
            return self.is_public
        if not force and self._public_access_audit_is_current(file_path=file_path):
            return self.public_access_audit.get("status") == "public"

        fingerprint = self.public_access_config_fingerprint()
        try:
            ok, issues, details = self.backend.check_public_access_for_prefix(file_path=file_path)
            details = details if isinstance(details, dict) else {}
            status = details.get("status")
            if status not in ("public", "private", "unknown"):
                status = "public" if ok else "private"
            method = details.get("method") or ("object" if file_path else "policy")
        except Exception as exc:
            status = "unknown"
            method = "object" if file_path else "policy"
            safe = exc.failure() if hasattr(exc, "failure") else safe_error_detail(
                exc, "s3.audit_public_access")
            issues = ["S3 public-access evidence could not be collected safely."]
            details = safe

        audit = {
            "version": self.PUBLIC_ACCESS_AUDIT_VERSION,
            "status": status,
            "method": method,
            "checked_at": timezone.now().isoformat(),
            "config_fingerprint": fingerprint,
            "issues": list(issues or []),
            "details": details,
        }
        self._public_access_audit_result = audit

        if persist:
            updates = {"public_access_audit": audit, "modified": timezone.now()}
            self.public_access_audit = audit
            if status in ("public", "private"):
                self.is_public = status == "public"
                updates["is_public"] = self.is_public
            type(self).objects.filter(pk=self.pk).update(**updates)

        return status == "public"

    def ensure_public_access_audited(self, file_path=None, force=False, persist=True):
        """Return safe effective access, auditing only stale personal S3 state."""
        if self.uses_cdn or not self._uses_automatic_public_access_audit():
            return self.is_public
        return self.audit_is_public(file_path=file_path, force=force, persist=persist)

    def check_public_access(self):
        """
        Dev-friendly boolean check.

        Returns:
            bool: True if this FileManager's configured S3 prefix appears publicly readable
                  (based on bucket policy + PublicAccessBlock audit), else False.
        """
        try:
            if not self.is_s3:
                return False
            self.backend.test_connection()
            ok, issues, details = self.backend.check_public_access_for_prefix(file_path=None)
            return bool(ok and (details or {}).get("status", "public") == "public")
        except Exception:
            return False

    def check_public_access_report(self):
        """
        Dev-friendly detailed report (previous behavior).

        Returns:
            dict: {status: bool, result: {ok: bool, issues: [...], details: {...}}} on success
                  or {status: False, error: "..."} on failure.
        """
        try:
            if not self.is_s3:
                return dict(status=False, error="Public access auditing is only supported for S3 backends.")
            self.backend.test_connection()
            ok, issues, details = self.backend.check_public_access_for_prefix(file_path=None)
            return dict(status=True, result=dict(ok=ok, issues=issues, details=details))
        except Exception as e:
            return dict(status=False, error="S3 public-access audit failed",
                        details=safe_error_detail(e, "s3.audit_public_access"))

    def on_action_test_connection(self, value):
        try:
            self.backend.test_connection()
            return dict(status=True)
        except Exception as e:
            return dict(status=False, error="Storage connection test failed",
                        details=safe_error_detail(e, "s3.test_connection"))

    def on_action_fix_cors(self, value):
        try:
            if not self.is_s3:
                return dict(status=False, error="CORS management is only supported for S3 backends.")
            # Validate connectivity first
            self.backend.test_connection()
            allowed_origins = self._resolve_allowed_origins_from_value_or_settings(value or {})
            result = self.update_cors(allowed_origins)
            return dict(status=True, result=result)
        except Exception as e:
            return dict(status=False, error="S3 CORS repair failed",
                        details=safe_error_detail(e, "s3.put_bucket_cors"))

    def on_action_check_cors(self, value):
        try:
            if not self.is_s3:
                return dict(status=False, error="CORS management is only supported for S3 backends.")
            self.backend.test_connection()
            # allowed_origins = self._resolve_allowed_origins_from_value_or_settings(value or {})
            result = self.check_cors_config(allowed_origins=self.allowed_origins)
            return dict(status=True, result=result)
        except Exception as e:
            return dict(status=False, error="S3 CORS audit failed",
                        details=safe_error_detail(e, "s3.get_bucket_cors"))

    def on_action_check_public_access(self, value):
        """
        Audit whether this FileManager is *really* publicly readable in S3.

        This is useful when `is_public=True` but direct URLs still return `AccessDenied`.
        The S3 backend checks:
          - Bucket PublicAccessBlock settings
          - Bucket policy statement for the configured prefix

        Returns:
            {status: bool, result: {ok: bool, issues: [...], details: {...}}}
        """
        return self.check_public_access_report()

    def _cdn_action_allowed(self):
        return self._is_cdn_admin()

    def on_action_enable_cdn(self, value):
        """Provision CloudFront delivery for this manager's bucket (maestro #8250).

        Runs under the deployment's AWS identity; global file admins only. `harden`
        in the value also turns on Block Public Access and Object Ownership,
        which breaks raw-S3 public links of managers opted out of the CDN.
        """
        if not self._cdn_action_allowed():
            raise me.PermissionDeniedException(
                reason="Only file admins (manage_files) can enable CDN delivery",
                model_name="FileManager", event_type="user_permission_denied",
                branch="fileman_cdn_enable")
        if not self.is_s3:
            return dict(status=False, error="CDN delivery is only supported for S3 backends.")
        from mojo.apps.fileman.services import cdn_provision
        harden = bool((value or {}).get("harden")) if isinstance(value, dict) else False
        return dict(status=True, result=cdn_provision.enable(
            self, harden=harden, **(self._cdn_provision_kwargs or {})))

    def on_action_check_cdn(self, value):
        """Read-only: validate the bucket's distribution, provisioned or BYO.

        File-admin only like the other two: it reads the account under the
        deployment identity and reports policy documents and ARNs.
        """
        if not self._cdn_action_allowed():
            raise me.PermissionDeniedException(
                reason="Only file admins (manage_files) can check CDN delivery",
                model_name="FileManager", event_type="user_permission_denied",
                branch="fileman_cdn_check")
        if not self.is_s3:
            return dict(status=False, error="CDN delivery is only supported for S3 backends.")
        from mojo.apps.fileman.services import cdn_provision
        return dict(status=True, result=cdn_provision.check(self, **(self._cdn_provision_kwargs or {})))

    def on_action_disable_cdn(self, value):
        """Every manager on the bucket reverts to S3 links. AWS is untouched
        unless `retire` is set, which disables the distribution (never deletes)."""
        if not self._cdn_action_allowed():
            raise me.PermissionDeniedException(
                reason="Only file admins (manage_files) can disable CDN delivery",
                model_name="FileManager", event_type="user_permission_denied",
                branch="fileman_cdn_disable")
        from mojo.apps.fileman.services import cdn_provision
        retire = bool((value or {}).get("retire")) if isinstance(value, dict) else False
        return dict(status=True, result=cdn_provision.disable(
            self, retire=retire, **(self._cdn_provision_kwargs or {})))

    def on_action_clone(self, value):
        secrets = self.secrets
        new_manager = FileManager(user=self.user, group=self.group)
        new_manager.name = f"Clone of {self.name}"
        new_manager.backend_url = self.backend_url
        new_manager.backend_type = self.backend_type
        # A clone shares the original's prefix, so it must share its visibility:
        # a public duplicate of a private prefix would be a public prefix.
        new_manager.is_public = self.is_public
        new_manager.set_secrets(secrets)
        new_manager.save()
        return dict(status=True, id=new_manager.id)

    def fix_cors(self):
        """
        Ensure bucket CORS allows direct uploads from configured origins.
        This uses manager settings and does not require manual AWS console changes.
        """
        if not self.is_s3:
            return
        allowed_origins = self._resolve_allowed_origins_from_value_or_settings({})
        self.update_cors(allowed_origins)

    # --- CORS helpers for S3 direct upload ---
    def _s3_client(self):
        # check_cors_config/update_cors are public and not every caller checks
        # is_s3 first, so keep the readable error rather than letting a
        # filesystem manager fail with AttributeError inside the backend.
        if not self.is_s3:
            raise ValueError("CORS management is only supported for S3 backends.")
        # Delegate to the backend so CORS runs under exactly the credentials
        # uploads use. Reading aws_key/aws_secret here consulted this manager's
        # OWN secrets, while the backend resolves primary_settings from the
        # parent — a child manager's CORS calls used a different identity.
        return self.backend.client

    def _resolve_allowed_origins_from_value_or_settings(self, value):
        """
        Resolve a list of allowed origins from the action value, this manager's
        own ``allowed_origins`` setting, or explicit global CORS settings.
        Accepts 'origins', 'allowed_origins', 'domains', or 'list_of_domains' keys.
        Falls back to ``*`` when none are configured. CORS controls which browser
        origins may use a presigned capability; it does not grant S3 access.
        """
        origins = []

        if isinstance(value, dict):
            for key in ("origins", "allowed_origins", "domains", "list_of_domains"):
                v = value.get(key)
                if v:
                    if isinstance(v, str):
                        origins.extend([s.strip() for s in v.split(",") if s.strip()])
                    elif isinstance(v, (list, tuple)):
                        origins.extend([str(s).strip() for s in v if str(s).strip()])
                    break

        # The manager's own configured allowed_origins (set via
        # set_allowed_origins). on_action_check_cors already consults this, so
        # the fix path must read the same source — otherwise fix_cors errors on
        # a manager that check_cors happily validates.
        mgr_origins = self.allowed_origins
        if mgr_origins:
            if isinstance(mgr_origins, str):
                origins.extend([s.strip() for s in mgr_origins.split(",") if s.strip()])
            elif isinstance(mgr_origins, (list, tuple)):
                origins.extend([str(s).strip() for s in mgr_origins if str(s).strip()])

        for key in ("CORS_ALLOWED_ORIGINS", "ALLOWED_ORIGINS"):
            v = settings.get(key)
            if v:
                if isinstance(v, str):
                    origins.extend([s.strip() for s in v.split(",") if s.strip()])
                elif isinstance(v, (list, tuple)):
                    origins.extend([str(s).strip() for s in v if str(s).strip()])

        # Normalize: dedupe, drop trailing slash
        cleaned = []
        seen = set()
        for o in origins:
            if not o:
                continue
            if o.endswith("/"):
                o = o[:-1]
            if o not in seen:
                seen.add(o)
                cleaned.append(o)

        return cleaned or ["*"]

    def check_cors_config(self, allowed_origins=None, required_methods=None, required_headers=None):
        """
        Check the current CORS configuration to ensure it supports direct uploads.
        Note: S3 CORS is bucket-wide. Prefix-level restriction must be enforced by IAM/policy and presigned URLs.
        """
        if not self.is_s3:
            raise ValueError("CORS management is only supported for S3 backends.")

        s3 = self._s3_client()
        bucket = self.root_location

        try:
            resp = s3.get_bucket_cors(Bucket=bucket)
            config = resp
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "NoSuchCORSConfiguration":
                return {"ok": False, "issues": ["No CORS configuration set on this bucket."], "config": None}
            return {
                "ok": False,
                "issues": ["S3 CORS configuration could not be inspected safely."],
                "config": None,
                "failure": safe_error_detail(
                    e, "s3.get_bucket_cors", "s3:GetBucketCORS"),
            }

        if not allowed_origins:
            allowed_origins = self._resolve_allowed_origins_from_value_or_settings({})

        required_methods = [m.upper() for m in (required_methods or ["GET", "PUT", "POST", "HEAD"])]
        required_headers = [h.lower() for h in (required_headers or ["content-type"])]

        rules = config.get("CORSRules", [])
        issues = []

        def origin_covered(origin: str) -> bool:
            for r in rules:
                origins = r.get("AllowedOrigins", [])
                if "*" in origins or origin in origins:
                    methods = [m.upper() for m in r.get("AllowedMethods", [])]
                    if not all(m in methods for m in required_methods):
                        continue
                    headers = [h.lower() for h in r.get("AllowedHeaders", [])]
                    if "*" in headers or all(h in headers for h in required_headers):
                        return True
            return False

        for origin in allowed_origins:
            if not origin_covered(origin):
                issues.append(f"Origin not covered for direct upload: {origin}")

        return {"ok": len(issues) == 0, "issues": issues, "config": config}

    def update_cors(self, allowed_origins, merge=True, allowed_methods=None, allowed_headers=None, expose_headers=None, max_age_seconds=3000):
        """
        Update bucket CORS to support direct uploads from allowed_origins.
        If merge=True, append our rule to any existing rules; otherwise replace entirely.
        """
        if not self.is_s3:
            raise ValueError("CORS management is only supported for S3 backends.")

        s3 = self._s3_client()
        bucket = self.root_location
        if not allowed_origins:
            raise ValueError("No allowed origins provided. Please pass at least one origin.")

        if allowed_methods is None:
            allowed_methods = ["POST", "HEAD"] if getattr(self.backend, "server_side_encryption", None) else ["PUT", "HEAD"]
        allowed_methods = [m.upper() for m in allowed_methods]
        allowed_headers = [h for h in (allowed_headers or ["*"])]
        expose_headers = expose_headers or ["ETag", "x-amz-request-id", "x-amz-id-2", "x-amz-version-id"]

        desired = {
            "CORSRules": [
                {
                    "AllowedOrigins": allowed_origins,
                    "AllowedMethods": allowed_methods,
                    "AllowedHeaders": allowed_headers,
                    "ExposeHeaders": expose_headers,
                    "MaxAgeSeconds": max_age_seconds,
                }
            ]
        }

        # If already compliant, no change
        verify_required_headers = [] if getattr(self.backend, "server_side_encryption", None) else ["content-type"]
        check = self.check_cors_config(allowed_origins, required_methods=allowed_methods, required_headers=verify_required_headers)
        if check["ok"]:
            return {"changed": False, "message": "Existing CORS already supports direct uploads.", "current": check["config"]}

        current = None
        try:
            current = s3.get_bucket_cors(Bucket=bucket)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != "NoSuchCORSConfiguration":
                raise

        if merge and current:
            merged = {"CORSRules": current.get("CORSRules", []) + desired["CORSRules"]}
            s3.put_bucket_cors(Bucket=bucket, CORSConfiguration=merged)
            applied = merged
        else:
            s3.put_bucket_cors(Bucket=bucket, CORSConfiguration=desired)
            applied = desired

        verify_required_headers = [] if getattr(self.backend, "server_side_encryption", None) else ["content-type"]
        verify = self.check_cors_config(allowed_origins, required_methods=allowed_methods, required_headers=verify_required_headers)
        return {"changed": True, "applied": applied, "verified": verify["ok"], "post_update_issues": verify["issues"]}

    @classmethod
    def get_from_request(cls, request):
        """Get the file manager from the request.

        A manager named by id in the body is returned only when the caller
        may use it (`_authorize_named_manager`); otherwise 403. With no id,
        the caller's own user/group manager is resolved as before.
        """
        fm_id = request.DATA.get(["fileman", "filemanager", "file_manager", "manager", "file_manager_id"])
        if fm_id:
            return cls._authorize_named_manager(request, fm_id)
        use = cls.clean_use(
            request.DATA.get(["use", "fileman_use", "file_manager_use"], ""), from_request=True)
        group = getattr(request, "group", None)
        if not group:
            return cls.get_for_user_group(user=request.user, use=use)
        existing = cls._existing_for_group(group, use)
        if existing is not None:
            return existing
        # Nothing exists yet: refuse before get_for_group writes any row
        # (or the group's uuid) for a caller who may not create it.
        if not cls._may_create_for(request, group):
            raise me.PermissionDeniedException(
                reason="File manager is unavailable",
                model_name="FileManager",
                event_type="user_permission_denied",
                branch="fileman_create_denied",
            )
        return cls.get_for_group(group=group, use=use)

    @classmethod
    def _may_create_for(cls, request, group, inline=False):
        """Whether this request may cause storage to be created for `group`.

        A person: an active member of the group (parents walked), or a holder
        of a global `manage_files`/`files` grant; on the inline path a global
        `manage_groups`/`groups` grant too. An ApiKey or group token: on the
        general file endpoint the SAVE_PERMS check it gets after resolution;
        on the inline path the dispatcher's own bound (`is_group_allowed`).
        """
        from mojo.helpers.request import is_request_user, restricted_identity

        ident = restricted_identity(request)
        if ident is not None:
            if inline:
                return ident.is_group_allowed(group)
            original_group = getattr(request, "group", None)
            try:
                return cls.rest_check_permission(
                    request, "SAVE_PERMS", cls(group=group, user=None))
            finally:
                request.group = original_group
        actor = getattr(request, "user", None)
        if actor is None or not is_request_user(request):
            return False
        perms = ["manage_files", "files"]
        if inline:
            perms += ["manage_groups", "groups"]
        if actor.has_permission(perms):
            return True
        return group.get_member_for_user(actor, check_parents=True) is not None

    def require_write_right(self, request, inline=False):
        """The upload rule for a write that names no storage in its body.

        The actor part of `authorize_for_upload` without its body-selector
        comparisons. An ApiKey or group token may write to group storage in a
        group it covers, and a key acting as a member to that member's own
        storage, as `_authorize_named_manager` allows.
        """
        from mojo.helpers.request import (
            is_override_user_session, is_request_user, restricted_identity)

        unavailable = me.PermissionDeniedException("File manager is unavailable")
        if not self.is_active:
            raise unavailable
        if self.group_id and not self.group.is_effectively_active():
            raise unavailable
        ident = restricted_identity(request)
        if ident is not None:
            if self.group_id is not None and self.user_id is None:
                if ident.is_group_allowed(self.group):
                    return
            elif (self.user_id is not None and self.group_id is None
                  and is_override_user_session(request)
                  and self.user_id == getattr(request.user, "id", None)):
                return
            raise unavailable
        actor = getattr(request, "user", None)
        if actor is None or not is_request_user(request):
            raise unavailable
        perms = ["manage_files", "files"]
        if inline:
            perms += ["manage_groups", "groups"]
        if actor.has_permission(perms):
            return
        if self.user_id is None and self.group_id is None:
            raise me.PermissionDeniedException("Global file storage permission is required")
        if self.user_id is not None and self.user_id != actor.id:
            raise unavailable
        if self.group_id is not None and self.group.get_member_for_user(actor, check_parents=True) is None:
            raise me.PermissionDeniedException("Active group membership is required")

    @classmethod
    def _system_default(cls):
        return cls.objects.filter(
            user=None, group=None, is_default=True, is_active=True
        ).order_by("id").first()

    @classmethod
    def _existing_for_user(cls, user):
        return cls.objects.filter(
            user=user, group=None, is_default=True, is_active=True
        ).order_by(*SELECTION_ORDER).first()

    @classmethod
    def get_for_user(cls, user):
        """Get the file manager for a specific user (without group)"""
        file_manager = cls._existing_for_user(user)
        if file_manager is None:
            sys_manager = cls._system_default()
            if sys_manager is not None:
                file_manager = cls._create_for_user(user, sys_manager)
        if file_manager is not None:
            file_manager.ensure_public_access_audited()
        return file_manager

    @classmethod
    def _create_for_user(cls, user, sys_manager):
        """Derive the user's manager from the system default. A lost race
        returns the manager that won it; this never raises to an uploader."""
        from django.db import transaction, IntegrityError

        # Generate name before creating to avoid race conditions
        temp_manager = cls(user=user, is_default=True, group=None, parent=sys_manager)
        temp_manager.set_backend_url(sys_manager.backend_url, user.uuid.hex)
        name = temp_manager.generate_name()

        try:
            with transaction.atomic():
                # Use get_or_create to handle race conditions
                file_manager, created = cls.objects.get_or_create(
                    user=user,
                    group=None,
                    name=name,
                    defaults={
                        'is_default': True,
                        'parent': sys_manager,
                        'backend_type': sys_manager.backend_type,
                        'backend_url': temp_manager.backend_url,
                        'is_active': True,
                        'is_public': sys_manager.is_public,
                    }
                )
                if created:
                    # Copy secrets from parent
                    file_manager.set_secrets(sys_manager.secrets)
                    file_manager.save()
        except (IntegrityError, DuplicateUseError):
            file_manager = cls._existing_for_user(user)
        return file_manager

    @classmethod
    def _existing_for_group(cls, group, use=""):
        # Prefer a manager matching the specific use if provided; otherwise default group manager
        if use != "":
            return cls.objects.filter(
                user=None, group=group, use=use, is_active=True
            ).order_by(*SELECTION_ORDER).first()
        return cls.objects.filter(
            user=None, group=group, use=use, is_default=True, is_active=True
        ).order_by(*SELECTION_ORDER).first()

    @classmethod
    def get_for_group(cls, group=None, use=""):
        use = cls.clean_use(use)
        file_manager = cls._existing_for_group(group, use)

        if file_manager is None:
            sys_manager = cls._system_default()
            if sys_manager is not None:
                file_manager = cls._create_for_group(group, use, sys_manager)
        return file_manager

    @classmethod
    def _create_for_group(cls, group, use, sys_manager):
        """Derive the group's manager for `use` from the system default. When
        another manager already holds the purpose (a race, or one saved under
        another name), that one is returned; this never raises to an uploader."""
        from django.db import transaction, IntegrityError

        # Generate name before creating to avoid race conditions
        temp_manager = cls(group=group, is_default=True, user=None, parent=sys_manager)
        if use:
            temp_manager.use = use
            temp_manager.set_backend_url(sys_manager.backend_url, group.get_uuid(), use)
        else:
            temp_manager.set_backend_url(sys_manager.backend_url, group.get_uuid())
        name = temp_manager.generate_name()

        try:
            with transaction.atomic():
                # Use get_or_create to handle race conditions
                defaults = {
                    'is_default': True,
                    'parent': sys_manager,
                    'backend_type': sys_manager.backend_type,
                    'backend_url': temp_manager.backend_url,
                    'is_active': True,
                    'is_public': sys_manager.is_public,
                }
                if use:
                    defaults['use'] = use
                file_manager, created = cls.objects.get_or_create(
                    user=None,
                    group=group,
                    name=name,
                    defaults=defaults
                )
                if created:
                    # Copy secrets from parent
                    file_manager.set_secrets(sys_manager.secrets)
                    file_manager.save()
                    file_manager.audit_is_public()
        except (IntegrityError, DuplicateUseError):
            file_manager = cls._existing_for_group(group, use)
        return file_manager

    @classmethod
    def get_for_user_group(cls, user=None, group=None, use=""):
        """Get the file manager from the user and/or group

        If group is provided, returns the group's FileManager (user-agnostic).
        If only user is provided, returns the user's FileManager.
        """
        if group:
            # Always use group manager when group is present, ignore user
            return cls.get_for_group(group=group, use=use or "")
        elif user:
            # Only use user manager if no group
            return cls.get_for_user(user=user)
        return None
