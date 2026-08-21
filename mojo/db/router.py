"""Django database router for an optional read replica."""

from django.db import connections

from mojo.db import pinning


class ReaderRouter:
    def db_for_read(self, model, **hints):
        if model._meta.app_label == "sessions":
            return "default"

        forced = pinning.forced_database()
        if forced == "primary":
            return "default"
        if pinning.is_pinned():
            return "default"

        in_atomic_block = connections["default"].in_atomic_block
        if forced == "reader":
            return "default" if in_atomic_block else "reader"
        if not pinning.is_active():
            return "default"
        if in_atomic_block:
            return "default"
        return "reader"

    def db_for_write(self, model, **hints):
        pinning.pin()
        return "default"

    def allow_relation(self, obj1, obj2, **hints):
        aliases = {"default", "reader"}
        db1 = getattr(getattr(obj1, "_state", None), "db", None)
        db2 = getattr(getattr(obj2, "_state", None), "db", None)
        if db1 in aliases and db2 in aliases:
            return True
        return None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        return db == "default"
