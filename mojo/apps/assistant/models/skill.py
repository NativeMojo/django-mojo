from django.db import models
from mojo.models import MojoModel


class Skill(models.Model, MojoModel):
    """A learned, reusable procedure the assistant can recall and replay."""

    TIER_CHOICES = [
        ("global", "Global"),
        ("user", "User"),
        ("group", "Group"),
    ]

    user = models.ForeignKey("account.User", on_delete=models.CASCADE,
                             null=True, blank=True, related_name="assistant_skills")
    group = models.ForeignKey("account.Group", on_delete=models.SET_NULL,
                              null=True, blank=True, related_name="assistant_skills")
    tier = models.CharField(max_length=8, choices=TIER_CHOICES, db_index=True)
    name = models.CharField(max_length=128, db_index=True)
    description = models.TextField(blank=True, default="")
    triggers = models.JSONField(default=list, blank=True)
    steps = models.JSONField(default=list)
    auto_execute = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True, db_index=True)
    metadata = models.JSONField(default=dict, blank=True)

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        ordering = ["-modified"]
        constraints = [
            models.UniqueConstraint(
                fields=["tier", "user", "group", "name"],
                name="unique_skill_per_scope",
            ),
        ]

    class RestMeta:
        VIEW_PERMS = ["view_admin", "assistant", "owner"]
        SAVE_PERMS = ["view_admin"]
        OWNER_FIELD = "user"
        CAN_DELETE = True
        GRAPHS = {
            "default": {
                "fields": [
                    "id", "tier", "name", "description", "auto_execute",
                    "is_active", "created", "modified",
                ],
            },
            "detail": {
                "fields": [
                    "id", "tier", "name", "description", "triggers", "steps",
                    "auto_execute", "is_active", "metadata", "created", "modified",
                ],
                "graphs": {"user": "basic"},
            },
        }

    def __str__(self):
        return f"Skill {self.pk} ({self.name})"
