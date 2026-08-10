"""Durable, resumable state for portal-driven System Setup operations."""

import uuid

from django.db import models
from django.db.models import Q

from mojo.models import MojoModel


class SystemSetupOperation(models.Model, MojoModel):
    MODE_CHOICES = (("check", "Check"), ("fix", "Fix"))
    ACTIVE_STATUSES = (
        "planned", "running", "waiting_for_choice", "reconciling")
    STATUS_CHOICES = tuple((value, value.replace("_", " ").title()) for value in (
        "planned", "running", "waiting_for_choice", "reconciling",
        "succeeded", "failed", "cancelled"))

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True, default=None)

    created_by = models.ForeignKey(
        "account.User", on_delete=models.PROTECT,
        related_name="system_setup_operations")
    mode = models.CharField(max_length=16, choices=MODE_CHOICES, db_index=True)
    section = models.CharField(max_length=80, blank=True, default="")
    status = models.CharField(
        max_length=32, choices=STATUS_CHOICES, default="planned", db_index=True)
    replay_fingerprint = models.CharField(max_length=64, unique=True)
    bound_origin = models.CharField(max_length=255)

    cursor = models.PositiveIntegerField(default=0)
    steps = models.JSONField(default=list)
    choices = models.JSONField(default=dict)
    report = models.JSONField(default=dict)
    operation_log = models.JSONField(default=list)

    lease_owner = models.CharField(max_length=64, blank=True, default="")
    lease_expires_at = models.DateTimeField(null=True, blank=True, default=None)

    class Meta:
        ordering = ["-created"]
        constraints = [
            models.UniqueConstraint(
                fields=["mode"],
                condition=Q(mode="fix", status__in=(
                    "planned", "running", "waiting_for_choice", "reconciling")),
                name="account_one_active_system_fix"),
        ]

    def __str__(self):
        return f"System setup {self.mode} {self.id} ({self.status})"
