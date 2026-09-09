"""Best-effort, receipt-idempotent MojoSec category volume monitoring."""

import datetime
import hashlib

from mojo.helpers import logit
from mojo.helpers.settings import settings


logger = logit.get_logger(__name__, "incident.log")

BUCKET_TTL_SECONDS = 2 * 60 * 60
DEFAULT_THRESHOLD = 10000
ALERT_CATEGORY = "system:health:mojosec_volume"
_REDIS_WARNED = False
_OBSERVE_LUA = """
local fresh = redis.call('set', KEYS[2], '1', 'NX', 'EX', ARGV[2])
if not fresh then
  return {0, tonumber(redis.call('get', KEYS[1]) or '0')}
end
local total = redis.call('incrby', KEYS[1], ARGV[1])
if total == tonumber(ARGV[1]) then
  redis.call('expire', KEYS[1], ARGV[2])
end
return {1, total}
"""


def _hour_bucket(created):
    if created.tzinfo is None or created.utcoffset() is None:
        raise ValueError("receipt.created must be timezone-aware")
    return created.astimezone(datetime.timezone.utc).strftime("%Y%m%dT%H")


def volume_keys(receipt, category):
    """Return same-slot keys for one server category/hour and receipt."""
    bucket = _hour_bucket(receipt.created)
    category_digest = hashlib.sha256(category.encode("utf-8")).hexdigest()[:24]
    slot = f"mojosec-volume-{bucket}-{category_digest}"
    prefix = f"mojosec:volume:{{{slot}}}"
    return (
        f"{prefix}:count",
        f"{prefix}:receipt:{receipt.api_key_id}:{receipt.pk}",
        bucket,
    )


def _threshold():
    return settings.get_static(
        "MOJOSEC_CATEGORY_VOLUME_ALERT_THRESHOLD", DEFAULT_THRESHOLD,
        kind="int")


def _warn_once():
    global _REDIS_WARNED
    if _REDIS_WARNED:
        return
    _REDIS_WARNED = True
    logger.warning(
        "MojoSec category volume monitoring is unavailable; "
        "durable receipt acknowledgements continue")


def _redis_ok():
    global _REDIS_WARNED
    _REDIS_WARNED = False


def observe(receipt, category, count, *, connection=None, reporter=None,
            threshold=None):
    """Count one durable receipt once; advisory failures never escape."""
    try:
        threshold = _threshold() if threshold is None else threshold
        if (isinstance(threshold, bool) or not isinstance(threshold, int) or
                threshold <= 0):
            return {"counted": False, "total": None, "reported": False}
        if (not isinstance(category, str) or not category or len(category) > 124 or
                isinstance(count, bool) or not isinstance(count, int) or count <= 0):
            raise ValueError("MojoSec volume observation is invalid")
        if connection is None:
            from mojo.helpers.redis import get_connection
            connection = get_connection()
        counter_key, marker_key, bucket = volume_keys(receipt, category)
        counted, total = connection.eval(
            _OBSERVE_LUA, 2, counter_key, marker_key,
            str(count), str(BUCKET_TTL_SECONDS))
        counted = bool(int(counted))
        total = int(total)
        _redis_ok()
        crossed = counted and total > threshold and total - count <= threshold
        reported = False
        if crossed:
            if reporter is None:
                from mojo.apps.incident import report_event_suppressed
                reporter = report_event_suppressed
            reported = bool(reporter(
                f"MojoSec category {category} exceeded {threshold} persisted "
                f"occurrences in UTC hour {bucket}; observed {total}.",
                key=f"{category}:{bucket}",
                title="MojoSec category volume threshold exceeded",
                category=ALERT_CATEGORY,
                level=8,
                scope="system",
                window=BUCKET_TTL_SECONDS,
                budget=100,
                fail_open=False,
                connection=connection,
                group=None,
                mojosec_category=category,
                utc_hour=bucket,
                observed_count=total,
                threshold=threshold,
            ))
        return {"counted": counted, "total": total, "reported": reported}
    except Exception:
        _warn_once()
        return {"counted": False, "total": None, "reported": False}
