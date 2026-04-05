"""
Nightly memory cleanup job for the admin assistant.

Two phases:
  Phase 1 (mechanical): orphan cleanup, size enforcement, suspicious patterns
  Phase 2 (dreaming): LLM-assisted consolidation, runs conditionally

Register as a scheduled job via the jobs framework:
    from mojo.apps import jobs
    jobs.publish("mojo.apps.assistant.jobs.assistant_memory_cleanup")
"""
from mojo.helpers import logit
from mojo.helpers.settings import settings

logger = logit.get_logger("assistant", "assistant.log")


def assistant_memory_cleanup(payload=None):
    """
    Nightly memory cleanup entry point.

    Phase 1: Mechanical cleanup (always runs)
    Phase 2: Dreaming (runs conditionally per tier)
    """
    if not settings.get("LLM_ADMIN_MEMORY_ENABLED", True, kind="bool"):
        logger.info("memory cleanup: memory disabled, skipping")
        return {"skipped": True, "reason": "memory_disabled"}

    from mojo.apps.assistant.services.memory import (
        cleanup_mechanical, should_dream, dream_tier,
        apply_dream_actions, mark_dreamed, _get_entries, _get_adapter,
        KEY_GLOBAL, KEY_USER, KEY_GROUP,
    )

    # Phase 1: mechanical cleanup
    logger.info("memory cleanup: starting phase 1 (mechanical)")
    mech_stats = cleanup_mechanical()

    # Phase 2: dreaming
    dream_enabled = settings.get("LLM_ADMIN_MEMORY_DREAM_ENABLED", True, kind="bool")
    if not dream_enabled:
        logger.info("memory cleanup: dreaming disabled, skipping phase 2")
        return {"mechanical": mech_stats, "dreaming": "disabled"}

    logger.info("memory cleanup: starting phase 2 (dreaming)")
    auto_apply = settings.get("LLM_ADMIN_MEMORY_DREAM_AUTO_APPLY", True, kind="bool")
    dream_stats = {"tiers_processed": 0, "tiers_skipped": 0, "total_actions": 0}

    # Dream global tier
    _dream_single_tier("global", None, KEY_GLOBAL, "Global (platform-wide)",
                       auto_apply, dream_stats)

    # Dream user and group tiers — scan Redis for existing keys
    adapter = _get_adapter()
    if adapter:
        _dream_scanned_tiers(adapter, "user", "assistant:memory:user:*",
                             KEY_USER, auto_apply, dream_stats)
        _dream_scanned_tiers(adapter, "group", "assistant:memory:group:*",
                             KEY_GROUP, auto_apply, dream_stats)

    logger.info("memory cleanup complete: mechanical=%s dreaming=%s", mech_stats, dream_stats)
    return {"mechanical": mech_stats, "dreaming": dream_stats}


def _dream_single_tier(tier, tier_suffix, redis_key, tier_label, auto_apply, stats):
    """Run dream pass on a single tier if needed."""
    from mojo.apps.assistant.services.memory import (
        should_dream, dream_tier, apply_dream_actions, mark_dreamed, _get_entries, _get_adapter,
    )

    if not should_dream(tier, tier_suffix):
        stats["tiers_skipped"] += 1
        return

    adapter = _get_adapter()
    if not adapter:
        return

    entries = _get_entries(adapter, redis_key)
    if not entries:
        mark_dreamed(tier, tier_suffix)
        stats["tiers_skipped"] += 1
        return

    logger.info("memory dream: processing %s (%d entries)", tier_label, len(entries))
    actions = dream_tier(tier, redis_key, tier_label=tier_label)

    if actions is None:
        logger.warning("memory dream: failed for %s, skipping", tier_label)
        return

    # Filter to non-keep actions
    changes = [a for a in actions if a.get("action") != "keep"]
    stats["tiers_processed"] += 1
    stats["total_actions"] += len(changes)

    if not changes:
        logger.info("memory dream: no changes needed for %s", tier_label)
        mark_dreamed(tier, tier_suffix)
        return

    if auto_apply:
        logger.info("memory dream: applying %d changes to %s", len(changes), tier_label)
        apply_stats = apply_dream_actions(redis_key, actions)
        logger.info("memory dream: applied to %s: %s", tier_label, apply_stats)
    else:
        logger.info("memory dream: log-only mode, %d changes proposed for %s:",
                     len(changes), tier_label)
        for a in changes:
            logger.info("  %s: key=%s reason=%s",
                        a.get("action"), a.get("key") or a.get("keys"), a.get("reason"))

    mark_dreamed(tier, tier_suffix)


def _dream_scanned_tiers(adapter, tier, pattern, key_template, auto_apply, stats):
    """Scan Redis for all keys matching a tier pattern and dream each."""
    try:
        client = adapter.get_client()
        cursor = 0
        while True:
            cursor, keys = client.scan(cursor, match=pattern, count=100)
            for key in keys:
                key_str = key if isinstance(key, str) else key.decode("utf-8")
                parts = key_str.split(":")
                if len(parts) >= 4:
                    tier_suffix = parts[3]
                    tier_label = f"User {tier_suffix}" if tier == "user" else f"Group {tier_suffix}"
                    _dream_single_tier(tier, tier_suffix, key_str, tier_label,
                                       auto_apply, stats)
            if cursor == 0:
                break
    except Exception:
        logger.exception("memory dream: scan failed for %s", pattern)
