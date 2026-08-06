from django.db import models

from mojo.models import MojoModel


class WebApp(models.Model, MojoModel):
    """
    A site whose build output this system tracks and promotes.

    django-mojo **tracks and orchestrates**; it does not build and it does not
    proxy the upload. CI builds, uploads to S3 directly, and registers what
    landed. The database — not an object in a bucket — is the authority on
    which release is live, because two sources of truth eventually disagree and
    nobody can tell which won.

    **`slug` is a label, not a path.** Nothing on disk is named after it: a
    vhost's web root is derived from `Vhost.pk` (see `vhost.py`). That is what
    keeps one tenant from pointing a vhost at another tenant's installed build
    output, which is what a shared string namespace would have allowed.
    """

    STATUS_FIELDS = ()

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    group = models.ForeignKey(
        "account.Group",
        related_name="web_apps",
        on_delete=models.CASCADE,
        help_text="Owns this site. Explicit, because vhost may be null.")

    slug = models.CharField(
        max_length=64, db_index=True,
        help_text="Human label for this site. NOT a filesystem path.")

    vhost = models.OneToOneField(
        "edge.Vhost",
        related_name="web_app",
        null=True, blank=True, default=None,
        on_delete=models.SET_NULL,
        help_text="Null for a site served elsewhere (e.g. CloudFront). "
                  "Such a site is registerable and rollbackable but never "
                  "appears in a node's desired state.")

    bucket = models.CharField(
        max_length=255,
        help_text="Must be one of EDGE_RELEASE_BUCKETS. Never caller-chosen.")

    prefix = models.CharField(
        max_length=255,
        help_text="DERIVED from group and id. Never caller-supplied.")

    api_key = models.OneToOneField(
        "account.ApiKey",
        related_name="web_app",
        null=True, blank=True, default=None,
        on_delete=models.SET_NULL,
        help_text="The CI credential allowed to register releases for this "
                  "site. One key per site, enforced by the schema.")

    auto_promote = models.BooleanField(
        default=False,
        help_text="Whether a verified upload goes live without a human.")

    current_release = models.ForeignKey(
        "edge.WebAppRelease",
        related_name="+",
        null=True, blank=True, default=None,
        on_delete=models.SET_NULL,
        help_text="What nodes should be serving. Rollback repoints this.")

    class Meta:
        db_table = "edge_web_app"
        constraints = [
            # Scoped per tenant, NOT globally unique: a global constraint lets
            # one tenant squat another's intended slug, and the duplicate error
            # leaks that the slug exists.
            models.UniqueConstraint(
                fields=["group", "slug"], name="edge_webapp_group_slug_uniq"),
        ]
        ordering = ["slug"]

    class RestMeta:
        CAN_DELETE = True
        GROUP_FIELD = "group"
        VIEW_PERMS = ["view_dns", "manage_dns", "security"]
        SAVE_PERMS = ["manage_dns", "security"]
        DELETE_PERMS = ["manage_dns", "security"]
        LOG_CHANGES = True
        SEARCH_FIELDS = ["slug"]
        # Every field below is either a promotion decision or an authorization
        # decision wearing a field's clothes.
        #
        # `bucket` and `prefix` are here for a specific reason: the API mints
        # presigned uploads signed with the PLATFORM's static AWS credentials
        # (mojo/helpers/aws/s3.py has one global S3Config, shared with KMS). A
        # writable `bucket` would let a tenant holding manage_dns name any
        # bucket that key can reach — fileman, terraform state, logs — and have
        # us sign uploads into it. `bucket` comes from an allowlist and
        # `prefix` is derived; neither is ever accepted from a caller.
        NO_SAVE_FIELDS = [
            "id", "pk", "created", "uuid",
            "group", "bucket", "prefix", "api_key", "current_release",
        ]
        GRAPHS = {
            "basic": {
                "fields": ["id", "slug", "auto_promote"],
            },
            "default": {
                "fields": [
                    "id", "created", "modified", "slug", "bucket", "prefix",
                    "auto_promote",
                ],
                "graphs": {
                    "group": "basic",
                    "vhost": "basic",
                    "current_release": "basic",
                },
            },
            "list": {
                "fields": ["id", "created", "slug", "auto_promote"],
                "graphs": {"current_release": "basic"},
            },
        }

    def __str__(self):
        return f"{self.slug} ({self.group})"

    def storage_prefix(self):
        """Where CI uploads for this site. Derived, never stored from input."""
        return f"webapps/{self.group_id}/{self.pk}"

    def release_prefix(self, version):
        from mojo.apps.edge import validators

        validators.validate_release_version(version)
        return f"{self.storage_prefix()}/releases/{version}"

    def save(self, *args, **kwargs):
        from mojo.apps.edge import validators

        validators.validate_web_app(self)
        if self.pk and self.prefix != self.storage_prefix():
            # Recomputed on every save so a hand-edited row heals rather than
            # keeping a prefix that no longer matches its group.
            self.prefix = self.storage_prefix()
        return super().save(*args, **kwargs)

    def on_rest_created(self):
        """`storage_prefix` needs a pk, which does not exist until the insert."""
        self.prefix = self.storage_prefix()
        self.save()
