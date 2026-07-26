from django.db import models
from mojo.models import MojoModel
from mojo.helpers import logit


PROVIDER_ROUTE53 = "route53"
PROVIDER_GODADDY = "godaddy"

STATUS_PENDING = "pending"
STATUS_REGISTERING = "registering"
STATUS_ACTIVE = "active"
STATUS_FAILED = "failed"


class Domain(models.Model, MojoModel):
    """
    A domain name whose DNS this system manages.

    A Domain arrives one of three ways: purchased through Route53 Domains,
    adopted from an existing Route53 hosted zone, or registered against a
    linked provider credential the caller already controls (BYO).

    Invariant — no `failed` rows persist. Every failure path deletes the row
    and leaves the DomainPurchase ledger as the audit trail, so the unique
    `name` constraint only ever guards live registrations and cannot be
    poisoned by a past failure.
    """

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    group = models.ForeignKey(
        "account.Group",
        related_name="dns_domains",
        null=True, blank=True, default=None,
        on_delete=models.CASCADE)

    user = models.ForeignKey(
        "account.User",
        related_name="dns_domains",
        null=True, blank=True, default=None,
        on_delete=models.SET_NULL,
        help_text="Who brought this domain into the system.")

    name = models.CharField(
        max_length=253,
        unique=True,
        db_index=True,
        help_text="Normalized domain name: lowercase, IDNA-encoded, no trailing dot.")

    provider = models.CharField(
        max_length=32,
        default=PROVIDER_ROUTE53,
        db_index=True,
        help_text="Where this domain's DNS lives: route53 | godaddy.")

    credential = models.ForeignKey(
        "dnsman.DnsCredential",
        related_name="domains",
        null=True, blank=True, default=None,
        on_delete=models.SET_NULL,
        help_text="Required for providers we do not hold house credentials for.")

    status = models.CharField(
        max_length=32,
        default=STATUS_PENDING,
        db_index=True,
        help_text="pending | registering | active | failed")

    hosted_zone_id = models.CharField(
        max_length=128, null=True, blank=True, default=None,
        help_text="Route53 hosted zone id. Null for non-route53 providers.")

    auto_renew = models.BooleanField(default=True)
    privacy = models.BooleanField(
        default=True,
        help_text="WHOIS privacy. Not offered by every TLD — see route53.TLDS_WITHOUT_PRIVACY.")

    verified = models.BooleanField(
        default=False,
        help_text="Ownership confirmed. Always true for domains we registered or adopted; "
                  "set by the provider API probe for BYO domains.")

    registered_on = models.DateTimeField(null=True, blank=True, default=None)
    expires = models.DateTimeField(null=True, blank=True, default=None, db_index=True)

    last_error = models.TextField(blank=True, null=True, default=None)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "dnsman_domain"
        indexes = [
            models.Index(fields=["group", "status"]),
            models.Index(fields=["provider", "status"]),
        ]
        ordering = ["name"]

    class RestMeta:
        # Rows are created only by the registrar/onboarding services — never by
        # a bare POST to the collection route. CAN_CREATE defaults to True, so
        # this has to be declared explicitly.
        CAN_CREATE = False
        CAN_DELETE = True
        VIEW_PERMS = ["view_dns", "manage_dns", "security"]
        SAVE_PERMS = ["manage_dns", "security"]
        DELETE_PERMS = ["manage_dns", "security"]
        SEARCH_FIELDS = ["name", "provider", "status"]
        # A declared NO_SAVE_FIELDS list REPLACES the framework default
        # (["id", "pk", "created", "uuid"]), so those have to be re-included.
        NO_SAVE_FIELDS = [
            "id", "pk", "created", "name", "provider", "status",
            "hosted_zone_id", "registered_on", "expires", "verified",
            "group", "user", "last_error",
        ]
        GRAPHS = {
            "basic": {
                "fields": ["id", "name", "provider", "status", "expires"]
            },
            "default": {
                "fields": [
                    "id", "created", "modified", "name", "provider", "status",
                    "hosted_zone_id", "auto_renew", "privacy", "verified",
                    "registered_on", "expires", "last_error",
                ],
                "graphs": {
                    "group": "basic",
                    "user": "basic",
                    "credential": "basic",
                },
            },
            "list": {
                "fields": [
                    "id", "created", "name", "provider", "status", "expires",
                ],
                "graphs": {"group": "basic"},
            },
        }

    def __str__(self):
        return self.name

    @property
    def is_active(self):
        return self.status == STATUS_ACTIVE

    @property
    def requires_credential(self):
        """Route53 rides house AWS credentials; everything else must bring its own."""
        return self.provider != PROVIDER_ROUTE53

    @property
    def has_usable_credential(self):
        if not self.requires_credential:
            return True
        return bool(self.credential and self.credential.is_usable)

    def on_rest_pre_save(self, changed_fields, created):
        if self.name:
            from mojo.apps.dnsman.services import naming
            self.name = naming.normalize_domain(self.name)

    def on_rest_saved(self, changed_fields=None, created=False):
        """
        Push auto_renew / privacy changes to the registrar.

        Best-effort by design: a registrar hiccup records itself in last_error
        rather than failing the caller's save, because the DB row is the thing
        the user just edited and a 500 here would be misleading.
        """
        if created or self.provider != PROVIDER_ROUTE53:
            return
        fields = set(changed_fields or [])
        if not fields.intersection({"auto_renew", "privacy"}):
            return
        from mojo.helpers.aws import route53
        try:
            if "auto_renew" in fields:
                route53.update_auto_renew(self.name, self.auto_renew)
            if "privacy" in fields:
                route53.update_privacy(self.name, self.privacy)
        except Exception as err:
            logit.error(f"dnsman: registrar push failed for {self.name}: {err}")
            self.last_error = str(err)
            self.save(update_fields=["last_error", "modified"])
