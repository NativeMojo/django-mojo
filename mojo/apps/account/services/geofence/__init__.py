"""Geofence policy engine — DSL rules + decision engine + Redis cache.

System rules (settings.GEOFENCE_SYSTEM_RULES) act as a hard floor.
Group rules (Group.metadata['geofence']) layer on top — can further restrict
but cannot loosen the system rules.

Default behavior is "no geofencing" — when both rule sets are empty, the
engine returns `allowed=True` without performing a geoip lookup.

Enforcement is via `@md.requires_geofence` (see mojo.decorators.geofence).
The pre-flight endpoint at `GET /api/geo/check` lets UIs render
"not available in your region" pages before the user attempts to log in.
"""
from .dsl import evaluate_rule, validate_rule
from .engine import GeoFenceEngine, GeoDecision
from . import enforcement

__all__ = [
    "GeoFenceEngine",
    "GeoDecision",
    "evaluate_rule",
    "validate_rule",
    "enforcement",
]
