from mojo import decorators as md
from mojo import JsonResponse
from mojo.apps.incident.models import Incident, IncidentHistory, Event, RuleSet, Rule


@md.URL('incident')
@md.URL('incident/<int:pk>')
def on_incident(request, pk=None):
    return Incident.on_rest_request(request, pk)

@md.URL('incident/history')
@md.URL('incident/<int:pk>/history')
def on_incident_history(request, pk=None):
    return IncidentHistory.on_rest_request(request, pk)


@md.URL('event')
@md.URL('event/<int:pk>')
def on_event(request, pk=None):
    return Event.on_rest_request(request, pk)


@md.GET('health/summary')
@md.requires_perms("view_security", "security")
def on_health_summary(request):
    """
    Return the most recent Event per ``system:health:*`` category (or any
    other namespaced category root via the ``prefix`` query param).

    One row per distinct category — used by the portal Security Dashboard's
    Health Strip so it can render an indicator per subsystem without
    hard-coding the category list or making N round-trips.

    The ``prefix`` parameter must be a namespace prefix — non-empty and
    colon-suffixed (``foo:bar:``). This bounds the endpoint to enumerating
    a single namespace root rather than acting as an open-ended category
    discovery oracle for any caller with view_security.
    """
    from mojo.errors import ValueException
    prefix = request.DATA.get("prefix", "system:health:")
    if not isinstance(prefix, str) or not prefix or not prefix.endswith(":"):
        raise ValueException(
            "prefix must be a non-empty namespace prefix ending in ':' (e.g. 'system:health:')",
            400,
        )
    categories = (
        Event.objects
        .filter(category__startswith=prefix)
        .values_list("category", flat=True)
        .distinct()
    )
    data = []
    for category in categories:
        latest = Event.objects.filter(category=category).order_by("-created").first()
        if latest is None:
            continue
        data.append({
            "category": category,
            "level": latest.level,
            "last_seen": latest.created.isoformat() if latest.created else None,
            "title": latest.title,
            "details": latest.details,
            "hostname": latest.hostname,
            "source_ip": latest.source_ip,
            "incident_id": latest.incident_id,
        })
    # Stable ordering for the UI — by category name.
    data.sort(key=lambda row: row["category"])
    return JsonResponse(dict(status=True, data=data))

@md.URL('event/ruleset')
@md.URL('event/ruleset/<int:pk>')
def on_event_ruleset(request, pk=None):
    return RuleSet.on_rest_request(request, pk)

@md.URL('event/ruleset/rule')
@md.URL('event/ruleset/rule/<int:pk>')
def on_event_ruleset_rule(request, pk=None):
    return Rule.on_rest_request(request, pk)
