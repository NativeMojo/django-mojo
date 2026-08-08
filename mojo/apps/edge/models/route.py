from django.db import models

from mojo.models import MojoModel


class VhostRoute(models.Model, MojoModel):
    """
    One proxied path prefix on a `site_api` vhost.

    The same structured-data rule as `Vhost` itself: the prefix is
    whitelist-matched (`validators.validate_route_prefix`) and the destination
    is a foreign key to a platform-declared `Upstream` — a route can never name
    a place traffic goes, only pick one the platform already allows.
    """

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    vhost = models.ForeignKey(
        "edge.Vhost",
        related_name="routes",
        on_delete=models.CASCADE,
        help_text="The site_api vhost this prefix belongs to.")

    path_prefix = models.CharField(
        max_length=128,
        help_text="Request-path prefix to proxy (e.g. /api). Whitelist-"
                  "checked; never nginx syntax.")

    upstream = models.ForeignKey(
        "edge.Upstream",
        related_name="routes",
        on_delete=models.PROTECT,
        help_text="Where this prefix proxies. Platform-declared rows only.")

    class Meta:
        db_table = "edge_vhost_route"
        constraints = [
            models.UniqueConstraint(
                fields=["vhost", "path_prefix"],
                name="edge_route_vhost_prefix_uniq"),
        ]
        ordering = ["vhost", "path_prefix"]

    class RestMeta:
        CAN_DELETE = True
        # Tenancy resolves through the owning vhost's domain, exactly as the
        # vhost's own does. The `__` path is traversed hop-by-hop by
        # `_resolve_group_from_instance`.
        GROUP_FIELD = "vhost__domain__group"
        VIEW_PERMS = ["view_dns", "manage_dns", "security"]
        SAVE_PERMS = ["manage_dns", "security"]
        DELETE_PERMS = ["manage_dns", "security"]
        LOG_CHANGES = True
        SEARCH_FIELDS = ["path_prefix"]
        GRAPHS = {
            "basic": {
                "fields": ["id", "path_prefix"],
                "graphs": {"upstream": "basic"},
            },
            "default": {
                "fields": ["id", "created", "modified", "path_prefix"],
                "graphs": {
                    "vhost": "basic",
                    "upstream": "basic",
                },
            },
        }

    @classmethod
    def on_rest_list_filter(cls, request, queryset):
        """Keep house serving inventory out of non-superuser lists —
        mirrors Vhost.on_rest_list_filter."""
        if getattr(request.user, "is_superuser", False) is not True:
            queryset = queryset.exclude(vhost__domain__group__isnull=True)
        return super().on_rest_list_filter(request, queryset)

    def __str__(self):
        return f"{self.vhost_id}:{self.path_prefix} -> {self.upstream_id}"

    def save(self, *args, **kwargs):
        """Validate on EVERY write path — see Upstream.save for the reasoning."""
        from mojo.apps.edge import validators

        validators.validate_route(self)
        return super().save(*args, **kwargs)
