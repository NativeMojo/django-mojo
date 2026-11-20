"""What an LLM safety policy change does, in plain words.

``llm_safety.activate_policy`` records the policy it activated beside the hash
(``LLM_SAFETY_POLICY_ACTIVATED``). This module compares that record with the
deployed policy and says what activating the deployed one would change: which
features gain or lose a model, which model or capability moves, and which
limits go up or down. The agent or person who activates it reads the ``tldr``
back to the owner, so the owner learns what changed without opening the config.

Pure functions over two policy dicts. The policy carries no secret: a route's
``credential`` is a label (``admin`` / ``handler`` / ``aws``), never a key.
"""

LIMIT_WORDS = {
    "requests_minute": "requests a minute",
    "requests_hour": "requests an hour",
    "requests_day": "requests a day",
    "tokens_minute": "tokens a minute",
    "tokens_hour": "tokens an hour",
    "tokens_day": "tokens a day",
    "concurrency": "calls at once",
    "max_input_bytes": "input size (bytes)",
    "max_output_tokens": "output size (tokens)",
    "timeout_seconds": "timeout (seconds)",
    "max_loop_calls": "calls per tool loop",
}
LIMIT_ORDER = tuple(LIMIT_WORDS)
BREAKER_WORDS = {
    "auth_failures": "sign-in failures before it trips",
    "rate_failures": "rate-limit failures before it trips",
    "server_failures": "server failures before it trips",
    "open_seconds": "seconds it stays tripped",
}
MAX_CHANGES = 60
SHARED = "all features together"


def _number(value):
    if isinstance(value, bool) or not isinstance(value, int):
        return str(value)
    return f"{value:,}"


def _dict(value):
    return value if isinstance(value, dict) else {}


def _model_words(route):
    route = _dict(route)
    words = f"{route.get('provider') or '?'} {route.get('model') or '?'}"
    if route.get("region"):
        words += f" in {route['region']}"
    extra = sorted(set(route.get("capabilities") or []) - {"text"})
    if extra:
        words += f", with {', '.join(extra)}"
    return words


def _limit_lines(label, old, new, counts):
    """One line per limit that moved, saying which way."""
    old, new = _dict(old), _dict(new)
    lines = []
    for key in LIMIT_ORDER + tuple(sorted((set(old) | set(new)) - set(LIMIT_ORDER))):
        if key not in old and key not in new:
            continue
        before, after = old.get(key), new.get(key)
        if before == after:
            continue
        word = LIMIT_WORDS.get(key, key)
        if isinstance(before, int) and isinstance(after, int) \
                and not isinstance(before, bool) and not isinstance(after, bool):
            way = "raised" if after > before else "lowered"
            counts[way] += 1
            lines.append(f"{label}: {word} {way} {_number(before)} → {_number(after)}.")
        else:
            lines.append(f"{label}: {word} {_number(before)} → {_number(after)}.")
    return lines


def _route_lines(feature, old, new):
    old, new = _dict(old), _dict(new)
    lines = []
    if (old.get("provider"), old.get("model")) != (new.get("provider"), new.get("model")):
        lines.append(
            f"{feature}: model {old.get('provider')} {old.get('model')} → "
            f"{new.get('provider')} {new.get('model')}.")
    if old.get("region") != new.get("region"):
        lines.append(f"{feature}: region {old.get('region') or 'none'} → "
                     f"{new.get('region') or 'none'}.")
    before = set(old.get("capabilities") or [])
    after = set(new.get("capabilities") or [])
    if after - before:
        lines.append(f"{feature}: can now use {', '.join(sorted(after - before))}.")
    if before - after:
        lines.append(f"{feature}: can no longer use {', '.join(sorted(before - after))}.")
    if old.get("credential") != new.get("credential"):
        lines.append(f"{feature}: credential {old.get('credential')} → "
                     f"{new.get('credential')}.")
    efforts_before = list(old.get("reasoning_efforts") or [])
    efforts_after = list(new.get("reasoning_efforts") or [])
    if efforts_before != efforts_after:
        lines.append(f"{feature}: reasoning levels {', '.join(efforts_before) or 'none'} "
                     f"→ {', '.join(efforts_after) or 'none'}.")
    return lines


def _join(names):
    names = list(names)
    if len(names) <= 3:
        return ", ".join(names)
    return f"{', '.join(names[:3])} and {len(names) - 3} more"


def _plural(count, word):
    return f"{count} {word}" + ("" if count == 1 else "s")


def describe(new, old=None):
    """Return ``{"tldr", "changes", "first"}`` for activating ``new`` over ``old``.

    ``old`` is the policy last activated, or None when nothing was recorded
    (an activation made before this record existed). Both are the raw policy
    dicts, without the computed ``hash``.
    """
    new = {key: value for key, value in _dict(new).items() if key != "hash"}
    if old is None:
        routes = _dict(new.get("routes"))
        changes = [f"{feature}: {_model_words(routes[feature])}."
                   for feature in sorted(routes)]
        tldr = (f"No earlier policy was recorded to compare with. This policy "
                f"turns on {_plural(len(routes), 'AI feature')}: "
                f"{_join(sorted(routes)) or 'none'}.")
        return {"tldr": tldr, "changes": _capped(changes), "first": True}

    old = {key: value for key, value in _dict(old).items() if key != "hash"}
    old_routes, new_routes = _dict(old.get("routes")), _dict(new.get("routes"))
    old_features = _dict(old.get("features"))
    new_features = _dict(new.get("features"))
    added = sorted(set(new_routes) - set(old_routes))
    removed = sorted(set(old_routes) - set(new_routes))
    kept = sorted(set(old_routes) & set(new_routes))
    counts = {"raised": 0, "lowered": 0}
    changes = []
    for feature in added:
        changes.append(f"Adds {feature}: {_model_words(new_routes[feature])}.")
    for feature in removed:
        changes.append(f"Removes {feature} (was {_model_words(old_routes[feature])}).")
    changed_routes = []
    for feature in kept:
        lines = _route_lines(feature, old_routes[feature], new_routes[feature])
        if lines:
            changed_routes.append(feature)
        changes.extend(lines)
    version_changed = old.get("version") != new.get("version")
    if version_changed:
        changes.append(f"Policy version {old.get('version')} → {new.get('version')}.")
    changes.extend(_limit_lines(SHARED, old.get("shared"), new.get("shared"), counts))
    for feature in sorted(set(old_features) | set(new_features)):
        if feature in added or feature in removed:
            continue
        changes.extend(_limit_lines(feature, old_features.get(feature),
                                    new_features.get(feature), counts))
    old_breaker, new_breaker = _dict(old.get("breaker")), _dict(new.get("breaker"))
    breaker_changed = False
    for key in sorted(set(old_breaker) | set(new_breaker)):
        if old_breaker.get(key) != new_breaker.get(key):
            breaker_changed = True
            changes.append(
                f"Failure breaker: {BREAKER_WORDS.get(key, key)} "
                f"{_number(old_breaker.get(key))} → {_number(new_breaker.get(key))}.")

    parts = []
    if added:
        parts.append(f"adds {_join(added)}")
    if removed:
        parts.append(f"removes {_join(removed)}")
    if changed_routes:
        parts.append(f"changes the model or settings of {_join(changed_routes)}")
    if counts["raised"]:
        parts.append(f"raises {_plural(counts['raised'], 'limit')}")
    if counts["lowered"]:
        parts.append(f"lowers {_plural(counts['lowered'], 'limit')}")
    if breaker_changed:
        parts.append("changes the failure breaker")
    if version_changed:
        parts.append("changes the policy version")
    if parts:
        tldr = f"This policy {'; '.join(parts)}."
    elif old != new:
        changes = ["Only the order of entries changed."]
        tldr = "This policy only reorders entries; nothing it allows changes."
    else:
        tldr = "Nothing in the policy changed."
    return {"tldr": tldr, "changes": _capped(changes), "first": False}


def _capped(changes):
    """At most MAX_CHANGES lines, the last one saying how many were left out."""
    if len(changes) <= MAX_CHANGES:
        return changes
    kept = changes[:MAX_CHANGES - 1]
    return kept + [f"…and {len(changes) - len(kept)} more changes."]
