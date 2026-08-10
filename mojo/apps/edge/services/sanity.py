"""Reusable node sanity checks shared by HTTP setup and ``sanity_check``."""

import time

import requests
from django.apps import apps
from django.conf import settings as django_settings
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


class SanityFailure(Exception):
    def __init__(self, check, detail):
        self.check = check
        self.detail = detail
        super().__init__(detail)


def check_apps(options=None):
    apps.check_apps_ready()


def check_database(options=None):
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()


def check_migrations(options=None):
    options = options or {}
    executor_cls = options.get("migration_executor_cls") or MigrationExecutor
    executor = executor_cls(connection)
    plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
    if plan:
        raise RuntimeError(
            f"{len(plan)} unapplied migration(s) — the schema does not match this code")


def check_static_directories(options=None):
    from pathlib import Path
    missing = []
    for path in getattr(django_settings, "STATICFILES_DIRS", []):
        if not Path(path).exists():
            missing.append(str(path))
    static_root = getattr(django_settings, "STATIC_ROOT", None)
    if static_root:
        if not Path(static_root).exists():
            missing.append(str(static_root))
    if missing:
        raise RuntimeError(f"missing static director{'y' if len(missing) == 1 else 'ies'}")


def check_redis(options=None):
    from mojo.helpers.redis import get_client
    get_client().ping()


def check_request(options=None):
    options = options or {}
    url = options.get("url", "http://127.0.0.1/api/version")
    timeout = float(options.get("timeout", 5.0))
    retries = max(1, int(options.get("retries", 10)))
    delay = max(0, float(options.get("delay", 2.0)))
    last_detail = "no attempt made"
    for _ in range(retries):
        try:
            response = requests.get(url, timeout=timeout)
            if response.status_code == 200:
                return
            last_detail = f"HTTP {response.status_code} from local API"
        except requests.RequestException as exc:
            last_detail = exc.__class__.__name__
        if delay:
            time.sleep(delay)
    raise RuntimeError(last_detail)


CHECKS = (
    ("django apps", check_apps),
    ("database", check_database),
    ("migrations", check_migrations),
    ("redis", check_redis),
    ("local request", check_request),
)


def run(options=None, stop_on_failure=False, on_pass=None):
    """Run ordered checks and return safe result dictionaries."""
    results = []
    for name, func in CHECKS:
        try:
            func(options or {})
            results.append({"name": name, "ok": True, "detail": "ready"})
            if on_pass:
                on_pass(name)
        except Exception as exc:
            detail = str(exc)[:240] if isinstance(exc, RuntimeError) else exc.__class__.__name__
            results.append({"name": name, "ok": False, "detail": detail})
            if stop_on_failure:
                raise SanityFailure(name, detail)
    return results
