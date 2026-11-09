import importlib
import re
import sys
from objict import objict
UNKNOWN = Ellipsis


def _django_ready():
    try:
        from django.apps import apps
        return apps.ready
    except Exception:
        return False


def _warn_coercion(name, value, kind, default):
    """A present-but-uncoercible value degrades to the declared default —
    loudly, so 'someone wrote garbage' is distinguishable from 'unset'.
    The raw value is deliberately NOT logged: DB-backed reads return decrypted
    plaintext for is_secret rows, and a log line must never leak one.
    logit is imported lazily: this module loads before almost everything."""
    from mojo.helpers import logit
    shown = None if default is UNKNOWN else default
    logit.warning(
        "settings",
        f"setting {name!r}: cannot coerce {type(value).__name__} value to "
        f"{kind}; using default {shown!r}")

REQUIRED_PROFILE_KEY = "MOJO_REQUIRED_PROFILE"
_REQUIRED_PROFILE_LINE = re.compile(r"^\s*MOJO_REQUIRED_PROFILE\s*=\s*(.*?)\s*$")
_PROFILE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


def required_profile_from_text(text):
    """Read MOJO_REQUIRED_PROFILE out of django.conf text, without the parser.

    Returns ("absent", None), ("set", name) or ("malformed", None). The last
    matching line wins, as in the conf loader; one pair of matching quotes is
    stripped. The conf parser is never called: it evals f-string values. Only
    a plain profile name counts — anything else leaves the guard dormant.
    """
    value = None
    for line in text.splitlines():
        match = _REQUIRED_PROFILE_LINE.match(line)
        if match:
            value = match.group(1)
    if value is None:
        return "absent", None
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]
    if not _PROFILE_NAME.match(value):
        return "malformed", None
    return "set", value


def resolve_profile(var_root, stderr=None):
    """The settings profile for this tree: var_root/profile, else "local".

    With MOJO_REQUIRED_PROFILE = <name> in var_root/django.conf (opt-in, set
    by the deployment's own operator), a missing, empty or different marker
    raises ImproperlyConfigured instead of falling back to "local". Without
    the key this is exactly the old behaviour, and writes nothing.
    """
    stderr = sys.stderr if stderr is None else stderr
    profile = "local"
    profile_file = var_root / "profile"
    marker = None
    if profile_file.exists():
        with open(profile_file, 'r') as file:
            marker = file.read().strip()
        profile = marker

    conf_file = var_root / "django.conf"
    try:
        text = conf_file.read_text(errors="replace")
    except OSError:
        text = ""
    state, required = required_profile_from_text(text)
    if state == "malformed":
        stderr.write(
            f"django-mojo: {REQUIRED_PROFILE_KEY} in {conf_file} is not a plain "
            "profile name; the line is ignored and the profile is not enforced\n")
    elif state == "set" and marker != required:
        if marker is None:
            found = "is missing"
        elif not marker:
            found = "is empty"
        else:
            shown = marker if _PROFILE_NAME.match(marker) else "an unexpected value"
            found = f"reads {shown[:64]}"
        from django.core.exceptions import ImproperlyConfigured
        raise ImproperlyConfigured(
            f"django-mojo refused to start: {conf_file} sets "
            f"{REQUIRED_PROFILE_KEY} = {required}, but {profile_file} {found}. "
            f"Fix: echo {required} > {profile_file} && "
            f"chown ec2-user:www {profile_file} && chmod 640 {profile_file}, "
            "then restart mojo-asgi (use your deployment's users if different).")
    return profile


def load_settings_profile(context):
    from mojo.helpers import modules, paths
    profile = resolve_profile(paths.VAR_ROOT)
    modules.load_module_to_globals("settings.defaults", context)
    modules.load_module_to_globals(f"settings.{profile}", context)


class SettingsHelper:
    """
    A helper class for accessing Django settings with support for:
    - Default values if settings are missing.
    - App-specific settings loading from `{app_name}.settings`.
    - Dictionary-style (`settings["KEY"]`) and attribute-style (`settings.KEY`) access.
    """

    def __init__(self, root_settings=None, defaults=None):
        self.root = root_settings
        self.defaults = defaults
        self._app_cache = {}

    def load_settings(self):
        from django.conf import settings as django_settings
        self.root = django_settings

    def _live_django_settings(self):
        """Always return the current django.conf.settings object.

        We intentionally re-import on every call rather than using self.root so
        that Django's override_settings context manager works correctly in tests.
        override_settings swaps the underlying LazySettings._wrapped object; any
        cached reference obtained before the override will silently read stale
        values. Importing django.conf.settings is cheap — it returns the same
        module-level LazySettings proxy each time, which always delegates to the
        currently active wrapped object.
        """
        from django.conf import settings as django_settings
        return django_settings


    def get_app_settings(self, app_name):
        key = f"{app_name.upper()}_SETTINGS"
        if key in self._app_cache:
            return self._app_cache[key]
        try:
            app_defaults = importlib.import_module(f"{app_name}.settings")
        except ModuleNotFoundError:
            app_defaults = {}
        self._app_cache[key] = SettingsHelper(self.get(key, {}), app_defaults)
        return self._app_cache[key]

    def _convert_value(self, value, kind, default=UNKNOWN, name=None):
        if not kind:
            return value

        if kind == "int":
            try:
                return int(value)
            except (TypeError, ValueError):
                _warn_coercion(name, value, kind, default)
                return int(default) if default is not UNKNOWN else 0

        if kind == "float":
            try:
                return float(value)
            except (TypeError, ValueError):
                _warn_coercion(name, value, kind, default)
                return float(default) if default is not UNKNOWN else 0.0

        if kind == "bool":
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                normalized = value.strip().lower()
                if normalized in ["true", "1", "yes", "on", "y"]:
                    return True
                if normalized in ["false", "0", "no", "off", "n", ""]:
                    return False
                # An unrecognized string is garbage, not truthy — degrade to
                # the DECLARED default (unset-but-loud). bool(value) here
                # silently failed OPEN for allow-flavored flags.
                _warn_coercion(name, value, kind, default)
                return bool(default) if default is not UNKNOWN else False
            return bool(value)

        if kind == "dict":
            if isinstance(value, dict):
                return value
            if isinstance(value, str):
                # No ignore_errors: it swallows parse errors by returning an
                # EMPTY objict, indistinguishable from a legit "{}".
                try:
                    parsed = objict.from_json(value)
                except Exception:
                    parsed = None
                if isinstance(parsed, dict):
                    return parsed
            _warn_coercion(name, value, kind, default)
            if isinstance(default, dict):
                return default
            return {}

        if kind == "list":
            if isinstance(value, list):
                return value
            if isinstance(value, str):
                if value.startswith("[") and value.endswith("]"):
                    try:
                        parsed = objict.from_json(value)
                    except Exception:
                        parsed = None
                    if isinstance(parsed, list):
                        return parsed
                    # Bracket-wrapped but unparsable: comma-splitting would
                    # manufacture nonsense entries (e.g. '["payments"', ']').
                    _warn_coercion(name, value, kind, default)
                    return default if isinstance(default, list) else []
                return [x.strip() for x in value.split(",") if x.strip()]
            _warn_coercion(name, value, kind, default)
            if isinstance(default, list):
                return default
            return []

        return value

    def get(self, name, default=UNKNOWN, group=None, kind=None):
        # When root is an explicit dict (app-settings sub-helper), read from it
        # directly — no Django settings involved.
        if self.root is not None and isinstance(self.root, dict):
            value = self.root.get(name, UNKNOWN)
            if value is not UNKNOWN and kind:
                return self._convert_value(value, kind, default, name=name)
            return value if value is not UNKNOWN else self.get_default(name, default)

        if _django_ready():
            # DB-backed settings: Redis cache -> DB (group parent chain -> global)
            db_value = self._get_db_setting(name, group)
            if db_value is not UNKNOWN:
                if kind:
                    return self._convert_value(db_value, kind, default, name=name)
                return db_value

        # Fallback: live Django settings (file-based)
        value = getattr(self._live_django_settings(), name, UNKNOWN)
        if value is not UNKNOWN and kind:
            return self._convert_value(value, kind, default, name=name)
        return value if value is not UNKNOWN else self.get_default(name, default)

    def get_static(self, name, default=None, kind=None):
        """
        Return a static value for setting `name`.

        kind:
          - "dict": always coerce to dict-like value (fallback default or {})
          - "bool": boolean view via __bool__
          - None: raw value
        """
        if self.root is not None and isinstance(self.root, dict):
            value = self.root.get(name, UNKNOWN)
            if value is not UNKNOWN and kind:
                return self._convert_value(value, kind, default, name=name)
            return value if value is not UNKNOWN else self.get_default(name, default)

        value = getattr(self._live_django_settings(), name, UNKNOWN)
        if value is not UNKNOWN and kind:
            return self._convert_value(value, kind, default, name=name)
        return value if value is not UNKNOWN else self.get_default(name, default)


    def _get_db_setting(self, name, group=None):
        """Lookup a setting from the DB-backed store (via Redis cache).

        Returns UNKNOWN if not found so callers can fall through.
        """
        try:
            from mojo.apps.account.models.setting import Setting
        except Exception:
            return UNKNOWN
        try:
            value = Setting.resolve(name, group=group)
        except Exception:
            return UNKNOWN
        if value is None:
            return UNKNOWN
        return value

    def get_default(self, name, default=None):
        if default is UNKNOWN:
            default = None
        if isinstance(self.defaults, dict):
            return self.defaults.get(name, default)
        return getattr(self.defaults, name, default)

    def is_app_installed(self, app_label):
        return app_label in self.get("INSTALLED_APPS")

    def __getattr__(self, name):
        return self.get_static(name, None)

    def __getitem__(self, key):
        return self.get_static(key)


# Create a global settings helper for accessing Django settings
settings = SettingsHelper()
