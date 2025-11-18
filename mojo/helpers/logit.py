import os
import sys
import logging
import threading
from decimal import Decimal
from collections import OrderedDict
from io import StringIO
from typing import Optional
import traceback
import re
# import traceback
# import time
# from datetime import datetime
# from binascii import hexlify
from . import paths

# Resolve paths
LOG_DIR = paths.LOG_ROOT

# Ensure log directory exists (but don't crash if we can't)
try:
    os.makedirs(LOG_DIR, exist_ok=True)
except (OSError, PermissionError) as e:
    # Can't create log directory - will fall back to console logging only
    sys.stderr.write(f"Warning: Cannot create log directory {LOG_DIR}: {e}\n")
    LOG_DIR = None

# Constants
MAX_LOG_SIZE = 10 * 1024 * 1024  # 10MB
LOG_BACKUP_COUNT = 3
COLOR_LOGS = True
LOG_MANAGER = None

def get_logger(name, filename=None, debug=False):
    global LOG_MANAGER
    if LOG_MANAGER is None:
        LOG_MANAGER = LogManager()
    return LOG_MANAGER.get_logger(name, filename, debug)


def pretty_print(msg):
    out = PrettyLogger.pretty_format(msg)
    print(out)

pp = pretty_print


def pretty_format(msg):
    return PrettyLogger.pretty_format(msg)


def color_print(msg, color, end="\n"):
    ConsoleLogger.print_message(msg, color, end)


# Convenience logging functions with automatic logger routing
_mojo_logger = None
_debug_logger = None
_error_logger = None

def info(*args):
    """Log info messages to mojo.log"""
    global _mojo_logger
    if _mojo_logger is None:
        _mojo_logger = get_logger("mojo", "mojo.log")
    _mojo_logger.info(*args)

def warn(*args):
    """Log warning messages to mojo.log"""
    global _mojo_logger
    if _mojo_logger is None:
        _mojo_logger = get_logger("mojo", "mojo.log")
    _mojo_logger.warning(*args)

def warning(*args):
    """Log warning messages to mojo.log"""
    global _mojo_logger
    if _mojo_logger is None:
        _mojo_logger = get_logger("mojo", "mojo.log")
    _mojo_logger.warning(*args)

def debug(*args):
    """Log debug messages to debug.log"""
    global _debug_logger
    if _debug_logger is None:
        _debug_logger = get_logger("debug", "debug.log", debug=True)
    _debug_logger.info(*args)

def error(*args):
    """Log error messages to error.log"""
    global _error_logger
    if _error_logger is None:
        _error_logger = get_logger("error", "error.log")
    _error_logger.error(*args)

def exception(*args):
    """Log exception messages to error.log"""
    global _error_logger
    if _error_logger is None:
        _error_logger = get_logger("error", "error.log")
    _error_logger.exception(*args)


# Mask sensitive data in the log
def mask_sensitive_data(text):
    sensitive_patterns = [
        r'("?(password|pwd|secret|token|access_token|api_key|authorization)"?\s*[:=]\s*"?)[^",\s]+',
        r'("?(ssn|credit_card|card_number|pin|cvv)"?\s*[:=]\s*"?)[^",\s]+',
    ]
    for pattern in sensitive_patterns:
        text = re.sub(pattern, r'\1*****', text, flags=re.IGNORECASE)
    return text

# Utility: Thread-safe lock handler
class ThreadSafeLock:
    def __init__(self):
        self.lock = threading.RLock()

    def __enter__(self):
        self.lock.acquire()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.lock.release()


# Log Manager to Handle Multiple Loggers
class LogManager:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(LogManager, cls).__new__(cls)
            cls._instance.loggers = {}
            cls._instance.streams = {}
            cls._instance.master_logger = None
            cls._instance.lock = ThreadSafeLock()
        return cls._instance

    def get_logger(self, name, filename=None, debug=False):
        """Retrieve or create a logger."""
        with self.lock:
            if name in self.loggers:
                return self.loggers[name]

            level = logging.DEBUG if debug else logging.INFO
            logger = logging.getLogger(name)
            logger.setLevel(level)

            # Create file handler (if we have permission to write logs)
            if filename and LOG_DIR:
                try:
                    log_path = os.path.join(LOG_DIR, filename)
                    file_handler = logging.FileHandler(log_path)
                    file_handler.setFormatter(self._get_formatter())
                    logger.addHandler(file_handler)
                except (OSError, PermissionError) as e:
                    # Can't write to log file - fall back to console only
                    sys.stderr.write(f"Warning: Cannot write to log file {filename}: {e}\n")
                    console_handler = logging.StreamHandler(sys.stderr)
                    console_handler.setFormatter(self._get_formatter())
                    logger.addHandler(console_handler)
            else:
                # No LOG_DIR or no filename - use console
                console_handler = logging.StreamHandler(sys.stderr)
                console_handler.setFormatter(self._get_formatter())
                logger.addHandler(console_handler)

            # Capture to master logger if exists
            if self.master_logger:
                logger.addHandler(logging.StreamHandler(sys.stdout))

            self.loggers[name] = Logger(name, filename, logger)
            return self.loggers[name]

    def set_master_logger(self, logger: logging.Logger):
        """Assign master logger for global logging."""
        with self.lock:
            self.master_logger = logger

    def _get_formatter(self) -> logging.Formatter:
        return logging.Formatter("%(asctime)s - %(levelname)s - %(name)s: %(message)s")


# Logger Wrapper
class Logger:
    def __init__(self, name, filename, logger):
        self.name = name
        self.filename = filename
        self.logger = logger

    def _build_log(self, *args):
        output = []
        for arg in args:
            if isinstance(arg, dict):
                output.append("")
                output.append(PrettyLogger.pretty_format(arg))
            else:
                output.append(str(arg))
        return "\n".join(output)

    def info(self, *args):
        self.logger.info(self._build_log(*args))

    def debug(self, *args):
        self.logger.debug(self._build_log(*args))

    def warning(self, *args):
        self.logger.warning(self._build_log(*args))

    def warn(self, *args):
        self.logger.warning(self._build_log(*args))

    def error(self, *args):
        self.logger.error(self._build_log(*args))

    def critical(self, *args):
        self.logger.critical(self._build_log(*args))

    def exception(self, *args):
        exc_info = sys.exc_info()
        err = None
        if exc_info:
            err = {
                "type": str(exc_info[0]),
                "message": str(exc_info[1]),
                "stack_trace": traceback.format_exception(*exc_info)
            }
            pretty_trace = PrettyLogger.pretty_format(err)
            args = (pretty_trace, *args)
        self.logger.exception(self._build_log(*args))
        return err

# Log Formatting with Colors
class ColorFormatter(logging.Formatter):
    COLORS = {
        "DEBUG": "\033[34m",  # Blue
        "INFO": "\033[32m",  # Green
        "WARNING": "\033[33m",  # Yellow
        "ERROR": "\033[31m",  # Red
        "CRITICAL": "\033[35m",  # Pink
        "BLUE": "\033[34m",  # Blue
        "GREEN": "\033[32m",  # Green
        "YELLOW": "\033[33m",  # Yellow
        "RED": "\033[31m",  # Red
        "PINK": "\033[35m",  # Pink
    }
    RESET = "\033[0m"

    def format(self, record):
        log_color = self.COLORS.get(record.levelname, self.RESET)
        return f"{log_color}{super().format(record)}{self.RESET}"


# Utility for Pretty Logging
class PrettyLogger:
    @staticmethod
    def pretty_format(data, max_length=500000) -> str:
        """Formats complex data structures for logging."""
        output = StringIO()
        PrettyLogger._recursive_format(data, output, 0, max_length)
        return output.getvalue()

    @staticmethod
    def _recursive_format(data, output=sys.stdout, indent=0, max_length=80):
        """Recursive function to pretty-print dictionaries and lists with proper indentation and colors."""

        base_indent = " " * indent  # Current level indentation
        next_indent = " " * (indent + 2)  # Indentation for nested structures

        if isinstance(data, dict):
            data = OrderedDict(sorted(data.items()))  # Ensure ordered keys
            output.write("{\n")  # Open dict at current indent
            last_index = len(data) - 1
            for i, (key, value) in enumerate(data.items()):
                output.write(next_indent + f"\033[34m\"{key}\"\033[0m: ")
                PrettyLogger._recursive_format(value, output, indent + 2, max_length)
                if i != last_index:
                    output.write(",")  # Add comma for all but last key-value pair
                output.write("\n")
            if indent == 0:
                base_indent = ""
            output.write(base_indent + "}")  # Close dict at the correct indent
        elif isinstance(data, list):
            output.write("[\n")  # Open list at correct indent
            last_index = len(data) - 1
            for i, item in enumerate(data):
                output.write(next_indent)
                PrettyLogger._recursive_format(item, output, indent + 2, max_length)
                if i != last_index:
                    output.write(",")  # Add comma for all but last item
                output.write("\n")
            output.write(base_indent + "]")  # Close list at correct indent
        elif isinstance(data, Decimal):
            output.write(f"\033[32m{str(data)}\033[0m")  # Green for Decimal
        elif isinstance(data, str):
            output.write(f"\033[31m\"{data}\"\033[0m")  # Red for strings
        else:
            output.write(f"\033[33m{str(data)}\033[0m")  # Yellow for other types

    @staticmethod
    def log_json(data, logger=None):
        """Logs data in JSON format."""
        if logger is None:
            logger = Logger("root")
        formatted_data = PrettyLogger.pretty_format(data)
        logger.info(formatted_data)


# Console Logger Utility
class ConsoleLogger:
    BLACK = "\033[30m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    PINK = "\033[35m"
    BLUE = "\033[34m"
    WHITE = "\033[37m"

    HBLACK = "\033[90m"
    HRED = "\033[91m"
    HGREEN = "\033[92m"
    HYELLOW = "\033[93m"
    HBLUE = "\033[94m"
    HPINK = "\033[95m"
    HWHITE = "\033[97m"

    HEADER = "\033[95m"
    FAIL = "\033[91m"
    OFF = "\033[0m"
    BOLD = "\033[1m"
    UNDERLINE = "\033[4m"

    @staticmethod
    def print_message(msg, color_code="\033[32m", end="\n"):
        """Prints a color-coded message to the console."""
        sys.stdout.write(f"{color_code}{msg}\033[0m{end}")
        sys.stdout.flush()


# Rotating File Handler
class RotatingLogger:
    def __init__(self, log_file="app.log", max_bytes=MAX_LOG_SIZE, backup_count=LOG_BACKUP_COUNT):
        self.logger = logging.getLogger("RotatingLogger")
        self.logger.setLevel(logging.INFO)

        handler = logging.handlers.RotatingFileHandler(
            os.path.join(LOG_DIR, log_file),
            maxBytes=max_bytes,
            backupCount=backup_count,
        )
        handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
        self.logger.addHandler(handler)

    def log(self, message, level=logging.INFO):
        self.logger.log(level, message)


# Usage Example
if __name__ == "__main__":
    print(BASE_DIR)
    log = Logger("AppLogger", "app.log", debug=True)
    log.info("🚀 Application started successfully!")
    log.debug("🔍 Debugging mode enabled")
    log.warning("⚠️ Warning: Low disk space")
    log.error("❌ An error occurred while processing request")
    log.critical("🔥 Critical system failure!")

    # Pretty print a dictionary
    sample_data = {
        "user": "John Doe",
        "email": "john.doe@example.com",
        "permissions": ["read", "write"],
        "settings": {"theme": "dark", "notifications": True},
    }
    PrettyLogger.log_json(sample_data, log)

    # Console logger
    ConsoleLogger.print_message("✔ Task completed successfully", "\033[32m")
