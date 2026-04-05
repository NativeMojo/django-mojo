from django.db import models
from django.template import Template, Context
from mojo.models import MojoModel


class EmailTemplate(models.Model, MojoModel):
    """
    EmailTemplate

    Stores Django template strings for subject, HTML, and plain text bodies.
    Used to render outbound emails with templating support.

    Rendering:
      - Call `render_all(context)` to render subject, text, and html.
      - Any of the template fields can be blank; rendering will return None for blanks.

    Notes:
      - Locale/i18n variations can be added later by introducing a related table or locale key.
      - This model does not send emails itself; use the email service or REST endpoint
        to send using rendered content.
    """

    created = models.DateTimeField(auto_now_add=True, editable=False)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    name = models.CharField(
        max_length=255,
        unique=True,
        db_index=True,
        help_text="Unique template name (used by callers to reference this template)"
    )

    subject_template = models.TextField(
        blank=True,
        default="",
        help_text="Django template string for the email subject"
    )
    html_template = models.TextField(
        blank=True,
        default="",
        help_text="Django template string for the HTML body"
    )
    text_template = models.TextField(
        blank=True,
        default="",
        help_text="Django template string for the plain text body"
    )

    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Arbitrary metadata for this template (e.g., description, tags)"
    )

    class Meta:
        db_table = "aws_email_template"
        indexes = [
            models.Index(fields=["modified"]),
            models.Index(fields=["name"]),
        ]
        ordering = ["name"]

    class RestMeta:
        VIEW_PERMS = ["manage_aws", "comms"]
        SAVE_PERMS = ["manage_aws", "comms"]
        DELETE_PERMS = ["manage_aws", "comms"]
        SEARCH_FIELDS = ["name"]
        GRAPHS = {
            "basic": {
                "fields": [
                    "id",
                    "name",
                    "created",
                    "modified",
                ]
            },
            "default": {
                "fields": [
                    "id",
                    "name",
                    "subject_template",
                    "html_template",
                    "text_template",
                    "metadata",
                    "created",
                    "modified",
                ]
            },
        }

    def __str__(self) -> str:
        return self.name

    # --- Rendering helpers -------------------------------------------------

    @staticmethod
    def _render_template(tpl_str: str, context: dict | None) -> str | None:
        """
        Render a Django template string with the provided context.
        Returns None if tpl_str is empty.
        """
        if not tpl_str:
            return None
        # Using Django's Template/Context is sufficient for inline strings.
        # For more advanced usage or custom engines, we can wire in django.template.engines.
        try:
            tpl = Template(tpl_str)
            ctx = Context(context or {})
            return tpl.render(ctx)
        except Exception as e:
            # We deliberately re-raise to let callers decide how to handle failures.
            raise e

    def render_subject(self, context: dict | None = None) -> str | None:
        return self._render_template(self.subject_template, context)

    def render_html(self, context: dict | None = None) -> str | None:
        return self._render_template(self.html_template, context)

    def render_text(self, context: dict | None = None) -> str | None:
        return self._render_template(self.text_template, context)

    def render_all(self, context: dict | None = None) -> dict:
        """
        Render subject, text, and html templates with the provided context.
        Returns a dict with keys: subject, text, html (values can be None).
        """
        return {
            "subject": self.render_subject(context),
            "text": self.render_text(context),
            "html": self.render_html(context),
        }

    # --- Seed auto-load ----------------------------------------------------

    @classmethod
    def get_or_load_from_seed(cls, name):
        """Look up template by name; auto-load from seed file if missing."""
        tpl = cls.objects.filter(name=name).first()
        if tpl is not None:
            return tpl
        # Try to load from seed file
        return cls._load_from_seed(name)

    @classmethod
    def _load_from_seed(cls, name):
        """Load a template from the seed file at seeds/email_templates/{name}.json."""
        import json
        import os
        import re
        # Validate name to prevent path traversal — only alphanumeric + underscores
        if not re.fullmatch(r'[a-z0-9_]{1,80}', name):
            return None
        seed_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "seeds", "email_templates")
        seed_path = os.path.join(seed_dir, f"{name}.json")
        # Belt-and-suspenders: verify resolved path is inside the seed directory
        if not os.path.abspath(seed_path).startswith(os.path.abspath(seed_dir) + os.sep):
            return None
        if not os.path.isfile(seed_path):
            return None
        try:
            with open(seed_path, "r") as f:
                data = json.load(f)
            tpl = cls.objects.create(
                name=data["name"],
                subject_template=data.get("subject_template", ""),
                html_template=data.get("html_template", ""),
                text_template=data.get("text_template", ""),
                metadata=data.get("metadata", {}),
            )
            return tpl
        except Exception as err:
            from mojo.helpers import logit
            logit.error(f"Failed to auto-load email template seed '{name}': {err}")
            return None
