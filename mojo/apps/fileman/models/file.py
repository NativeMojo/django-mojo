from django.db import models, transaction
from mojo.models import MojoModel
from objict import objict
import io
import uuid
import hashlib
import base64
import magic
import mimetypes
from datetime import datetime
import os
from mojo.apps.fileman import utils
from mojo.apps.fileman.models import FileManager
from mojo.helpers import logit

logger = logit.get_logger("fileman", "fileman.log")


# --- shortlink integration helpers -------------------------------------------
# Shortlink is an optional dependency: fileman works fine without it. These
# helpers no-op (or return the direct URL) when the shortlink app is not
# installed, or when the global/per-manager toggle is off.

FILEMAN_SHORTLINK_SOURCE = "fileman"
FILEMAN_SHARE_SOURCE = "fileman-share"


def _shortlink_installed():
    from django.apps import apps
    return apps.is_installed("mojo.apps.shortlink")


def _shortlinks_enabled(file_manager):
    """Effective on/off state for file URL shortening.

    Order of precedence:
      1. If the shortlink app is not installed → False.
      2. Per-FileManager setting `use_shortlinks` (True/False), if set.
      3. Global setting `FILEMAN_USE_SHORTLINKS` (default True).
    """
    if not _shortlink_installed():
        return False
    per_fm = file_manager.get_setting("use_shortlinks", None) if file_manager else None
    if per_fm is not None:
        return bool(per_fm)
    from mojo.helpers.settings import settings
    return bool(settings.get("FILEMAN_USE_SHORTLINKS", True))


def _shortlink_base_url():
    from mojo.helpers.settings import settings
    base = (
        settings.get("SHORTLINK_BASE_URL")
        or settings.get("WEBAPP_BASE_URL")
        or settings.get("BASE_URL", "")
    )
    return (base or "").rstrip("/")


def _compose_short_url(code):
    return f"{_shortlink_base_url()}/s/{code}"


def _extract_code_from_short_url(url):
    # URLs look like "<base>/s/<code>"; take the last path segment.
    if not url:
        return None
    return url.rstrip("/").rsplit("/", 1)[-1] or None


def _get_or_create_shortlink_url(file=None, rendition=None):
    """Return the composed tier-1 short URL for a File or FileRendition.

    If a shortlink_code is already cached on the row and points to an active
    ShortLink, compose and return `<base>/s/<code>`. Otherwise call
    `shortlink.shorten(...)` to mint a new row, write the code back to the
    owning row using a race-safe idempotent UPDATE, and return the URL.

    A stale shortlink_code (row deleted or deactivated) triggers a regenerate.
    """
    from mojo.apps.shortlink import shorten
    from mojo.apps.shortlink.models import ShortLink

    owner = rendition or file
    if owner is None or not getattr(owner, "id", None):
        return None

    # Happy path: cached code + active shortlink row → compose and return.
    code = getattr(owner, "shortlink_code", None)
    if code:
        link = ShortLink.objects.filter(code=code, is_active=True).first()
        if link is not None:
            return _compose_short_url(code)
        # Stale code — null it and mint a fresh link.
        type(owner).objects.filter(pk=owner.pk).update(shortlink_code=None)
        owner.shortlink_code = None

    # Resolve per-FileManager shortlink options.
    file_manager = rendition.file_manager if rendition else file.file_manager
    expire_days = int(file_manager.get_setting("shortlink_expire_days", 0) or 0)
    track_clicks = bool(file_manager.get_setting("shortlink_track_clicks", False))
    owner_user = getattr(owner, "user", None)
    owner_group = getattr(owner, "group", None)
    # FileRendition has no direct user/group — inherit from parent File.
    if rendition is not None:
        owner_user = rendition.original_file.user
        owner_group = rendition.original_file.group

    # Mint a new ShortLink. resolve_file=True → each click regenerates the
    # backend URL via get_direct_download_url().
    kwargs = dict(
        source=FILEMAN_SHORTLINK_SOURCE,
        expire_days=expire_days,
        expire_hours=0,
        track_clicks=track_clicks,
        resolve_file=True,
        bot_passthrough=False,
        user=owner_user,
        group=owner_group,
    )
    if rendition is not None:
        short_url = shorten(rendition=rendition, **kwargs)
    else:
        short_url = shorten(file=file, **kwargs)

    new_code = _extract_code_from_short_url(short_url)
    if not new_code:
        return short_url

    # Race-safe idempotent write: only set if still NULL. If another concurrent
    # call already wrote a code, keep that one (and our minted row becomes a
    # harmless orphan — acceptable waste; cleanup cron can sweep by source+age).
    updated = type(owner).objects.filter(pk=owner.pk, shortlink_code__isnull=True).update(
        shortlink_code=new_code
    )
    if updated == 0:
        # Re-read to pick up the winner's code.
        owner.refresh_from_db(fields=["shortlink_code"])
    else:
        owner.shortlink_code = new_code

    return _compose_short_url(owner.shortlink_code or new_code)


def _mint_share_link(owner, value):
    """Mint a tier-2 share shortlink for a File or FileRendition.

    `value` is `True` or a dict of {expire_days, expire_hours, track_clicks, note}.
    Returns {url, code, expires_at, track_clicks} on success, or
    {status: False, error: ...} when shortlink is unavailable.
    """
    if not _shortlink_installed():
        return {"status": False, "error": "shortlink app is not installed"}

    opts = value if isinstance(value, dict) else {}
    # Sanitize / clamp.
    max_days = getattr(owner, "MAX_SHARE_EXPIRE_DAYS", 3650)
    max_note = getattr(owner, "MAX_SHARE_NOTE_LEN", 512)
    try:
        expire_days = int(opts.get("expire_days", 0) or 0)
    except (TypeError, ValueError):
        expire_days = 0
    expire_days = max(0, min(expire_days, max_days))
    try:
        expire_hours = int(opts.get("expire_hours", 0) or 0)
    except (TypeError, ValueError):
        expire_hours = 0
    expire_hours = max(0, min(expire_hours, 24 * max_days))
    track_clicks = bool(opts.get("track_clicks", False))
    note = opts.get("note")
    if note is not None:
        note = str(note)[:max_note]
    metadata = {"note": note} if note else None

    # Resolve who the sharer is (prefer active request user, fall back to owner's user).
    req = getattr(owner, "active_request", None)
    sharer = getattr(req, "user", None) if req is not None else None
    from mojo.apps.fileman.models.rendition import FileRendition
    if sharer is None:
        if isinstance(owner, FileRendition):
            sharer = owner.original_file.user
        else:
            sharer = getattr(owner, "user", None)

    if isinstance(owner, FileRendition):
        group = owner.original_file.group
    else:
        group = getattr(owner, "group", None)

    from mojo.apps.shortlink import shorten
    kwargs = dict(
        source=FILEMAN_SHARE_SOURCE,
        expire_days=expire_days,
        expire_hours=expire_hours,
        track_clicks=track_clicks,
        resolve_file=True,
        bot_passthrough=False,
        metadata=metadata,
        user=sharer,
        group=group,
    )
    if isinstance(owner, FileRendition):
        short_url = shorten(rendition=owner, **kwargs)
    else:
        short_url = shorten(file=owner, **kwargs)

    code = _extract_code_from_short_url(short_url)
    # Look up the row we just created to capture expires_at.
    from mojo.apps.shortlink.models import ShortLink
    link = ShortLink.objects.filter(code=code).first() if code else None
    expires_at = link.expires_at.isoformat() if (link and link.expires_at) else None
    # NB: key is named `shortlink_code` (not just `code`) to avoid colliding with
    # `mojo.helpers.response.JsonResponse` which injects `code = <http_status>`.
    return {
        "url": short_url,
        "shortlink_code": code,
        "expires_at": expires_at,
        "track_clicks": track_clicks,
    }


def _delete_fileman_shortlinks(file=None, rendition=None):
    """Delete auto-generated shortlink rows for a File or FileRendition on delete.

    Scoped to our `source` values so human-created shortlinks pointing at the
    same file/rendition are preserved.
    """
    if not _shortlink_installed():
        return
    try:
        from mojo.apps.shortlink.models import ShortLink
        q = ShortLink.objects.filter(
            source__in=[FILEMAN_SHORTLINK_SOURCE, FILEMAN_SHARE_SOURCE]
        )
        if rendition is not None:
            q = q.filter(rendition=rendition)
        elif file is not None:
            q = q.filter(file=file)
        else:
            return
        q.delete()
    except Exception as e:
        logger.warning("_delete_fileman_shortlinks: cleanup failed: %s", str(e))


class File(models.Model, MojoModel):
    """
    File model representing uploaded files with metadata and storage information
    """

    class RestMeta:
        CAN_CREATE = True
        CAN_DELETE = True
        DEFAULT_SORT = "-created"
        VIEW_PERMS = ["view_fileman", "manage_files", "files"]
        SAVE_PERMS = ["manage_files", "files"]
        SEARCH_FIELDS = ["filename", "content_type"]
        POST_SAVE_ACTIONS = ["action", "regenerate_renditions", "share"]
        SEARCH_TERMS = [
            "filename",  "content_type",
            ("group", "group__name"),
            ("file_manager", "file_manager__name")]

        GRAPHS = {
            "upload": {
                "fields": ["id", "filename", "content_type", "file_size", "upload_url"],
            },
            "detailed": {
                "extra": ["url", "renditions"],
                "graphs": {
                    "group": "basic",
                    "file_manager": "basic",
                    "user": "basic"
                }
            },
            "basic": {
                "fields": ["id", "filename", "content_type", "category"],
                "extra": ["url", "thumbnail"],
            },
            "default": {
                "extra": ["url", "renditions"],
            },
            "list": {
                "extra": ["url", "renditions"],
                "graphs": {
                    "group": "basic",
                    "file_manager": "basic",
                    "user": "basic"
                }
            }
        }

    # Upload status choices
    PENDING = 'pending'
    UPLOADING = 'uploading'
    COMPLETED = 'completed'
    FAILED = 'failed'
    EXPIRED = 'expired'

    STATUS_CHOICES = [
        (PENDING, 'Pending Upload'),
        (UPLOADING, 'Uploading'),
        (COMPLETED, 'Upload Completed'),
        (FAILED, 'Upload Failed'),
        (EXPIRED, 'Upload Expired'),
    ]

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True)

    group = models.ForeignKey(
        "account.Group",
        related_name="files",
        null=True,
        blank=True,
        default=None,
        on_delete=models.CASCADE,
        help_text="Group that owns this file"
    )

    user = models.ForeignKey(
        "account.User",
        related_name="files",
        null=True,
        blank=True,
        default=None,
        on_delete=models.SET_NULL,
        help_text="User who uploaded this file"
    )

    file_manager = models.ForeignKey(
        "fileman.FileManager",
        related_name="files",
        on_delete=models.CASCADE,
        help_text="File manager configuration used for this file"
    )

    filename = models.CharField(
        max_length=255,
        db_index=True,
        help_text="User-provided filename"
    )

    storage_filename = models.CharField(
        max_length=255,
        help_text="Storage filename",
        default=None,
        blank=True,
        null=True,
    )

    storage_file_path = models.TextField(
        help_text="Full path to file in storage backend"
    )

    download_url = models.TextField(
        blank=True,
        null=True,
        default=None,
        help_text="Persistent URL for downloading the file, (if allowed)"
    )

    file_size = models.BigIntegerField(
        null=True,
        blank=True,
        help_text="File size in bytes"
    )

    content_type = models.CharField(
        max_length=255,
        db_index=True,
        help_text="MIME type of the file"
    )

    category = models.CharField(
        max_length=255,
        db_index=True,
        default=None,
        blank=True,
        null=True,
        help_text="A category for the file, like 'image', 'document', 'video', etc."
    )

    checksum = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text="File checksum (MD5, SHA256, etc.)"
    )

    upload_token = models.CharField(
        max_length=64,
        db_index=True,
        help_text="Unique token for tracking direct uploads"
    )

    upload_status = models.CharField(
        max_length=32,
        choices=STATUS_CHOICES,
        default=PENDING,
        db_index=True,
        help_text="Current status of the file upload"
    )

    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Additional file metadata and custom properties"
    )

    is_active = models.BooleanField(
        default=True,
        help_text="Whether this file is active and accessible"
    )

    is_public = models.BooleanField(
        default=False,
        help_text="Whether this file can be accessed without authentication"
    )

    shortlink_code = models.CharField(
        max_length=10,
        null=True,
        blank=True,
        default=None,
        db_index=True,
        help_text="Code of the shortlink that wraps this file's download URL (tier 1, internal)"
    )

    upload_url = None

    # Upper bound on how many rendition roles a single regenerate request
    # may target. Prevents a caller with manage_files from kicking off an
    # unbounded ffmpeg loop on the renditions worker.
    MAX_REGENERATE_ROLES = 20
    # Maximum days a share shortlink may live. Shares above this are clamped.
    MAX_SHARE_EXPIRE_DAYS = 3650
    # Max length of the free-form `note` stored in a share shortlink's metadata.
    MAX_SHARE_NOTE_LEN = 512

    class Meta:
        indexes = [
            models.Index(fields=['upload_status', 'created']),
            models.Index(fields=['file_manager', 'upload_status']),
            models.Index(fields=['group', 'is_active']),
            models.Index(fields=['content_type', 'is_active']),
        ]

    def __str__(self):
        return f"{self.filename} ({self.get_upload_status_display()})"

    def on_rest_pre_save(self, changed_fields, created):
        if created:
            if not hasattr(self, "file_manager") or self.file_manager is None:
                self.file_manager = FileManager.get_from_request(self.active_request)
            if not self.content_type:
                self.content_type = mimetypes.guess_type(self.filename)[0] or 'application/octet-stream'
            self.category = utils.get_file_category(self.content_type)
            if not self.storage_filename:
                self.generate_storage_filename()

    def on_rest_pre_delete(self):
        # Remove every rendition's storage object (each row knows its own
        # storage_path — the renderers use inconsistent path conventions,
        # so walking rows is the only layout-agnostic way to clean up).
        backend = self.file_manager.backend
        for rendition in self.file_renditions.all():
            if rendition.storage_path:
                try:
                    backend.delete(rendition.storage_path)
                except Exception as e:
                    logger.warning("on_rest_pre_delete: failed to delete rendition %s (%s): %s",
                                   rendition.id, rendition.storage_path, str(e))
        # Then the original.
        if self.storage_file_path:
            try:
                backend.delete(self.storage_file_path)
            except Exception as e:
                logger.warning("on_rest_pre_delete: failed to delete original %s: %s",
                               self.storage_file_path, str(e))
        # Drop auto-generated shortlink rows (tier-1 display + tier-2 share).
        # Human-created shortlinks (different `source`) stay intact.
        _delete_fileman_shortlinks(file=self)

    def generate_upload_token(self, commit=False):
        """Generate a unique upload token"""
        self.upload_token = hashlib.sha256(f"{uuid.uuid4()}{datetime.now()}".encode()).hexdigest()[:32]
        if commit:
            self.save()

    def generate_storage_filename(self):
        """Generate a unique filename for storage"""
        name, ext = os.path.splitext(self.filename)
        # timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        unique_id = str(uuid.uuid4())[:8]
        self.storage_filename = f"{name}_{unique_id}{ext}"
        self.storage_file_path = os.path.join(self.file_manager.root_path, self.storage_filename)

    def request_upload_url(self):
        """Request a pre-signed URL for direct upload"""
        if not self.file_manager.backend.supports_direct_upload():
            self.generate_upload_token(True)
            self.upload_url = f"/api/fileman/upload/{self.upload_token}"
        else:
            data = self.file_manager.backend.generate_upload_url(self.storage_file_path, self.content_type, self.file_size)
            self.debug("request_upload_url", data)
            if "url" in data:
                self.upload_url = data['url']
            else:
                self.upload_url = data
        return self.upload_url

    def get_metadata(self, key, default=None):
        """Get a specific metadata value"""
        return self.metadata.get(key, default)

    def set_metadata(self, key, value):
        """Set a specific metadata value"""
        self.metadata[key] = value

    _renditions = None
    @property
    def renditions(self):
        if self._renditions is None:
            self._renditions = objict.from_dict({r.role: r.to_dict() for r in self.file_renditions.all()})
        return self._renditions

    @property
    def is_pending(self):
        return self.upload_status == self.PENDING

    @property
    def is_uploading(self):
        return self.upload_status == self.UPLOADING

    @property
    def is_completed(self):
        return self.upload_status == self.COMPLETED

    @property
    def is_failed(self):
        return self.upload_status == self.FAILED

    @property
    def is_expired(self):
        return self.upload_status == self.EXPIRED

    @property
    def url(self):
        return self.generate_download_url()

    @property
    def thumbnail(self):
        r = self.get_rendition_by_role('thumbnail')
        if r:
            return r.url
        return None

    def get_rendition_by_role(self, role):
        return self.file_renditions.filter(role=role).first()

    def get_direct_download_url(self):
        """Return the raw backend URL (presigned for private, public for public),
        bypassing any shortlink wrapping. Used by the shortlink resolver and
        as the fallback when shortlinks are disabled.
        """
        if self.file_manager.is_public:
            if not self.download_url:
                self.download_url = self.file_manager.backend.get_url(self.storage_file_path)
            return self.download_url
        # Private — regenerate a fresh presigned URL every call.
        return self.file_manager.backend.get_url(
            self.storage_file_path,
            self.file_manager.get_setting("urls_expire_in", 3600),
        )

    def generate_download_url(self):
        """Return the URL clients should use.

        When shortlinks are enabled (global + per-manager toggle, and the
        shortlink app is installed), returns a `/s/<code>` short URL backed
        by a tier-1 ShortLink row tied to this file. Otherwise returns the
        raw backend URL (identical to pre-shortlink behavior).
        """
        if not _shortlinks_enabled(self.file_manager):
            return self.get_direct_download_url()
        return _get_or_create_shortlink_url(self, rendition=None)

    def on_action_action(self, action):
        # Legacy dispatch for the single-verb {"action": "..."} shape.
        # Kept for existing UI compatibility on lifecycle transitions.
        if action == "mark_as_completed":
            self.mark_as_completed(commit=True)
        elif action == "mark_as_failed":
            self.mark_as_failed(commit=True)
        elif action == "mark_as_uploading":
            self.mark_as_uploading(commit=True)

    def on_action_regenerate_renditions(self, value):
        """Enqueue a rendition regenerate job.

        Triggered by {"regenerate_renditions": true | ["role1", "role2"]}.
        - value=True → regenerate all default roles
        - value=list → regenerate only the named roles
        """
        roles = value if isinstance(value, list) else None
        self.publish_regenerate_renditions(roles=roles)
        return {"queued": True, "roles": roles}

    def on_action_share(self, value):
        """Mint a new tier-2 share shortlink for this file.

        Triggered by {"share": true | {"expire_days": 30, "track_clicks": true, "note": "..."}}.
        Returns {url, code, expires_at, track_clicks}. If shortlink is not
        installed, returns {status: False, error: "..."}.
        """
        return _mint_share_link(self, value)

    def set_filename(self, filename):
        self.filename = filename
        if not self.content_type:
            self.content_type = mimetypes.guess_type(filename)[0]
            self.category = utils.get_file_category(self.content_type)


    def publish_renditions(self):
        """Enqueue an async job to build all default renditions for this file.

        Uses transaction.on_commit so the worker never reads pre-commit state,
        and an idempotency key so repeat publishes for the same file collapse.
        """
        from mojo.apps import jobs

        file_id = self.id
        if not file_id:
            return None

        def _publish():
            try:
                jobs.publish(
                    "mojo.apps.fileman.asyncjobs.process_file_renditions",
                    {"file_id": file_id},
                    channel="renditions",
                    idempotency_key=f"renditions:{file_id}",
                    max_exec_seconds=1800,
                )
            except Exception as e:
                logger.exception("publish_renditions: file %s failed: %s", file_id, str(e))

        # on_commit fires immediately when there is no active transaction,
        # so this works in both request-wrapped and standalone code paths.
        transaction.on_commit(_publish)

    def publish_regenerate_renditions(self, roles=None):
        """Enqueue an async job to regenerate renditions (all or specified roles).

        `roles` is sanitized: non-iterables become None (regenerate all);
        each entry is coerced to a stripped string, blanks dropped, and the
        list is capped at MAX_REGENERATE_ROLES to prevent unbounded worker
        loops from a compromised/overeager caller.
        """
        from mojo.apps import jobs

        file_id = self.id
        if not file_id:
            return None

        sanitized = None
        if roles:
            if isinstance(roles, str) or not hasattr(roles, "__iter__"):
                # Not a list — ignore silently; caller gets a full regenerate.
                sanitized = None
            else:
                sanitized = []
                for r in roles:
                    if not isinstance(r, str):
                        continue
                    r = r.strip()
                    if r:
                        sanitized.append(r)
                sanitized = sanitized[: self.MAX_REGENERATE_ROLES] or None

        payload = {"file_id": file_id}
        if sanitized:
            payload["roles"] = sanitized

        def _publish():
            try:
                jobs.publish(
                    "mojo.apps.fileman.asyncjobs.regenerate_renditions",
                    payload,
                    channel="renditions",
                    max_exec_seconds=1800,
                )
            except Exception as e:
                logger.exception("publish_regenerate_renditions: file %s failed: %s", file_id, str(e))

        transaction.on_commit(_publish)

    def mark_as_uploading(self, commit=False):
        """Mark file as currently being uploaded"""
        self.upload_status = self.UPLOADING
        if commit:
            self.atomic_save()

    def mark_as_completed(self, file_size=None, checksum=None, commit=False):
        """Mark file upload as completed"""
        if file_size:
            self.file_size = file_size
        if checksum:
            self.checksum = checksum
        if self.file_manager.backend.exists(self.storage_file_path):
            self.upload_status = self.COMPLETED
            if commit:
                self.atomic_save()
            # Rendition creation is offloaded to the async jobs engine.
            # transaction.on_commit keeps the worker from racing the commit.
            self.publish_renditions()
            return
        else:
            self.upload_status = self.FAILED
        if commit:
            self.atomic_save()

    def mark_as_failed(self, error_message=None, commit=False):
        """Mark file upload as failed"""
        self.upload_status = self.FAILED
        if error_message:
            self.set_metadata('error_message', error_message)
        if commit:
            self.atomic_save()

    def mark_as_expired(self):
        """Mark file upload as expired"""
        self.upload_status = self.EXPIRED
        self.save(update_fields=['upload_status', 'modified'])

    def get_file_extension(self):
        """Get the file extension"""
        import os
        return os.path.splitext(self.filename)[1].lower()

    def get_human_readable_size(self):
        """Get human readable file size"""
        if not self.file_size:
            return "Unknown"

        size = float(self.file_size)
        for unit in ['B', 'KB', 'MB', 'GB', 'TB', 'PB']:
            if size < 1024.0 or unit == 'PB':
                return f"{size:.1f} {unit}"
            size /= 1024.0

    def can_be_accessed_by(self, user=None, group=None):
        """Check if file can be accessed by user/group"""
        if not self.is_active:
            return False

        if self.is_public:
            return True

        if user and self.user == user:
            return True

        if group and self.group == group:
            return True

        return False

    def on_rest_save_file(self, name, file):
        self.content_type = file.content_type
        self.category = utils.get_file_category(self.content_type)
        self.set_filename(file.name)
        if not getattr(self, "file_manager", None):
            req = self.active_request
            if req:
                self.file_manager = FileManager.get_from_request(req)
            else:
                self.file_manager = FileManager.get_for_user_group(self.user, self.group)
        self.generate_storage_filename()
        self.mark_as_uploading(True)
        self.file_manager.backend.save(file, self.storage_file_path, self.content_type)
        self.mark_as_completed(commit=True)

    @classmethod
    def create_from_file(cls, file, name, request=None, user=None, group=None, file_manager=None):
        """Create a new file instance from a file"""
        if file_manager is None:
            if request:
                file_manager = FileManager.get_from_request(request)
            else:
                file_manager = FileManager.get_for_user_group(user, group)
        instance = cls()
        instance.filename = file.name
        instance.file_size = file.size
        instance.file_manager = file_manager
        instance.user = user
        instance.group = group
        instance.set_filename(file.name)
        instance.category = utils.get_file_category(instance.content_type)
        instance.on_rest_pre_save({}, True)
        instance.save()

        # now we need to upload the file
        instance.on_rest_save_file(name, file)

        return instance

    @classmethod
    def on_rest_related_save(cls, related_instance, related_field_name, field_value, current_instance=None):
        # this allows us to handle json posts with inline base64 file data
        if isinstance(field_value, str):
            mime_type = None
            b64_data = field_value

            # Check for and parse Data URL scheme (e.g., "data:image/png;base64,iVBOR...")
            if field_value.startswith('data:') and ',' in field_value:
                header, b64_data = field_value.split(',', 1)
                mime_type = header.split(';')[0].split(':')[1]

            # Fix incorrect padding, which can occur with base64 strings from web clients
            missing_padding = len(b64_data) % 4
            if missing_padding:
                b64_data += '=' * (4 - missing_padding)

            try:
                file_bytes = base64.b64decode(b64_data)
            except (TypeError, base64.binascii.Error):
                # If decoding fails, it's not a valid base64 string.
                # In a real app, you might want to raise a validation error here.
                return

            # If mime_type wasn't in the data URL, detect it with python-magic
            if not mime_type:
                mime_type = magic.from_buffer(file_bytes, mime=True)

            # Safely guess the extension, defaulting to an empty string if unknown
            ext = mimetypes.guess_extension(mime_type) or ''

            file_obj = io.BytesIO(file_bytes)
            file_obj.name = f"{related_field_name}{ext}"
            file_obj.content_type = mime_type
            file_obj.size = len(file_bytes)

            # now we need to upload the file
            instance = cls.create_from_file(file_obj, file_obj.name)
            setattr(related_instance, related_field_name, instance)

        elif isinstance(field_value, int):
            # assume file id
            instance = File.objects.get(id=field_value)
            setattr(related_instance, related_field_name, instance)
