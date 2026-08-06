from mojo import decorators as md
from mojo.apps import metrics
from mojo.apps.metrics import utils
from mojo.helpers import redis
from mojo.helpers.response import JsonResponse
import mojo.errors as me

from .helpers import check_view_permissions


DISCOVERY_RESOURCES = ("accounts", "categories", "slugs")
DISCOVERY_DEFAULT_SIZE = 50
DISCOVERY_MAX_SIZE = 500
DISCOVERY_MAX_ACCOUNTS = 1000
DISCOVERY_MAX_ACCOUNT_LENGTH = 256
DISCOVERY_MAX_CATEGORY_LENGTH = 256
DISCOVERY_MAX_SEARCH_LENGTH = 128
DISCOVERY_QUERY_ERROR = "Invalid metrics discovery query"
DISCOVERY_ACCOUNTS_LUA = """
local count = redis.call('SCARD', KEYS[1])
if count > tonumber(ARGV[1]) then
    return {count}
end
return {count, redis.call('SMEMBERS', KEYS[1])}
"""


DISCOVERY_DOCS = {
    "summary": "Discover viewable metrics accounts, categories, and slugs",
    "description": (
        "Returns one permission-filtered, paginated metrics registry resource. "
        "Slugs are returned exactly as stored."
    ),
    "parameters": [
        {"name": "resource", "in": "query", "required": True,
         "schema": {"type": "string", "enum": list(DISCOVERY_RESOURCES)}},
        {"name": "account", "in": "query", "required": False,
         "schema": {"type": "string", "maxLength": DISCOVERY_MAX_ACCOUNT_LENGTH}},
        {"name": "category", "in": "query", "required": False,
         "schema": {"type": "string", "maxLength": DISCOVERY_MAX_CATEGORY_LENGTH}},
        {"name": "search", "in": "query", "required": False,
         "schema": {"type": "string", "maxLength": DISCOVERY_MAX_SEARCH_LENGTH}},
        {"name": "start", "in": "query", "required": False,
         "schema": {"type": "integer", "minimum": 0, "default": 0}},
        {"name": "size", "in": "query", "required": False,
         "schema": {"type": "integer", "minimum": 1,
                    "maximum": DISCOVERY_MAX_SIZE, "default": DISCOVERY_DEFAULT_SIZE}},
    ],
}


def _invalid_query():
    raise me.ValueException(DISCOVERY_QUERY_ERROR)


def _parse_integer(value, minimum, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        _invalid_query()
    if isinstance(value, str) and (not value or value != value.strip()):
        _invalid_query()
    try:
        value = int(value)
    except (TypeError, ValueError):
        _invalid_query()
    if value < minimum or (maximum is not None and value > maximum):
        _invalid_query()
    return value


def _bounded_string(query, key, maximum, required=False):
    if key not in query:
        if required:
            _invalid_query()
        return None
    value = query[key]
    if not isinstance(value, str) or len(value) > maximum:
        _invalid_query()
    if required and not value:
        _invalid_query()
    return value


def _parse_query(request):
    """Validate the framework-normalized request data without coercing shapes."""
    allowed = {"resource", "account", "category", "search", "start", "size"}
    if any(key not in allowed for key in request.DATA):
        _invalid_query()
    query = request.DATA

    resource = _bounded_string(query, "resource", 16, required=True)
    if resource not in DISCOVERY_RESOURCES:
        _invalid_query()
    account = _bounded_string(
        query, "account", DISCOVERY_MAX_ACCOUNT_LENGTH,
        required=resource in ("categories", "slugs"))
    category = _bounded_string(
        query, "category", DISCOVERY_MAX_CATEGORY_LENGTH)
    search = _bounded_string(query, "search", DISCOVERY_MAX_SEARCH_LENGTH)
    if search is None:
        search = ""

    if resource == "accounts" and (account is not None or category is not None):
        _invalid_query()
    if resource == "categories" and category is not None:
        _invalid_query()

    return {
        "resource": resource,
        "account": account,
        "category": category,
        "search": search,
        "start": _parse_integer(query.get("start", 0), 0),
        "size": _parse_integer(
            query.get("size", DISCOVERY_DEFAULT_SIZE), 1, DISCOVERY_MAX_SIZE),
    }


def _validate_reserved_account(account):
    for prefix in ("group-", "user-"):
        if account.startswith(prefix):
            suffix = account[len(prefix):]
            if not suffix.isascii() or not suffix.isdigit() or int(suffix) < 1:
                raise me.PermissionDeniedException()
            return


def _check_account_view(request, account):
    _validate_reserved_account(account)
    check_view_permissions(request, account)


def _paginate(items, start, size):
    ordered = sorted(set(items))
    count = len(ordered)
    data = ordered[start:start + size]
    next_start = start + size if start + size < count else None
    return {
        "data": data,
        "start": start,
        "size": size,
        "count": count,
        "page_count": len(data),
        "next_start": next_start,
    }


def _search(items, value):
    if not value:
        return items
    needle = value.casefold()
    return [item for item in items if needle in item.casefold()]


def _candidate_accounts():
    redis_con = redis.get_connection()
    accounts_key = utils.generate_accounts_key()
    result = redis_con.eval(
        DISCOVERY_ACCOUNTS_LUA, 1, accounts_key, DISCOVERY_MAX_ACCOUNTS)
    count = int(result[0])
    if count > DISCOVERY_MAX_ACCOUNTS:
        raise me.ValueException("Metrics discovery account index exceeds its limit")
    members = result[1] if len(result) > 1 else []
    accounts = {
        account.decode("utf-8") if isinstance(account, bytes) else account
        for account in members
    }
    return accounts.union({"public", "global"})


def _visible_accounts(request, candidates):
    visible = []
    for account in sorted(set(candidates)):
        try:
            _check_account_view(request, account)
        except me.PermissionDeniedException:
            continue
        visible.append(account)
    return visible


def _filters(query):
    if query["resource"] == "accounts":
        return {"search": query["search"]}
    if query["resource"] == "categories":
        return {"account": query["account"], "search": query["search"]}
    return {
        "account": query["account"],
        "category": query["category"],
        "search": query["search"],
    }


@md.GET("discover", docs=DISCOVERY_DOCS)
@md.custom_security("protected by metrics account view permissions")
def on_metrics_discover(request):
    query = _parse_query(request)
    resource = query["resource"]

    if resource == "accounts":
        check_view_permissions(request, "global")
        items = _visible_accounts(request, _candidate_accounts())
    else:
        account = query["account"]
        _check_account_view(request, account)
        if resource == "categories":
            items = metrics.get_categories(account=account)
        elif query["category"] is not None:
            items = metrics.get_category_slugs(
                query["category"], account=account)
        else:
            items = metrics.get_account_slugs(account)

    page = _paginate(
        _search(items, query["search"]), query["start"], query["size"])
    return JsonResponse({
        "status": True,
        "resource": resource,
        "filters": _filters(query),
        **page,
    })
