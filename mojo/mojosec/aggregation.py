"""Persistent aggregation policy used by the SQLite spool."""


def merge(existing, observation, window_seconds):
    if existing is None:
        return {
            "fingerprint": observation["fingerprint"],
            "observation": observation,
            "count": 1,
            "first_seen": observation["observed_at"],
            "last_seen": observation["observed_at"],
            "flush_after": window_seconds,
        }
    if observation["observed_at"] >= existing["last_seen"]:
        existing["observation"] = observation
    existing["count"] += 1
    existing["first_seen"] = min(existing["first_seen"], observation["observed_at"])
    existing["last_seen"] = max(existing["last_seen"], observation["observed_at"])
    return existing


def should_flush(aggregate, flush_count, due=False):
    return due or aggregate["count"] >= flush_count
