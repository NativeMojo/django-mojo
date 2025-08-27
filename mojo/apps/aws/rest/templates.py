from mojo import decorators as md
from mojo.apps.aws.models import EmailTemplate

"""
EmailTemplate REST Handlers

CRUD endpoints:
- GET/POST/PUT/DELETE /aws/email/template
- GET/POST/PUT/DELETE /aws/email/template/<int:pk>

These delegate to the model's on_rest_request, leveraging RestMeta for permissions and graphs.
"""


@md.URL('aws/email/template')
@md.URL('aws/email/template/<int:pk>')
@md.requires_perms("manage_aws")
def on_email_template(request, pk=None):
    return EmailTemplate.on_rest_request(request, pk)
