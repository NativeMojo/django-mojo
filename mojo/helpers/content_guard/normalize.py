"""
Normalization utilities for content_guard.

Provides deterministic text normalization and variant generation
for username and text moderation.
"""
import re
import unicodedata


# ── Character maps ───────────────────────────────────────────────────────────

# leet speak mapping (single char)
LEET_MAP = {
    "0": "o",
    "1": "i",
    "3": "e",
    "4": "a",
    "5": "s",
    "6": "g",
    "7": "t",
    "8": "b",
    "9": "g",
    "@": "a",
    "$": "s",
    "!": "i",
    "|": "i",
    "+": "t",
}

# multi-char phonetic substitutions (applied after leet)
PHONETIC_MAP = {
    "ph": "f",
    "kn": "n",
    "wr": "r",
}

# homoglyph map: visually similar Unicode chars → ASCII Latin
HOMOGLYPH_MAP = {
    # Cyrillic → Latin
    "\u0430": "a",  # а
    "\u0435": "e",  # е
    "\u043e": "o",  # о
    "\u0440": "p",  # р
    "\u0441": "c",  # с
    "\u0443": "y",  # у
    "\u0445": "x",  # х
    "\u043a": "k",  # к
    "\u0456": "i",  # і (Ukrainian)
    "\u0458": "j",  # ј (Serbian)
    "\u0442": "t",  # т
    "\u043d": "h",  # н
    # Greek → Latin
    "\u03b1": "a",  # alpha
    "\u03b5": "e",  # epsilon
    "\u03bf": "o",  # omicron
    "\u03c1": "p",  # rho
    "\u03ba": "k",  # kappa
    "\u03b9": "i",  # iota
    "\u03c4": "t",  # tau
    "\u03bd": "n",  # nu
    # common symbol subs
    "\u00f8": "o",  # ø
    "\u0142": "l",  # ł
}

# extended separator characters
SEPARATOR_CHARS = "_.-~\u2013\u2014"

# vowels for consonant skeleton
_VOWELS = set("aeiou")

# ── Compiled regexes ─────────────────────────────────────────────────────────

# zero-width and invisible unicode characters
_ZERO_WIDTH_RE = re.compile(
    r"[\u200b\u200c\u200d\u200e\u200f\ufeff\u00ad\u034f"
    r"\u2060\u2061\u2062\u2063\u2064\u2066\u2067\u2068\u2069\u206a-\u206f]"
)

# collapse whitespace
_MULTI_SPACE_RE = re.compile(r"\s+")

# non-alphanumeric (for searchable form)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9\s]")


# ── Pure functions ───────────────────────────────────────────────────────────

def strip_zero_width(text):
    """Remove zero-width and invisible unicode characters."""
    return _ZERO_WIDTH_RE.sub("", text)


def normalize_whitespace(text):
    """Collapse all whitespace runs to a single space, strip edges."""
    return _MULTI_SPACE_RE.sub(" ", text).strip()


def strip_accents(text):
    """Remove combining diacritical marks (accents) from text."""
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in nfkd if unicodedata.category(ch) != "Mn")


def apply_homoglyphs(text):
    """Replace visually similar Unicode characters with ASCII Latin equivalents."""
    return "".join(HOMOGLYPH_MAP.get(ch, ch) for ch in text)


def apply_leet(text):
    """Replace common leet-speak characters with their letter equivalents."""
    return "".join(LEET_MAP.get(ch, ch) for ch in text)


def apply_phonetic(text):
    """Apply multi-character phonetic simplifications (ph->f, ck->k, etc)."""
    result = []
    i = 0
    while i < len(text):
        if i + 1 < len(text):
            pair = text[i:i + 2]
            if pair in PHONETIC_MAP:
                result.append(PHONETIC_MAP[pair])
                i += 2
                continue
        result.append(text[i])
        i += 1
    return "".join(result)


def collapse_separators(text, separators="_"):
    """Remove separator characters from text."""
    for sep in separators:
        text = text.replace(sep, "")
    return text


def dedup_chars(text, max_run=2):
    """Collapse repeated character runs to at most max_run length."""
    if not text:
        return text
    result = [text[0]]
    count = 1
    for ch in text[1:]:
        if ch == result[-1]:
            count += 1
            if count <= max_run:
                result.append(ch)
        else:
            count = 1
            result.append(ch)
    return "".join(result)


def consonant_skeleton(text):
    """Remove vowels to produce a consonant skeleton for fuzzy matching."""
    return "".join(ch for ch in text if ch not in _VOWELS)


# ── Variant generators ───────────────────────────────────────────────────────

def username_variants(username, allow_dot=False):
    """
    Generate normalized variants of a username for matching.

    Returns a dict of variant_name -> variant_string:
        raw: lowercased original
        collapsed: separators removed
        leet: leet-speak decoded
        dedup: repeated chars collapsed (max 2)
        squeezed: repeated chars collapsed (max 1)
        combined: collapsed + leet + squeezed
        homoglyph: homoglyph-normalized
        phonetic: phonetic applied to combined
        skeleton: consonant skeleton of combined (vowels removed)
    """
    # normalize homoglyphs and accents first so deny matching works on clean text
    raw = username.lower().strip()
    raw = strip_accents(apply_homoglyphs(raw))

    seps = SEPARATOR_CHARS if allow_dot else SEPARATOR_CHARS.replace(".", "")

    collapsed = collapse_separators(raw, seps)
    leet = apply_leet(raw)
    dedup = dedup_chars(raw)
    squeezed = dedup_chars(raw, max_run=1)
    combined = dedup_chars(apply_leet(collapse_separators(raw, seps)), max_run=1)
    phonetic = apply_phonetic(combined)
    skeleton = consonant_skeleton(combined)

    return {
        "raw": raw,
        "collapsed": collapsed,
        "leet": leet,
        "dedup": dedup,
        "squeezed": squeezed,
        "combined": combined,
        "phonetic": phonetic,
        "skeleton": skeleton,
    }


def normalize_text(text):
    """
    Normalize block text for moderation matching.

    Returns (display_text, searchable_text, decoded_text):
        display_text: cleaned but readable (for span mapping)
        searchable_text: lowercase, no punctuation (for matching)
        decoded_text: leet/phonetic decoded (catches evasions in text)
    """
    # strip invisible chars, normalize whitespace
    display = strip_zero_width(text)
    display = normalize_whitespace(display)

    # searchable: lowercase, strip accents, homoglyphs, remove punctuation
    searchable = display.lower()
    searchable = strip_accents(searchable)
    searchable = apply_homoglyphs(searchable)
    searchable = _NON_ALNUM_RE.sub("", searchable)
    searchable = normalize_whitespace(searchable)

    # decoded: leet + phonetic + dedup for catching evasions
    decoded = apply_leet(searchable)
    decoded = apply_phonetic(decoded)
    decoded = dedup_chars(decoded, max_run=1)

    return display, searchable, decoded
