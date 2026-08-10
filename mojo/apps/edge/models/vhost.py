from django.db import models
from django.db.models import Q

from mojo.models import MojoModel


KIND_API = "api"
KIND_SITE = "site"
KIND_SITE_API = "site_api"
KIND_REDIRECT = "redirect"

KINDS = [
    (KIND_API, "Whole-host reverse proxy to a declared upstream"),
    (KIND_SITE, "Static site / single-page app"),
    (KIND_SITE_API, "Static site with proxied path prefixes"),
    (KIND_REDIRECT, "Permanent redirect to another host"),
]


class Vhost(models.Model, MojoModel):
    """
    One nginx server block pair (443 + 80), as structured data.

    Nothing here is nginx syntax. `server_name` is derived from the `Domain`
    FK, the web root is derived from this row's own primary key, and a proxy
    destination is a foreign key to a platform-declared `Upstream` — directly
    for `api`, per path prefix through `VhostRoute` for `site_api`. There is no
    field an admin can put a `;` into, which is the whole point — see
    `mojo/apps/edge/validators.py`.

    **There is no `root_slug`.** An earlier design had one, and a
    tenant-writable slug is a cross-tenant read the moment webapp releases land
    (item #1435): point it at another tenant's slug and serve their private
    build output from your own domain. The root is `<generation>/www/<pk>` — an
    integer cannot traverse, collide, or name someone else's content.
    """

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    domain = models.ForeignKey(
        "dnsman.Domain",
        related_name="vhosts",
        on_delete=models.CASCADE,
        help_text="Owns the name. Tenancy resolves through this.")

    label = models.CharField(
        max_length=63, blank=True, default="",
        help_text="'' serves the apex, '*' the wildcard, else one DNS label.")

    kind = models.CharField(
        max_length=16, choices=KINDS, default=KIND_SITE,
        help_text="api | site | site_api | redirect")

    upstream = models.ForeignKey(
        "edge.Upstream",
        related_name="vhosts",
        null=True, blank=True, default=None,
        on_delete=models.PROTECT,
        help_text="Required for kind=api, forbidden otherwise "
                  "(site_api proxies per-route, never whole-host).")

    certificate = models.ForeignKey(
        "dnsman.Certificate",
        related_name="vhosts",
        on_delete=models.PROTECT,
        help_text="Must belong to this domain and cover the server name.")

    pool = models.CharField(
        max_length=32, default="default", db_index=True,
        help_text="Which fleet pool serves this vhost. Nodes poll by pool.")

    spa = models.BooleanField(
        default=False,
        help_text="site/site_api only: history-fallback to index.html "
                  "instead of a 404.")

    body_size_mb = models.IntegerField(
        default=50,
        help_text="client_max_body_size, in megabytes (1-4096). Always "
                  "rendered — nginx's 1m default is never silently in play.")

    quiet_paths = models.JSONField(
        default=list, blank=True,
        help_text="api/site_api only: exact request paths kept out of the "
                  "main access log (health checks). Each is whitelist-checked.")

    serve_static = models.BooleanField(
        default=False,
        help_text="api/site_api only: serve the fleet's Django static root "
                  "at /static/ instead of proxying it.")

    redirect_to = models.CharField(
        max_length=253, null=True, blank=True, default=None,
        help_text="redirect only: target HOST (FQDN, validated as a server "
                  "name — never a free URL).")

    is_enabled = models.BooleanField(default=True, db_index=True)

    class Meta:
        db_table = "edge_vhost"
        constraints = [
            # Two ENABLED vhosts cannot claim the same name. This constraint —
            # not `nginx -t` — is the collision defence: nginx treats a
            # duplicate server_name as a WARNING, silently ignores one block,
            # and still exits 0. A disabled row may sit alongside a live one so
            # a replacement can be staged.
            models.UniqueConstraint(
                fields=["domain", "label"],
                condition=Q(is_enabled=True),
                name="edge_vhost_unique_enabled_server_name"),
            # The kind decides which destination fields a row may carry, and
            # the DB enforces it so a bulk write cannot produce a hybrid row
            # the renderer has no branch for.
            models.CheckConstraint(
                condition=(
                    Q(kind=KIND_API, upstream__isnull=False,
                      redirect_to__isnull=True)
                    | Q(kind__in=(KIND_SITE, KIND_SITE_API),
                        upstream__isnull=True, redirect_to__isnull=True)
                    | Q(kind=KIND_REDIRECT, upstream__isnull=True,
                        redirect_to__isnull=False)
                ),
                name="edge_vhost_kind_fields"),
        ]
        indexes = [
            models.Index(fields=["pool", "is_enabled"]),
            models.Index(fields=["certificate", "is_enabled"]),
        ]
        ordering = ["domain", "label"]

    class RestMeta:
        CAN_DELETE = True
        # Tenancy resolves through the owning Domain, exactly as Certificate
        # does — a Vhost has no group of its own.
        GROUP_FIELD = "domain__group"
        VIEW_PERMS = ["view_dns", "manage_dns", "security"]
        SAVE_PERMS = ["manage_dns", "security"]
        DELETE_PERMS = ["manage_dns", "security"]
        LOG_CHANGES = True
        SEARCH_FIELDS = ["label", "kind", "pool"]
        # A declared NO_SAVE_FIELDS list REPLACES the framework default, so the
        # defaults have to be re-included.
        #
        # `domain` is deliberately NOT here. NO_SAVE_FIELDS is applied on the
        # CREATE path too (mojo/models/rest.py), so pinning it would make a
        # vhost impossible to create over REST at all. Immutability after
        # create is enforced in on_rest_pre_save below instead.
        NO_SAVE_FIELDS = ["id", "pk", "created", "uuid"]
        GRAPHS = {
            "basic": {
                "fields": ["id", "kind", "is_enabled"],
                "extra": ["server_name"],
            },
            "default": {
                "fields": [
                    "id", "created", "modified", "label", "kind",
                    "pool", "spa", "body_size_mb", "quiet_paths",
                    "serve_static", "redirect_to", "is_enabled",
                ],
                "extra": ["server_name"],
                "graphs": {
                    "domain": "basic",
                    "upstream": "basic",
                    "certificate": "basic",
                },
            },
            "list": {
                "fields": ["id", "created", "kind", "pool", "is_enabled"],
                "extra": ["server_name"],
                "graphs": {"domain": "basic"},
            },
        }

    @classmethod
    def on_rest_list_filter(cls, request, queryset):
        """Keep house serving inventory out of non-superuser lists."""
        if getattr(request.user, "is_superuser", False) is not True:
            queryset = queryset.exclude(domain__group__isnull=True)
        return super().on_rest_list_filter(request, queryset)

    def __str__(self):
        return f"{self.server_name} [{self.kind}]"

    @property
    def server_name(self):
        """Derived, never stored. See validators.server_name_for."""
        from mojo.apps.edge import validators

        if not self.domain_id:
            return None
        return validators.server_name_for(self.domain.name, self.label or "")

    @property
    def web_root_name(self):
        """The generation-relative directory this vhost serves from.

        An integer, so it cannot traverse or collide. #1435 stages
        `<generation>/www/<this>` as a symlink to the installed release.
        """
        return str(self.pk)

    def on_rest_pre_save(self, changed_fields, created):
        """`domain` is set once and never moved.

        Re-pointing it would move the row between TENANTS, so it cannot be a
        plain writable field — but it also cannot live in NO_SAVE_FIELDS, which
        is enforced on create as well. This is the middle: settable exactly
        once.
        """
        import mojo.errors as me

        if created:
            if not self.domain_id:
                raise me.ValueException("a vhost requires a domain")
            return
        if "domain" in (changed_fields or {}):
            raise me.ValueException(
                "a vhost cannot be moved to another domain")

    def save(self, *args, **kwargs):
        """Validate on EVERY write path — see Upstream.save for the reasoning."""
        from mojo.apps.edge import validators
        from mojo.apps.edge.services import convergence

        previous_pool = None
        if self.pk:
            previous_pool = type(self).objects.filter(pk=self.pk).values_list(
                "pool", flat=True).first()
        validators.validate_vhost(self)
        result = super().save(*args, **kwargs)
        convergence.publish_after_commit(previous_pool, self.pool)
        return result

    def delete(self, *args, **kwargs):
        from mojo.apps.edge.services import convergence
        pool = self.pool
        result = super().delete(*args, **kwargs)
        convergence.publish_after_commit(pool)
        return result
