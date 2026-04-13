from mojo.helpers import logit

logger = logit.get_logger("fileman", "fileman.log")


def cleanup_expired_files(job):
    """Delete files whose metadata.expires_at has passed.

    Works for any file with an expires_at in metadata — not just assistant exports.
    Deletes both the storage backend file and the database record.
    """
    from django.utils import timezone
    from mojo.apps.fileman.models import File

    now = timezone.now()
    now_iso = now.isoformat()

    # Find files that have an expires_at key in metadata.
    # We filter for files that have the key, then compare in Python
    # because JSON string comparison across DB backends is unreliable.
    candidates = File.objects.filter(
        metadata__has_key="expires_at",
        is_active=True,
    )

    deleted = 0
    for f in candidates.iterator():
        expires_at = f.metadata.get("expires_at", "") if isinstance(f.metadata, dict) else ""
        if not expires_at:
            continue
        # ISO string comparison — both are ISO 8601 format
        if expires_at < now_iso:
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
