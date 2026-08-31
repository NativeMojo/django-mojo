from django.db import models

from mojo.models import MojoModel


class LLMRequest(models.Model, MojoModel):
    STATUS_CHOICES = tuple((value, value.replace("_", " ").title()) for value in (
        "started", "succeeded", "failed", "blocked", "unknown"))

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True, default=None)
    feature = models.CharField(max_length=32, db_index=True)
    operation = models.CharField(max_length=64)
    provider = models.CharField(max_length=32, db_index=True)
    model = models.CharField(max_length=128)
    credential_fingerprint = models.CharField(max_length=64, db_index=True)
    policy_hash = models.CharField(max_length=64, db_index=True)
    provider_request_id = models.CharField(max_length=128, blank=True, default="")
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default="started", db_index=True)
    error_code = models.CharField(max_length=64, blank=True, default="")
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    cache_read_input_tokens = models.PositiveIntegerField(default=0)
    cache_creation_input_tokens = models.PositiveIntegerField(default=0)
    reserved_tokens = models.PositiveIntegerField(default=0)
    duration_ms = models.PositiveIntegerField(default=0)
    job_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    incident_id = models.BigIntegerField(null=True, blank=True, default=None, db_index=True)
    conversation_id = models.BigIntegerField(null=True, blank=True, default=None, db_index=True)
    file_id = models.BigIntegerField(null=True, blank=True, default=None)

    class Meta:
        ordering = ["-created"]
        indexes = [
            models.Index(fields=("provider", "credential_fingerprint", "created"),
                         name="account_llmr_provider_idx"),
            models.Index(fields=("feature", "status", "created"),
                         name="account_llmr_feature_idx"),
        ]
