from django.db import models

from mojo.models import MojoModel


class MojoSecDetectorFeedbackHead(models.Model, MojoModel):
    """Mutable lock row pointing at one subject's immutable current label."""

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)
    subject_key = models.CharField(max_length=160, unique=True)
    current = models.OneToOneField(
        "incident.MojoSecDetectorFeedback", null=True, blank=True, default=None,
        related_name="current_subject_head", on_delete=models.PROTECT)

    class Meta:
        ordering = ["subject_key"]

    class RestMeta:
        VIEW_PERMS = ["view_security", "manage_security", "security"]
        SAVE_PERMS = ["manage_security", "security"]
        CAN_CREATE = False
        CAN_UPDATE = False
        CAN_DELETE = False
        DENY_AI = True
