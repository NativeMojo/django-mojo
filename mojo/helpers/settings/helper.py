import importlib

UNKNOWN = Ellipsis


def load_settings_profile(context):
    from mojo.helpers import modules, paths
    # Set default profile
    profile = "local"
    # Check if a profile file exists and override profile
    profile_file = paths.VAR_ROOT / "profile"
    if profile_file.exists():
        with open(profile_file, 'r') as file:
            profile = file.read().strip()
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

    def get(self, name, default=UNKNOWN, group=None):
        # When root is an explicit dict (app-settings sub-helper), read from it
        # directly — no Django settings involved.
        if self.root is not None and isinstance(self.root, dict):
            value = self.root.get(name, UNKNOWN)
            return value if value is not UNKNOWN else self.get_default(name, default)

        # DB-backed settings: Redis cache -> DB (group parent chain -> global)
        db_value = self._get_db_setting(name, group)
        if db_value is not UNKNOWN:
            return db_value

        # Fallback: live Django settings (file-based)
        value = getattr(self._live_django_settings(), name, UNKNOWN)
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
        return self.get(name, None)

    def __getitem__(self, key):
        return self.get(key)


# Create a global settings helper for accessing Django settings
settings = SettingsHelper()
