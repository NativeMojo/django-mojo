from mojo import decorators as md
from mojo.apps.incident.models import MaestroBoard, MaestroBoardLink


@md.URL('maestro/board')
@md.URL('maestro/board/<int:pk>')
@md.uses_model_security(MaestroBoard)
def on_maestro_board(request, pk=None):
    return MaestroBoard.on_rest_request(request, pk)


@md.URL('maestro/link')
@md.URL('maestro/link/<int:pk>')
@md.uses_model_security(MaestroBoardLink)
def on_maestro_link(request, pk=None):
    return MaestroBoardLink.on_rest_request(request, pk)
