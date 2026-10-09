from datetime import timezone as dt_timezone
from mojo.helpers import logit
from mojo.apps.fileman.utils import parse_expires_at

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
        rndr.raise_for_failures()
    else:
        # Wipe all existing renditions and recreate defaults.
        rndr.cleanup_renditions()
        created = rndr.create_all_renditions()

    logger.info("regenerate_renditions: file %s recreated %d renditions", file_id, len(created))
    return f"completed:created={len(created)}"


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
    failed = 0
    for f in candidates.iterator():
        # One file that cannot be handled must not end the run for the rest.
        try:
            raw = f.metadata.get("expires_at", "") if isinstance(f.metadata, dict) else ""
            expires_at = parse_expires_at(raw)
            if expires_at is None:
                continue
            # A value with no timezone (or a date alone) is read as UTC
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=dt_timezone.utc)
            if expires_at < now:
                f.on_rest_pre_delete()
                f.delete()
                deleted += 1
        except Exception as e:
            failed += 1
            logger.warning("cleanup_expired_files: failed on file %s: %s", f.pk, str(e))

    if deleted > 0:
        logger.info("cleanup_expired_files: deleted %d expired files", deleted)
    if failed > 0:
        logger.warning("cleanup_expired_files: %d files failed and were kept", failed)
        return f"completed:deleted={deleted},failed={failed}"
    return f"completed:deleted={deleted}"
