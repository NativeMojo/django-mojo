"""
Generic aggregation surface for MojoModel list endpoints.

When a list endpoint receives `?_mode=count|top|distinct|summary|histogram`,
`on_rest_list` branches here instead of paginating records. The queryset
passed in has already had permissions, group scope, field filters, and
date-range filters applied — this module never re-implements any of that.

Public entry point: `on_rest_list_aggregate(cls, request, queryset)`.

Reserved query-param prefix: every key starting with `_` is consumed by
the aggregation layer; downstream apps must not invent their own.
"""
import datetime
import json
import time

from django.core.exceptions import FieldError, ValidationError
from django.db import Error as DBError, models as dm
from django.db.models import Count, Sum, Avg, Min, Max
from django.db.models.functions import (
    TruncMinute, TruncHour, TruncDay, TruncWeek, TruncMonth,
)

from mojo.helpers import logit
from mojo.helpers.response import JsonResponse
from mojo.helpers.settings import settings
from mojo import errors as me


VALID_MODES = ("list", "count", "top", "distinct", "summary", "histogram")
VALID_AGGS = ("count", "sum", "avg", "min", "max")
VALID_BUCKETS = ("minute", "hour", "day", "week", "month")

NUMERIC_FIELD_TYPES = (
    dm.IntegerField, dm.BigIntegerField, dm.SmallIntegerField,
    dm.PositiveIntegerField, dm.PositiveSmallIntegerField, dm.PositiveBigIntegerField,
    dm.FloatField, dm.DecimalField,
)
DATETIME_FIELD_TYPES = (dm.DateTimeField, dm.DateField)
REJECTED_FIELD_TYPES = (dm.TextField, dm.JSONField, dm.EmailField)

AGG_FUNCS = {
    "count": Count,
    "sum": Sum,
    "avg": Avg,
    "min": Min,
    "max": Max,
}

TRUNC_FUNCS = {
    "minute": TruncMinute,
    "hour": TruncHour,
    "day": TruncDay,
    "week": TruncWeek,
    "month": TruncMonth,
}

BUCKET_DELTAS = {
    "minute": datetime.timedelta(minutes=1),
    "hour": datetime.timedelta(hours=1),
    "day": datetime.timedelta(days=1),
    "week": datetime.timedelta(weeks=1),
    # month handled separately — calendar-aware
}


# Server-side caps. Loaded at import; consumers can override via
# Django settings (or testit `th.server_settings`) without code changes.
def _cap(name, default):
    return int(settings.get_static(name, default))


TOP_CAP = _cap("MOJO_REST_AGG_TOP_CAP", 100)
DISTINCT_CAP = _cap("MOJO_REST_AGG_DISTINCT_CAP", 1000)
HISTOGRAM_CAP = _cap("MOJO_REST_AGG_HISTOGRAM_CAP", 10000)
# Max number of named filter bundles a single `_mode=count&_stats=...` request
# may ask for. Each bundle is one extra COUNT query on the scoped queryset.
STATS_CAP = _cap("MOJO_REST_AGG_STATS_CAP", 12)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def on_rest_list_aggregate(cls, request, queryset):
    mode = request.DATA.get("_mode")
    if mode not in VALID_MODES or mode == "list":
        raise me.ValueException(
            f"unknown _mode: {mode!r}. valid: {', '.join(VALID_MODES)}",
        )

    started = time.perf_counter()
    if mode == "count":
        body = _agg_count(cls, request, queryset)
    elif mode == "top":
        body = _agg_top(cls, request, queryset)
    elif mode == "distinct":
        body = _agg_distinct(cls, request, queryset)
    elif mode == "summary":
        body = _agg_summary(cls, request, queryset)
    else:  # histogram
        body = _agg_histogram(cls, request, queryset)

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    # Round to 10ms to dampen timing-oracle inference on filter-match counts.
    body["took_ms"] = (elapsed_ms // 10) * 10
    body.setdefault("status", True)
    return JsonResponse(body)


# ---------------------------------------------------------------------------
# Mode handlers
# ---------------------------------------------------------------------------

def _agg_count(cls, request, queryset):
    body = {"count": queryset.count()}
    bundles = _parse_stats_bundles(request)
    if bundles is not None:
        body["stats"] = _count_bundles(cls, request, queryset, bundles)
    return body


def _parse_stats_bundles(request):
    """Parse + validate the optional ``_stats`` param (count mode only).

    ``_stats`` maps chip names to filter-param bundles, e.g.
    ``{"open": {"status": "open"}, "high": {"priority__gt": 7}}``. Each bundle's
    count is evaluated AND-ed onto the already-scoped queryset by
    ``_count_bundles``.

    Returns the bundle dict, or ``None`` when ``_stats`` is absent. Structural
    problems (bad JSON, wrong shape, too many bundles) raise ``ValueException``
    (400) — a malformed request is a caller bug and should fail loud. A bad
    *value* inside an otherwise well-formed bundle is NOT rejected here; it is
    handled per-bundle as a null count in ``_count_bundles`` so one broken chip
    never fails the whole strip.
    """
    raw = request.DATA.get("_stats")
    if raw is None or raw == "":
        return None
    # A query param arrives as a JSON string; a JSON request body arrives as a
    # dict already. Accept both. Never objict.from_json(..., ignore_errors) —
    # that would mask a malformed payload as an empty object (see DM-023).
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raise me.ValueException(
                "_stats must be a JSON object mapping bundle names to filter objects",
            )
    if not isinstance(raw, dict):
        raise me.ValueException(
            "_stats must be a JSON object mapping bundle names to filter objects",
        )
    if len(raw) > STATS_CAP:
        raise me.ValueException(
            f"_stats supports at most {STATS_CAP} bundles",
        )
    for name, params in raw.items():
        if not isinstance(name, str) or not name or len(name) > 64:
            raise me.ValueException(
                "_stats bundle names must be non-empty strings of at most 64 characters",
            )
        if not isinstance(params, dict):
            raise me.ValueException(
                f"_stats bundle {name!r} must map to a filter object",
            )
    return raw


def _count_bundles(cls, request, queryset, bundles):
    """Evaluate each named bundle as a filtered count on the already-scoped,
    already-filtered queryset.

    Reuses the list-endpoint filter parser (``cls.build_rest_filters``) so a
    bundle's semantics match the equivalent list query params exactly — the
    count equals what the caller would see after clicking the chip. A bundle
    that fails to build or evaluate yields ``null``: a single bad chip must
    never fail the whole strip (the frontend renders that chip label-only).
    """
    stats = {}
    for name, params in bundles.items():
        try:
            filters, excludes = cls.build_rest_filters(request, params)
            qs = queryset.filter(**filters)
            if excludes:
                qs = qs.exclude(**excludes)
            stats[name] = qs.count()
        except (me.ValueException, FieldError, ValidationError,
                ValueError, TypeError, AttributeError, DBError) as err:
            # Soft per-bundle failure: a single bad chip must never fail the
            # whole strip. Covers build-time errors (bad value / field / type)
            # AND DB-execution errors (invalid regex, numeric overflow,
            # unsupported lookup) raised at .count() — those escape the
            # Python-layer types, so DBError is required to honor the contract.
            # Deliberately narrow to ValueException, NOT the MojoException base:
            # a permission-class error from a model override must surface as a
            # denial, never be silently converted to a null count. Logged at
            # debug so a systemic breakage (e.g. a renamed field nulling every
            # bundle) is observable rather than invisible.
            logit.debug(f"_stats bundle {name!r} failed: {err!r}")
            stats[name] = None
    return stats


def _agg_top(cls, request, queryset):
    field = _require_field(request, "_field")
    group_field = _validate_field(cls, field)

    agg_name = _resolve_agg(request.DATA.get("_agg", "count"))
    agg_field = request.DATA.get("_agg_field")
    if agg_name != "count":
        if not agg_field:
            raise me.ValueException(
                f"_agg_field required when _agg={agg_name}",
            )
        _validate_numeric_field(cls, agg_field)
        agg_target = agg_field
    else:
        agg_target = "id"

    size = _resolve_int(request.DATA.get("_size"), default=10, cap=TOP_CAP)
    min_count = _resolve_int(request.DATA.get("_min_count"), default=1, cap=None)

    annotations = {"value": AGG_FUNCS[agg_name](agg_target)}
    dt_field = _first_datetime_field(cls)
    if dt_field and dt_field != group_field:
        annotations["first_seen"] = Min(dt_field)
        annotations["last_seen"] = Max(dt_field)

    rows = (
        queryset.values(group_field)
        .annotate(**annotations)
        .filter(value__gte=min_count)
        .order_by("-value")[:size]
    )

    data = []
    for row in rows:
        item = {
            "key": _stringify_key(row[group_field]),
            "value": _coerce_value(row["value"]),
        }
        if "first_seen" in row and row["first_seen"] is not None:
            item["first_seen"] = _to_epoch(row["first_seen"])
        if "last_seen" in row and row["last_seen"] is not None:
            item["last_seen"] = _to_epoch(row["last_seen"])
        data.append(item)

    return {
        "graph": "top",
        "field": field,
        "agg": agg_name,
        "size": size,
        "data": data,
    }


def _agg_distinct(cls, request, queryset):
    field = _require_field(request, "_field")
    group_field = _validate_field(cls, field)
    min_count = _resolve_int(request.DATA.get("_min_count"), default=1, cap=None)

    # DB-level slice to DISTINCT_CAP+1 — bounds the DB's group-by work
    # so a malicious caller can't force unbounded materialization just
    # to be told the response is over the cap.
    rows = list(
        queryset.values(group_field)
        .annotate(value=Count("id"))
        .filter(value__gte=min_count)
        .order_by(group_field)[: DISTINCT_CAP + 1]
    )

    if len(rows) > DISTINCT_CAP:
        raise me.ValueException(
            f"distinct cardinality exceeds cap {DISTINCT_CAP}; "
            f"narrow the queryset with filters",
        )

    data = [
        {"key": _stringify_key(row[group_field]), "value": row["value"]}
        for row in rows
    ]
    return {
        "graph": "distinct",
        "field": field,
        "data": data,
    }


def _agg_summary(cls, request, queryset):
    agg_name = _resolve_agg(request.DATA.get("_agg", "count"))
    field = request.DATA.get("_field")
    agg_field = request.DATA.get("_agg_field") or field

    if agg_name == "count":
        # Count is row-count; no field arithmetic.
        if field:
            _validate_field(cls, field)
        target = "id"
        numeric_target = None
    else:
        if not agg_field:
            raise me.ValueException(
                f"_field or _agg_field required for _agg={agg_name}",
            )
        _validate_numeric_field(cls, agg_field)
        target = agg_field
        numeric_target = agg_field

    aggregates = {
        "value": AGG_FUNCS[agg_name](target),
        "n": Count("id"),
    }
    if numeric_target:
        aggregates["min"] = Min(numeric_target)
        aggregates["max"] = Max(numeric_target)

    result = queryset.aggregate(**aggregates)
    body = {
        "graph": "summary",
        "field": field or agg_field,
        "agg": agg_name,
        "value": _coerce_value(result.get("value")),
        "n": result.get("n", 0),
    }
    if numeric_target:
        body["min"] = _coerce_value(result.get("min"))
        body["max"] = _coerce_value(result.get("max"))
    return body


def _agg_histogram(cls, request, queryset):
    field = _require_field(request, "_field")
    bucket = request.DATA.get("_bucket")
    if bucket not in VALID_BUCKETS:
        raise me.ValueException(
            f"_bucket required for _mode=histogram; valid: {', '.join(VALID_BUCKETS)}",
        )
    if not _datetime_field(cls, field):
        raise me.ValueException(
            f"_field={field!r} is not a DateTimeField/DateField on {cls.__name__}",
        )

    # Resolve bucketing window. dr_start/dr_end have already been
    # applied to the queryset by on_rest_list_date_range_filter; we
    # re-parse them here only to bound the gap-fill walk. If absent,
    # fall back to Min/Max on the queryset itself.
    from mojo.helpers import dates as date_helpers
    dr_start_raw = request.DATA.get("dr_start")
    dr_end_raw = request.DATA.get("dr_end")
    dr_start = date_helpers.parse_datetime(dr_start_raw) if dr_start_raw else None
    dr_end = date_helpers.parse_datetime(dr_end_raw) if dr_end_raw else None

    if dr_start is None or dr_end is None:
        bounds = queryset.aggregate(_lo=Min(field), _hi=Max(field))
        if dr_start is None:
            dr_start = bounds.get("_lo")
        if dr_end is None:
            dr_end = bounds.get("_hi")

    if dr_start is None or dr_end is None:
        return {
            "graph": "histogram",
            "field": field,
            "bucket": bucket,
            "agg": "count",
            "data": [],
        }

    dr_start = _bucket_floor(dr_start, bucket)
    dr_end = _bucket_floor(dr_end, bucket)

    bucket_count = _estimate_bucket_count(dr_start, dr_end, bucket)
    if bucket_count > HISTOGRAM_CAP:
        raise me.ValueException(
            f"histogram window produces {bucket_count} buckets, exceeds cap "
            f"{HISTOGRAM_CAP}; pick a coarser _bucket",
        )

    trunc_kwargs = {"output_field": dm.DateTimeField()}
    rows = (
        queryset.annotate(_ts=TRUNC_FUNCS[bucket](field, **trunc_kwargs))
        .values("_ts")
        .annotate(value=Count("id"))
        .order_by("_ts")
    )
    counts_by_bucket = {}
    for row in rows:
        ts = row["_ts"]
        if ts is None:
            continue
        counts_by_bucket[_to_epoch(_bucket_floor(ts, bucket))] = row["value"]

    data = []
    cursor = dr_start
    while cursor <= dr_end:
        epoch = _to_epoch(cursor)
        data.append({"ts": epoch, "value": counts_by_bucket.get(epoch, 0)})
        cursor = _next_bucket(cursor, bucket)

    return {
        "graph": "histogram",
        "field": field,
        "bucket": bucket,
        "agg": "count",
        "data": data,
    }


# ---------------------------------------------------------------------------
# Validation / helpers
# ---------------------------------------------------------------------------

def _require_field(request, param):
    value = request.DATA.get(param)
    if not value:
        raise me.ValueException(f"{param} required")
    return value


def _validate_field(cls, name):
    """Validate `_field` / `_agg_field` and return the ORM field path.

    Raises 400 on relation without `__id`, JSON-path drilling,
    text/JSON/email types, sensitive fields, or anything outside an
    `AGGREGATION_FIELDS` allow-list when the model defines one.

    Precedence (each gate fires independently — `AGGREGATION_FIELDS`
    is an *additional* restriction, not an override):
      1. Field exists on model.
      2. Relation fields require `__id` (else 400).
      3. Non-relation fields cannot use `__` (else 400).
      4. Type-based reject: TextField, JSONField, EmailField (else 400).
      5. `RestMeta.SENSITIVE_FIELDS` reject (else 400).
      6. `RestMeta.AGGREGATION_FIELDS` allow-list (when defined; else 400).

    A model author cannot use `AGGREGATION_FIELDS` to *grant* access
    to a TextField — gate (4) fires first. The allow-list narrows the
    set of aggregatable fields below the type-based default; it does
    not widen it.

    Returns the (possibly relation `__id`-suffixed) ORM lookup path.
    """
    if not hasattr(cls, "__rest_field_names__"):
        cls.__rest_field_names__ = [f.name for f in cls._meta.get_fields()]

    parts = name.split("__")
    base = parts[0]

    if base not in cls.__rest_field_names__:
        raise me.ValueException(f"_field={name!r} is not a field of {cls.__name__}")

    try:
        field_obj = cls._meta.get_field(base)
    except Exception:
        raise me.ValueException(f"_field={name!r} is not a field of {cls.__name__}")

    if field_obj.is_relation:
        # Only `<relation>__id` is allowed for FK fields.
        if len(parts) == 1 or parts[1:] != ["id"]:
            raise me.ValueException(
                f"_field={name!r}: relation fields require '__id' suffix "
                f"(use {base}__id)",
            )
    else:
        # Non-relation fields cannot have any `__` drilling
        # (blocks JSON-path extraction like metadata__rule_id).
        if len(parts) > 1:
            raise me.ValueException(
                f"_field={name!r}: non-relation fields cannot use '__' suffix",
            )
        if isinstance(field_obj, REJECTED_FIELD_TYPES):
            raise me.ValueException(
                f"_field={name!r} ({type(field_obj).__name__}) is not aggregatable; "
                f"use a categorical or numeric column",
            )

    rest_meta = getattr(cls, "RestMeta", None)
    sensitive = getattr(rest_meta, "SENSITIVE_FIELDS", None) if rest_meta else None
    if sensitive and base in sensitive:
        raise me.ValueException(
            f"_field={name!r} is marked sensitive on {cls.__name__}",
        )

    allowlist = getattr(rest_meta, "AGGREGATION_FIELDS", None) if rest_meta else None
    if allowlist is not None and base not in allowlist and name not in allowlist:
        raise me.ValueException(
            f"_field={name!r} is not in AGGREGATION_FIELDS allow-list of {cls.__name__}",
        )

    return name


def _validate_numeric_field(cls, name):
    _validate_field(cls, name)
    base = name.split("__")[0]
    try:
        field_obj = cls._meta.get_field(base)
    except Exception:
        raise me.ValueException(f"_agg_field={name!r} is not a field of {cls.__name__}")
    # FK __id is integer-typed.
    if name.endswith("__id"):
        return
    if not isinstance(field_obj, NUMERIC_FIELD_TYPES):
        raise me.ValueException(
            f"_agg_field={name!r} ({type(field_obj).__name__}) is not numeric; "
            f"sum/avg/min/max require a numeric column",
        )


def _resolve_agg(name):
    if name not in VALID_AGGS:
        raise me.ValueException(
            f"_agg={name!r} invalid; valid: {', '.join(VALID_AGGS)}",
        )
    return name


def _resolve_int(value, default, cap):
    if value is None or value == "":
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise me.ValueException(f"expected integer, got {value!r}")
    if n < 0:
        raise me.ValueException(f"expected non-negative integer, got {n}")
    if cap is not None and n > cap:
        return cap
    return n


def _datetime_field(cls, name):
    parts = name.split("__")
    if len(parts) > 1:
        return False
    try:
        field_obj = cls._meta.get_field(parts[0])
    except Exception:
        return False
    return isinstance(field_obj, DATETIME_FIELD_TYPES)


def _first_datetime_field(cls):
    if "created" in getattr(cls, "__rest_field_names__", []):
        try:
            f = cls._meta.get_field("created")
            if isinstance(f, DATETIME_FIELD_TYPES):
                return "created"
        except Exception:
            pass
    for field in cls._meta.get_fields():
        if isinstance(field, DATETIME_FIELD_TYPES):
            return field.name
    return None


def _stringify_key(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (datetime.datetime, datetime.date)):
        return str(_to_epoch(value))
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _coerce_value(value):
    """Cast aggregate scalars into JSON-friendly primitives."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    # Decimal, timedelta, datetime, etc — float() works for Decimal,
    # str() is the safe fallback.
    try:
        return float(value)
    except (TypeError, ValueError):
        return str(value)


def _to_epoch(dt):
    if isinstance(dt, datetime.datetime):
        if dt.tzinfo is None:
            return int(dt.replace(tzinfo=datetime.timezone.utc).timestamp())
        return int(dt.timestamp())
    if isinstance(dt, datetime.date):
        return int(datetime.datetime(
            dt.year, dt.month, dt.day, tzinfo=datetime.timezone.utc
        ).timestamp())
    return int(dt)


def _bucket_floor(dt, bucket):
    if isinstance(dt, datetime.date) and not isinstance(dt, datetime.datetime):
        dt = datetime.datetime(dt.year, dt.month, dt.day, tzinfo=datetime.timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    else:
        dt = dt.astimezone(datetime.timezone.utc)
    if bucket == "minute":
        return dt.replace(second=0, microsecond=0)
    if bucket == "hour":
        return dt.replace(minute=0, second=0, microsecond=0)
    if bucket == "day":
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if bucket == "week":
        # ISO week — Monday start.
        floored = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        return floored - datetime.timedelta(days=floored.weekday())
    if bucket == "month":
        return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return dt


def _next_bucket(dt, bucket):
    if bucket == "month":
        # Calendar-aware month step.
        if dt.month == 12:
            return dt.replace(year=dt.year + 1, month=1)
        return dt.replace(month=dt.month + 1)
    return dt + BUCKET_DELTAS[bucket]


def _estimate_bucket_count(start, end, bucket):
    if end < start:
        return 0
    if bucket == "month":
        return (end.year - start.year) * 12 + (end.month - start.month) + 1
    delta = end - start
    seconds = delta.total_seconds()
    step = BUCKET_DELTAS[bucket].total_seconds()
    return int(seconds // step) + 1
