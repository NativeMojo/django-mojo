from mojo import decorators as md
from mojo.apps.aws.models import IncomingEmail, SentMessage

"""
AWS Email Messages REST Handlers

Endpoints:
- Incoming emails (list/detail via model.on_rest_request):
  - GET/POST/PUT/DELETE /email/incoming
  - GET/POST/PUT/DELETE /email/incoming/<int:pk>

- Sent messages (list/detail via model.on_rest_request):
  - GET/POST/PUT/DELETE /email/sent
  - GET/POST/PUT/DELETE /email/sent/<int:pk>

All endpoints require the "manage_aws" permission and delegate to the models' on_rest_request
for CRUD operations, leveraging RestMeta permissions and graphs.
"""


@md.URL('email/incoming')
@md.URL('email/incoming/<int:pk>')
@md.requires_perms("manage_aws")
def on_incoming_email(request, pk=None):
    return IncomingEmail.on_rest_request(request, pk)


@md.URL('email/sent')
@md.URL('email/sent/<int:pk>')
@md.requires_perms("manage_aws")
def on_sent_message(request, pk=None):
    return SentMessage.on_rest_request(request, pk)
