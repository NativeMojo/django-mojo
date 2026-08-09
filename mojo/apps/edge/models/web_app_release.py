from django.db import models

from mojo.models import MojoModel


STATUS_PENDING = "pending"
STATUS_UPLOADED = "uploaded"
STATUS_LIVE = "live"
STATUS_SUPERSEDED = "superseded"

STATUSES = [
    (STATUS_PENDING, "Registered, upload not verified"),
    (STATUS_UPLOADED, "Verified in S3, not promoted"),
    (STATUS_LIVE, "Serving"),
    (STATUS_SUPERSEDED, "Was live, retained for rollback"),
]


class WebAppRelease(models.Model, MojoModel):
    """
    One immutable build of a `WebApp`.

    **`pending` is a real state, not bookkeeping.** CI registers a release and
    declares its manifest BEFORE uploading, so the API can mint one upload URL
    per declared file and then verify what actually landed. A release that is
    registered but unverified must never be promotable — an abandoned CI run
    would otherwise leave a row that looks shippable and 404s in production.

    `uploaded` and `live` are deliberately different states. Verification
    reaches `uploaded`; the WebApp deployment coordinator owns the transition
    to desired/live state and proves the active fleet separately.

    Rows are immutable once created. Rollback is repointing
    `WebApp.current_release` at an older row, which is why nothing here is
    editable and nothing is deletable.
    """

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    webapp = models.ForeignKey(
        "edge.WebApp",
        related_name="releases",
        on_delete=models.CASCADE)

    version = models.CharField(
        max_length=128, db_index=True,
        help_text="Build id or git SHA. Immutable and unique per site.")

    manifest = models.JSONField(
        default=list, blank=True,
        help_text="[{path, size, sha256}] — declared by CI before upload, "
                  "verified against S3 before the release becomes promotable.")

    status = models.CharField(
        max_length=16, choices=STATUSES, default=STATUS_PENDING, db_index=True)

    created_by = models.ForeignKey(
        "account.User",
        related_name="web_app_releases",
        null=True, blank=True, default=None,
        on_delete=models.SET_NULL)

    class Meta:
        db_table = "edge_web_app_release"
        constraints = [
            # Immutable releases are the property rollback depends on: a
            # version that could be re-registered would silently change what an
            # older, still-referenced row means.
            models.UniqueConstraint(
                fields=["webapp", "version"],
                name="edge_release_webapp_version_uniq"),
        ]
        indexes = [
            models.Index(fields=["webapp", "status"]),
        ]
        ordering = ["-created"]

    class RestMeta:
        # Rows are made by the register action only, and history is not
        # deletable — a rollback target that can be deleted is not a rollback
        # target.
        CAN_CREATE = False
        CAN_DELETE = False
        GROUP_FIELD = "webapp__group"
        VIEW_PERMS = ["view_dns", "manage_dns", "security"]
        SAVE_PERMS = ["manage_dns", "security"]
        LOG_CHANGES = True
        SEARCH_FIELDS = ["version", "status"]
        # `status` is a promotion decision. Leaving it savable would let a
        # manage_dns holder mark a pending release live with a field write,
        # bypassing both the manifest verification and the manage_webapp gate.
        NO_SAVE_FIELDS = [
            "id", "pk", "created", "uuid",
            "webapp", "version", "manifest", "status", "created_by",
        ]
        GRAPHS = {
            "basic": {
                "fields": ["id", "version", "status", "created"],
            },
            "default": {
                "fields": [
                    "id", "created", "modified", "version", "status",
                ],
                "extra": ["file_count"],
                "graphs": {
                    "webapp": "basic",
                    "created_by": "basic",
                },
            },
        }

    def __str__(self):
        return f"{self.webapp.slug}@{self.version} [{self.status}]"

    @property
    def file_count(self):
        return len(self.manifest or [])

    @property
    def is_promotable(self):
        """Only a verified upload may go live.

        `pending` means "registered, not verified" — the state an abandoned CI
        run leaves behind.
        """
        return self.status in (STATUS_UPLOADED, STATUS_LIVE, STATUS_SUPERSEDED)

    def storage_prefix(self):
        return self.webapp.release_prefix(self.version)
