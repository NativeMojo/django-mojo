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
        """
        self._validate_config_file()
        self._parse_config_file(context)
        self._apply_admin_site_config(context)

    def _validate_config_file(self):
        """Validate that the configuration file exists."""
        if not self.config_path.exists():
            raise Exception(f"Required configuration file not found: {self.config_path}")

    def _parse_config_file(self, context):
        """Parse the configuration file and populate the context."""
        with open(self.config_path, 'r') as file:
            for line in file:
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
        elif self._is_quoted_string(value):
            return self._parse_quoted_string(value)
        elif self._is_f_string(value):
            return eval(value)
        elif self._is_boolean(value):
            return self._parse_boolean(value)
        else:
            return self._parse_numeric_or_string(value)

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


def load_settings_config(context):
    """
    Load Django configuration from django.conf file.

    :param context: Dictionary to load configuration values into.
    """
    loader = DjangoConfigLoader()
    loader.load_config(context)
