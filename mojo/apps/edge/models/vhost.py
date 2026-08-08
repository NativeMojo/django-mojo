from django.db import models
from django.db.models import Q

from mojo.models import MojoModel


KIND_STATIC = "static"
KIND_SPA = "spa"
KIND_PROXY = "proxy"

KINDS = [
    (KIND_STATIC, "Static files"),
    (KIND_SPA, "Single-page app (history fallback)"),
    (KIND_PROXY, "Reverse proxy to a declared upstream"),
]


class Vhost(models.Model, MojoModel):
    """
    One nginx `server` block, as structured data.

    Nothing here is nginx syntax. `server_name` is derived from the `Domain`
    FK, the web root is derived from this row's own primary key, and a proxy
    destination is a foreign key to a platform-declared `Upstream`. There is no
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
        max_length=16, choices=KINDS, default=KIND_STATIC,
        help_text="static | spa | proxy")

    upstream = models.ForeignKey(
        "edge.Upstream",
        related_name="vhosts",
        null=True, blank=True, default=None,
        on_delete=models.PROTECT,
        help_text="Required for kind=proxy, forbidden otherwise.")

    certificate = models.ForeignKey(
        "dnsman.Certificate",
        related_name="vhosts",
        on_delete=models.PROTECT,
        help_text="Must belong to this domain and cover the server name.")

    pool = models.CharField(
        max_length=32, default="default", db_index=True,
        help_text="Which fleet pool serves this vhost. Nodes poll by pool.")

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
            models.CheckConstraint(
                condition=(
                    Q(kind=KIND_PROXY, upstream__isnull=False)
                    | (~Q(kind=KIND_PROXY) & Q(upstream__isnull=True))
                ),
                name="edge_vhost_kind_upstream"),
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
                    "pool", "is_enabled",
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

        validators.validate_vhost(self)
        return super().save(*args, **kwargs)
