import mojo.decorators as md
import mojo.errors as me
from django.db.models.functions import Lower
from mojo.apps.account.models import Group
from mojo.apps.dnsman.models import DnsCredential
from mojo.apps.dnsman.services import onboarding


GROUP_CHOICE_DEFAULT_SIZE = 25
GROUP_CHOICE_MAX_SIZE = 50
GROUP_CHOICE_MAX_START = 100000
GROUP_CHOICE_MAX_SEARCH = 100
GROUP_CHOICE_MAX_ID = 9223372036854775807
GROUP_CHOICE_QUERY_ERROR = "Invalid credential group-choice query"


def _group_choice_integer(value, minimum, maximum):
    """Parse one scalar decimal query value without accepting bool/list input."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise me.ValueException(GROUP_CHOICE_QUERY_ERROR)
    if isinstance(value, str) and (not value or value != value.strip()):
        raise me.ValueException(GROUP_CHOICE_QUERY_ERROR)
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise me.ValueException(GROUP_CHOICE_QUERY_ERROR)
    if value < minimum or value > maximum:
        raise me.ValueException(GROUP_CHOICE_QUERY_ERROR)
    return value


def _group_choice_query(request):
    """Read this route's query string without request.DATA normalization.

    QueryDict.lists() preserves duplicate keys and multi-values. That matters
    here: the generic request parser collapses shapes such as ``id=1&id=2`` or
    bracket/dotted keys before a route handler can reject them consistently.
    """
    allowed = {"id", "search", "start", "size"}
    query = {}
    for key, values in request.GET.lists():
        if key not in allowed or len(values) != 1:
            raise me.ValueException(GROUP_CHOICE_QUERY_ERROR)
        query[key] = values[0]

    if "id" in query:
        if len(query) != 1:
            raise me.ValueException(GROUP_CHOICE_QUERY_ERROR)
        return {
            "id": _group_choice_integer(query["id"], 1, GROUP_CHOICE_MAX_ID),
            "search": "",
            "start": 0,
            "size": 1,
        }

    search = query.get("search", "")
    if not isinstance(search, str):
        raise me.ValueException(GROUP_CHOICE_QUERY_ERROR)
    search = search.strip()
    if len(search) > GROUP_CHOICE_MAX_SEARCH:
        raise me.ValueException(GROUP_CHOICE_QUERY_ERROR)

    return {
        "id": None,
        "search": search,
        "start": _group_choice_integer(
            query.get("start", 0), 0, GROUP_CHOICE_MAX_START),
        "size": _group_choice_integer(
            query.get("size", GROUP_CHOICE_DEFAULT_SIZE),
            1, GROUP_CHOICE_MAX_SIZE),
    }


def _credential_group_choices():
    """Eligible groups, matching Group.is_effectively_active(max_depth=8).

    Self is checked by the initial filter; the eight exclusions check parent
    hops 1 through 8. Longer chains fail closed, matching Group.get_active().
    """
    queryset = Group.objects.filter(is_active=True)
    parent_path = "parent"
    for _ in range(8):
        queryset = queryset.exclude(**{f"{parent_path}__is_active": False})
        parent_path = f"{parent_path}__parent"
    return queryset.filter(**{f"{parent_path}__isnull": True})


@md.GET("credential/group-choice")
@md.requires_global_perms("manage_dns", "security")
def on_credential_group_choice(request):
    """Minimal, global-only Group choices for credential assignment UI."""
    query = _group_choice_query(request)
    queryset = _credential_group_choices()

    if query["id"] is not None:
        data = list(
            queryset.filter(pk=query["id"]).values("id", "name")[:1])
        count = len(data)
        return {
            "status": True,
            "data": data,
            "start": 0,
            "size": 1,
            "count": count,
        }

    if query["search"]:
        queryset = queryset.filter(name__icontains=query["search"])
    count = queryset.count()
    start = query["start"]
    size = query["size"]
    data = list(
        queryset.order_by(Lower("name"), "id")
        .values("id", "name")[start:start + size])
    return {
        "status": True,
        "data": data,
        "start": start,
        "size": size,
        "count": count,
    }


@md.URL('credential')
@md.URL('credential/<int:pk>')
@md.uses_model_security(DnsCredential)
def on_credential(request, pk=None):
    """List / detail / update. Creation goes through credential/link so a
    credential is never stored before the provider confirms it works."""
    return DnsCredential.on_rest_request(request, pk)


@md.POST('credential/link')
@md.requires_params("group", "provider", "api_key", "api_secret")
def on_credential_link(request):
    """
    Link (or rotate) a provider credential.

    The working credential IS the proof of control, so it is verified against
    the provider API before anything is persisted. A failed first link stores
    nothing at all.
    """
    DnsCredential.rest_check_permission_or_raise(request, "SAVE_PERMS")

    # requires_params only proves the key was PRESENT. If it did not resolve to
    # an active group, request.group is None and the service would create a
    # house-scope credential — which also escapes list scoping. Refuse instead
    # of silently widening the credential's reach.
    if request.group is None:
        raise me.ValueException("A valid group is required to link a credential")

    credential = None
    pk = request.DATA.get("credential", None)
    if pk:
        credential = DnsCredential.get_instance_or_404(pk)
        # Rotation targets an existing row — re-check against THAT row, whose
        # group may differ from the caller's currently selected one.
        DnsCredential.rest_check_permission_or_raise(request, "SAVE_PERMS", credential)

    result = onboarding.link_credential(
        group=request.group,
        provider=request.DATA.get("provider"),
        api_key=request.DATA.get("api_key"),
        api_secret=request.DATA.get("api_secret"),
        name=request.DATA.get("name", None),
        credential=credential,
    )
    return result.on_rest_get(request)
