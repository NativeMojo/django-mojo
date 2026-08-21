"""Database routing helpers for django-mojo applications."""

from mojo.db.pinning import use_primary, use_reader

__all__ = ["use_primary", "use_reader"]
