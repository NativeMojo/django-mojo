from django.db import models

from mojo.models import MojoModel


class WebAppRoute(models.Model, MojoModel):
    """A WebApp's durable custom path-routing intent.

    ``VhostRoute`` rows are materialized edge configuration: one copy for the
    primary address and one for every alias. They disappear with a vhost when
    an app goes offline. This row belongs to the app instead, so restoring or
    changing an address can reproduce the same custom serving contract.

    Framework-managed authentication routes never belong here. They remain
    derived from ``webapp_auth_routes.auth_route_prefixes()`` and are rebuilt
    independently, so a settings change cannot leave a stored ``managed`` bit
    lying about who owns a path.
    """

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    web_app = models.ForeignKey(
        "edge.WebApp",
        related_name="desired_routes",
        on_delete=models.CASCADE,
        help_text="The app whose every serving address materializes this path.",
    )

    path_prefix = models.CharField(
        max_length=128,
        help_text="Canonical custom request-path prefix, such as /api.",
    )

    upstream = models.ForeignKey(
        "edge.Upstream",
        related_name="web_app_routes",
        on_delete=models.PROTECT,
        help_text="Declared destination retained while the app is offline.",
    )

    class Meta:
        db_table = "edge_web_app_route"
        constraints = [
            models.UniqueConstraint(
                fields=["web_app", "path_prefix"],
                name="edge_webapp_route_app_prefix_uniq",
            ),
        ]
        ordering = ["web_app", "path_prefix"]

    def __str__(self):
        return f"{self.web_app_id}:{self.path_prefix} -> {self.upstream_id}"

    def save(self, *args, **kwargs):
        """Keep desired rows canonical and inside the app's own tenancy."""
        from mojo import errors as me
        from mojo.apps.edge import validators

        prefix = str(self.path_prefix or "").rstrip("/") or "/"
        validators.validate_route_prefix(prefix)
        self.path_prefix = prefix
        if not self.web_app_id:
            raise me.ValueException("a WebApp route requires an app")
        if not self.upstream_id:
            raise me.ValueException("a WebApp route requires an upstream")
        if self.upstream.group_id not in (None, self.web_app.group_id):
            raise me.ValueException(
                "the upstream must be shared or belong to this WebApp's group")
        return super().save(*args, **kwargs)
