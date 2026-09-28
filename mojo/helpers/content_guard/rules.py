"""
Rule loading and compiled pattern storage for content_guard.

Loads word lists from plain text files and compiles regex patterns
for efficient matching.
"""
import os
import re

from .normalize import consonant_skeleton, decode_base, decode_key, dedup_chars


# path to bundled data files
_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")


def _load_wordlist(filepath):
    """Load a word list from a text file. Skips comments (#) and blank lines."""
    words = set()
    if not os.path.isfile(filepath):
        return words
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                words.add(line.lower())
    return words


class Rules:
    """Holds loaded word lists, precompiled patterns, and deny skeletons."""

    def __init__(self, deny=None, high_severity=None, safe=None, reserved=None,
                 forms=None, slurs=None, stopwords=None):
        self.deny = deny or set()
        self.high_severity = high_severity or set()
        self.safe = safe or set()
        self.reserved = reserved or set()
        self.forms = forms or set()
        self.slurs = slurs or set()
        self.stopwords = stopwords or set()

        # whole-word text matching: a listed word (term or form) is profane and
        # scores every deny term it contains, as written or decoded -- the
        # same terms the old substring matcher found in it
        self.listed = self.deny | self.forms
        self.listed_terms = {}
        for word in self.listed:
            decoded = dedup_chars(decode_base(word), max_run=1)
            self.listed_terms[word] = sorted(
                term for term in self.deny if term in word or term in decoded)
        # decoded (leet/phonetic/dedup) key -> listed word
        self.listed_decoded = {}
        for word in sorted(self.listed):
            self.listed_decoded.setdefault(decode_key(word), word)

        # pre-compute consonant skeletons for deny terms (skeleton -> original term)
        self.deny_skeletons = {}
        for term in self.deny:
            skel = consonant_skeleton(term)
            if len(skel) >= 3:
                self.deny_skeletons[skel] = term

        # compile spam detection patterns
        self.link_re = re.compile(
            r"https?://\S+|www\.\S+|\S+\.(?:com|net|org|io|co|info|biz|xyz)\b",
            re.IGNORECASE,
        )
        # alphanumeric boundaries: a digit run inside a hash or word is not a phone
        self.phone_re = re.compile(
            r"(?<![0-9A-Za-z])"
            r"(?:\+?\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}"
            r"(?![0-9A-Za-z])"
        )
        # \S: blanked link spans (runs of spaces) are not repetition
        self.repeated_char_re = re.compile(r"(\S)\1{4,}")


def load_rules(
    deny_path=None,
    high_severity_path=None,
    safe_path=None,
    reserved_path=None,
    extra_deny=None,
    extra_safe=None,
    extra_reserved=None,
    forms_path=None,
    slurs_path=None,
    stopwords_path=None,
    extra_forms=None,
    extra_slurs=None,
    extra_stopwords=None,
):
    """
    Load moderation rules from word list files.

    Uses bundled default files if no paths are provided.
    Extra sets are merged into the loaded lists.

    Returns a Rules instance with all lists loaded and patterns compiled.
    """
    deny = _load_wordlist(deny_path or os.path.join(_DATA_DIR, "deny.txt"))
    high_sev = _load_wordlist(high_severity_path or os.path.join(_DATA_DIR, "high_severity.txt"))
    safe = _load_wordlist(safe_path or os.path.join(_DATA_DIR, "safe.txt"))
    reserved = _load_wordlist(reserved_path or os.path.join(_DATA_DIR, "reserved.txt"))
    forms = _load_wordlist(forms_path or os.path.join(_DATA_DIR, "forms.txt"))
    slurs = _load_wordlist(slurs_path or os.path.join(_DATA_DIR, "slurs.txt"))
    stopwords = _load_wordlist(stopwords_path or os.path.join(_DATA_DIR, "stopwords.txt"))

    if extra_deny:
        deny |= set(w.lower() for w in extra_deny)
    if extra_safe:
        safe |= set(w.lower() for w in extra_safe)
    if extra_reserved:
        reserved |= set(w.lower() for w in extra_reserved)
    if extra_forms:
        forms |= set(w.lower() for w in extra_forms)
    if extra_slurs:
        slurs |= set(w.lower() for w in extra_slurs)
    if extra_stopwords:
        stopwords |= set(w.lower() for w in extra_stopwords)

    return Rules(
        deny=deny,
        high_severity=high_sev,
        safe=safe,
        reserved=reserved,
        forms=forms,
        slurs=slurs,
        stopwords=stopwords,
    )
