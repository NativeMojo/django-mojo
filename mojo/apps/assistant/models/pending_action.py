import uuid

from django.db import models
from mojo.models import MojoModel


class PendingAction(models.Model, MojoModel):
    """One server-owned approval record for one mutating assistant tool call.

    The row IS the authority. A mutating tool never runs because the model asked
    for it — it runs because the bound operator resolved this record over an
    authenticated transport, within the window, exactly once. Everything the
    execution reads (``args``, the bound user, conversation and group) is stored
    here at proposal time; nothing the client or the model sends at approve time
    is consulted beyond the ``uuid`` and the decision.

    ``NO_REST`` is load-bearing: no generic CRUD plane may ever write ``state``.
    Resolution goes through ``services/approvals.resolve()``, which is the only
    code that moves a row out of ``pending``.
    """

    STATE_PENDING = "pending"
    STATE_EXECUTING = "executing"
    STATE_COMPLETED = "completed"
    STATE_FAILED = "failed"
    STATE_CANCELED = "canceled"
    STATE_EXPIRED = "expired"
    STATE_SUPERSEDED = "superseded"

    STATE_CHOICES = [
        (STATE_PENDING, "Pending"),
        (STATE_EXECUTING, "Executing"),
        (STATE_COMPLETED, "Completed"),
        (STATE_FAILED, "Failed"),
        (STATE_CANCELED, "Canceled"),
        (STATE_EXPIRED, "Expired"),
        (STATE_SUPERSEDED, "Superseded"),
    ]

    TERMINAL_STATES = (
        STATE_COMPLETED, STATE_FAILED, STATE_CANCELED,
        STATE_EXPIRED, STATE_SUPERSEDED,
    )

    # A process that dies mid-handler leaves an `executing` row that is never
    # re-executable. After this long it is reported as an unknown outcome —
    # the same reconcile-don't-retry contract the ambiguous infrastructure
    # writes already carry. A module constant, not a setting: a second knob
    # here buys nothing.
    EXECUTING_TIMEOUT_SECONDS = 900

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False,
                            db_index=True)
    user = models.ForeignKey("account.User", on_delete=models.CASCADE,
                             related_name="assistant_pending_actions")
    conversation = models.ForeignKey("assistant.Conversation", on_delete=models.CASCADE,
                                     related_name="pending_actions")
    group = models.ForeignKey("account.Group", on_delete=models.SET_NULL,
                              null=True, blank=True,
                              related_name="assistant_pending_actions")

    tool_name = models.CharField(max_length=128, db_index=True)
    permission = models.CharField(max_length=128, blank=True, default="")
    args = models.JSONField(default=dict)
    args_fingerprint = models.CharField(max_length=64, db_index=True)
    summary = models.TextField(blank=True, default="")
    preview = models.JSONField(default=None, null=True, blank=True)

    # Gate snapshot — recorded for audit and rendering. Execution re-reads the
    # live registry; see services/approvals.py.
    fresh_auth_seconds = models.IntegerField(null=True, blank=True, default=None)
    requires_superuser = models.BooleanField(default=False)
    requires_managed_infrastructure = models.BooleanField(default=False)
    revision = models.CharField(max_length=128, blank=True, default="")

    state = models.CharField(max_length=16, choices=STATE_CHOICES,
                             default=STATE_PENDING, db_index=True)
    expires_at = models.DateTimeField(db_index=True)
    resolved_at = models.DateTimeField(null=True, blank=True, default=None)
    result = models.JSONField(default=None, null=True, blank=True)
    failure_code = models.CharField(max_length=32, blank=True, default="")

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        ordering = ["-created"]
        indexes = [
            models.Index(fields=["conversation", "state"]),
            models.Index(fields=["conversation", "tool_name", "args_fingerprint"]),
        ]

    class RestMeta:
        NO_REST = True
        VIEW_PERMS = ["view_admin"]
        GRAPHS = {
            "default": {
                "fields": [
                    "id", "uuid", "tool_name", "summary", "state",
                    "failure_code", "expires_at", "resolved_at",
                    "created", "modified",
                ],
            },
        }

    def effective_state(self, now=None):
        """The state a reader must act on, without waiting for the sweep.

        Expiry is authoritative and lazy: a ``pending`` row past ``expires_at``
        is already expired, and an ``executing`` row older than
        ``EXECUTING_TIMEOUT_SECONDS`` is already an unknown outcome. Deciding
        this here — and repeating the same predicate in the atomic claim — is
        what stops the sweep from ever racing a resolution.
        """
        from mojo.helpers import dates

        if now is None:
            now = dates.utcnow()
        if self.state == self.STATE_PENDING:
            if self.expires_at is not None and self.expires_at <= now:
                return self.STATE_EXPIRED
        elif self.state == self.STATE_EXECUTING:
            if self.modified is not None:
                elapsed = (now - self.modified).total_seconds()
                if elapsed > self.EXECUTING_TIMEOUT_SECONDS:
                    return self.STATE_FAILED
        return self.state

    def is_live(self, now=None):
        """True when this row can still be approved."""
        return self.effective_state(now=now) == self.STATE_PENDING

    def __str__(self):
        return f"PendingAction {self.uuid} ({self.tool_name}/{self.state})"
