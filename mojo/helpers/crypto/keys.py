"""
SECRET_KEY accessor honoring Django's SECRET_KEY_FALLBACKS.

Django's own SECRET_KEY_FALLBACKS only covers Django signing (sessions,
password-reset tokens, signed cookies). Mojo's crypto derives from SECRET_KEY
directly — bouncer tokens, filevault key wrapping — so those paths iterate
secret_keys() when verifying/unwrapping and use secret_keys()[0] when
signing/wrapping. A fallback is never used to produce new material; otherwise
a rotation would never complete.
"""

from mojo.helpers.settings import settings


def secret_keys():
    """Primary SECRET_KEY first, then each SECRET_KEY_FALLBACKS entry.

    File-based only (get_static): a DB-settable fallback list would be a
    runtime-injectable key-acceptance list. Verify/unwrap paths iterate this;
    sign/wrap paths use secret_keys()[0].
    """
    return build_key_list(
        settings.get_static("SECRET_KEY", ""),
        settings.get_static("SECRET_KEY_FALLBACKS", []))


def build_key_list(primary, fallbacks):
    """Normalize (primary, fallbacks) into an ordered candidate list.

    The primary is always index 0 — even when empty — so sign/wrap callers
    reading [0] see exactly what settings.SECRET_KEY holds today. Fallback
    entries are dropped when empty or non-string and de-duped preserving
    order. A bare string (a var/django.conf value written without list
    syntax) is ONE key, never iterated character by character.
    """
    if primary is None:
        primary = ""
    if fallbacks is None:
        fallbacks = []
    elif isinstance(fallbacks, str):
        fallbacks = [fallbacks]
    elif not isinstance(fallbacks, (list, tuple)):
        fallbacks = [fallbacks]
    keys = [primary]
    for key in fallbacks:
        if not key or not isinstance(key, str):
            continue
        if key not in keys:
            keys.append(key)
    return keys
