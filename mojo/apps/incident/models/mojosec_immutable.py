import hashlib
import json
import secrets

from django.db import models


class MojoSecImmutableQuerySet(models.QuerySet):
    """Application-facing guard for append-only learning history."""

    def _refuse(self):
        raise ValueError("MojoSec learning history is immutable")

    def update(self, **kwargs):
        self._refuse()

    def delete(self):
        self._refuse()

    def bulk_create(self, objs, **kwargs):
        self._refuse()

    def bulk_update(self, objs, fields, **kwargs):
        self._refuse()


class MojoSecImmutableManager(models.Manager.from_queryset(MojoSecImmutableQuerySet)):
    pass


def canonical_digest(value):
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def assert_canonical_digest(value, expected, label):
    actual = canonical_digest(value)
    if not isinstance(expected, str) or not secrets.compare_digest(actual, expected):
        raise ValueError(f"{label} failed its immutable row integrity check")
