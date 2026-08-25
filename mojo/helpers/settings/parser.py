import hashlib


class DjangoConfigLoader:
    """
    A clean, expandable class for loading Django configuration from django.conf files.
    """

    def __init__(self, config_path=None):
        """
        Initialize the config loader.

        :param config_path: Path to the django.conf file. If None, uses default VAR_ROOT path.
        """
        if config_path is None:
            from mojo.helpers import paths
            self.config_path = paths.VAR_ROOT / "django.conf"
        else:
            self.config_path = config_path

    def load_config(self, context):
        """
        Load configuration from django.conf file into the provided context.

        :param context: Dictionary to load configuration values into.
        :raises Exception: If the required configuration file is not found.
        :return: sha256 of the exact bytes that were parsed.
        """
        self._validate_config_file()
        raw = self.config_path.read_bytes()
        self._parse_config_file(context, raw=raw)
        self._apply_admin_site_config(context)
        return hashlib.sha256(raw).hexdigest()

    def _validate_config_file(self):
        """Validate that the configuration file exists."""
        if not self.config_path.exists():
            raise Exception(f"Required configuration file not found: {self.config_path}")

    def _parse_config_file(self, context, raw=None):
        """Parse the configuration file and populate the context.

        `raw` is the already-read file content. It is passed in rather than
        re-read so the fingerprint load_config returns describes exactly the
        bytes that produced these settings — re-reading would let a concurrent
        write make the process advertise a config it never actually loaded.
        """
        if raw is None:
            raw = self.config_path.read_bytes()
        for line in raw.decode('utf-8', errors='replace').splitlines():
            if '=' in line:
                key, value = line.strip().split('=', 1)
                parsed_value = self._parse_value(value.strip())
                context[key.strip()] = parsed_value

    def _parse_value(self, value):
        """
        Parse a configuration value string into the appropriate Python type.

        :param value: String value to parse.
        :return: Parsed value with appropriate type.
        """
        if self._is_list_value(value):
            return self._parse_list_value(value)
        elif self._is_dict_value(value):
            return self._parse_dict_value(value)
        elif self._is_quoted_string(value):
            return self._parse_quoted_string(value)
        elif self._is_f_string(value):
            return eval(value)
        elif self._is_boolean(value):
            return self._parse_boolean(value)
        else:
            return self._parse_numeric_or_string(value)

    def _is_dict_value(self, value):
        """Check if value is a dict literal."""
        return value.startswith('{') and value.endswith('}')

    def _parse_dict_value(self, value):
        """Parse a Python dict literal using ast.literal_eval (safe — no code exec)."""
        import ast
        try:
            return ast.literal_eval(value)
        except (ValueError, SyntaxError):
            # Malformed dict literal — fall back to raw string so the bad value
            # is visible in debug rather than silently swallowed.
            return value

    def _is_list_value(self, value):
        """Check if value is a list format."""
        return value.startswith('[') and value.endswith(']')

    def _is_quoted_string(self, value):
        """Check if value is a quoted string."""
        return ((value.startswith('"') and value.endswith('"')) or
                (value.startswith("'") and value.endswith("'")))

    def _is_f_string(self, value):
        """Check if value is an f-string."""
        return value.startswith('f"') or value.startswith("f'")

    def _is_boolean(self, value):
        """Check if value is a boolean."""
        return value.lower() in ('true', 'false')

    def _parse_list_value(self, value):
        """Parse a list value string into a Python list."""
        list_content = value[1:-1].strip()
        if not list_content:
            return []

        items = []
        for item in list_content.split(','):
            item = item.strip()
            parsed_item = self._parse_list_item(item)
            items.append(parsed_item)
        return items

    def _parse_list_item(self, item):
        """Parse an individual list item."""
        if self._is_quoted_string(item):
            return item[1:-1]  # Remove quotes
        else:
            return self._parse_numeric_or_string(item)

    def _parse_quoted_string(self, value):
        """Parse a quoted string by removing the quotes."""
        return value[1:-1]

    def _parse_boolean(self, value):
        """Parse a boolean string."""
        return value.lower() == 'true'

    def _parse_numeric_or_string(self, value):
        """Parse a value as numeric if possible, otherwise return as string."""
        try:
            if '.' in value:
                return float(value)
            else:
                return int(value)
        except ValueError:
            return value

    def _apply_admin_site_config(self, context):
        """Apply Django admin site configuration if enabled."""
        if context.get("ALLOW_ADMIN_SITE", True):
            installed_apps = context.get("INSTALLED_APPS", [])
            if "django.contrib.admin" not in installed_apps:
                installed_apps.insert(0, "django.contrib.admin")
                context["INSTALLED_APPS"] = installed_apps


# Fingerprint of the django.conf this process actually loaded, set once at import
# time by load_settings_config() below. Stays None in a process that never loaded
# one.
#
# django.conf is read exactly once per process, when the Django settings module is
# imported, so a config change only takes effect after a restart. Hashing the bytes
# at that single point is therefore the only honest answer to "which config is this
# process serving under?" -- a hash taken anywhere later could describe a file the
# process never read. testit uses it to tell a freshly reloaded worker from the old
# one it is replacing (see testit/helpers.py server_settings).
CONF_FINGERPRINT = None


def conf_fingerprint():
    """sha256 of the django.conf this process loaded, or None if it loaded none."""
    return CONF_FINGERPRINT


def load_settings_config(context):
    """
    Load Django configuration from django.conf file.

    :param context: Dictionary to load configuration values into.
    """
    global CONF_FINGERPRINT

    loader = DjangoConfigLoader()
    # The fingerprint comes back from load_config itself, hashed from the very
    # bytes it parsed. Hashing a second read here would let a write landing
    # between the two make this process advertise a config it never loaded --
    # and the whole stale-worker guard rests on "advertised sha == loaded
    # settings" being true by construction.
    CONF_FINGERPRINT = loader.load_config(context)

    # This runs while the settings module is still importing, before Django
    # can cache DATABASES or construct the middleware stack. The local import
    # keeps the parser's normal import path independent of the routing feature.
    # Connection defaults run after the reader so a derived reader alias is
    # covered too (its own explicit CONN_MAX_AGE still wins via setdefault).
    from mojo.db.config import apply_connection_defaults, apply_reader_database
    apply_reader_database(context)
    apply_connection_defaults(context)
