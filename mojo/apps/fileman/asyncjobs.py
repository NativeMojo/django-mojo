from datetime import datetime
from mojo.helpers import logit

logger = logit.get_logger("fileman", "fileman.log")


def process_file_renditions(job):
    """Create all default renditions for a completed File.

    Payload:
        file_id: int — the File primary key

    Idempotent: the renderer short-circuits roles that already exist.
    """
    from mojo.apps.fileman.models import File
    from mojo.apps.fileman import renderer

    file_id = job.payload.get("file_id") if isinstance(job.payload, dict) else None
    if not file_id:
        logger.warning("process_file_renditions: missing file_id in payload")
        return "completed:skipped=no-file-id"

    try:
        f = File.objects.get(pk=file_id)
    except File.DoesNotExist:
        logger.info("process_file_renditions: file %s no longer exists", file_id)
        return "completed:skipped=file-missing"

    if not f.is_completed:
        logger.info("process_file_renditions: file %s not completed (status=%s)",
                    file_id, f.upload_status)
        return "completed:skipped=not-completed"

    created = renderer.create_all_renditions(f)
    logger.info("process_file_renditions: file %s created %d renditions", file_id, len(created))
    return f"completed:created={len(created)}"


def regenerate_renditions(job):
    """Regenerate specific or all renditions for a File.

    Payload:
        file_id: int — the File primary key
        roles: list[str] | None — specific roles to regenerate (None = all defaults)
    """
    from mojo.apps.fileman.models import File, FileRendition
    from mojo.apps.fileman import renderer

    payload = job.payload if isinstance(job.payload, dict) else {}
    file_id = payload.get("file_id")
    roles = payload.get("roles")

    if not file_id:
        logger.warning("regenerate_renditions: missing file_id in payload")
        return "completed:skipped=no-file-id"

    try:
        f = File.objects.get(pk=file_id)
    except File.DoesNotExist:
        logger.info("regenerate_renditions: file %s no longer exists", file_id)
        return "completed:skipped=file-missing"

    rndr = renderer.get_renderer_for_file(f)
    if rndr is None:
        logger.warning("regenerate_renditions: no renderer for file %s (category=%s)",
                       file_id, f.category)
        return "completed:skipped=no-renderer"

    created = []
    if roles:
        # Delete only the requested roles, then recreate each.
        FileRendition.objects.filter(original_file=f, role__in=roles).delete()
        for role in roles:
            try:
                r = rndr.create_rendition(role)
                if r:
                    created.append(r)
            except Exception as e:
                logger.exception("regenerate_renditions: role=%s failed: %s", role, str(e))
    else:
        # Wipe all existing renditions and recreate defaults.
        rndr.cleanup_renditions()
        created = rndr.create_all_renditions()

    logger.info("regenerate_renditions: file %s recreated %d renditions", file_id, len(created))
    return f"completed:created={len(created)}"


def _parse_expires_at(value):
    """Parse an ISO 8601 expires_at value into a datetime. Returns None on failure."""
    if not value or not isinstance(value, str):
        return None
    try:
        # Handle both +00:00 and Z suffixes
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def cleanup_expired_files(job):
    """Delete files whose metadata.expires_at has passed.

    Works for any file with an expires_at in metadata — not just assistant exports.
    Deletes both the storage backend file and the database record.
    """
    from django.utils import timezone
    from mojo.apps.fileman.models import File

    now = timezone.now()

    # Find files that have an expires_at key in metadata.
    # We filter for the key in the DB, then parse and compare in Python
    # to handle format variations (Z vs +00:00, etc.) safely.
    candidates = File.objects.filter(
        metadata__has_key="expires_at",
        is_active=True,
    )

    deleted = 0
    for f in candidates.iterator():
        raw = f.metadata.get("expires_at", "") if isinstance(f.metadata, dict) else ""
        expires_at = _parse_expires_at(raw)
        if expires_at is None:
            continue
        # Make naive datetimes UTC-aware for comparison
        if expires_at.tzinfo is None:
            from django.utils.timezone import utc
            expires_at = expires_at.replace(tzinfo=utc)
        if expires_at < now:
            source = f.metadata.get("source", "unknown") if isinstance(f.metadata, dict) else "unknown"
            try:
                f.on_rest_pre_delete()
                f.delete()
                deleted += 1
            except Exception as e:
                logger.warning("cleanup_expired_files: failed to delete file %s (source=%s): %s",
                               f.pk, source, str(e))

    if deleted > 0:
        logger.info("cleanup_expired_files: deleted %d expired files", deleted)
    return f"completed:deleted={deleted}"
