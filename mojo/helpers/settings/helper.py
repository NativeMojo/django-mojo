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

    def get(self, name, default=UNKNOWN):
        if self.root is None:
            self.load_settings()
        if isinstance(self.root, dict):
            value = self.root.get(name, UNKNOWN)
        else:
            value = getattr(self.root, name, UNKNOWN)
        return value if value is not UNKNOWN else self.get_default(name, default)

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
