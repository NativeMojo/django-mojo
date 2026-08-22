"""The two ordering-critical WebApp teardowns, owned in one place.

Both used to live inline in the surface that called them — "take offline" in
``rest/web_app.py`` and "delete the app" in ``WebApp.on_rest_delete``. That was
fine while the browser was the only surface. It stops being fine the moment a
second surface (the Admin Assistant) needs the same operation: a copied
destructive transaction is a fork that agrees today and drifts tomorrow.

Neither function performs any authorization. Each caller applies its own — the
REST layer through ``rest_check_permission_or_raise``, the assistant through
``webapp_authority`` — because the two surfaces authenticate differently and
only the caller knows who is asking.
"""

from django.db import transaction


def take_offline(web_app):
    """Take a site offline: unlink and delete its serving vhost, keep the app.

    The vhost's own ``delete()`` publishes fleet convergence, so nodes drop the
    server block without waiting for the sweep.

    Every ALIAS address goes too, in the same transaction. "Offline" that left
    the customer's own domain serving would be the opposite of what was asked,
    and desired state drops an app's alias rows the moment it has no primary
    anyway (see ``releases.desired_webapps``).
    """
    from mojo.apps.edge.models import Vhost, WebApp

    with transaction.atomic():
        locked = WebApp.objects.select_for_update().get(pk=web_app.pk)
        vhost = locked.vhost
        if vhost is not None:
            locked.vhost = None
            locked.save(update_fields=["vhost", "modified"])
            if vhost.kind == "site":
                vhost.delete()
        for alias in Vhost.objects.filter(alias_of=locked):
            alias.delete()
    return {"webapp": web_app.pk, "address": None}


def teardown(web_app):
    """Delete the app, its deploy key, and its addresses in ONE transaction.

    The framework ``on_rest_pre_delete`` hook runs OUTSIDE the delete
    transaction, so teardown there could commit while the row delete later
    raises — a live site whose address was torn down and whose CI key was
    already killed. A bare cascade fails the other way: it orphans the serving
    vhost (nginx keeps a rootless server block) and leaves the
    ``MOJO_DEPLOY_KEY`` credential active long after the site it was scoped to
    is gone. Doing both here, atomically, keeps delete all-or-nothing.

    Raises whatever the underlying writes raise; the REST delete path turns
    that into its own 400 response.
    """
    from mojo.apps.edge.models import Vhost, WebApp

    with transaction.atomic():
        # api_key is nullable; a select_related join cannot be locked with
        # SELECT FOR UPDATE, so resolve it through the FK after the row lock
        # (same reasoning as webapp_keys._mint_locked).
        locked = WebApp.objects.select_for_update().get(pk=web_app.pk)
        api_key = locked.api_key
        if api_key is not None:
            api_key.is_active = False
            api_key.save(update_fields=["is_active", "modified"])
            locked.api_key = None
            locked.save(update_fields=["api_key", "modified"])
        vhost = locked.vhost
        if vhost is not None and vhost.kind in ("site", "site_api"):
            locked.vhost = None
            locked.save(update_fields=["vhost", "modified"])
            # Vhost.delete() publishes fleet convergence on commit, so nodes
            # drop the server block without waiting for the sweep.
            vhost.delete()
        # Alias addresses die with the app, in this same transaction.
        # `alias_of` cascades, but a bare cascade deletes the ROWS without
        # running Vhost.delete() — so nodes would keep serving every custom
        # domain until the next sweep. One explicit loop, one convergence
        # publish each.
        for alias in Vhost.objects.filter(alias_of=locked):
            alias.delete()
        locked.delete()
    return {"webapp": web_app.pk, "deleted": True}
