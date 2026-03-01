"""
Rule loading and compiled pattern storage for content_guard.

Loads word lists from plain text files and compiles regex patterns
for efficient matching.
"""
import os
import re

from .normalize import consonant_skeleton


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

    def __init__(self, deny=None, high_severity=None, safe=None, reserved=None):
        self.deny = deny or set()
        self.high_severity = high_severity or set()
        self.safe = safe or set()
        self.reserved = reserved or set()

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
        self.phone_re = re.compile(
            r"(?:\+?\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}"
        )
        self.repeated_char_re = re.compile(r"(.)\1{4,}")


def load_rules(
    deny_path=None,
    high_severity_path=None,
    safe_path=None,
    reserved_path=None,
    extra_deny=None,
    extra_safe=None,
    extra_reserved=None,
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

    if extra_deny:
        deny |= set(w.lower() for w in extra_deny)
    if extra_safe:
        safe |= set(w.lower() for w in extra_safe)
    if extra_reserved:
        reserved |= set(w.lower() for w in extra_reserved)

    return Rules(
        deny=deny,
        high_severity=high_sev,
        safe=safe,
        reserved=reserved,
    )
