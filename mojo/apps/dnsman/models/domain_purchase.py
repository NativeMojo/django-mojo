from django.db import models
from mojo.models import MojoModel


KIND_REGISTER = "register"
KIND_RENEW = "renew"

STATUS_QUOTED = "quoted"
STATUS_SUBMITTED = "submitted"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_EXPIRED = "expired"


class DomainPurchase(models.Model, MojoModel):
    """
    The purchase-history ledger: who bought what, for how much, and what
    happened.

    This row is durable in a way the Domain row is not. A registration that
    fails deletes its Domain (see the no-failed-rows invariant) but keeps the
    purchase row with its error, so there is always an audit trail for money
    that was — or might have been — spent.

    Read-only over REST. Rows are written exclusively by the registrar service.
    """

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    group = models.ForeignKey(
        "account.Group",
        related_name="domain_purchases",
        null=True, blank=True, default=None,
        on_delete=models.CASCADE)

    user = models.ForeignKey(
        "account.User",
        related_name="domain_purchases",
        null=True, blank=True, default=None,
        on_delete=models.SET_NULL)

    domain_name = models.CharField(max_length=253, db_index=True)

    kind = models.CharField(max_length=32, default=KIND_REGISTER)

    status = models.CharField(
        max_length=32,
        default=STATUS_QUOTED,
        db_index=True,
        help_text="quoted | submitted | completed | failed | expired")

    price = models.DecimalField(
        max_digits=9, decimal_places=2, null=True, blank=True, default=None,
        help_text="What the registry quoted.")

    cost = models.DecimalField(
        max_digits=9, decimal_places=2, null=True, blank=True, default=None,
        help_text="Our core cost. Equal to price today; a separate column so "
                  "attribution/margin can diverge later without a migration.")

    currency = models.CharField(max_length=8, default="USD")
    years = models.IntegerField(default=1)

    confirm_token = models.CharField(
        max_length=128, null=True, blank=True, default=None,
        help_text="SHA-256 hash of the confirm token. The raw token is returned "
                  "exactly once, in the quote response, and never stored.")

    quote_expires = models.DateTimeField(null=True, blank=True, default=None, db_index=True)

    operation_id = models.CharField(
        max_length=128, null=True, blank=True, default=None, db_index=True,
        help_text="Registrar operation id. Absent on a submitted row means the "
                  "crash window — the poller reconciles it via list_operations.")

    error = models.TextField(blank=True, null=True, default=None)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "dnsman_domain_purchase"
        indexes = [
            models.Index(fields=["group", "status"]),
            models.Index(fields=["status", "created"]),
        ]
        ordering = ["-created"]

    class RestMeta:
        SENSITIVE_FIELDS = ['confirm_token']
        # Written only by the registrar service; REST exposes GET routes only.
        #
        # CAN_UPDATE=False is load-bearing, not decoration. It defaults to True,
        # and the save gate checks ["SAVE_PERMS", "VIEW_PERMS"] as a FALLBACK
        # CHAIN (first declared wins — mojo/models/rest.py get_rest_meta_prop),
        # so a model with no SAVE_PERMS silently authorizes writes with its
        # VIEW_PERMS. Without this, a read-only view_dns holder could POST to
        # this row and rewrite the money ledger — including resurrecting a
        # consumed quote by resetting status/quote_expires and planting a
        # confirm_token hash of their choosing. SAVE_PERMS and NO_SAVE_FIELDS
        # below are belt-and-braces for the same reason.
        CAN_CREATE = False
        CAN_UPDATE = False
        CAN_DELETE = False
        VIEW_PERMS = ["view_dns", "manage_dns", "security"]
        SAVE_PERMS = ["manage_dns", "security"]
        NO_SAVE_FIELDS = [
            "id", "pk", "created", "group", "user", "domain_name", "kind",
            "status", "price", "cost", "currency", "years", "confirm_token",
            "quote_expires", "operation_id", "error",
        ]
        SEARCH_FIELDS = ["domain_name", "status"]
        # Belt and braces: the graphs below already list fields explicitly, so
        # confirm_token could not serialize anyway.
        NO_SHOW_FIELDS = ["confirm_token"]
        GRAPHS = {
            "basic": {
                "fields": ["id", "domain_name", "status", "price", "currency"]
            },
            "default": {
                "fields": [
                    "id", "created", "modified", "domain_name", "kind", "status",
                    "price", "cost", "currency", "years", "quote_expires",
                    "operation_id", "error",
                ],
                "graphs": {
                    "group": "basic",
                    "user": "basic",
                },
            },
        }

    def __str__(self):
        return f"{self.domain_name} [{self.status}]"

    @property
    def is_open(self):
        return self.status in (STATUS_QUOTED, STATUS_SUBMITTED)
