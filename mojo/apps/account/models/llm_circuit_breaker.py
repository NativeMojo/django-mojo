from django.db import models

from mojo.models import MojoModel


class LLMCircuitBreaker(models.Model, MojoModel):
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)
    provider = models.CharField(max_length=32)
    credential_fingerprint = models.CharField(max_length=64)
    state = models.CharField(max_length=16, default="closed", db_index=True)
    generation = models.PositiveBigIntegerField(default=0)
    failure_count = models.PositiveIntegerField(default=0)
    error_code = models.CharField(max_length=64, blank=True, default="")
    opened_until = models.DateTimeField(null=True, blank=True, default=None, db_index=True)
    half_open_owner = models.CharField(max_length=64, blank=True, default="")
    half_open_expires_at = models.DateTimeField(null=True, blank=True, default=None)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("provider", "credential_fingerprint"),
                name="account_llm_breaker_credential_uniq"),
        ]
        indexes = [
            models.Index(fields=("provider", "state"), name="account_llmb_state_idx"),
        ]

    class RestMeta:
        VIEW_PERMS = ["view_security", "manage_security", "security"]
        SAVE_PERMS = ["manage_security", "security"]
        CAN_CREATE = False
        CAN_UPDATE = False
        CAN_DELETE = False
        DENY_AI = True
        SENSITIVE_FIELDS = ["credential_fingerprint", "half_open_owner"]
        GRAPHS = {"default": {"fields": [
            "id", "created", "modified", "provider", "state", "generation",
            "failure_count", "error_code", "opened_until",
        ]}}
