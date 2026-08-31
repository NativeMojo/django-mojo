from django.db import models
from django.db.models import Q

from mojo.models import MojoModel


class IncidentLLMAttempt(models.Model, MojoModel):
    ACTIVE_STATES = ("claimed", "queued", "running", "retryable")
    STATES = tuple((value, value.replace("_", " ").title()) for value in (
        "claimed", "queued", "running", "retryable", "succeeded", "terminal"))

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)
    incident = models.ForeignKey(
        "incident.Incident", related_name="llm_attempts", on_delete=models.SET_NULL,
        null=True, blank=True, default=None)
    feature = models.CharField(max_length=32, db_index=True)
    logical_key = models.CharField(max_length=64, unique=True)
    state = models.CharField(
        max_length=16, choices=STATES, default="claimed", db_index=True)
    prior_status = models.CharField(max_length=50, blank=True, default="")
    event_id = models.BigIntegerField(null=True, blank=True, default=None)
    ruleset_id = models.BigIntegerField(null=True, blank=True, default=None)
    ticket_id = models.BigIntegerField(null=True, blank=True, default=None)
    note_id = models.BigIntegerField(null=True, blank=True, default=None)
    job_id = models.CharField(max_length=32, blank=True, default="", db_index=True)
    attempt_count = models.PositiveIntegerField(default=0)
    max_attempts = models.PositiveIntegerField(default=3)
    lease_owner = models.CharField(max_length=64, blank=True, default="")
    lease_expires_at = models.DateTimeField(null=True, blank=True, default=None)
    retry_at = models.DateTimeField(null=True, blank=True, default=None, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True, default=None)
    error_code = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        ordering = ["created", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=("incident", "feature"),
                condition=Q(state__in=("claimed", "queued", "running", "retryable")),
                name="incident_one_active_llm_attempt"),
        ]
        indexes = [
            models.Index(fields=("state", "retry_at"), name="incident_llma_retry_idx"),
            models.Index(fields=("incident", "created"), name="incident_llma_inc_idx"),
        ]

    class RestMeta:
        VIEW_PERMS = ["view_security", "manage_security", "security"]
        SAVE_PERMS = ["manage_security", "security"]
        CAN_CREATE = False
        CAN_UPDATE = False
        CAN_DELETE = False
        DENY_AI = True
        SENSITIVE_FIELDS = ["logical_key", "lease_owner"]
        GRAPHS = {"default": {"fields": [
            "id", "created", "modified", "feature", "state", "prior_status",
            "event_id", "ruleset_id", "ticket_id", "note_id", "job_id", "attempt_count",
            "max_attempts", "retry_at", "finished_at", "error_code",
        ], "extra": ["incident_id"]}}
