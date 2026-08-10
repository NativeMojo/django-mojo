import uuid

from django.db import models


class WebAppKeyOperation(models.Model):
    """Non-secret receipt for an idempotent WebApp credential change."""

    ACTION_MINT = "mint"
    ACTION_ROTATE = "rotate"
    ACTION_REVOKE = "revoke"
    ACTION_CHOICES = (
        (ACTION_MINT, "Mint"),
        (ACTION_ROTATE, "Rotate"),
        (ACTION_REVOKE, "Revoke"),
    )

    created = models.DateTimeField(auto_now_add=True, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)
    operation_id = models.UUIDField(default=uuid.uuid4, editable=False)
    action = models.CharField(max_length=12, choices=ACTION_CHOICES)
    web_app = models.ForeignKey(
        "edge.WebApp", related_name="key_operations", on_delete=models.CASCADE)
    api_key = models.ForeignKey(
        "account.ApiKey", related_name="webapp_key_operations",
        null=True, blank=True, on_delete=models.SET_NULL)
    actor = models.ForeignKey(
        "account.User", related_name="webapp_key_operations",
        null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        db_table = "edge_web_app_key_operation"
        ordering = ["-created"]
        constraints = [
            models.UniqueConstraint(
                fields=["web_app", "operation_id"],
                name="edge_webapp_key_operation_uniq"),
        ]

    def __str__(self):
        return f"{self.web_app_id}:{self.action}:{self.operation_id}"
