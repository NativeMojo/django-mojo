from django.db import models
from mojo.models import MojoModel
from mojo.helpers import logit

logger = logit.get_logger(__name__, "incident.log")

class Ticket(models.Model, MojoModel):
    class Meta:
        ordering = ['-modified']

    class RestMeta:
        VIEW_PERMS = ["view_security", "security"]
        SAVE_PERMS = ["manage_security", "security"]
        DELETE_PERMS = ["manage_security"]
        CAN_DELETE = True
        POST_SAVE_ACTIONS = ["enable_llm", "disable_llm"]
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
            old_status = changed_fields['status']
            new_status = self.status
            self.add_note(
                f"Status changed from {old_status} to {new_status}",
                user=self.active_request.user,
                metadata={
                    "type": "status_change",
                    "old_status": old_status,
                    "new_status": new_status,
                },
            )

    def is_llm_enabled(self):
        meta = self.metadata or {}
        return meta.get("llm_enabled") or meta.get("llm_linked", False)

    def on_action_enable_llm(self, value):
        if not self.metadata:
            self.metadata = {}
        self.metadata["llm_enabled"] = True
        self.save(update_fields=["metadata"])
        try:
            from mojo.apps import jobs
            jobs.publish(
                "mojo.apps.incident.handlers.llm_agent.execute_llm_ticket_reply",
                {"ticket_id": self.pk, "note_id": None},
                channel="incident_handlers",
            )
        except Exception:
            logger.exception("Failed to invoke LLM on enable for ticket %s", self.pk)

    def on_action_disable_llm(self, value):
        if not self.metadata:
            self.metadata = {}
        self.metadata["llm_enabled"] = False
        self.save(update_fields=["metadata"])

    def add_note(self, note, user, metadata=None):
        logit.info(f"Adding note to ticket {self.id}: {note}")
        kwargs = {"parent": self, "note": note, "group": self.group, "user": user}
        if metadata:
            kwargs["metadata"] = metadata
        TicketNote.objects.create(**kwargs)

class TicketNote(models.Model, MojoModel):
    class Meta:
        ordering = ['-created']

    class RestMeta:
        VIEW_PERMS = ["view_security", "security"]
        SAVE_PERMS = ["manage_security", "security"]
        CAN_DELETE = True
        JSON_REPLACE_FIELDS = ["metadata"]
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
    user = models.ForeignKey("account.User", related_name="+", null=True, blank=True, default=None, on_delete=models.SET_NULL)
    note = models.TextField(blank=True, null=True)
    media = models.ForeignKey("fileman.File", related_name="+", null=True, blank=True, default=None, on_delete=models.SET_NULL)
    metadata = models.JSONField(default=dict, blank=True)

    def on_rest_saved(self, changed_fields, created):
        if not hasattr(self, 'group') or not self.group:
            if self.parent.group:
                self.group = self.parent.group
                self.save(update_fields=['group'])

        if not created:
            return

        # Action response dispatch — structured actions take priority
        response_meta = (self.metadata or {}).get("action_response")
        if response_meta:
            from mojo.apps.incident.handlers.ticket_actions import dispatch_action
            dispatch_action(self.parent, self, response_meta)
            return

        # LLM re-invocation for non-action notes on LLM-enabled tickets
        if self.parent.is_llm_enabled() and not self._is_llm_note():
            try:
                from mojo.apps import jobs
                jobs.publish(
                    "mojo.apps.incident.handlers.llm_agent.execute_llm_ticket_reply",
                    {"ticket_id": self.parent_id, "note_id": self.pk},
                    channel="incident_handlers",
                )
            except Exception:
                logger.exception("Failed to re-invoke LLM for ticket %s", self.parent_id)

    def _is_llm_note(self):
        """Check if this note was posted by the LLM (avoid infinite loop)."""
        return bool(self.note and self.note.startswith("[LLM Agent]"))
