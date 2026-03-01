"""
Core public API for content_guard.

Provides check_username, check_text, and suggest_username functions.
"""
import re

from objict import objict

from .normalize import (
    username_variants,
    normalize_text,
    consonant_skeleton,
    dedup_chars,
    apply_leet,
    collapse_separators,
)
from .rules import load_rules as _load_rules

# default rules loaded once at import time
_DEFAULT_RULES = _load_rules()


def _match(type="", value="", span=None, variant=None):
    return objict(type=type, value=value, span=span, variant=variant)


def _result(decision="allow", reasons=None, matches=None, score=0, normalized=None):
    return objict(
        decision=decision,
        reasons=reasons or [],
        matches=matches or [],
        score=score,
        normalized=normalized,
    )


# ── Default policy ───────────────────────────────────────────────────────────

DEFAULT_POLICY = {
    # username format
    "username_min_len": 3,
    "username_max_len": 20,
    "allow_dot_in_username": False,
    "forbid_leading_sep": True,
    "forbid_trailing_sep": True,
    "forbid_double_sep": True,
    "forbid_all_digits": True,
    # deny matching
    "deny_substring_min_len": 3,
    "enable_ed1_high_sev": True,
    "ed1_max_len": 6,
    # advanced matching
    "enable_skeleton_match": True,
    "enable_reversed_match": True,
    "enable_text_decoded_match": True,
    # text thresholds
    "text_warn_threshold": 35,
    "text_block_threshold": 70,
    # spam weights (added to score)
    "link_weight": 25,
    "phone_weight": 20,
    "repetition_weight": 15,
    "caps_weight": 10,
    # deny hit weights
    "deny_weight": 30,
    "high_sev_weight": 50,
    "repeat_deny_weight": 15,
    # debug
    "include_debug_normalized": False,
}


def _merge_policy(policy):
    """Merge user policy over defaults."""
    merged = dict(DEFAULT_POLICY)
    if policy:
        merged.update(policy)
    return merged


# ── Username format regex builder ────────────────────────────────────────────

def _build_username_re(policy):
    """Build the format validation regex for usernames."""
    min_len = policy["username_min_len"]
    max_len = policy["username_max_len"]
    allowed = "a-z0-9_"
    if policy["allow_dot_in_username"]:
        allowed += "."
    return re.compile(r"^[" + allowed + r"]{" + str(min_len) + r"," + str(max_len) + r"}$")


# ── Edit distance (simple Levenshtein for short strings) ─────────────────────

def _edit_distance(a, b):
    """Compute Levenshtein edit distance between two strings."""
    if len(a) > len(b):
        a, b = b, a
    prev = list(range(len(a) + 1))
    for j in range(1, len(b) + 1):
        curr = [j] + [0] * len(a)
        for i in range(1, len(a) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            curr[i] = min(curr[i - 1] + 1, prev[i] + 1, prev[i - 1] + cost)
        prev = curr
    return prev[len(a)]


# ── Username checking ────────────────────────────────────────────────────────

def check_username(username, rules=None, policy=None):
    """
    Check a username for validity and policy violations.

    Returns a Result with decision "allow" or "block".
    Score is 0 (allow) or 100 (block) for usernames.
    """
    rules = rules or _DEFAULT_RULES
    p = _merge_policy(policy)
    reasons = []
    matches = []

    raw = username.lower().strip()

    # format validation
    fmt_re = _build_username_re(p)
    if not fmt_re.match(raw):
        if len(raw) < p["username_min_len"]:
            reasons.append("too_short")
        elif len(raw) > p["username_max_len"]:
            reasons.append("too_long")
        else:
            reasons.append("invalid_chars")
        return _result(decision="block", reasons=reasons, matches=matches, score=100)

    # structural checks
    seps = "_." if p["allow_dot_in_username"] else "_"
    if p["forbid_leading_sep"] and raw[0] in seps:
        reasons.append("leading_separator")
    if p["forbid_trailing_sep"] and raw[-1] in seps:
        reasons.append("trailing_separator")
    if p["forbid_double_sep"]:
        for sep in seps:
            if sep * 2 in raw:
                reasons.append("double_separator")
                break
    if p["forbid_all_digits"] and raw.replace("_", "").replace(".", "").isdigit():
        reasons.append("all_digits")

    if reasons:
        return _result(decision="block", reasons=reasons, matches=matches, score=100)

    # reserved check
    if raw in rules.reserved:
        reasons.append("reserved")
        matches.append(_match(type="reserved", value=raw, variant="raw"))
        return _result(decision="block", reasons=reasons, matches=matches, score=100)

    # generate variants for deny matching
    variants = username_variants(raw, allow_dot=p["allow_dot_in_username"])
    debug_norm = variants if p["include_debug_normalized"] else None

    # deny matching across variants (skip skeleton — handled separately)
    for variant_name, variant_val in variants.items():
        if variant_name == "skeleton":
            continue

        # check if the whole variant is safelisted
        if variant_val in rules.safe:
            continue

        # exact deny match
        if variant_val in rules.deny:
            if raw in rules.safe:
                continue
            reasons.append("deny_exact")
            matches.append(_match(type="deny_exact", value=variant_val, variant=variant_name))

        # substring deny match (only for terms >= min len)
        for term in rules.deny:
            if len(term) < p["deny_substring_min_len"]:
                continue
            if term in variant_val and variant_val != term:
                if raw in rules.safe:
                    continue
                reasons.append("deny_substring")
                matches.append(_match(
                    type="deny_substring",
                    value=term,
                    variant=variant_name,
                ))

    # consonant skeleton matching
    if p["enable_skeleton_match"] and raw not in rules.safe:
        skeleton_val = variants.get("skeleton", "")
        if skeleton_val:
            for deny_skel, deny_term in rules.deny_skeletons.items():
                if len(deny_skel) < p["deny_substring_min_len"]:
                    continue
                if deny_skel in skeleton_val:
                    if not any(m.value == deny_term and m.variant == "skeleton" for m in matches):
                        reasons.append("deny_skeleton")
                        matches.append(_match(
                            type="deny_skeleton",
                            value=deny_term,
                            variant="skeleton",
                        ))

    # reversed matching (high severity only, use collapsed to preserve char runs)
    if p["enable_reversed_match"] and raw not in rules.safe:
        reversed_combined = variants.get("collapsed", "")[::-1]
        if reversed_combined:
            for term in rules.high_severity:
                if len(term) < p["deny_substring_min_len"]:
                    continue
                if term in reversed_combined or reversed_combined == term:
                    if not any(m.value == term and m.variant == "reversed" for m in matches):
                        reasons.append("deny_reversed")
                        matches.append(_match(
                            type="deny_reversed",
                            value=term,
                            variant="reversed",
                        ))

    # edit distance check for high severity terms
    if p["enable_ed1_high_sev"]:
        for term in rules.high_severity:
            if len(term) > p["ed1_max_len"]:
                continue
            for variant_name, variant_val in variants.items():
                if variant_name == "skeleton":
                    continue
                if variant_val in rules.safe or raw in rules.safe:
                    continue
                if _edit_distance(variant_val, term) <= 1 and variant_val != term:
                    if not any(m.value == term and m.variant == variant_name for m in matches):
                        reasons.append("deny_ed1")
                        matches.append(_match(
                            type="deny_ed1",
                            value=term,
                            variant=variant_name,
                        ))

    if reasons:
        reasons = list(dict.fromkeys(reasons))
        return _result(
            decision="block",
            reasons=reasons,
            matches=matches,
            score=100,
            normalized=debug_norm,
        )

    return _result(decision="allow", reasons=[], matches=[], score=0, normalized=debug_norm)


# ── Text checking ────────────────────────────────────────────────────────────

def check_text(text, rules=None, surface="comment", policy=None):
    """
    Check block text (comments, profile descriptions) for moderation issues.

    surface: "comment", "profile_text", etc. (for future per-surface tuning)

    Returns a Result with decision "allow", "warn", or "block",
    a score 0..100, and detailed matches.
    """
    rules = rules or _DEFAULT_RULES
    p = _merge_policy(policy)
    reasons = []
    matches = []
    score = 0

    if not text or not text.strip():
        return _result(decision="allow", reasons=[], matches=[], score=0)

    display, searchable, decoded = normalize_text(text)
    lower_text = display.lower()
    debug_norm = {"display": display, "searchable": searchable, "decoded": decoded} if p["include_debug_normalized"] else None

    use_decoded = p["enable_text_decoded_match"]

    # ── deny term hits ───────────────────────────────────────────────────
    deny_hit_count = 0
    for term in rules.deny:
        # check in both searchable and decoded forms
        found_in = None
        if term in searchable:
            found_in = "searchable"
        elif use_decoded and term in decoded:
            found_in = "decoded"

        if found_in is None:
            continue

        # check safelist against the form that matched
        search_form = searchable if found_in == "searchable" else decoded
        safelisted = False
        for safe_word in rules.safe:
            if term in safe_word and safe_word in search_form:
                safelisted = True
                break
        if safelisted:
            continue

        is_high = term in rules.high_severity
        weight = p["high_sev_weight"] if is_high else p["deny_weight"]

        # find span in lower_text
        idx = lower_text.find(term)
        span = (idx, idx + len(term)) if idx >= 0 else None

        score += weight
        deny_hit_count += 1
        reasons.append("high_severity" if is_high else "deny_hit")
        matches.append(_match(
            type="deny_high_sev" if is_high else "deny_substring",
            value=term,
            span=span,
            variant=found_in,
        ))

    # repeated profanity bonus
    if deny_hit_count > 1:
        score += p["repeat_deny_weight"] * (deny_hit_count - 1)
        reasons.append("repeated_profanity")

    # ── spam: links ──────────────────────────────────────────────────────
    link_matches = rules.link_re.findall(display)
    if link_matches:
        score += p["link_weight"] * len(link_matches)
        reasons.append("spam_link")
        for lm in link_matches:
            idx = display.find(lm)
            matches.append(_match(
                type="spam_link",
                value=lm,
                span=(idx, idx + len(lm)) if idx >= 0 else None,
            ))

    # ── spam: phone numbers ──────────────────────────────────────────────
    phone_hits = list(rules.phone_re.finditer(display))
    if phone_hits:
        score += p["phone_weight"] * len(phone_hits)
        reasons.append("spam_phone")
        for ph in phone_hits:
            matches.append(_match(
                type="spam_phone",
                value=ph.group(),
                span=(ph.start(), ph.end()),
            ))

    # ── spam: excessive repetition ───────────────────────────────────────
    rep_hits = list(rules.repeated_char_re.finditer(display))
    if rep_hits:
        score += p["repetition_weight"]
        reasons.append("excessive_repetition")
        for rh in rep_hits:
            matches.append(_match(
                type="repetition",
                value=rh.group(),
                span=(rh.start(), rh.end()),
            ))

    # repeated words (same word 4+ times)
    words = searchable.split()
    if words:
        word_counts = {}
        for w in words:
            word_counts[w] = word_counts.get(w, 0) + 1
        for w, count in word_counts.items():
            if count >= 4:
                score += p["repetition_weight"]
                reasons.append("repeated_words")
                matches.append(_match(type="repeated_words", value=w))
                break

    # ── spam: excessive caps ─────────────────────────────────────────────
    alpha_chars = [ch for ch in display if ch.isalpha()]
    if len(alpha_chars) > 10:
        caps_ratio = sum(1 for ch in alpha_chars if ch.isupper()) / len(alpha_chars)
        if caps_ratio > 0.7:
            score += p["caps_weight"]
            reasons.append("excessive_caps")

    # cap score at 100
    score = min(score, 100)

    # deduplicate reasons
    reasons = list(dict.fromkeys(reasons))

    # decision based on thresholds
    warn_t = p["text_warn_threshold"]
    block_t = p["text_block_threshold"]
    if score >= block_t:
        decision = "block"
    elif score >= warn_t:
        decision = "warn"
    else:
        decision = "allow"

    return _result(
        decision=decision,
        reasons=reasons,
        matches=matches,
        score=score,
        normalized=debug_norm,
    )


# ── Username suggestion ──────────────────────────────────────────────────────

def suggest_username(username, rules=None, policy=None):
    """
    Suggest a cleaned version of a username, or None if unsalvageable.

    Strips invalid characters and checks the cleaned version.
    """
    rules = rules or _DEFAULT_RULES
    p = _merge_policy(policy)
    allowed_chars = "abcdefghijklmnopqrstuvwxyz0123456789_"
    if p["allow_dot_in_username"]:
        allowed_chars += "."

    # clean: lowercase, keep only allowed chars
    cleaned = "".join(ch for ch in username.lower() if ch in allowed_chars)

    # strip leading/trailing separators
    seps = "_." if p["allow_dot_in_username"] else "_"
    cleaned = cleaned.strip(seps)

    # collapse double separators
    for sep in seps:
        while sep * 2 in cleaned:
            cleaned = cleaned.replace(sep * 2, sep)

    if len(cleaned) < p["username_min_len"]:
        return None

    if len(cleaned) > p["username_max_len"]:
        cleaned = cleaned[:p["username_max_len"]].rstrip(seps)

    # check if the cleaned version passes
    result = check_username(cleaned, rules, policy=policy)
    if result.decision == "allow":
        return cleaned

    return None


# ── Policy type coercion ────────────────────────────────────────────────────

# policy keys that are integers
_INT_KEYS = {
    "username_min_len", "username_max_len", "deny_substring_min_len",
    "ed1_max_len", "text_warn_threshold", "text_block_threshold",
    "link_weight", "phone_weight", "repetition_weight", "caps_weight",
    "deny_weight", "high_sev_weight", "repeat_deny_weight",
}

# policy keys that are booleans
_BOOL_KEYS = {
    "allow_dot_in_username", "forbid_leading_sep", "forbid_trailing_sep",
    "forbid_double_sep", "forbid_all_digits", "enable_ed1_high_sev",
    "enable_skeleton_match", "enable_reversed_match",
    "enable_text_decoded_match", "include_debug_normalized",
}

_BOOL_TRUE = {"true", "1", "yes"}


def _coerce_policy_value(key, value):
    """Coerce a request data value to the expected type for a policy key."""
    if key in _BOOL_KEYS:
        if isinstance(value, bool):
            return value
        return str(value).lower().strip() in _BOOL_TRUE
    if key in _INT_KEYS:
        return int(value)
    return value


def _serialize_result(result):
    """Convert a result objict to a plain dict for JSON response."""
    out = {
        "decision": result.decision,
        "reasons": result.reasons,
        "score": result.score,
        "matches": [],
    }
    for m in result.matches:
        out["matches"].append({
            "type": m.type,
            "value": m.value,
            "span": m.span,
            "variant": m.variant,
        })
    if result.normalized is not None:
        out["normalized"] = result.normalized
    return out


# ── REST request handler ────────────────────────────────────────────────────

def _data_get(data, key, default=None):
    """Get a value from request data (works with both dict and objict)."""
    if key in data:
        return data[key]
    return default


def on_rest_request(request):
    """
    Handle a Django REST request for content checking.

    Reads request.DATA for:
        username — check as username
        text — check as text/comment
        surface — surface name for text checks (default "comment")
        Any DEFAULT_POLICY key — override that policy setting

    Returns a dict suitable for JSON response.
    """
    data = request.DATA
    username = _data_get(data, "username")
    text = _data_get(data, "text")

    if not username and not text:
        return {"error": "Provide 'username' and/or 'text' to check", "code": 400}

    # build policy from recognized keys in request data
    policy = {}
    for key in DEFAULT_POLICY:
        val = _data_get(data, key)
        if val is not None:
            try:
                policy[key] = _coerce_policy_value(key, val)
            except (ValueError, TypeError):
                pass

    response = {}

    if username:
        result = check_username(username, policy=policy or None)
        response["username"] = _serialize_result(result)

    if text:
        surface = _data_get(data, "surface", "comment")
        result = check_text(text, surface=surface, policy=policy or None)
        response["text"] = _serialize_result(result)

    return response
