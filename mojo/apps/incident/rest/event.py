from mojo import decorators as md
from mojo.apps.incident.models import Incident, Event, RuleSet, Rule


@md.URL('incident')
@md.URL('incident/<int:pk>')
def on_incident(request, pk=None):
    return Incident.on_rest_request(request, pk)

@md.URL('event')
@md.URL('event/<int:pk>')
def on_event(request, pk=None):
    return Event.on_rest_request(request, pk)

@md.URL('event/ruleset')
@md.URL('event/ruleset/<int:pk>')
def on_event_ruleset(request, pk=None):
    return RuleSet.on_rest_request(request, pk)

@md.URL('event/ruleset/rule')
@md.URL('event/ruleset/rule/<int:pk>')
def on_event_ruleset_rule(request, pk=None):
    return Rule.on_rest_request(request, pk)
