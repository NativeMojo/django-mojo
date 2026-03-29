import logging
from django.db import models
from mojo.models import MojoModel
from mojo.helpers import logit

logger = logging.getLogger(__name__)

class Ticket(models.Model, MojoModel):
    class Meta:
        ordering = ['-modified']

    class RestMeta:
        VIEW_PERMS = ["view_security", "security"]
        SAVE_PERMS = ["manage_security", "security"]
        DELETE_PERMS = ["manage_security"]
        CAN_DELETE = True
        GRAPHS = {
            "default": {
                "graphs": {
                    "assignee": "basic",
                    "incident": "basic",
                    "user": "basic",
                    "group": "basic"
                }
            },
        }

    created = models.DateTimeField(auto_now_add=True, editable=False)
    modified = models.DateTimeField(auto_now=True)

    user = models.ForeignKey("account.User", blank=True, null=True, default=None, related_name="+", on_delete=models.SET_NULL)
    group = models.ForeignKey("account.Group", blank=True, null=True, default=None, related_name="+", on_delete=models.SET_NULL)

    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True, default=None)

    status = models.CharField(max_length=50, default='open', db_index=True)
    priority = models.IntegerField(default=1, db_index=True)
    category = models.CharField(max_length=80, default='ticket', db_index=True)

    assignee = models.ForeignKey("account.User", blank=True, null=True, default=None, related_name="assigned_tickets", on_delete=models.SET_NULL)
    incident = models.ForeignKey("incident.Incident", blank=True, null=True, default=None, related_name="tickets", on_delete=models.SET_NULL)

    metadata = models.JSONField(default=dict, blank=True)

    def on_rest_created(self):
        logit.info("Ticket created")
        if self.description:
            self.add_note(self.description, user=self.active_request.user)

    def on_rest_saved(self, changed_fields, created):
        if 'status' in changed_fields:
            self.add_note(f"Status changed from {changed_fields['status']} to {self.status}", user=self.active_request.user)

    def add_note(self, note, user):
        logit.info(f"Adding note to ticket {self.id}: {note}")
        TicketNote.objects.create(parent=self, note=note, group=self.group, user=user)

class TicketNote(models.Model, MojoModel):
    class Meta:
        ordering = ['-created']

    class RestMeta:
        VIEW_PERMS = ["view_security", "security"]
        SAVE_PERMS = ["manage_security", "security"]
        CAN_DELETE = True
        GRAPHS = {
            "default": {
                "graphs": {
                    "user": "basic",
                    "media": "basic"
                }
            },
        }

    parent = models.ForeignKey(Ticket, related_name="notes", on_delete=models.CASCADE)
    created = models.DateTimeField(auto_now_add=True, editable=False)

    group = models.ForeignKey("account.Group", related_name="+", on_delete=models.CASCADE, blank=True, null=True, default=None)
    user = models.ForeignKey("account.User", related_name="+", on_delete=models.CASCADE)
    note = models.TextField(blank=True, null=True)
    media = models.ForeignKey("fileman.File", related_name="+", null=True, blank=True, default=None, on_delete=models.SET_NULL)

    def on_rest_saved(self, changed_fields, created):
        if not hasattr(self, 'group') or not self.group:
            if self.parent.group:
                self.group = self.parent.group
                self.save(update_fields=['group'])

        # Re-invoke LLM agent when a human replies to an llm_linked ticket
        if created and self._is_llm_ticket() and not self._is_llm_note():
            try:
                from mojo.apps import jobs
                jobs.publish(
                    "mojo.apps.incident.handlers.llm_agent.execute_llm_ticket_reply",
                    {"ticket_id": self.parent_id, "note_id": self.pk},
                    channel="incident_handlers",
                )
            except Exception:
                logger.exception("Failed to re-invoke LLM for ticket %s", self.parent_id)

    def _is_llm_ticket(self):
        """Check if the parent ticket is LLM-linked."""
        return (self.parent.metadata or {}).get("llm_linked", False)

    def _is_llm_note(self):
        """Check if this note was posted by the LLM (avoid infinite loop)."""
        return bool(self.note and self.note.startswith("[LLM Agent]"))
