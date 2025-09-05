from mojo import decorators as md
from mojo.apps.incident.models import Ticket, TicketNote
from mojo.helpers.response import JsonResponse

@md.URL('ticket')
@md.URL('ticket/<int:pk>')
def on_ticket(request, pk=None):
    return Ticket.on_rest_request(request, pk)


@md.URL('ticket/note')
@md.URL('ticket/<int:pk>/note')
def on_ticket_note(request, pk=None):
    return TicketNote.on_rest_request(request, pk)


@md.GET('stats')
@md.requires_auth()
def on_incident_stats(request, pk=None):
    from mojo.apps.incident.models import Incident, Event
    import datetime
    recent = datetime.datetime.now() - datetime.timedelta(days=1)
    events = Event.objects.filter(created__gte=recent)
    incidents = Incident.objects.filter(created__gte=recent)
    resp = {
        'tickets': {
            'new': Ticket.objects.filter(status='new').count(),
            'open': Ticket.objects.filter(status='open').count(),
            'paused': Ticket.objects.filter(status='paused').count()
        },
        'incidents': {
            'new': Incident.objects.filter(status='new').count(),
            'open': Incident.objects.filter(status='open').count(),
            'paused': Incident.objects.filter(status='paused').count(),
            'recent': incidents.count()
        },
        'events': {
            'recent': events.count(),
            'warnings': events.filter(level__lte=7).count(),
            'critical': events.filter(level__gt=7).count()
        }
    }
    return JsonResponse(dict(status=True, data=resp))
