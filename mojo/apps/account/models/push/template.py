from django.db import models
from mojo.models import MojoModel


class NotificationTemplate(models.Model, MojoModel):
    """
    Reusable notification templates with variable substitution support.
    """
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    group = models.ForeignKey("account.Group", on_delete=models.CASCADE,
                             related_name="notification_templates", null=True, blank=True,
                             help_text="Organization for this template. Null = system template")

    name = models.CharField(max_length=100, db_index=True)
    title_template = models.CharField(max_length=200, blank=True, null=True)
    body_template = models.TextField(blank=True, null=True)
    action_url = models.URLField(blank=True, null=True, help_text="Template URL with variable support")
    data_template = models.JSONField(default=dict, blank=True,
                                   help_text="Template data payload with variable support")

    # Delivery preferences
    category = models.CharField(max_length=50, default="general", db_index=True)
    priority = models.CharField(max_length=20, choices=[
        ('low', 'Low'),
        ('normal', 'Normal'),
        ('high', 'High')
    ], default='normal', db_index=True)

    # Template variables documentation
    variables = models.JSONField(default=dict, blank=True,
                               help_text="Expected template variables and descriptions for title, body, action_url, and data_template")

    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        ordering = ['group__name', 'name']
        unique_together = [('group', 'name')]

    class RestMeta:
        VIEW_PERMS = ["manage_notifications", "manage_groups", "owner", "manage_users"]
        SAVE_PERMS = ["manage_notifications", "manage_groups"]
        SEARCH_FIELDS = ["name", "category"]
        LIST_DEFAULT_FILTERS = {"is_active": True}
        GRAPHS = {
            "basic": {
                "fields": ["id", "name", "category", "priority", "is_active"]
            },
            "default": {
                "fields": ["id", "name", "title_template", "body_template", "action_url",
                          "data_template", "category", "priority", "variables", "is_active"],
                "graphs": {
                    "group": "basic"
                }
            },
            "full": {
                "graphs": {
                    "group": "default"
                }
            }
        }

    def __str__(self):
        org = self.group.name if self.group else "System"
        return f"{self.name} ({org})"

    def clean(self):
        """Validate that at least one template field is provided."""
        from django.core.exceptions import ValidationError

        has_title = self.title_template and self.title_template.strip()
        has_body = self.body_template and self.body_template.strip()
        has_data = self.data_template and bool(self.data_template)

        if not (has_title or has_body or has_data):
            raise ValidationError(
                "Template must have at least one of: title_template, body_template, or data_template"
            )

    def render(self, context):
        """
        Render template with provided context variables.
        Returns tuple of (title, body, action_url, data)
        """
        title = self.title_template.format(**context) if self.title_template else None
        body = self.body_template.format(**context) if self.body_template else None
        action_url = self.action_url.format(**context) if self.action_url else None

        # Render data template with context
        data = {}
        if self.data_template:
            for key, value in self.data_template.items():
                if isinstance(value, str):
                    data[key] = value.format(**context)
                else:
                    data[key] = value

        return title, body, action_url, data
