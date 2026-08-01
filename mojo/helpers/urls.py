"""
URL helpers.

One thing lives here: `safe_nav_url`, the scheme guard for a caller-supplied
navigation target that the server renders into a link.
"""
from urllib.parse import urlsplit

SAFE_URL_SCHEMES = ("http", "https")

# A browser removes these from a URL wherever they appear before parsing it, so
# "java<TAB>script:alert(1)" navigates as javascript: — strip them here too or
# the guard judges a scheme the browser will never see.
_STRIP_ANYWHERE = "\t\r\n"

# ...and it strips leading C0 controls and spaces. CPython's urlsplit does the
# same as of the CVE-2023-24329 fix, but pyproject allows Python >=3.10 and the
# backport only landed in 3.10.12 — on 3.10.0-3.10.11 " javascript:alert(1)"
# parses with an EMPTY scheme and would be admitted as a relative path. Keep
# this strip until the floor moves past 3.10.12.
_STRIP_LEADING = "".join(chr(code) for code in range(0x21))


def safe_nav_url(value, default=""):
    """
    Return `value` unchanged when it is safe to put in an href, else `default`.

    Safe means: a non-empty `str` that resolves to an `http`/`https` scheme, OR
    carries no scheme at all (a path-relative or scheme-relative reference).
    Everything else — `javascript:`, `data:`, `vbscript:`, `mailto:`, `tel:`, a
    custom app scheme like `myapp://`, a non-`str`, or a value `urlsplit` cannot
    parse — returns `default`. Non-`str` matters in practice: `request.DATA`
    yields a **list** for `?redirect=a&redirect=b` and a **dict** for
    `?redirect.x=1`, where `request.GET.get()` always yielded a string.

    Three things worth knowing before changing this:

    1. **This is the server-side half of a two-sided contract.** The browser
       half is `safeNavUrl()` in `mojo/apps/account/templates/account/auth_base.html`,
       which resolves against `location.href` and judges the resolved protocol.
       The two must keep agreeing on what is refused. They differ on one point
       only, deliberately: the browser twin returns the *resolved absolute* href,
       while this returns the input verbatim — normalizing `/dashboard` into
       `https://host/dashboard` server-side would break callers that rely on
       relative destinations rendering unchanged.

    2. **The host is deliberately NOT restricted.** A legitimate cross-origin
       `https://` destination must keep working. Host restriction is the
       separate, opt-in concern of `ALLOWED_REDIRECT_URLS` /
       `AUTH_HANDOFF_ALLOWED_URLS` (see
       `mojo.apps.account.services.redirect_allowlist`). Do not fold a host
       check in here.

    3. **`//evil.test` and `\\evil.test` are ADMITTED, and that is not an
       oversight.** They parse scheme-less and may well resolve off-origin, so
       "same-origin relative paths" is the wrong way to describe what passes:
       scheme-relative and path-relative values pass through unchanged and may
       still navigate off-origin. Since a cross-origin `https://evil.test` is
       explicitly permitted here, refusing its equivalent spelling would be
       incoherent — and the browser twin admits it for the same reason. The
       in-tree sibling `_safe_home_url` in `mojo/apps/shortlink/rest/redirect.py`
       is the same check for the same reason but **refuses** protocol-relative,
       and is deliberately NOT migrated to this helper: its input is an
       admin-writable setting naming that deployment's own home page, where an
       off-site value is genuinely unwanted. Unifying the two would silently
       loosen shortlink.
    """
    if not isinstance(value, str) or not value:
        return default

    candidate = value
    for char in _STRIP_ANYWHERE:
        candidate = candidate.replace(char, "")
    candidate = candidate.lstrip(_STRIP_LEADING)

    try:
        parts = urlsplit(candidate)
    except ValueError:
        # Malformed authority, e.g. an unclosed IPv6 bracket.
        return default

    if not parts.scheme:
        # No scheme at all — a relative reference. See note 3 above.
        return value
    if parts.scheme.lower() in SAFE_URL_SCHEMES:
        return value
    return default
