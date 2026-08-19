"""Which django-mojo this node runs, and whether a newer one is published.

Read-only and pin-aware. An operator who froze the fleet at a version is not
offered an upgrade link — the answer to "should I update?" is no when the
answer to "would a deploy even install that?" is no.

Never raises: PyPI being unreachable is a normal state on a laptop, in the
suite, and behind an egress-restricted VPC, and it must degrade the framework
row rather than take the whole Admin dashboard down with it.
"""

import requests

from django.core.cache import cache
from django.utils import timezone

from mojo.helpers import logit
from mojo.helpers.settings import settings


logger = logit.get_logger("framework_version", "edge.log")

CACHE_KEY = "mojo:framework-version:pypi"
DEFAULT_TTL = 21600
DEFAULT_ERROR_TTL = 900
# (connect, read). A dashboard read cannot wait on PyPI; the deploy path uses
# its own, longer budget because a wrong answer there is a failed deploy.
REQUEST_TIMEOUT = (2, 2)


def _ttl(name, default):
    try:
        return max(60, int(settings.get_static(name, default)))
    except (TypeError, ValueError):
        return default


def _version_key(value):
    """Sortable tuple for a released version — 1.10.0 must beat 1.9.0.

    Non-numeric trailers (``1.13.0rc1``) stop the tuple rather than raising, so
    a prerelease compares on the components it does publish.
    """
    key = []
    for chunk in str(value or "").split("."):
        if not chunk.isdigit():
            break
        key.append(int(chunk))
    return tuple(key)


def _published_version():
    """Newest published version, or None. Absorbs every failure."""
    from mojo.apps.edge.services import deploy
    try:
        response = requests.get(deploy.pypi_url(), timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        version = ((response.json() or {}).get("info") or {}).get("version")
    except Exception as err:
        logger.warning("PyPI version lookup unavailable: %s", type(err).__name__)
        return None
    if not deploy.is_valid_version(version or ""):
        return None
    return version


def _pin():
    from mojo.apps.edge.services import deploy
    try:
        value = deploy.framework_version_pin()
    except Exception:
        # A junk pin refuses the next deploy loudly on its own; it must not
        # take this read-only status down with it.
        return {"mode": "latest", "value": None}
    if value == deploy.FRAMEWORK_HOLD:
        return {"mode": "hold", "value": value}
    if value:
        return {"mode": "pinned", "value": value}
    return {"mode": "latest", "value": None}


def status():
    """Installed vs published django-mojo, with the fleet's pin applied."""
    import mojo

    entry = cache.get(CACHE_KEY)
    served_from_cache = isinstance(entry, dict) and "latest" in entry
    if not served_from_cache:
        latest = _published_version()
        entry = {"latest": latest, "checked_at": timezone.now().isoformat()}
        cache.set(CACHE_KEY, entry, _ttl("EDGE_PYPI_VERSION_TTL", DEFAULT_TTL)
                  if latest else
                  _ttl("EDGE_PYPI_VERSION_ERROR_TTL", DEFAULT_ERROR_TTL))
    latest = entry.get("latest")
    if not latest:
        source = "unavailable"
    else:
        source = "cache" if served_from_cache else "pypi"

    pin = _pin()
    installed = mojo.__version__
    return {
        "installed": installed,
        "latest": latest,
        # A pinned or held fleet would not install the newer version even if
        # an operator asked, so it is never offered one.
        "update_available": bool(
            pin["mode"] == "latest" and latest
            and _version_key(latest) > _version_key(installed)),
        "checked_at": entry.get("checked_at"),
        "source": source,
        "pin": pin,
    }


def refresh():
    """Drop the cached PyPI answer so the next status() asks again."""
    cache.delete(CACHE_KEY)
