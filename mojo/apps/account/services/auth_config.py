"""
Auth config — group-owned configuration for the hosted auth experience.

Drives how the login and registration pages of any web app backed by this
framework look and behave. A single structured object with three sections
resolved per group:

    theme         — how the login/register pages look
    registration  — which fields the signup form collects, which signup
                    methods are offered, passkey-during-registration policy
    login         — which login methods are offered

Resolution order (deep-merged, last wins):

    DEFAULT_AUTH_CONFIG (code)
      <- AUTH_CONFIG setting (deployment-wide default)
      <- group.metadata["auth_config"], walked root -> group down the chain

Deep-merge semantics: dicts merge key-by-key, lists and scalars replace
wholesale. So a group setting `login.methods` replaces the inherited list
rather than appending to it.

The per-group login-method restriction is a UX convenience, not a security
boundary — it is only enforced when a `group_uuid` resolves a group on the
request. See `assert_login_method`.
"""
import copy
import json
import re
from urllib.parse import urlsplit

from objict import objict

from mojo import errors as merrors
from mojo.helpers import test_mode as _tm
from mojo.helpers.settings import settings


# Every login method the group config can toggle on/off.
LOGIN_METHODS = ("password", "sms", "passkey", "magic", "google", "apple", "github")
# Registration methods. Passkey-at-signup is `passkey_prompt`, not a method;
# SMS-verified phone signup is the `password` method with a phone identity.
REGISTRATION_METHODS = ("password", "google", "apple", "github")
PASSKEY_PROMPTS = ("off", "optional", "required")
LAYOUTS = ("minimal", "compact", "branded-panel", "editorial")
LAYOUT_ALIASES = {"card": "compact", "fullscreen": "branded-panel"}
APPEARANCES = ("light", "dark", "system")
HERO_IMAGE_POSITIONS = ("center", "top", "bottom", "left", "right")
THEME_PUBLIC_KEYS = (
    "app_title", "auth_provider_name", "logo_url", "favicon_url", "hero_image_url",
    "hero_image_url_light", "hero_image_url_dark", "hero_headline",
    "hero_subheadline", "hero_image_position", "back_to_website_url",
    "back_to_website_label", "terms_url", "layout", "appearance",
    "accent_color", "api_base", "success_redirect", "custom_css",
    "custom_css_url",
)


# Code defaults — reproduce today's behavior when nothing is customized:
# email + password registration, every login method on, no passkey prompt.
DEFAULT_AUTH_CONFIG = {
    "theme": {
        "app_title": "DJANGO MOJO",
        "auth_provider_name": "DJANGO MOJO",
        "logo_url": "",
        "favicon_url": "",
        "hero_image_url": "",
        "hero_image_url_light": "",
        "hero_image_url_dark": "",
        "hero_headline": "Welcome back",
        "hero_subheadline": "Admin Portal",
        "hero_image_position": "center",
        "back_to_website_url": "",
        "back_to_website_label": "Back to website",
        "terms_url": "",
        "layout": "minimal",
        "appearance": "system",
        "accent_color": "#6384ff",
        "api_base": "",
        "success_redirect": "/",
        "custom_css": "",
        "custom_css_url": "",
    },
    "registration": {
        "enabled": True,
        # None -> register_schema falls back to its DEFAULT_FIELDS.
        "fields": None,
        # [] -> no extra (non-canonical) fields rendered/captured.
        # Each entry: {"name", "label"?, "required"?}. See register_schema.
        "extra_fields": [],
        # "" -> register_schema auto-picks (email > phone).
        "identity_field": "",
        "min_age": None,
        "methods": list(REGISTRATION_METHODS),
        "passkey_prompt": "off",
    },
    "login": {
        "methods": list(LOGIN_METHODS),
        "heading": "Sign In",
        "supporting_copy": "",
    },
}


def _deep_merge(base, override):
    """Deep-merge `override` into a copy of `base`.

    Dicts merge key-by-key; lists and scalars replace wholesale.
    """
    out = copy.deepcopy(base)
    if not isinstance(override, dict):
        return out
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _read_test_header(request, header_name):
    """Return an X-Mojo-Test-* header value, only when the test-mode gate
    passes (loopback + flag + no proxy chain)."""
    if request is None:
        return None
    if not _tm.is_test_request(request):
        return None
    key = "HTTP_" + header_name.upper().replace("-", "_")
    return request.META.get(key)


def _group_chain(group):
    """Return [root, ..., group] — ancestors first, the group itself last."""
    chain = []
    seen = set()
    current = group
    while current is not None and current.pk not in seen:
        chain.append(current)
        seen.add(current.pk)
        current = current.parent
    chain.reverse()
    return chain


def _global_config():
    """The AUTH_CONFIG setting as a dict, or {} when unset/malformed."""
    raw = settings.get("AUTH_CONFIG", None)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            return {}
    return raw if isinstance(raw, dict) else {}


def resolve_auth_config(group=None, request=None):
    """Resolve the auth config for `group`, returned as an objict.

    `request` enables the `X-Mojo-Test-Auth-Config` header override (JSON
    object), honored only when the test-mode gate passes.
    """
    cfg = copy.deepcopy(DEFAULT_AUTH_CONFIG)
    cfg = _deep_merge(cfg, _global_config())

    if group is not None:
        for ancestor in _group_chain(group):
            override = (ancestor.metadata or {}).get("auth_config")
            if isinstance(override, dict):
                cfg = _deep_merge(cfg, override)

    header_value = _read_test_header(request, "X-Mojo-Test-Auth-Config")
    if header_value:
        try:
            override = json.loads(header_value)
        except (json.JSONDecodeError, TypeError, ValueError):
            override = None
        if isinstance(override, dict):
            cfg = _deep_merge(cfg, override)

    return objict.from_dict(cfg)


def public_auth_config(cfg):
    """Return the safe, public subset of a resolved auth config.

    Everything here is already rendered into the public auth pages, so the
    subset is a stable-shape whitelist rather than a redaction.
    """
    cfg = cfg or {}
    theme = cfg.get("theme") or {}
    registration = cfg.get("registration") or {}
    login = cfg.get("login") or {}
    return objict.from_dict({
        "theme": {key: theme.get(key) for key in THEME_PUBLIC_KEYS},
        "registration": {
            "enabled": registration.get("enabled", True),
            "fields": registration.get("fields"),
            "extra_fields": list(registration.get("extra_fields") or []),
            "identity_field": registration.get("identity_field", ""),
            "min_age": registration.get("min_age"),
            "methods": list(registration.get("methods") or []),
            "passkey_prompt": registration.get("passkey_prompt", "off"),
        },
        "login": {
            "methods": list(login.get("methods") or []),
            "heading": login.get("heading", "Sign In"),
            "supporting_copy": login.get("supporting_copy", ""),
        },
    })


def normalize_layout(value, fallback="minimal"):
    """Return a canonical layout token, including legacy aliases."""
    value = LAYOUT_ALIASES.get(value, value)
    return value if value in LAYOUTS else fallback


def normalize_appearance(value, fallback="system"):
    return value if value in APPEARANCES else fallback


def normalize_accent_color(value, fallback="#6384ff"):
    if isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        return value
    return fallback


def normalize_navigation_url(value):
    """Return a browser-safe http(s) or relative navigation URL, else empty."""
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if not value or "\\" in value:
        return ""
    try:
        parts = urlsplit(value)
    except ValueError:
        return ""
    if parts.scheme:
        if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
            return ""
    return value


# ---------------------------------------------------------------------------
# Validation — run on Group save so a bad metadata.auth_config is rejected at
# write time, not discovered at render time.
# ---------------------------------------------------------------------------

def _validate_methods(methods, allowed, label):
    if not isinstance(methods, list):
        raise merrors.ValueException(f"{label} must be a list")
    for method in methods:
        if method not in allowed:
            raise merrors.ValueException(
                f"{label} has an unknown method '{method}' "
                f"(allowed: {', '.join(allowed)})")


def _validate_https_url(url, label):
    if not isinstance(url, str) or not url.startswith("https://"):
        raise merrors.ValueException(f"{label} must be an https:// URL")


def _validate_text(value, label, allow_empty=False):
    if not isinstance(value, str):
        raise merrors.ValueException(f"{label} must be a string")
    if not allow_empty and not value.strip():
        raise merrors.ValueException(f"{label} cannot be empty")


def validate_custom_css(css):
    """Reject custom CSS that could break out of the <style> tag (XSS) or
    pull in external resources (CSS-based data exfiltration)."""
    if not isinstance(css, str):
        raise merrors.ValueException("auth_config.theme.custom_css must be a string")
    # A <style> raw-text element can only be terminated by a sequence
    # starting with '<'. Valid CSS never needs '<' — reject it outright.
    if "<" in css:
        raise merrors.ValueException(
            "auth_config.theme.custom_css cannot contain '<' — it would allow "
            "breaking out of the <style> tag")
    lowered = css.lower()
    if "@import" in lowered:
        raise merrors.ValueException(
            "auth_config.theme.custom_css cannot use @import (external resource)")
    if "://" in lowered or re.search(r"url\(\s*['\"]?\s*//", lowered):
        raise merrors.ValueException(
            "auth_config.theme.custom_css cannot reference external URLs "
            "(data: URIs are allowed)")


def validate_auth_config(cfg):
    """Validate an auth config object. Raises ValueException on bad config."""
    if not isinstance(cfg, dict):
        raise merrors.ValueException("auth config must be an object")

    theme = cfg.get("theme")
    if theme is not None:
        if not isinstance(theme, dict):
            raise merrors.ValueException("auth_config.theme must be an object")
        layout = theme.get("layout")
        if layout is not None and layout not in LAYOUTS and layout not in LAYOUT_ALIASES:
            raise merrors.ValueException(
                f"auth_config.theme.layout must be one of: "
                f"{', '.join(LAYOUTS + tuple(LAYOUT_ALIASES))}")
        appearance = theme.get("appearance")
        if appearance is not None and appearance not in APPEARANCES:
            raise merrors.ValueException(
                f"auth_config.theme.appearance must be one of: {', '.join(APPEARANCES)}")
        accent_color = theme.get("accent_color")
        if accent_color is not None and normalize_accent_color(accent_color, "") == "":
            raise merrors.ValueException(
                "auth_config.theme.accent_color must be a six-digit hex color")
        image_position = theme.get("hero_image_position")
        if image_position is not None and image_position not in HERO_IMAGE_POSITIONS:
            raise merrors.ValueException(
                "auth_config.theme.hero_image_position must be one of: "
                f"{', '.join(HERO_IMAGE_POSITIONS)}")
        if "back_to_website_label" in theme:
            _validate_text(theme["back_to_website_label"],
                           "auth_config.theme.back_to_website_label")
        if theme.get("back_to_website_url") and not normalize_navigation_url(
                theme["back_to_website_url"]):
            raise merrors.ValueException(
                "auth_config.theme.back_to_website_url must be a relative or http(s) URL")
        if "auth_provider_name" in theme:
            _validate_text(theme["auth_provider_name"],
                           "auth_config.theme.auth_provider_name")
        if theme.get("custom_css"):
            validate_custom_css(theme["custom_css"])
        if theme.get("custom_css_url"):
            _validate_https_url(theme["custom_css_url"], "auth_config.theme.custom_css_url")

    login = cfg.get("login")
    if login is not None:
        if not isinstance(login, dict):
            raise merrors.ValueException("auth_config.login must be an object")
        methods = login.get("methods")
        if methods is not None:
            _validate_methods(methods, LOGIN_METHODS, "auth_config.login.methods")
            if len(methods) == 0:
                raise merrors.ValueException(
                    "auth_config.login.methods cannot be empty — no way to log in")
        if "heading" in login:
            _validate_text(login["heading"], "auth_config.login.heading")
        if "supporting_copy" in login:
            _validate_text(login["supporting_copy"],
                           "auth_config.login.supporting_copy", allow_empty=True)

    registration = cfg.get("registration")
    if registration is not None:
        if not isinstance(registration, dict):
            raise merrors.ValueException("auth_config.registration must be an object")
        methods = registration.get("methods")
        if methods is not None:
            _validate_methods(methods, REGISTRATION_METHODS, "auth_config.registration.methods")
        prompt = registration.get("passkey_prompt")
        if prompt is not None and prompt not in PASSKEY_PROMPTS:
            raise merrors.ValueException(
                f"auth_config.registration.passkey_prompt must be one of: "
                f"{', '.join(PASSKEY_PROMPTS)}")
        fields = registration.get("fields")
        if fields:
            from mojo.apps.account.services import register_schema
            register_schema.validate_fields_config(fields)
        extra_fields = registration.get("extra_fields")
        if extra_fields:
            from mojo.apps.account.services import register_schema
            register_schema.validate_extra_fields_config(extra_fields)


def resolve_group_from_request(request):
    """Resolve an EFFECTIVELY active Group (it and every ancestor — DM-048)
    from `group_uuid` on the request. None if absent or unknown — callers
    treat None as 'no group context, no restriction'."""
    if not hasattr(request, "DATA"):
        return None
    group_uuid = (request.DATA.get("group_uuid") or "").strip()
    if not group_uuid:
        return None
    from mojo.apps.account.models.group import Group
    group = Group.objects.filter(uuid=group_uuid, is_active=True).first()
    if group is not None and not group.is_effectively_active():
        return None
    return group


def assert_login_method(method, group):
    """UX-only soft gate: raise if `method` is disabled for a resolved group.

    No-op when `group` is None — absent group context means no restriction.
    This is deliberately not a security boundary; an API caller that omits
    `group_uuid` is not restricted.
    """
    if group is None:
        return
    cfg = resolve_auth_config(group)
    methods = cfg.login.methods or []
    if method not in methods:
        raise merrors.PermissionDeniedException(
            "This sign-in method is not available for this app", 403, 403)
