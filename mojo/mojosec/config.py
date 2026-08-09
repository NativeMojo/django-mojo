"""Strict, settings-free MojoSec JSON configuration."""

import json
import os
import re
import stat
import urllib.parse


CONFIG_VERSION = 1
MAX_CONFIG_BYTES = 256 * 1024

DEFAULTS = {
    "version": CONFIG_VERSION,
    "policy_revision": "",
    "state_dir": "/var/lib/mojosec",
    "status_path": "/run/mojosec/status.json",
    "credential_path": "/etc/mojosec/credential",
    "poll_seconds": 5,
    "collectors": {
        "journal": {
            "enabled": True,
            "max_lines": 2000,
            "timeout_seconds": 10,
            "lookback_seconds": 300,
        },
        "nginx": {
            "enabled": True,
            "paths": ["/var/log/nginx/mojosec.json.log"],
            "max_bytes_per_poll": 2 * 1024 * 1024,
            "max_line_bytes": 16 * 1024,
        },
        "fim": {
            "enabled": True,
            "targets": [],
            "interval_seconds": 60,
            "max_entries": 20000,
            "max_file_bytes": 16 * 1024 * 1024,
        },
    },
    "aggregation": {
        "window_seconds": 60,
        "flush_count": 25,
        "max_aggregates": 10000,
    },
    "delivery": {
        "batch_events": 100,
        "batch_bytes": 256 * 1024,
        "timeout_seconds": 15,
        "retry_min_seconds": 5,
        "retry_max_seconds": 300,
        "gzip": True,
        "max_spool_events": 50000,
        "critical_reserve_events": 1000,
    },
}

_TOP_KEYS = set(DEFAULTS) | {"sensor_id", "endpoint"}
_COLLECTOR_KEYS = set(DEFAULTS["collectors"])
_TARGET_KEYS = {"path", "recursive", "exclude"}
_SENSOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class ConfigError(ValueError):
    pass


def _copy(value):
    if isinstance(value, dict):
        return {key: _copy(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_copy(child) for child in value]
    return value


def _reject_unknown(value, allowed, label):
    if not isinstance(value, dict):
        raise ConfigError(f"{label} must be an object")
    unknown = set(value) - set(allowed)
    if unknown:
        raise ConfigError(f"{label} has unknown fields: {', '.join(sorted(unknown))}")


def _merge(defaults, supplied):
    result = _copy(defaults)
    for key, value in supplied.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = _copy(value)
    return result


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError(f"config contains duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value):
    raise ConfigError(f"config contains non-finite number: {value}")


def _integer(value, label, minimum, maximum):
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ConfigError(f"{label} must be an integer from {minimum} to {maximum}")


def _absolute(value, label):
    if not isinstance(value, str) or not os.path.isabs(value) or "\x00" in value:
        raise ConfigError(f"{label} must be an absolute path")


def validate_config(value):
    _reject_unknown(value, _TOP_KEYS, "config")
    if value.get("version") != CONFIG_VERSION:
        raise ConfigError(f"config version must be {CONFIG_VERSION}")
    sensor_id = value.get("sensor_id")
    if not isinstance(sensor_id, str) or not _SENSOR_RE.fullmatch(sensor_id):
        raise ConfigError("sensor_id is required and may contain letters, numbers, . _ : -")
    endpoint = value.get("endpoint")
    if not isinstance(endpoint, str) or len(endpoint) > 2048:
        raise ConfigError("endpoint must be an https URL")
    parsed_endpoint = urllib.parse.urlsplit(endpoint)
    if (parsed_endpoint.scheme != "https" or not parsed_endpoint.hostname or
            parsed_endpoint.username or parsed_endpoint.password or parsed_endpoint.fragment):
        raise ConfigError("endpoint must be an https URL without credentials or a fragment")
    if parsed_endpoint.path.rstrip("/") != "/api/incident/mojosec/batch":
        raise ConfigError("endpoint must target /api/incident/mojosec/batch")
    for field in ("state_dir", "status_path", "credential_path"):
        _absolute(value[field], field)
    if value["status_path"].startswith(value["state_dir"].rstrip("/") + "/"):
        raise ConfigError("status_path must not expose the private state directory")
    _integer(value["poll_seconds"], "poll_seconds", 1, 3600)

    collectors = value["collectors"]
    _reject_unknown(collectors, _COLLECTOR_KEYS, "collectors")
    for name in _COLLECTOR_KEYS:
        if name not in collectors:
            raise ConfigError(f"collectors.{name} is required")
        _reject_unknown(collectors[name], DEFAULTS["collectors"][name], f"collectors.{name}")
        if not isinstance(collectors[name]["enabled"], bool):
            raise ConfigError(f"collectors.{name}.enabled must be a boolean")
    _integer(collectors["journal"]["max_lines"], "collectors.journal.max_lines", 1, 20000)
    _integer(collectors["journal"]["timeout_seconds"], "collectors.journal.timeout_seconds", 1, 120)
    _integer(collectors["journal"]["lookback_seconds"], "collectors.journal.lookback_seconds", 1, 86400)
    nginx = collectors["nginx"]
    if not isinstance(nginx["paths"], list) or not nginx["paths"] or len(nginx["paths"]) > 32:
        raise ConfigError("collectors.nginx.paths must contain 1-32 paths")
    for path in nginx["paths"]:
        _absolute(path, "collectors.nginx.paths[]")
    _integer(nginx["max_bytes_per_poll"], "collectors.nginx.max_bytes_per_poll", 4096, 64 * 1024 * 1024)
    _integer(nginx["max_line_bytes"], "collectors.nginx.max_line_bytes", 512, 1024 * 1024)

    fim = collectors["fim"]
    if not isinstance(fim["targets"], list) or len(fim["targets"]) > 128:
        raise ConfigError("collectors.fim.targets must be a list with at most 128 entries")
    seen = set()
    for target in fim["targets"]:
        _reject_unknown(target, _TARGET_KEYS, "collectors.fim.targets[]")
        _absolute(target.get("path"), "collectors.fim.targets[].path")
        if target["path"] in seen:
            raise ConfigError(f"duplicate FIM target: {target['path']}")
        seen.add(target["path"])
        if "recursive" in target and not isinstance(target["recursive"], bool):
            raise ConfigError("FIM target recursive must be a boolean")
        excludes = target.get("exclude", [])
        if not isinstance(excludes, list) or len(excludes) > 64:
            raise ConfigError("FIM target exclude must be a list with at most 64 entries")
        for pattern in excludes:
            if not isinstance(pattern, str) or not pattern or len(pattern) > 256:
                raise ConfigError("FIM excludes must be non-empty strings up to 256 characters")
    _integer(fim["interval_seconds"], "collectors.fim.interval_seconds", 5, 86400)
    _integer(fim["max_entries"], "collectors.fim.max_entries", 1, 1000000)
    _integer(fim["max_file_bytes"], "collectors.fim.max_file_bytes", 0, 1024 * 1024 * 1024)

    aggregation = value["aggregation"]
    _reject_unknown(aggregation, DEFAULTS["aggregation"], "aggregation")
    _integer(aggregation["window_seconds"], "aggregation.window_seconds", 1, 86400)
    _integer(aggregation["flush_count"], "aggregation.flush_count", 1, 1000000)
    _integer(aggregation["max_aggregates"], "aggregation.max_aggregates", 100, 10000000)

    delivery = value["delivery"]
    _reject_unknown(delivery, DEFAULTS["delivery"], "delivery")
    for name in ("batch_events", "batch_bytes", "timeout_seconds", "retry_min_seconds",
                 "retry_max_seconds", "max_spool_events", "critical_reserve_events"):
        limits = {
            "batch_events": (1, 500), "batch_bytes": (16 * 1024, 512 * 1024),
            "timeout_seconds": (1, 120), "retry_min_seconds": (1, 3600),
            "retry_max_seconds": (1, 86400), "max_spool_events": (100, 10000000),
            "critical_reserve_events": (0, 1000000),
        }[name]
        _integer(delivery[name], f"delivery.{name}", *limits)
    if not isinstance(delivery["gzip"], bool):
        raise ConfigError("delivery.gzip must be a boolean")
    if delivery["retry_max_seconds"] < delivery["retry_min_seconds"]:
        raise ConfigError("delivery.retry_max_seconds must be at least retry_min_seconds")
    if delivery["critical_reserve_events"] >= delivery["max_spool_events"]:
        raise ConfigError("delivery.critical_reserve_events must be less than max_spool_events")
    return value


def load_config(path):
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
    except OSError as err:
        raise ConfigError(f"cannot open config {path}: {err}") from err
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ConfigError("config must be a regular file, not a symlink or device")
        if info.st_size > MAX_CONFIG_BYTES:
            raise ConfigError("config file is too large")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = None
            supplied = json.load(
                handle, object_pairs_hook=_strict_object, parse_constant=_reject_constant
            )
    except (OSError, UnicodeError, json.JSONDecodeError) as err:
        raise ConfigError(f"cannot read config {path}: {err}") from err
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not isinstance(supplied, dict):
        raise ConfigError("config must be a JSON object")
    merged = _merge(DEFAULTS, supplied)
    return validate_config(merged)


def check_file_security(path, require_root=False):
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    problems = []
    if not stat.S_ISREG(info.st_mode):
        problems.append("not a regular file")
    if info.st_mode & 0o077:
        problems.append("permissions must be 0600 or stricter")
    if require_root and info.st_uid != 0:
        problems.append("must be owned by root")
    return problems
