"""WebApp deployment-key linkage shared by REST and bootstrap tooling."""

from django.db import transaction

from mojo import errors as me

from mojo.apps.edge.models import WebApp


@transaction.atomic
def link(web_app, rotate=False):
    """Link a release-only key, refusing implicit credential rotation."""
    from mojo.apps.account.models import ApiKey

    # api_key is nullable, so joining it here creates an outer join that
    # PostgreSQL cannot lock with SELECT FOR UPDATE.
    locked = WebApp.objects.select_for_update().select_related(
        "group").get(pk=web_app.pk)
    previous = locked.api_key
    if previous is not None and not rotate:
        raise me.ValueException(
            "this WebApp already has a deployment key; pass rotate explicitly")

    api_key, token = ApiKey.create_for_group(
        locked.group,
        f"webapp:{locked.slug}",
        permissions={"release_webapp": True})
    locked.api_key = api_key
    locked.save(update_fields=["api_key", "modified"])

    if previous is not None:
        previous.is_active = False
        previous.save(update_fields=["is_active", "modified"])

    locked.log(
        f"Release key {'rotated' if previous else 'minted'} for "
        f"'{locked.slug}'",
        "edge:webapp_key")
    return locked, api_key, token, bool(previous)
