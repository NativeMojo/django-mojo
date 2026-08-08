from django.db import models
from django.db.models import Q

from mojo.models import MojoModel


KIND_HTTP = "http"
KIND_UNIX = "unix"

KINDS = [
    (KIND_HTTP, "HTTP host and port"),
    (KIND_UNIX, "Unix domain socket"),
]


class Upstream(models.Model, MojoModel):
    """
    A declared destination a `proxy` vhost may forward to.

    **This row IS the allowlist.** The reason a vhost cannot proxy to the
    instance metadata service is not a filter on a text field — it is that
    `Vhost.upstream` is a foreign key, and the only way to create a row on the
    other end of it is a platform-admin REST action (see
    `mojo/apps/edge/rest/upstream.py`). A tenant admin picks from the upstreams
    visible to them; they can never mint one.

    A group-less row is a house upstream, offered to every tenant. A row with a
    group is that tenant's own.
    """

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    group = models.ForeignKey(
        "account.Group",
        related_name="edge_upstreams",
        null=True, blank=True, default=None,
        on_delete=models.CASCADE,
        help_text="Null for a house upstream every tenant may select.")

    name = models.CharField(max_length=64, db_index=True)

    kind = models.CharField(
        max_length=16, choices=KINDS, default=KIND_HTTP,
        help_text="http | unix")

    host = models.CharField(
        max_length=253, null=True, blank=True, default=None,
        help_text="kind=http only. Hostname or IPv4 address; never a URL.")

    port = models.IntegerField(
        null=True, blank=True, default=None,
        help_text="kind=http only.")

    socket_path = models.CharField(
        max_length=255, null=True, blank=True, default=None,
        help_text="kind=unix only. Must resolve under EDGE_SOCKET_BASE.")

    is_enabled = models.BooleanField(default=True, db_index=True)

    class Meta:
        db_table = "edge_upstream"
        constraints = [
            models.UniqueConstraint(
                fields=["group", "name"],
                name="edge_upstream_group_name_uniq"),
            # A house upstream has a NULL group, and NULLs do not collide in a
            # unique index — so the constraint above cannot keep house names
            # distinct. This one does.
            models.UniqueConstraint(
                fields=["name"],
                condition=Q(group__isnull=True),
                name="edge_upstream_house_name_uniq"),
            models.CheckConstraint(
                condition=(
                    Q(kind="http", host__isnull=False, port__isnull=False,
                      socket_path__isnull=True)
                    | Q(kind="unix", socket_path__isnull=False,
                        host__isnull=True, port__isnull=True)
                ),
                name="edge_upstream_kind_fields"),
        ]
        ordering = ["name"]

    class RestMeta:
        # Creation is a platform-admin act and goes through
        # POST edge/upstream/declare, never a bare POST to the collection.
        CAN_CREATE = False
        CAN_DELETE = False
        GROUP_FIELD = "group"
        VIEW_PERMS = ["view_dns", "manage_dns", "security"]
        SAVE_PERMS = ["manage_dns", "security"]
        LOG_CHANGES = True
        SEARCH_FIELDS = ["name", "kind"]
        # Everything that decides WHERE traffic goes is platform-controlled.
        # Leaving any of these savable would hand a tenant admin the free-form
        # destination the foreign key exists to deny them.
        NO_SAVE_FIELDS = [
            "id", "pk", "created", "group", "name", "kind",
            "host", "port", "socket_path",
        ]
        GRAPHS = {
            "basic": {
                "fields": ["id", "name", "kind"]
            },
            "default": {
                "fields": [
                    "id", "created", "modified", "name", "kind",
                    "host", "port", "socket_path", "is_enabled",
                ],
                "graphs": {"group": "basic"},
            },
        }

    @classmethod
    def on_rest_list_filter(cls, request, queryset):
        """Offer shared upstreams alongside the active tenant's rows.

        The framework applies ``GROUP_FIELD`` before this hook, so a request
        carrying an active group arrives with only that group's rows. House
        upstreams are deliberately shared; add them back here, then keep the
        standard search and request-filter pipeline intact.
        """
        if request.group is not None:
            queryset = queryset | cls.objects.filter(group__isnull=True)
        return super().on_rest_list_filter(request, queryset)

    def __str__(self):
        return f"{self.name} ({self.target})"

    @property
    def target(self):
        """The destination as nginx will name it."""
        if self.kind == KIND_UNIX:
            return f"unix:{self.socket_path}"
        return f"{self.host}:{self.port}"

    def save(self, *args, **kwargs):
        """Validate on EVERY write path, not only REST.

        The DB CheckConstraint above catches the kind/field pairing, but it
        cannot parse a host or normalise a socket path. Doing this in save()
        rather than in a REST hook is what makes a service-layer or shell write
        equally safe.
        """
        from mojo.apps.edge import validators

        validators.validate_upstream(self)
        return super().save(*args, **kwargs)
