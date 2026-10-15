from django.db import models

from mojo.models import MojoModel


SOURCE_PROVISIONED = "provisioned"
SOURCE_BYO = "byo"
SOURCES = [
    (SOURCE_PROVISIONED, "Created by django-mojo"),
    (SOURCE_BYO, "Brought by the operator"),
]

STATUS_PENDING = "pending"
STATUS_DEPLOYED = "deployed"
STATUS_UNKNOWN = "unknown"


class CdnDistribution(models.Model, MojoModel):
    """
    CDN delivery for ONE S3 bucket (maestro #8250).

    A CloudFront distribution fronts one bucket, so this is the unit: every
    file manager that stores into `bucket_name` shares it and gets its own
    cache behavior for its prefix (public prefixes unsigned, private prefixes
    signed). Keeping the state here rather than in manager settings matters
    because `FileManager.get_for_user/get_for_group` copy the system
    manager's secrets into every child manager they create — per-manager CDN
    settings would fan out into thousands of copies that disable and key
    rotation could never keep in step. Here, disabling the CDN or rotating
    the key is one row and every manager on the bucket follows at once.

    A manager opts out with its own (or top-parent) setting `cdn = "off"`.
    """

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    bucket_name = models.CharField(max_length=63, unique=True)
    # CloudFront ids: distributions, OACs and public keys are 13-14 characters,
    # key groups are 36-character UUIDs (maestro #8305). 64 leaves room.
    distribution_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    distribution_arn = models.CharField(max_length=128, blank=True, default="")
    domain_name = models.CharField(
        max_length=128, blank=True, default="",
        help_text="The distribution's own host, e.g. d111111abcdef8.cloudfront.net.")
    alias = models.CharField(
        max_length=253, blank=True, default="",
        help_text="Optional custom domain served by the distribution. Links "
                  "use it when set.")
    oac_id = models.CharField(max_length=64, blank=True, default="")
    key_group_id = models.CharField(max_length=64, blank=True, default="")
    signing_key = models.ForeignKey(
        "fileman.CdnSigningKey", related_name="distributions",
        null=True, blank=True, default=None, on_delete=models.SET_NULL,
        help_text="The key that signs private links for this bucket.")
    source = models.CharField(max_length=16, choices=SOURCES, default=SOURCE_PROVISIONED)
    is_enabled = models.BooleanField(default=True, db_index=True)
    status = models.CharField(max_length=16, default=STATUS_UNKNOWN)
    last_check = models.JSONField(
        default=dict, blank=True,
        help_text="The most recent check/enable findings, aws-check shaped.")

    class Meta:
        db_table = "fileman_cdn_distribution"

    class RestMeta:
        # Driven through FileManager actions and the fileman_cdn command; the
        # graph is what those reports and the portal's CDN row render.
        CAN_CREATE = False
        CAN_DELETE = False
        VIEW_PERMS = ["view_fileman", "manage_files", "files"]
        SAVE_PERMS = []  # written by provisioning and the fileman_cdn command only
        GRAPHS = {
            "default": {
                "fields": ["id", "created", "modified", "bucket_name", "distribution_id",
                           "domain_name", "alias", "oac_id", "key_group_id", "source",
                           "is_enabled", "status", "last_check"],
                "extra": ["base_url"],
                "graphs": {"signing_key": "basic"},
            },
            "basic": {
                "fields": ["id", "bucket_name", "distribution_id", "domain_name", "alias",
                           "source", "is_enabled", "status"],
                "extra": ["base_url"],
            },
        }

    def __str__(self):
        return f"CDN for {self.bucket_name} ({self.distribution_id or 'unprovisioned'})"

    @property
    def base_url(self):
        host = (self.alias or self.domain_name or "").strip().rstrip("/")
        if not host:
            return ""
        if host.startswith("https://"):
            return host
        return f"https://{host}"
