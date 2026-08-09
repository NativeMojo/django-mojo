from django.db import models

from mojo.helpers import dates
from mojo.models import MojoModel
from .mojosec_immutable import MojoSecImmutableManager


class MojoSecDetectorFeedbackHead(models.Model, MojoModel):
    """Mutable lock row pointing at one subject's immutable current label."""

    objects = MojoSecImmutableManager()

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)
    subject_key = models.CharField(max_length=160, unique=True)
    current = models.OneToOneField(
        "incident.MojoSecDetectorFeedback", null=True, blank=True, default=None,
        related_name="current_subject_head", on_delete=models.PROTECT)

    class Meta:
        ordering = ["subject_key"]
        indexes = [
            models.Index(
                fields=("-modified", "-id"),
                name="incident_msfh_modified_idx"),
        ]

    class RestMeta:
        VIEW_PERMS = ["view_security", "manage_security", "security"]
        SAVE_PERMS = ["manage_security", "security"]
        CAN_CREATE = False
        CAN_UPDATE = False
        CAN_DELETE = False
        DENY_AI = True

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValueError("MojoSec feedback heads change only through guarded transitions")
        if self.current_id is not None:
            raise ValueError("a new MojoSec feedback head must start empty")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("MojoSec feedback heads may not be deleted")

    def advance(self, new_current):
        """Atomically advance to the next integrity-checked label revision."""
        if self.pk is None or new_current is None or new_current.pk is None:
            raise ValueError("a persisted head and feedback revision are required")
        new_current.assert_integrity()
        if new_current.subject_key != self.subject_key:
            raise ValueError("feedback head and revision subjects must match")
        if new_current.reverses_id != self.current_id:
            raise ValueError("feedback head transitions must extend the current revision")
        using = self._state.db or "default"
        updated = models.QuerySet(model=type(self), using=using).filter(
            pk=self.pk,
            subject_key=self.subject_key,
            current_id=self.current_id,
        ).update(current_id=new_current.pk, modified=dates.utcnow())
        if updated != 1:
            raise ValueError("the MojoSec feedback head changed concurrently")
        self.current = new_current
        return self
