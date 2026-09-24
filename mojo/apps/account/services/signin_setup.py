"""System sign-in setup for the Admin: the hosted login page's look and feel
and the Google / Apple / GitHub sign-in providers, on one page.

Admins are trusted here. Anyone holding global ``manage_settings`` or
``admin`` (or a superuser) can change the system login page and paste
provider credentials; there is no step-up, approval or second writer.

Provider credentials are ordinary global ``Setting`` rows under the exact
keys the OAuth providers already read with ``settings.get`` (a database row
outranks ``django.conf``), so a save takes effect on the next sign-in with no
deploy and no restart. Secret values are stored ``is_secret=True`` and are
never returned; reads report only whether a value is set, where it comes from
and a short hint.
"""

from urllib.parse import urlsplit

from django.db import transaction

from mojo import errors as merrors
from mojo.helpers import logit
from mojo.helpers.settings import settings


# Field tuples: (setting key, label, secret, multiline)
PROVIDERS = {
    "google": {
        "label": "Google",
        "console_url": "https://console.cloud.google.com/apis/credentials",
        "help": ("In Google Cloud Console create an OAuth client ID of type "
                 "\"Web application\", add each callback URL below as an "
                 "authorized redirect URI, then paste the client ID and secret."),
        "fields": (
            ("GOOGLE_CLIENT_ID", "Client ID", False, False),
            ("GOOGLE_CLIENT_SECRET", "Client secret", True, False),
        ),
    },
    "apple": {
        "label": "Apple",
        "console_url": "https://developer.apple.com/account/resources/identifiers/list/serviceId",
        "help": ("In the Apple Developer portal enable Sign in with Apple on your "
                 "App ID, create a Services ID with each login domain and callback "
                 "URL below, and create a Sign in with Apple key. Paste the Team ID, "
                 "the Services ID, the key's ID and the whole .p8 file."),
        "fields": (
            ("APPLE_TEAM_ID", "Team ID", False, False),
            ("APPLE_CLIENT_ID", "Services ID", False, False),
            ("APPLE_KEY_ID", "Key ID", False, False),
            ("APPLE_PRIVATE_KEY", "Private key (.p8)", True, True),
        ),
    },
    "github": {
        "label": "GitHub",
        "console_url": "https://github.com/settings/developers",
        "help": ("In GitHub Developer settings create an OAuth App, set its "
                 "authorization callback URL to the callback URL below, then paste "
                 "the client ID and a client secret."),
        "fields": (
            ("GITHUB_CLIENT_ID", "Client ID", False, False),
            ("GITHUB_CLIENT_SECRET", "Client secret", True, False),
        ),
    },
}


def require_editor(actor):
    """Global manage_settings, admin, or superuser. Nothing more."""
    from mojo.apps.account.services.admin_settings import require_catalog_writer
    return require_catalog_writer(actor)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def _stored(key):
    from mojo.apps.account.models import Setting
    row = Setting.objects.filter(key=key, group=None).order_by("pk").first()
    if row is None:
        return None
    try:
        return row.get_value()
    except Exception:
        return None


def _hint(value):
    from mojo.apps.account.services.provider_setup import key_hint
    hint = key_hint(value.strip()) if isinstance(value, str) else ""
    return f"…{hint}" if hint else None


def _field_state(key, label, secret, multiline):
    stored = _stored(key)
    if stored not in (None, ""):
        source, value = "admin", stored
    else:
        deployed = settings.get_static(key, None)
        source, value = ("deployment", deployed) if deployed else ("none", None)
    configured = source != "none"
    return {
        "key": key,
        "label": label,
        "secret": secret,
        "multiline": multiline,
        "configured": configured,
        "source": source,
        "value": None if (secret or not configured) else str(value),
        "hint": _hint(value) if (secret and configured) else None,
    }


def _origin(url):
    if not isinstance(url, str):
        return None
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}"


def login_origins(request=None):
    """Origins a hosted login page is likely served from, most likely first.

    The provider callback is derived from the Origin of the page that starts
    sign-in (``rest/oauth.py`` ``_get_origin``), so each origin the login page
    runs on needs its own callback URL registered with the provider.
    """
    candidates = list(settings.get("ALLOWED_REDIRECT_URLS", [], kind="list") or [])
    candidates.append(settings.get("BASE_URL", None))
    if request is not None:
        from mojo.apps.account.rest.oauth import _get_origin
        candidates.append(_get_origin(request))
    origins = []
    for candidate in candidates:
        origin = _origin(candidate)
        if origin and origin not in origins:
            origins.append(origin)
    return origins


def _system_auth():
    from mojo.apps.account.services import auth_config
    return auth_config.resolve_auth_config()


def provider_state(name, request=None, resolved=None):
    spec = PROVIDERS[name]
    resolved = resolved if resolved is not None else _system_auth()
    fields = [_field_state(*field) for field in spec["fields"]]
    login_methods = (resolved.get("login") or {}).get("methods") or []
    callbacks = [f"{origin}/api/auth/oauth/{name}/callback"
                 for origin in login_origins(request)]
    return {
        "name": name,
        "label": spec["label"],
        "enabled": name in login_methods,
        "ready": all(field["configured"] for field in fields),
        "missing": [field["label"] for field in fields if not field["configured"]],
        "callback_url": callbacks[0] if callbacks else None,
        "callback_urls": callbacks,
        "domains": [urlsplit(url).hostname for url in callbacks],
        "console_url": spec["console_url"],
        "help": spec["help"],
        "fields": fields,
    }


def state(request=None):
    from mojo.apps.account.services import auth_config, system_settings
    resolved = _system_auth()
    return {
        "schema_version": 1,
        "auth": auth_config.public_auth_config(resolved),
        "editable": sorted(system_settings.AUTH_SAFE_PATHS),
        "options": {
            "login_methods": list(auth_config.LOGIN_METHODS),
            "registration_methods": list(auth_config.REGISTRATION_METHODS),
            "layouts": list(auth_config.LAYOUTS),
            "appearances": list(auth_config.APPEARANCES),
            "hero_image_positions": list(auth_config.HERO_IMAGE_POSITIONS),
            "passkey_prompts": list(auth_config.PASSKEY_PROMPTS),
        },
        "providers": [provider_state(name, request, resolved) for name in PROVIDERS],
    }


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def _clean(key, value):
    if not isinstance(value, str):
        raise merrors.ValueException(f"{key} must be text")
    value = value.replace("\r\n", "\n").strip()
    if key == "APPLE_PRIVATE_KEY" and value and "PRIVATE KEY" not in value:
        raise merrors.ValueException(
            "The Apple private key should be the whole .p8 file, "
            "including the BEGIN/END PRIVATE KEY lines")
    return value


def save_provider(actor, name, values=None, enabled=None):
    """Store provider credentials and/or switch the provider on or off.

    ``values`` maps setting keys to text: omitted or "" keeps the stored
    value, ``None`` clears it. ``enabled`` adds or removes the provider from
    the system login and registration methods.
    """
    actor = require_editor(actor)
    if name not in PROVIDERS:
        raise merrors.ValueException(f"Unknown sign-in provider: {name}")
    spec = {field[0]: field for field in PROVIDERS[name]["fields"]}
    values = values or {}
    if not isinstance(values, dict):
        raise merrors.ValueException("values must be an object")
    unknown = sorted(set(values) - set(spec))
    if unknown:
        raise merrors.ValueException(f"{unknown[0]} is not a {name} setting")
    if enabled is not None and not isinstance(enabled, bool):
        raise merrors.ValueException("enabled must be true or false")

    from mojo.apps.account.models import Setting
    changed = []
    with transaction.atomic():
        for key, raw in values.items():
            if raw is None:
                Setting.remove(key)
                changed.append(f"{key} cleared")
                continue
            value = _clean(key, raw)
            if not value:
                continue
            Setting.set(key, value, is_secret=spec[key][2])
            changed.append(f"{key} set")
        if enabled is not None:
            _set_enabled(actor, name, enabled)
            changed.append(f"{'enabled' if enabled else 'disabled'}")
    if changed:
        logit.info("admin.signin",
                   f"{actor.username} updated {name} sign-in: {', '.join(changed)}")


def _set_enabled(actor, name, enabled):
    from mojo.apps.account.services import auth_config
    resolved = _system_auth()
    patch = {}
    for section, allowed in (("login", auth_config.LOGIN_METHODS),
                             ("registration", auth_config.REGISTRATION_METHODS)):
        if name not in allowed:
            continue
        methods = list((resolved.get(section) or {}).get("methods") or [])
        if enabled and name not in methods:
            methods.append(name)
        elif not enabled and name in methods:
            methods.remove(name)
        else:
            continue
        patch[f"{section}.methods"] = methods
    if patch:
        save_auth(actor, patch)


def save_auth(actor, patch):
    """Merge look-and-feel / method changes into the system AUTH_CONFIG."""
    from mojo.apps.account.services import system_settings
    return system_settings.set_auth_safe_fields(actor, patch)
