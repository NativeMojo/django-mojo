"""Scheduler loop progress, separate from its leadership lease."""
import hashlib
from datetime import datetime, timezone

from mojo.apps.jobs.keys import JobKeys

PROGRESS_TTL = 30


def progress_key():
    return f"{JobKeys().prefix}:scheduler:progress"


def lease_digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else str(value).encode()).hexdigest()


def progress_sample(lease, scheduler_id, now=None):
    return {"version": 1, "lease": lease_digest(lease),
            "scheduler_id": scheduler_id,
            "observed_at": (now or datetime.now(timezone.utc)).isoformat()}
