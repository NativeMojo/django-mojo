from datetime import datetime
from mojo.helpers import logit

logger = logit.get_logger("fileman", "fileman.log")


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
