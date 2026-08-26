"""Strict, nonsecret PostgreSQL application-name identity for pooled workers."""

import hashlib
import os
import re


_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_FIELDS = ("project", "node", "application", "deployment")
_MAX_APPLICATION_NAME = 63


class PoolIdentityError(ValueError):
    """Pool identity is missing, ambiguous, or unsafe for PostgreSQL."""


def validate_identity(identity):
    if not isinstance(identity, dict):
        raise PoolIdentityError("DATABASE_POOL_IDENTITY must be a dictionary")
    if set(identity) != set(_FIELDS):
        raise PoolIdentityError(
            "DATABASE_POOL_IDENTITY must contain exactly " + ", ".join(_FIELDS))
    clean = {}
    for field in _FIELDS:
        value = identity.get(field)
        if not isinstance(value, str) or not _COMPONENT.fullmatch(value):
            raise PoolIdentityError(
                f"DATABASE_POOL_IDENTITY.{field} must use bounded printable identity characters")
        clean[field] = value
    return clean


def application_name(identity, role, alias, pid=None):
    clean = validate_identity(identity)
    process_id = os.getpid() if pid is None else pid
    raw = "|".join([
        "mojo",
        clean["project"],
        clean["node"],
        str(role),
        str(alias),
        clean["application"],
        clean["deployment"],
        f"p{process_id}",
    ])
    if len(raw.encode("ascii")) <= _MAX_APPLICATION_NAME:
        return raw
    digest = hashlib.sha256(raw.encode("ascii")).hexdigest()[:12]
    prefix_budget = _MAX_APPLICATION_NAME - len(digest) - 1
    return raw[:prefix_budget] + "|" + digest
