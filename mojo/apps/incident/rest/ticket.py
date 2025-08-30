from mojo import decorators as md
from mojo.apps.incident.models import Ticket, TicketNote


@md.URL('ticket')
@md.URL('ticket/<int:pk>')
def on_ticket(request, pk=None):
    return Ticket.on_rest_request(request, pk)


@md.URL('ticket/note')
@md.URL('ticket/<int:pk>/note')
def on_ticket_note(request, pk=None):
    return TicketNote.on_rest_request(request, pk)
