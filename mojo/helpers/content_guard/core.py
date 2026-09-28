"""
Core public API for content_guard.

Provides check_username, check_text, and suggest_username functions.
"""
import re
from urllib.parse import urlsplit

from objict import objict

from .normalize import (
    username_variants,
    normalize_text,
    consonant_skeleton,
    dedup_chars,
    apply_leet,
    collapse_separators,
    decode_base,
    searchable_words,
    split_tokens,
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
    # links to these hosts (or their subdomains) score nothing and are not read
    "link_allow_domains": (),
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

# ── Links ────────────────────────────────────────────────────────────────────

# [label](address "optional title") -- only an optional quoted title may follow
_MD_LINK_RE = re.compile(
    r"""\[([^\]\n]{0,500})\]\(\s*<?([^)\s>]+)>?(?:\s+("[^"]*"|'[^']*'))?\s*\)""")
_SCHEME_RE = re.compile(r"https?://", re.IGNORECASE)
_HAS_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
# the rest of a bare address after the matched suffix: more host labels,
# a port, then a path, query or fragment
_ADDRESS_TAIL_RE = re.compile(r"[A-Za-z0-9.-]*(?::\d+)?(?:[/?#]\S*)?")
_ADDRESS_TRAIL = ".,;:!?)]}>'\"*_"
_ADDRESS_LEAD = "([{<*_`'\""


def _allow_domains(value):
    """Normalize a domain allowlist given as a list or a comma-separated string."""
    if not value:
        return ()
    if isinstance(value, str):
        value = value.split(",")
    domains = []
    for domain in value:
        domain = str(domain).strip().lower()
        if domain.startswith("*."):
            domain = domain[2:]
        domain = domain.lstrip(".")
        if domain:
            domains.append(domain)
    return tuple(domains)


def _link_host(address):
    """Host of a link address, or None when it cannot be parsed safely."""
    address = address.replace("\\", "/")
    if not _HAS_SCHEME_RE.match(address):
        address = "http://" + address
    try:
        host = urlsplit(address).hostname
    except ValueError:
        return None
    if host and host.endswith("."):
        host = host[:-1]
    if not host or not host.isascii():
        return None
    return host


def _host_allowed(host, domains):
    return bool(host) and any(host == d or host.endswith("." + d) for d in domains)


def _link(start, end, address, domains):
    host = _link_host(address)
    return objict(span=(start, end), address=address, host=host,
                  allowed=_host_allowed(host, domains))


def _allowed_address_re(domains):
    """Bare addresses on an allowed domain, whatever its suffix (e.g. ".ai")."""
    if not domains:
        return None
    names = "|".join(re.escape(d) for d in sorted(domains, key=len, reverse=True))
    return re.compile(
        r"(?<![A-Za-z0-9.-])(?:[A-Za-z0-9-]+\.)*(?:%s)(?![A-Za-z0-9-])" % names,
        re.IGNORECASE)


def _find_links(display, rules, domains):
    """
    Return every link in display as objict(span, address, host, allowed).

    Markdown links come first and only their address is a link: the label,
    title and surrounding text stay readable (and are searched for bare links).
    Bare links use rules.link_re, extended over the rest of the address (more
    host labels, a port, a path or query) so the host is the address's own.
    A bare address on an allowed domain is a link too when rules.link_re does
    not know its suffix, but only when its whole host is allowed.
    """
    links = [_link(m.start(2), m.end(2), m.group(2), domains)
             for m in _MD_LINK_RE.finditer(display)]
    rest = _blank_spans(display, [link.span for link in links])
    for m in rules.link_re.finditer(rest):
        start, end = m.span()
        end = _ADDRESS_TAIL_RE.match(rest, end).end()
        address = rest[start:end]
        # "[see](https://x" -- start at the scheme when nothing before it is a link
        scheme = _SCHEME_RE.search(address)
        if scheme and scheme.start() > 0 and not rules.link_re.search(address[:scheme.start()]):
            start += scheme.start()
            address = address[scheme.start():]
        address = address.lstrip(_ADDRESS_LEAD).rstrip(_ADDRESS_TRAIL)
        links.append(_link(start, end, address or rest[start:end], domains))
    allowed_re = _allowed_address_re(domains)
    if allowed_re:
        rest = _blank_spans(display, [link.span for link in links])
        for m in allowed_re.finditer(rest):
            start = m.start()
            end = _ADDRESS_TAIL_RE.match(rest, m.end()).end()
            link = _link(start, end, rest[start:end].rstrip(_ADDRESS_TRAIL), domains)
            if link.allowed:
                links.append(link)
    links.sort(key=lambda link: link.span)
    return links


def _blank_spans(display, spans):
    """Replace each span with the same number of spaces, keeping offsets aligned."""
    if not spans:
        return display
    chars = list(display)
    for start, end in spans:
        for i in range(start, end):
            chars[i] = " "
    return "".join(chars)


def _listed(word, table):
    """Return the listed word `word` matches (itself, or itself minus a plural s)."""
    if word in table:
        return table[word] if isinstance(table, dict) else word
    if len(word) > 1 and word.endswith("s") and word[:-1] in table:
        return table[word[:-1]] if isinstance(table, dict) else word[:-1]
    return None


def _profane_word(word, rules, use_decoded):
    """
    Return (listed_word, variant) when `word` is profane, else (None, None).

    A word is profane when it equals a deny term or a form, or either plus "s".
    Terms are never matched inside a word that is not itself listed.
    """
    hit = _listed(word, rules.listed)
    if hit:
        return hit, "searchable"
    if not use_decoded or not any(ch.isalpha() for ch in word):
        return None, None
    base = decode_base(word)
    if dedup_chars(base, max_run=1) == word:
        return None, None
    for key in (dedup_chars(base, max_run=1), dedup_chars(base, max_run=2)):
        hit = _listed(key, rules.listed_decoded)
        if hit:
            return hit, "decoded"
    return None, None


def _term_reason(term, rules):
    """Reason code for a counted term: slurs are always hidden."""
    if term in rules.slurs:
        return "high_severity"
    if term in rules.high_severity:
        return "strong_profanity"
    return "deny_hit"


def check_text(text, rules=None, surface="comment", policy=None):
    """
    Check block text (comments, profile descriptions) for moderation issues.

    surface: "comment", "profile_text", etc. (for future per-surface tuning)

    Deny terms match whole words only: a word scores when it is a listed term
    or form (rules.forms), and then scores every term it contains. Link text
    is blanked before any word, phone, repetition or caps check; a link whose
    host is in policy["link_allow_domains"] (or a subdomain) scores nothing,
    and any other link scores link_weight plus the listed words in its address.

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
    debug_norm = {"display": display, "searchable": searchable, "decoded": decoded} if p["include_debug_normalized"] else None

    use_decoded = p["enable_text_decoded_match"]

    # ── links: found first, then blanked so nothing reads inside them ────
    links = _find_links(display, rules, _allow_domains(p["link_allow_domains"]))
    masked = _blank_spans(display, [link.span for link in links])
    counted_links = [link for link in links if not link.allowed]

    # ── deny term hits (whole words) ─────────────────────────────────────
    words = searchable_words(masked)
    candidates = words + split_tokens(masked)
    # a link that is not allowed is read too: whole words of its address only
    for link in counted_links:
        candidates += [(word, link.span) for word, _span in split_tokens(link.address)]
    link_spans = set(link.span for link in counted_links)
    counted = set()
    for word, span in candidates:
        if span in link_spans:
            listed_word, variant = _listed(word, rules.listed), "link"
        else:
            listed_word, variant = _profane_word(word, rules, use_decoded)
        if not listed_word:
            continue
        terms = rules.listed_terms.get(listed_word, ())
        # link words are matched as written: no decoded terms there
        if use_decoded and variant != "link":
            terms = list(terms) + rules.listed_terms_decoded.get(listed_word, [])
        for term in terms:
            if term in counted:
                continue
            counted.add(term)
            is_high = term in rules.high_severity or term in rules.slurs
            score += p["high_sev_weight"] if is_high else p["deny_weight"]
            reasons.append(_term_reason(term, rules))
            matches.append(_match(
                type="deny_high_sev" if is_high else "deny_word",
                value=term,
                span=span,
                variant=variant,
            ))

    # repeated profanity bonus
    if len(counted) > 1:
        score += p["repeat_deny_weight"] * (len(counted) - 1)
        reasons.append("repeated_profanity")

    # ── spam: links ──────────────────────────────────────────────────────
    if counted_links:
        score += p["link_weight"] * len(counted_links)
        reasons.append("spam_link")
        for link in counted_links:
            matches.append(_match(
                type="spam_link",
                value=link.address,
                span=link.span,
            ))

    # ── spam: phone numbers ──────────────────────────────────────────────
    phone_hits = list(rules.phone_re.finditer(masked))
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
    rep_hits = list(rules.repeated_char_re.finditer(masked))
    if rep_hits:
        score += p["repetition_weight"]
        reasons.append("excessive_repetition")
        for rh in rep_hits:
            matches.append(_match(
                type="repetition",
                value=rh.group(),
                span=(rh.start(), rh.end()),
            ))

    # repeated words (same word 4+ times), ignoring stopwords
    word_counts = {}
    for w, _span in words:
        if w in rules.stopwords:
            continue
        word_counts[w] = word_counts.get(w, 0) + 1
    for w, count in word_counts.items():
        if count >= 4:
            score += p["repetition_weight"]
            reasons.append("repeated_words")
            matches.append(_match(type="repeated_words", value=w))
            break

    # ── spam: excessive caps ─────────────────────────────────────────────
    alpha_chars = [ch for ch in masked if ch.isalpha()]
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

# policy keys that are lists (a list, or a comma-separated string)
_LIST_KEYS = {"link_allow_domains"}

_BOOL_TRUE = {"true", "1", "yes"}


def _coerce_policy_value(key, value):
    """Coerce a request data value to the expected type for a policy key."""
    if key in _BOOL_KEYS:
        if isinstance(value, bool):
            return value
        return str(value).lower().strip() in _BOOL_TRUE
    if key in _INT_KEYS:
        return int(value)
    if key in _LIST_KEYS:
        if isinstance(value, (list, tuple)):
            return [str(v) for v in value]
        return [v.strip() for v in str(value).split(",") if v.strip()]
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
        return {"error": "Provide 'username' and/or 'text' to check", "code": 400, "status": False}

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
