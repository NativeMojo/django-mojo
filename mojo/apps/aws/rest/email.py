from mojo import decorators as md
from mojo.apps.aws.models import EmailDomain, Mailbox


"""
AWS Email REST Handlers

Endpoints:
- Domain CRUD:
  - GET/POST/PUT/DELETE /aws/email/domain
  - GET/POST/PUT/DELETE /aws/email/domain/<int:pk>

- Mailbox CRUD:
  - GET/POST/PUT/DELETE /aws/email/mailbox
  - GET/POST/PUT/DELETE /aws/email/mailbox/<int:pk>

These handlers delegate to the models' on_rest_request, which uses RestMeta for
permission checks, graphs, and default CRUD behavior.
"""


@md.URL('email/domain')
@md.URL('email/domain/<int:pk>')
@md.requires_perms("manage_aws")
def on_email_domain(request, pk=None):
    return EmailDomain.on_rest_request(request, pk)


@md.URL('email/mailbox')
@md.URL('email/mailbox/<int:pk>')
@md.requires_perms("manage_aws")
def on_mailbox(request, pk=None):
    return Mailbox.on_rest_request(request, pk)
