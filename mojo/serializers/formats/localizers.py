from decimal import Decimal
from datetime import datetime, date, time
import locale
from mojo.helpers import logit

logger = logit.get_logger("localizers", "localizers.log")

# Registry of available localizers
LOCALIZER_REGISTRY = {}


def register_localizer(name, func):
    """
    Register a localizer function.

    :param name: Name to register the localizer under
    :param func: Localizer function that takes (value, extra=None)
    """
    LOCALIZER_REGISTRY[name] = func
    logger.debug(f"Registered localizer: {name}")


def get_localizer(name):
    """
    Get a localizer function by name.

    :param name: Localizer name
    :return: Localizer function or None
    """
    return LOCALIZER_REGISTRY.get(name)


def list_localizers():
    """
    Get list of all registered localizer names.

    :return: List of localizer names
    """
    return list(LOCALIZER_REGISTRY.keys())


# Currency localizers
def cents_to_currency(value, extra=None):
    """
    Convert cents to currency format.

    :param value: Value in cents
    :param extra: Currency symbol (default: no symbol)
    :return: Formatted currency string
    """
    if value is None:
        return "0.00"

    try:
        currency, cents = divmod(int(value), 100)
        if extra:
            return f"{extra}{currency}.{cents:02d}"
        return f"{currency}.{cents:02d}"
    except (ValueError, TypeError):
        return str(value)


def cents_to_dollars(value, extra=None):
    """
    Convert cents to dollar format.

    :param value: Value in cents
    :param extra: Not used
    :return: Dollar-formatted string
    """
    if value is None:
        return "$0.00"

    try:
        dollars, cents = divmod(int(value), 100)
        return f"${dollars}.{cents:02d}"
    except (ValueError, TypeError):
        return str(value)


def currency_format(value, extra="$"):
    """
    Format value as currency with specified symbol.

    :param value: Numeric value
    :param extra: Currency symbol
    :return: Formatted currency string
    """
    if value is None:
        return f"{extra}0.00"

    try:
        if isinstance(value, (int, float, Decimal)):
            return f"{extra}{value:.2f}"
        return f"{extra}{float(value):.2f}"
    except (ValueError, TypeError):
        return str(value)


# Date/time localizers
def date_format(value, extra="%Y-%m-%d", timezone=None):
    """
    Format date with specified format.

    :param value: Date value
    :param extra: Date format string
    :param timezone: Timezone to convert to before formatting
    :return: Formatted date string
    """
    if value is None:
        return ""

    try:
        # Convert to target timezone if specified
        if timezone and isinstance(value, datetime):
            try:
                from mojo.helpers import dates
                value = dates.get_local_time(timezone, value)
            except Exception as e:
                logger.warning(f"Timezone conversion failed: {e}")

        if isinstance(value, datetime):
            return value.strftime(extra)
        elif isinstance(value, date):
            return value.strftime(extra)
        elif isinstance(value, str):
            # Try to parse string date
            parsed_date = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if timezone:
                try:
                    from mojo.helpers import dates
                    parsed_date = dates.get_local_time(timezone, parsed_date)
                except Exception:
                    pass
            return parsed_date.strftime(extra)
        return str(value)
    except (ValueError, AttributeError):
        return str(value)


def datetime_format(value, extra="%Y-%m-%d %H:%M:%S", timezone=None):
    """
    Format datetime with specified format.

    :param value: Datetime value
    :param extra: Datetime format string
    :param timezone: Timezone to convert to before formatting
    :return: Formatted datetime string
    """
    if value is None:
        return ""

    try:
        # Convert to target timezone if specified
        if timezone and isinstance(value, datetime):
            try:
                from mojo.helpers import dates
                value = dates.get_local_time(timezone, value)
            except Exception as e:
                logger.warning(f"Timezone conversion failed: {e}")

        if isinstance(value, datetime):
            return value.strftime(extra)
        elif isinstance(value, str):
            # Try to parse string datetime
            parsed_datetime = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if timezone:
                try:
                    from mojo.helpers import dates
                    parsed_datetime = dates.get_local_time(timezone, parsed_datetime)
                except Exception:
                    pass
            return parsed_datetime.strftime(extra)
        return str(value)
    except (ValueError, AttributeError):
        return str(value)


def time_format(value, extra="%H:%M:%S", timezone=None):
    """
    Format time with specified format.

    :param value: Time value
    :param extra: Time format string
    :param timezone: Timezone to convert to before formatting
    :return: Formatted time string
    """
    if value is None:
        return ""

    try:
        # Convert to target timezone if specified
        if timezone and isinstance(value, datetime):
            try:
                from mojo.helpers import dates
                value = dates.get_local_time(timezone, value)
            except Exception as e:
                logger.warning(f"Timezone conversion failed: {e}")

        if isinstance(value, time):
            return value.strftime(extra)
        elif isinstance(value, datetime):
            return value.time().strftime(extra)
        return str(value)
    except (ValueError, AttributeError):
        return str(value)


def timezone_format(value, extra=None):
    """
    Format datetime with timezone information.

    :param value: Datetime value
    :param extra: Timezone name (optional)
    :return: Formatted datetime with timezone
    """
    if value is None:
        return ""

    try:
        # Try to import timezone helper
        try:
            from mojo.helpers import dates
            if extra:
                localized_value = dates.get_local_time(extra, value)
                return localized_value.strftime("%Y-%m-%d %H:%M:%S %Z")
            elif hasattr(value, 'strftime'):
                return value.strftime("%Y-%m-%d %H:%M:%S %Z")
        except ImportError:
            logger.warning("mojo.helpers.dates not available for timezone formatting")

        if hasattr(value, 'strftime'):
            return value.strftime("%Y-%m-%d %H:%M:%S")
        return str(value)
    except Exception as e:
        logger.warning(f"Timezone formatting failed: {e}")
        return str(value)


# Number formatters
def number_format(value, extra="2"):
    """
    Format number with specified decimal places.

    :param value: Numeric value
    :param extra: Number of decimal places (default: 2)
    :return: Formatted number string
    """
    if value is None:
        return "0"

    try:
        decimal_places = int(extra) if extra else 2
        if isinstance(value, (int, float, Decimal)):
            return f"{float(value):.{decimal_places}f}"
        return f"{float(value):.{decimal_places}f}"
    except (ValueError, TypeError):
        return str(value)


def percentage_format(value, extra="2"):
    """
    Format value as percentage.

    :param value: Numeric value (0.1 = 10%)
    :param extra: Number of decimal places
    :return: Formatted percentage string
    """
    if value is None:
        return "0%"

    try:
        decimal_places = int(extra) if extra else 2
        percentage = float(value) * 100
        return f"{percentage:.{decimal_places}f}%"
    except (ValueError, TypeError):
        return str(value)


def thousands_separator(value, extra=","):
    """
    Add thousands separator to number.

    :param value: Numeric value
    :param extra: Separator character (default: comma)
    :return: Formatted number with separators
    """
    if value is None:
        return "0"

    try:
        separator = extra if extra else ","
        return f"{int(value):,}".replace(",", separator)
    except (ValueError, TypeError):
        return str(value)


# Text formatters
def title_case(value, extra=None):
    """
    Convert text to title case.

    :param value: Text value
    :param extra: Not used
    :return: Title case text
    """
    if value is None:
        return ""

    return str(value).title()


def upper_case(value, extra=None):
    """
    Convert text to upper case.

    :param value: Text value
    :param extra: Not used
    :return: Upper case text
    """
    if value is None:
        return ""

    return str(value).upper()


def lower_case(value, extra=None):
    """
    Convert text to lower case.

    :param value: Text value
    :param extra: Not used
    :return: Lower case text
    """
    if value is None:
        return ""

    return str(value).lower()


def truncate_text(value, extra="50"):
    """
    Truncate text to specified length.

    :param value: Text value
    :param extra: Maximum length (default: 50)
    :return: Truncated text
    """
    if value is None:
        return ""

    try:
        max_length = int(extra) if extra else 50
        text = str(value)
        if len(text) <= max_length:
            return text
        return text[:max_length-3] + "..."
    except (ValueError, TypeError):
        return str(value)


# Boolean formatters
def yes_no(value, extra=None):
    """
    Convert boolean to Yes/No.

    :param value: Boolean value
    :param extra: Not used
    :return: "Yes" or "No"
    """
    if value is None:
        return "No"

    return "Yes" if bool(value) else "No"


def true_false(value, extra=None):
    """
    Convert boolean to True/False.

    :param value: Boolean value
    :param extra: Not used
    :return: "True" or "False"
    """
    if value is None:
        return "False"

    return "True" if bool(value) else "False"


def on_off(value, extra=None):
    """
    Convert boolean to On/Off.

    :param value: Boolean value
    :param extra: Not used
    :return: "On" or "Off"
    """
    if value is None:
        return "Off"

    return "On" if bool(value) else "Off"


# List/collection formatters
def join_list(value, extra=", "):
    """
    Join list items with separator.

    :param value: List or iterable
    :param extra: Separator string (default: ", ")
    :return: Joined string
    """
    if value is None:
        return ""

    try:
        separator = extra if extra else ", "
        if isinstance(value, (list, tuple)):
            return separator.join(str(item) for item in value)
        return str(value)
    except Exception:
        return str(value)


def list_count(value, extra=None):
    """
    Return count of items in list.

    :param value: List or iterable
    :param extra: Not used
    :return: Count as string
    """
    if value is None:
        return "0"

    try:
        if hasattr(value, '__len__'):
            return str(len(value))
        elif hasattr(value, 'count'):
            return str(value.count())
        return "1"
    except Exception:
        return "0"


# File size formatter
def file_size(value, extra="auto"):
    """
    Format file size in human readable format.

    :param value: File size in bytes
    :param extra: Unit preference ("auto", "KB", "MB", "GB")
    :return: Formatted file size
    """
    if value is None:
        return "0 B"

    try:
        size = float(value)
        if extra != "auto":
            unit = extra.upper()
            if unit == "KB":
                return f"{size/1024:.2f} KB"
            elif unit == "MB":
                return f"{size/(1024**2):.2f} MB"
            elif unit == "GB":
                return f"{size/(1024**3):.2f} GB"

        # Auto-detect best unit
        if size < 1024:
            return f"{size:.0f} B"
        elif size < 1024**2:
            return f"{size/1024:.2f} KB"
        elif size < 1024**3:
            return f"{size/(1024**2):.2f} MB"
        else:
            return f"{size/(1024**3):.2f} GB"
    except (ValueError, TypeError):
        return str(value)


# Register all localizers
register_localizer('cents_to_currency', cents_to_currency)
register_localizer('cents_to_dollars', cents_to_dollars)
register_localizer('currency', currency_format)
register_localizer('date', date_format)
register_localizer('datetime', datetime_format)
register_localizer('time', time_format)
register_localizer('timezone', timezone_format)
register_localizer('number', number_format)
register_localizer('percentage', percentage_format)
register_localizer('thousands', thousands_separator)
register_localizer('title', title_case)
register_localizer('upper', upper_case)
register_localizer('lower', lower_case)
register_localizer('truncate', truncate_text)
register_localizer('yes_no', yes_no)
register_localizer('true_false', true_false)
register_localizer('on_off', on_off)
register_localizer('join', join_list)
register_localizer('count', list_count)
register_localizer('filesize', file_size)


# Custom localizer decorator
def localizer(name):
    """
    Decorator to register a custom localizer.

    Usage:
        @localizer('my_formatter')
        def my_custom_formatter(value, extra=None):
            return f"Custom: {value}"
    """
    def decorator(func):
        register_localizer(name, func)
        return func
    return decorator


# Legacy support
def apply_localizer(value, localizer_config):
    """
    Apply localizer based on configuration string.

    :param value: Value to localize
    :param localizer_config: Configuration string like "currency|$" or "date|%Y-%m-%d"
    :return: Localized value
    """
    if not localizer_config:
        return value

    try:
        if '|' in localizer_config:
            localizer_name, extra = localizer_config.split('|', 1)
        else:
            localizer_name, extra = localizer_config, None

        localizer_func = get_localizer(localizer_name)
        if localizer_func:
            return localizer_func(value, extra)
        else:
            logger.warning(f"Unknown localizer: {localizer_name}")
            return value
    except Exception as e:
        logger.error(f"Localizer error for '{localizer_config}': {e}")
        return value
