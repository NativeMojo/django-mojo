"""Metrics domain tools — discovery, fetch, gauges, slug explanation.

Structure:

- Shared helpers (permission wrappers, granularity, retention, request shim)
- Discovery tools: list_metric_accounts, list_metric_categories,
  list_metric_slugs, list_metric_gauges, describe_metric_slug,
  resolve_group_account
- Fetch tools: fetch_metrics, fetch_metric_values,
  fetch_metrics_by_category, get_metric_gauge
- Write tools: set_metric_gauge (only write in this domain)
- Retained aggregates: get_system_health, get_incident_trends

Per-account permissions are delegated to the same helpers the REST layer
uses (``mojo.apps.metrics.rest.helpers.check_{view,write}_permissions``).
Metrics functions themselves do not check permissions, so we must always
call those helpers before reading or writing.
"""
import re
from pathlib import Path

import objict

from mojo.apps.assistant import tool
from mojo.helpers import dates, logit

logger = logit.get_logger("assistant", "assistant.log")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_GRANULARITIES = ("minutes", "hours", "days", "weeks", "months", "years")

DEFAULT_SLUG_LIMIT = 500
MAX_SLUG_LIMIT = 2000
DEFAULT_CATEGORY_MAX_SLUGS = 50
MAX_CATEGORY_SLUGS = 200
DESCRIBE_MAX_HITS = 10
DESCRIBE_SNIPPET_LEN = 200

_VALID_ACCOUNT_RE = re.compile(r"^(public|global|group-\d+|user-\d+|[A-Za-z0-9_.:\-]+)$")
_GROUP_ACCOUNT_RE = re.compile(r"^group-(\d+)$")
_USER_ACCOUNT_RE = re.compile(r"^user-(\d+)$")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _build_request(user, request_meta=None, method="GET", path="/assistant/metrics"):
    """Build a synthetic request for check_{view,write}_permissions.

    Matches the shape expected by ``mojo/apps/metrics/rest/helpers.py``.
    When ``request_meta`` is provided, its values seed the synthetic request
    so downstream security events record the originating HTTP context.
    """
    req = objict.objict()
    req.user = user
    req.DATA = objict.objict()
    req.QUERY_PARAMS = objict.objict()
    req.method = method
    req.group = None
    req.bearer = None
    req.ip = "assistant"
    req.path = path
    req.META = {}
    if request_meta is not None:
        if request_meta.get("ip"):
            req.ip = request_meta.ip
        if request_meta.get("user_agent"):
            req.META["HTTP_USER_AGENT"] = request_meta.user_agent
    req.api_key = getattr(user, "api_key", None)
    return req


def _report_security_event(category, level, details, user, request=None, **extra):
    """Report a security event through the incident system. Never raises."""
    try:
        from mojo.apps.incident import reporter
    except Exception:
        return
    if request is not None and "source_ip" not in extra:
        ip = getattr(request, "ip", None)
        if ip and ip != "assistant":
            extra["source_ip"] = ip
    try:
        reporter.report_event(
            details,
            title=details[:80],
            category=category,
            level=level,
            scope="assistant",
            uid=user.id if user else None,
            **extra,
        )
    except Exception:
        logger.exception("Failed to report security event")


def _audit_log(user, kind, message, request=None, conversation=None, payload=None):
    """Write an audit entry to logit.Log. Values are never recorded."""
    if user is None:
        return
    try:
        import ujson
        from mojo.apps.logit.models import Log
        body = ujson.dumps(payload) if payload else None
        Log.logit(
            request, message, kind=kind,
            model_name="metrics.gauge", model_id=0,
            payload=body,
        )
    except Exception:
        logger.exception("Failed to write metrics audit entry")


def _check_account_view(request, account, tool_name, user):
    """Verify the user may view the given account. Returns None on success,
    or a ``{"error": ...}`` dict on denial."""
    from mojo.apps.metrics.rest.helpers import check_view_permissions
    from mojo.errors import PermissionDeniedException
    try:
        check_view_permissions(request, account)
    except PermissionDeniedException:
        details = (
            f"Permission denied: {tool_name} on account='{account}' "
            f"by user {getattr(user, 'id', 'anon')}"
        )
        logger.warning(details)
        _report_security_event(
            "assistant_permission_denied", 5, details, user,
            request=request, model_name="metrics",
        )
        return {"error": f"Permission denied for account '{account}'"}
    return None


def _check_account_write(request, account, tool_name, user):
    """Verify the user may write to the given account. Returns None on success,
    or a ``{"error": ...}`` dict on denial."""
    from mojo.apps.metrics.rest.helpers import check_write_permissions
    from mojo.errors import PermissionDeniedException
    try:
        check_write_permissions(request, account)
    except PermissionDeniedException:
        details = (
            f"Permission denied: {tool_name} write on account='{account}' "
            f"by user {getattr(user, 'id', 'anon')}"
        )
        logger.warning(details)
        _report_security_event(
            "assistant_permission_denied", 5, details, user,
            request=request, model_name="metrics",
        )
        return {"error": f"Write permission denied for account '{account}'"}
    return None


def _validate_granularity(granularity):
    """Return (value, error_dict_or_none)."""
    if granularity is None:
        return None, None
    if granularity not in VALID_GRANULARITIES:
        return None, {
            "error": (
                f"Invalid granularity '{granularity}'. "
                f"Choose one of: {', '.join(VALID_GRANULARITIES)}."
            ),
        }
    return granularity, None


def _validate_account(account):
    """Return error dict or None. Accepts the five REST forms plus custom
    slug-safe strings."""
    if not account:
        return {"error": "Account is required"}
    if not _VALID_ACCOUNT_RE.match(account):
        return {"error": f"Invalid account format: '{account}'"}
    return None


def _auto_granularity(dt_start, dt_end):
    """Pick a sensible granularity given a range. Falls back to 'hours' when
    either bound is missing."""
    if dt_start is None or dt_end is None:
        return "hours"
    try:
        delta = dt_end - dt_start
        minutes = delta.total_seconds() / 60.0
    except Exception:
        return "hours"
    if minutes <= 180:  # <= 3 hours
        return "minutes"
    if minutes <= 60 * 24 * 3:  # <= 3 days
        return "hours"
    if minutes <= 60 * 24 * 90:  # <= 90 days
        return "days"
    return "days"


def _retention_note(granularity, dt_start):
    """Return an advisory string when ``dt_start`` predates the granularity's
    TTL window, else None."""
    if dt_start is None or granularity is None:
        return None
    try:
        from mojo.apps.metrics.utils import GRANULARITY_EXPIRES_DAYS
    except Exception:
        return None
    ttl_days = GRANULARITY_EXPIRES_DAYS.get(granularity)
    if not ttl_days:
        return None
    try:
        cutoff = dates.subtract(days=ttl_days)
    except Exception:
        return None
    # Normalize both to aware UTC for comparison when possible.
    try:
        if dt_start.tzinfo is None and cutoff.tzinfo is not None:
            from datetime import timezone as _tz
            dt_start_cmp = dt_start.replace(tzinfo=_tz.utc)
        else:
            dt_start_cmp = dt_start
        if dt_start_cmp >= cutoff:
            return None
    except Exception:
        return None
    return (
        f"{granularity} granularity retains ~{ttl_days} days of data; "
        f"buckets before {cutoff.date().isoformat()} return 0."
    )


def _echo_meta(account=None, granularity=None, dt_start=None, dt_end=None, slug_count=None):
    """Build a standard metadata dict to echo in fetch responses."""
    meta = {}
    if account is not None:
        meta["account"] = account
    if granularity is not None:
        meta["granularity"] = granularity
    if dt_start is not None:
        meta["dt_start"] = dt_start.isoformat() if hasattr(dt_start, "isoformat") else str(dt_start)
    if dt_end is not None:
        meta["dt_end"] = dt_end.isoformat() if hasattr(dt_end, "isoformat") else str(dt_end)
    if slug_count is not None:
        meta["slug_count"] = slug_count
    return meta


def _parse_date(value):
    """Parse an ISO date string. Returns None if value is falsy."""
    if not value:
        return None
    return dates.parse(value)


def _has_global_view(user):
    """True if the user can see every account."""
    try:
        return bool(user.has_permission(["view_metrics", "metrics"]))
    except Exception:
        return False


def _has_global_write(user):
    try:
        return bool(user.has_permission(["write_metrics", "metrics"]))
    except Exception:
        return False


def _resolve_user_accessible_accounts(user, all_accounts):
    """Return the subset of ``all_accounts`` the user can view without global
    perms. Always includes ``public`` and ``user-<self>``. Group accounts
    depend on membership-level perms. Custom accounts depend on Redis-stored
    view perms the user has.
    """
    accessible = {"public"}
    uid = getattr(user, "pk", None) or getattr(user, "id", None)
    if uid:
        accessible.add(f"user-{uid}")

    # Group accounts the user can access (member with view_metrics/metrics)
    try:
        group_qs = user.get_groups_with_permission(["view_metrics", "metrics"])
        for g in group_qs:
            accessible.add(f"group-{g.pk}")
    except Exception:
        pass

    # Custom accounts — only include those whose view perms the user satisfies
    from mojo.apps import metrics as metrics_mod
    for acct in all_accounts:
        if acct in accessible:
            continue
        if acct in ("public", "global"):
            continue
        if _GROUP_ACCOUNT_RE.match(acct) or _USER_ACCOUNT_RE.match(acct):
            # Group/user accounts handled by membership checks above.
            continue
        try:
            perms = metrics_mod.get_view_perms(acct)
        except Exception:
            perms = None
        if perms == "public":
            accessible.add(acct)
        elif perms:
            try:
                if user.has_permission(perms):
                    accessible.add(acct)
            except Exception:
                pass
    return accessible & all_accounts


# ---------------------------------------------------------------------------
# Discovery tools
# ---------------------------------------------------------------------------

@tool(
    name="list_metric_accounts",
    domain="metrics",
    permission="view_metrics",
    description=(
        "List every metrics account visible to the current user. "
        "Start here when the user says 'our metrics' without naming a scope. "
        "Unions accounts with configured permissions and accounts with recorded data. "
        "Users with global view_metrics/metrics see everything; others see only "
        "public, their user-<id>, accessible group-<id>s, and custom accounts they can view. "
        "Account forms: 'public' (open), 'global' (requires view_metrics/metrics), "
        "'group-<id>' (per-group scope), 'user-<id>' (per-user scope), or a custom string."
    ),
    input_schema={
        "type": "object",
        "properties": {},
    },
)
def _tool_list_metric_accounts(params, user):
    from mojo.apps import metrics
    try:
        configured = set(metrics.list_accounts() or [])
        inferred = set(metrics.list_accounts_with_data() or [])
    except Exception:
        logger.exception("list_metric_accounts: redis error")
        return {"error": "Metrics backend unavailable"}

    all_accounts = configured | inferred | {"public", "global"}

    if _has_global_view(user):
        return {
            "accounts": sorted(all_accounts),
            "count": len(all_accounts),
            "scoped": False,
        }

    visible = _resolve_user_accessible_accounts(user, all_accounts)
    return {
        "accounts": sorted(visible),
        "count": len(visible),
        "scoped": True,
    }


@tool(
    name="list_metric_categories",
    domain="metrics",
    permission="view_metrics",
    description=(
        "List metric categories on a given account. "
        "Use after list_metric_accounts to drill into a specific scope. "
        "Requires view access to the account."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "account": {
                "type": "string",
                "description": "Account scope: public | global | group-<id> | user-<id> | custom",
                "default": "public",
            },
        },
    },
)
def _tool_list_metric_categories(params, user, *, request_meta=None):
    from mojo.apps import metrics
    account = params.get("account", "public")
    err = _validate_account(account)
    if err:
        return err
    request = _build_request(user, request_meta=request_meta)
    err = _check_account_view(request, account, "list_metric_categories", user)
    if err:
        return err
    try:
        categories = sorted(metrics.get_categories(account=account) or [])
    except Exception:
        logger.exception("list_metric_categories: redis error")
        return {"error": "Metrics backend unavailable"}
    return {
        "account": account,
        "categories": categories,
        "count": len(categories),
    }


@tool(
    name="list_metric_slugs",
    domain="metrics",
    permission="view_metrics",
    description=(
        "List time-series metric slugs on an account. Optionally filter by "
        "category or slug prefix. Use the prefix filter on large accounts to "
        "narrow dimensional slug groups (e.g. prefix='login_attempts:ip:' to "
        "list per-IP variants). Default limit 500, max 2000."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "account": {
                "type": "string",
                "description": "Account scope: public | global | group-<id> | user-<id> | custom",
                "default": "public",
            },
            "category": {
                "type": "string",
                "description": "Optional — restrict to a single category",
            },
            "prefix": {
                "type": "string",
                "description": "Optional — only return slugs starting with this prefix",
            },
            "limit": {
                "type": "integer",
                "description": f"Max slugs to return (default {DEFAULT_SLUG_LIMIT}, max {MAX_SLUG_LIMIT})",
            },
        },
    },
)
def _tool_list_metric_slugs(params, user, *, request_meta=None):
    from mojo.apps import metrics
    account = params.get("account", "public")
    err = _validate_account(account)
    if err:
        return err
    request = _build_request(user, request_meta=request_meta)
    err = _check_account_view(request, account, "list_metric_slugs", user)
    if err:
        return err

    category = params.get("category") or None
    prefix = params.get("prefix") or None
    limit = min(max(int(params.get("limit", DEFAULT_SLUG_LIMIT)), 1), MAX_SLUG_LIMIT)

    try:
        if category:
            raw = metrics.get_category_slugs(category, account=account) or set()
        else:
            raw = metrics.get_account_slugs(account) or set()
    except Exception:
        logger.exception("list_metric_slugs: redis error")
        return {"error": "Metrics backend unavailable"}

    slugs = sorted(raw)
    if prefix:
        slugs = [s for s in slugs if s.startswith(prefix)]
    total = len(slugs)
    truncated = total > limit
    return {
        "account": account,
        "category": category,
        "prefix": prefix,
        "slugs": slugs[:limit],
        "count": min(total, limit),
        "total": total,
        "truncated": truncated,
    }


@tool(
    name="list_metric_gauges",
    domain="metrics",
    permission="view_metrics",
    description=(
        "List gauge (non-time-series) slug names on an account. Gauges are "
        "written via set_metric_gauge and read via get_metric_gauge — think "
        "maintenance_mode, feature flags, rate-limit overrides. Returns slug "
        "names only, not values. Optional prefix filter."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "account": {
                "type": "string",
                "description": "Account scope",
                "default": "public",
            },
            "prefix": {"type": "string", "description": "Optional slug prefix filter"},
            "limit": {
                "type": "integer",
                "description": f"Max gauges to return (default {DEFAULT_SLUG_LIMIT})",
            },
        },
    },
)
def _tool_list_metric_gauges(params, user, *, request_meta=None):
    from mojo.apps import metrics
    account = params.get("account", "public")
    err = _validate_account(account)
    if err:
        return err
    request = _build_request(user, request_meta=request_meta)
    err = _check_account_view(request, account, "list_metric_gauges", user)
    if err:
        return err

    prefix = params.get("prefix") or None
    limit = min(max(int(params.get("limit", DEFAULT_SLUG_LIMIT)), 1), MAX_SLUG_LIMIT)
    try:
        result = metrics.list_gauge_slugs(account, prefix=prefix, limit=limit)
    except Exception:
        logger.exception("list_metric_gauges: redis error")
        return {"error": "Metrics backend unavailable"}

    result["account"] = account
    result["prefix"] = prefix
    return result


@tool(
    name="describe_metric_slug",
    domain="metrics",
    permission="view_metrics",
    description=(
        "Explain what a metric slug tracks by grepping the codebase for "
        "metrics.record() call sites that reference it. Use when the user "
        "asks 'what does <slug> mean?' or pastes a slug that isn't "
        "self-evident. Returns up to 10 hits with file, line, and snippet. "
        "No permission check on slug name itself — slug strings are source-"
        "code literals, not secrets."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "slug": {
                "type": "string",
                "description": "The slug to explain. Can be a full slug or just the prefix.",
            },
        },
        "required": ["slug"],
    },
)
def _tool_describe_metric_slug(params, user):
    slug = (params.get("slug") or "").strip()
    if not slug:
        return {"error": "slug is required"}

    roots = _describe_search_roots()
    hits = _scan_for_slug(roots, slug)
    if not hits:
        return {
            "slug": slug,
            "hits": [],
            "count": 0,
            "message": (
                "No metrics.record() call sites found for this slug. It may be "
                "recorded dynamically (f-string), set via set_value, or defined "
                "in an uninstalled app."
            ),
        }
    return {"slug": slug, "hits": hits, "count": len(hits)}


def _describe_search_roots():
    """Return de-duplicated roots to scan for describe_metric_slug."""
    roots = []
    try:
        import mojo
        mojo_root = Path(mojo.__file__).resolve().parent
        roots.append(mojo_root)
    except Exception:
        pass
    try:
        from mojo.helpers.settings import settings
        base = settings.get("BASE_DIR")
        if base:
            base_path = Path(base).resolve()
            # Skip if BASE_DIR is inside the mojo root or vice versa
            if not any(_is_subpath(base_path, r) or _is_subpath(r, base_path) for r in roots):
                roots.append(base_path)
    except Exception:
        pass
    return roots


def _is_subpath(child, parent):
    try:
        child.relative_to(parent)
        return True
    except Exception:
        return False


def _scan_for_slug(roots, slug):
    """Walk each root for .py files and collect metrics.record lines that
    mention the slug. Bounded by DESCRIBE_MAX_HITS."""
    # Match metrics.record( ... 'slug...' ... ) — slug may appear inside an
    # f-string prefix, so we check that the slug literal is present within
    # the same quoted string that opens the first arg.
    escaped = re.escape(slug)
    pattern = re.compile(
        r"metrics\.record\(\s*[fF]?['\"]([^'\"]*" + escaped + r"[^'\"]*)['\"]"
    )
    hits = []
    for root in roots:
        if len(hits) >= DESCRIBE_MAX_HITS:
            break
        for path in root.rglob("*.py"):
            if len(hits) >= DESCRIBE_MAX_HITS:
                break
            # Skip common noise
            parts = path.parts
            if "__pycache__" in parts or ".venv" in parts or "node_modules" in parts:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            for m in pattern.finditer(text):
                line_num = text.count("\n", 0, m.start()) + 1
                line_start = text.rfind("\n", 0, m.start()) + 1
                line_end = text.find("\n", m.end())
                if line_end == -1:
                    line_end = len(text)
                snippet = text[line_start:line_end].strip()[:DESCRIBE_SNIPPET_LEN]
                try:
                    relpath = str(path.relative_to(root))
                except Exception:
                    relpath = str(path)
                hits.append({
                    "file": relpath,
                    "line": line_num,
                    "snippet": snippet,
                })
                if len(hits) >= DESCRIBE_MAX_HITS:
                    break
    return hits


@tool(
    name="resolve_group_account",
    domain="metrics",
    permission="view_metrics",
    description=(
        "Resolve a group name or id to a 'group-<id>' account string for use "
        "in other metrics tools. Call this when the user names a group by "
        "name instead of passing the account string. Numeric input → pk; "
        "string input → case-insensitive exact name match. Ambiguous names "
        "return a candidates list so the user can pick."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name_or_id": {
                "type": "string",
                "description": "Group name (case-insensitive exact match) or pk",
            },
        },
        "required": ["name_or_id"],
    },
)
def _tool_resolve_group_account(params, user):
    from mojo.apps.account.models import Group

    raw = params.get("name_or_id")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {"error": "name_or_id is required"}

    # Try numeric first
    group = None
    try:
        pk = int(raw)
    except (TypeError, ValueError):
        pk = None

    if pk is not None:
        group = Group.objects.filter(pk=pk).first()
        if group is None:
            return {"error": f"no group with pk={pk}"}
    else:
        qs = Group.objects.filter(name__iexact=str(raw).strip())
        total = qs.count()
        if total == 0:
            return {"error": f"no group found for '{raw}'"}
        if total > 1:
            return {
                "error": "ambiguous group name",
                "candidates": [
                    {"pk": g.pk, "name": g.name} for g in qs[:10]
                ],
                "count": total,
            }
        group = qs.first()

    # Access check — either group-level or system-level perm
    try:
        has_access = group.user_has_permission(user, ["view_metrics", "metrics"])
    except Exception:
        has_access = _has_global_view(user)
    if not has_access:
        return {"error": f"no access to group-{group.pk}"}

    return {
        "account": f"group-{group.pk}",
        "group": {"pk": group.pk, "name": group.name},
    }


# ---------------------------------------------------------------------------
# Fetch tools
# ---------------------------------------------------------------------------

@tool(
    name="fetch_metrics",
    domain="metrics",
    permission="view_metrics",
    description=(
        "Fetch time-series metric data for one or many slugs. "
        "Granularities: minutes, hours, days, weeks, months, years. "
        "If granularity is omitted it's picked from the range: <=3h → minutes, "
        "<=3d → hours, else days. "
        "Call list_metric_slugs first if the user hasn't named a specific slug. "
        "Response echoes {account, granularity, dt_start, dt_end, slug_count} "
        "plus a retention_note when the requested range predates the "
        "granularity's TTL (older buckets return 0)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "slugs": {
                "oneOf": [
                    {"type": "array", "items": {"type": "string"}},
                    {"type": "string"},
                ],
                "description": "Slug or list of slugs (e.g. 'login_attempts' or ['api_calls','api_errors'])",
            },
            "dt_start": {"type": "string", "description": "Start datetime (ISO format)"},
            "dt_end": {"type": "string", "description": "End datetime (ISO format)"},
            "granularity": {
                "type": "string",
                "description": "minutes | hours | days | weeks | months | years (auto-picked if omitted)",
            },
            "account": {
                "type": "string",
                "description": "Account scope",
                "default": "public",
            },
            "with_labels": {
                "type": "boolean",
                "description": "Include bucket labels in the response (default true)",
                "default": True,
            },
            "allow_empty": {
                "type": "boolean",
                "description": "Include slugs with all-zero values (default true)",
                "default": True,
            },
        },
        "required": ["slugs"],
    },
)
def _tool_fetch_metrics(params, user, *, request_meta=None):
    from mojo.apps import metrics

    slugs = params.get("slugs")
    if not slugs:
        return {"error": "At least one slug is required"}
    if isinstance(slugs, str):
        slugs = [slugs]
    if not isinstance(slugs, list) or not slugs:
        return {"error": "slugs must be a non-empty string or list"}

    account = params.get("account", "public")
    err = _validate_account(account)
    if err:
        return err

    request = _build_request(user, request_meta=request_meta)
    err = _check_account_view(request, account, "fetch_metrics", user)
    if err:
        return err

    try:
        dt_start = _parse_date(params.get("dt_start"))
        dt_end = _parse_date(params.get("dt_end"))
    except Exception as e:
        return {"error": f"Invalid date format: {e}"}

    gran_param = params.get("granularity")
    granularity, gran_err = _validate_granularity(gran_param)
    if gran_err:
        return gran_err
    if granularity is None:
        granularity = _auto_granularity(dt_start, dt_end)

    with_labels = bool(params.get("with_labels", True))
    allow_empty = bool(params.get("allow_empty", True))

    # metrics.fetch accepts a single slug (str) or a list/set
    fetch_arg = slugs[0] if len(slugs) == 1 else slugs

    try:
        records = metrics.fetch(
            fetch_arg,
            dt_start=dt_start,
            dt_end=dt_end,
            granularity=granularity,
            account=account,
            with_labels=with_labels,
            allow_empty=allow_empty,
        )
    except Exception:
        logger.exception("fetch_metrics: backend error")
        return {"error": "Metrics backend unavailable"}

    result = {"data": records}
    result.update(_echo_meta(
        account=account,
        granularity=granularity,
        dt_start=dt_start,
        dt_end=dt_end,
        slug_count=len(slugs),
    ))
    note = _retention_note(granularity, dt_start)
    if note:
        result["retention_note"] = note
    return result


@tool(
    name="fetch_metric_values",
    domain="metrics",
    permission="view_metrics",
    description=(
        "Fetch a point-in-time snapshot of multiple slugs in one call. "
        "Returns {slug: int} for the bucket containing `when` (defaults to now) "
        "at the given granularity. Use for dashboard-style current values "
        "across many slugs."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "slugs": {
                "oneOf": [
                    {"type": "array", "items": {"type": "string"}},
                    {"type": "string"},
                ],
                "description": "List of slugs or comma-separated string",
            },
            "when": {"type": "string", "description": "ISO datetime (default now)"},
            "granularity": {
                "type": "string",
                "description": "minutes | hours | days | weeks | months | years (default hours)",
                "default": "hours",
            },
            "account": {"type": "string", "description": "Account scope", "default": "public"},
        },
        "required": ["slugs"],
    },
)
def _tool_fetch_metric_values(params, user, *, request_meta=None):
    from mojo.apps import metrics

    slugs = params.get("slugs")
    if not slugs:
        return {"error": "At least one slug is required"}

    account = params.get("account", "public")
    err = _validate_account(account)
    if err:
        return err

    granularity, gran_err = _validate_granularity(params.get("granularity", "hours"))
    if gran_err:
        return gran_err

    request = _build_request(user, request_meta=request_meta)
    err = _check_account_view(request, account, "fetch_metric_values", user)
    if err:
        return err

    try:
        when = _parse_date(params.get("when"))
    except Exception as e:
        return {"error": f"Invalid date format: {e}"}

    try:
        result = metrics.fetch_values(slugs, when=when, granularity=granularity, account=account)
    except Exception:
        logger.exception("fetch_metric_values: backend error")
        return {"error": "Metrics backend unavailable"}
    # result already contains data/slugs/when/granularity/account
    return result


@tool(
    name="fetch_metrics_by_category",
    domain="metrics",
    permission="view_metrics",
    description=(
        "Fetch time-series data for every slug in a category at once. "
        "Capped at max_slugs (default 50, max 200) to keep token budgets sane. "
        "Returns {data, truncated, total_slugs, retention_note?} plus the "
        "standard fetch metadata."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "category": {"type": "string", "description": "Category name"},
            "account": {"type": "string", "description": "Account scope", "default": "public"},
            "dt_start": {"type": "string", "description": "ISO datetime"},
            "dt_end": {"type": "string", "description": "ISO datetime"},
            "granularity": {
                "type": "string",
                "description": "minutes | hours | days | weeks | months | years",
            },
            "with_labels": {"type": "boolean", "default": True},
            "max_slugs": {
                "type": "integer",
                "description": f"Max slugs to include (default {DEFAULT_CATEGORY_MAX_SLUGS}, max {MAX_CATEGORY_SLUGS})",
            },
        },
        "required": ["category"],
    },
)
def _tool_fetch_metrics_by_category(params, user, *, request_meta=None):
    from mojo.apps import metrics

    category = (params.get("category") or "").strip()
    if not category:
        return {"error": "category is required"}

    account = params.get("account", "public")
    err = _validate_account(account)
    if err:
        return err

    request = _build_request(user, request_meta=request_meta)
    err = _check_account_view(request, account, "fetch_metrics_by_category", user)
    if err:
        return err

    try:
        dt_start = _parse_date(params.get("dt_start"))
        dt_end = _parse_date(params.get("dt_end"))
    except Exception as e:
        return {"error": f"Invalid date format: {e}"}

    gran_param = params.get("granularity")
    granularity, gran_err = _validate_granularity(gran_param)
    if gran_err:
        return gran_err
    if granularity is None:
        granularity = _auto_granularity(dt_start, dt_end)

    max_slugs = min(
        max(int(params.get("max_slugs", DEFAULT_CATEGORY_MAX_SLUGS)), 1),
        MAX_CATEGORY_SLUGS,
    )
    with_labels = bool(params.get("with_labels", True))

    try:
        all_slugs = sorted(metrics.get_category_slugs(category, account=account) or set())
    except Exception:
        logger.exception("fetch_metrics_by_category: redis error")
        return {"error": "Metrics backend unavailable"}

    total_slugs = len(all_slugs)
    if total_slugs == 0:
        return {
            "category": category,
            "account": account,
            "data": {},
            "slug_count": 0,
            "total_slugs": 0,
            "truncated": False,
        }

    slugs = all_slugs[:max_slugs]
    truncated = total_slugs > max_slugs

    fetch_arg = slugs[0] if len(slugs) == 1 else slugs
    try:
        records = metrics.fetch(
            fetch_arg,
            dt_start=dt_start,
            dt_end=dt_end,
            granularity=granularity,
            account=account,
            with_labels=with_labels,
            allow_empty=True,
        )
    except Exception:
        logger.exception("fetch_metrics_by_category: fetch error")
        return {"error": "Metrics backend unavailable"}

    result = {
        "category": category,
        "data": records,
        "truncated": truncated,
        "total_slugs": total_slugs,
    }
    result.update(_echo_meta(
        account=account,
        granularity=granularity,
        dt_start=dt_start,
        dt_end=dt_end,
        slug_count=len(slugs),
    ))
    note = _retention_note(granularity, dt_start)
    if note:
        result["retention_note"] = note
    return result


@tool(
    name="get_metric_gauge",
    domain="metrics",
    permission="view_metrics",
    description=(
        "Read one or more gauge values (non-time-series KV). Use for feature "
        "flags, maintenance_mode, and other operational toggles. Accepts a "
        "single slug or a list. Missing slugs return the `default` value."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "slug": {"type": "string", "description": "Single gauge slug"},
            "slugs": {
                "oneOf": [
                    {"type": "array", "items": {"type": "string"}},
                    {"type": "string"},
                ],
                "description": "Multiple gauge slugs (list or comma-separated)",
            },
            "account": {"type": "string", "default": "public"},
            "default": {"description": "Value returned when a slug is missing"},
        },
    },
)
def _tool_get_metric_gauge(params, user, *, request_meta=None):
    from mojo.apps import metrics

    account = params.get("account", "public")
    err = _validate_account(account)
    if err:
        return err

    request = _build_request(user, request_meta=request_meta)
    err = _check_account_view(request, account, "get_metric_gauge", user)
    if err:
        return err

    default = params.get("default")

    slug_list = []
    if params.get("slug"):
        slug_list.append(str(params["slug"]).strip())
    raw_slugs = params.get("slugs")
    if raw_slugs:
        if isinstance(raw_slugs, str):
            if "," in raw_slugs:
                slug_list.extend(s.strip() for s in raw_slugs.split(",") if s.strip())
            else:
                slug_list.append(raw_slugs.strip())
        elif isinstance(raw_slugs, list):
            slug_list.extend(str(s).strip() for s in raw_slugs if s)

    # De-dup while preserving order
    seen = set()
    ordered = []
    for s in slug_list:
        if s and s not in seen:
            seen.add(s)
            ordered.append(s)

    if not ordered:
        return {"error": "At least one slug is required (slug or slugs)"}

    data = {}
    for s in ordered:
        try:
            data[s] = metrics.get_value(s, account=account, default=default)
        except Exception:
            logger.exception("get_metric_gauge: backend error")
            return {"error": "Metrics backend unavailable"}
    return {"account": account, "data": data, "slugs": ordered}


# ---------------------------------------------------------------------------
# Write tool
# ---------------------------------------------------------------------------

@tool(
    name="set_metric_gauge",
    domain="metrics",
    permission="write_metrics",
    mutates=True,
    description=(
        "OPERATIONAL TOGGLE — write a non-time-series gauge value. "
        "Use for maintenance_mode, feature flags, rate-limit overrides, and "
        "other administrative toggles. Never call without explicit user "
        "approval; present an action block first and wait for confirmation. "
        "Requires write_metrics (or the 'metrics' category permission) plus "
        "write access to the target account. Every successful write is "
        "recorded in the audit log."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "slug": {"type": "string", "description": "Gauge key (e.g. 'maintenance_mode')"},
            "value": {"description": "Value to store (coerced to string)"},
            "account": {"type": "string", "default": "public"},
        },
        "required": ["slug", "value"],
    },
)
def _tool_set_metric_gauge(params, user, *, request_meta=None, conversation=None):
    from mojo.apps import metrics

    slug = (params.get("slug") or "").strip()
    if not slug:
        return {"error": "slug is required"}
    if any(ord(c) < 32 for c in slug):
        return {"error": "slug contains invalid control characters"}

    if "value" not in params:
        return {"error": "value is required"}
    value = params.get("value")
    if value is None:
        value = ""
    value = str(value)

    account = params.get("account", "public")
    err = _validate_account(account)
    if err:
        return err

    request = _build_request(user, request_meta=request_meta, method="POST",
                             path="/assistant/metrics/gauge")
    err = _check_account_write(request, account, "set_metric_gauge", user)
    if err:
        return err

    try:
        metrics.set_value(slug, value, account=account)
    except Exception:
        logger.exception("set_metric_gauge: backend error")
        return {"error": "Metrics backend unavailable"}

    conv_pk = getattr(conversation, "pk", None) if conversation is not None else None
    payload = {"slug": slug, "account": account}
    if conv_pk is not None:
        payload["conversation_id"] = conv_pk
    _audit_log(
        user, "assistant:metric:gauge_set",
        f"set gauge {slug} on {account}",
        request=request, conversation=conversation, payload=payload,
    )
    logger.info("set_metric_gauge slug=%s account=%s user=%s",
                slug, account, getattr(user, "id", None))
    return {"ok": True, "slug": slug, "account": account}


# ---------------------------------------------------------------------------
# Retained aggregates (unchanged behavior)
# ---------------------------------------------------------------------------

@tool(
    name="get_system_health",
    domain="metrics",
    permission="view_admin",
    description="Overview of system health: active users, job queue depth, error rates, open incident counts.",
    input_schema={
        "type": "object",
        "properties": {},
    },
)
def _tool_get_system_health(params, user):
    """Aggregate cross-domain health stats."""
    from mojo.apps.account.models import User
    from mojo.apps.incident.models import Incident, Event
    from mojo.apps.jobs.models import Job

    now_minus_1h = dates.subtract(minutes=60)
    now_minus_24h = dates.subtract(minutes=1440)

    active_users = User.objects.filter(
        last_activity__gte=now_minus_1h, is_active=True
    ).count()
    open_incidents = Incident.objects.filter(
        status__in=["new", "open", "investigating"]
    ).count()
    events_1h = Event.objects.filter(created__gte=now_minus_1h).count()
    pending_jobs = Job.objects.filter(status="pending").count()
    running_jobs = Job.objects.filter(status="running").count()
    failed_24h = Job.objects.filter(
        status="failed", created__gte=now_minus_24h
    ).count()

    return {
        "active_users_1h": active_users,
        "open_incidents": open_incidents,
        "events_last_hour": events_1h,
        "pending_jobs": pending_jobs,
        "running_jobs": running_jobs,
        "failed_jobs_24h": failed_24h,
    }


@tool(
    name="get_incident_trends",
    domain="metrics",
    permission="view_security",
    description="Incident and event trends over time (1h, 6h, 24h, 7d) with category breakdown.",
    input_schema={
        "type": "object",
        "properties": {},
    },
)
def _tool_get_incident_trends(params, user):
    """Incident and event counts over recent time periods for comparison."""
    from mojo.apps.incident.models import Incident, Event

    periods = [
        ("last_1h", 60),
        ("last_6h", 360),
        ("last_24h", 1440),
        ("last_7d", 10080),
    ]

    result = {}
    for label, minutes in periods:
        since = dates.subtract(minutes=minutes)
        result[label] = {
            "incidents": Incident.objects.filter(created__gte=since).count(),
            "events": Event.objects.filter(created__gte=since).count(),
        }

    from django.db.models import Count
    since_24h = dates.subtract(minutes=1440)
    categories = list(
        Event.objects.filter(created__gte=since_24h)
        .values("category")
        .annotate(count=Count("id"))
        .order_by("-count")[:20]
    )
    result["categories_24h"] = categories
    return result
