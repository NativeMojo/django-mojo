"""Email (SES) feature capabilities for the built-in Admin portal."""

from django.apps import apps


def describe(request, capabilities):
    installed = apps.is_installed("mojo.apps.aws")
    values = {
        # One capability tier today: no read-only email permission exists in
        # this repo, so view and manage carry the same predicate. The pair is
        # kept so a future read-only grant is an additive change here, not a
        # page rewrite.
        "view": bool(installed and capabilities.get("email")),
        "manage": bool(installed and capabilities.get("email")),
    }
    return {"id": "email", "enabled": values["view"], "capabilities": values}
