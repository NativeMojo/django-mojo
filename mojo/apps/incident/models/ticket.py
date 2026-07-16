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
        POST_SAVE_ACTIONS = ["enable_llm", "disable_llm", "push_to_board"]
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

        # Maestro board sync (DM-040): REST edits to synced fields enqueue an
        # async push per linked board. Webhook-applied changes use direct ORM
        # saves and never enter this hook — that asymmetry is the echo guard.
        if not created:
            changed = {"title", "description", "status"} & set(changed_fields)
            if changed:
                from mojo.apps.incident.services import maestro_sync
                for link_id in self.board_links.filter(
                        maestro_board__is_active=True).values_list("id", flat=True):
                    maestro_sync.enqueue_sync(link_id, sorted(changed))

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

    def on_action_push_to_board(self, value):
        """POST {"push_to_board": <maestro board id>} — queue an async push of
        this ticket into the board (fail-closed validation, fail-open push)."""
        from mojo.errors import PermissionDeniedException, ValueException
        from mojo.apps.incident.services import maestro_sync
        from mojo.apps.incident.models.maestro_board import MaestroBoard
        try:
            board_id = int(value)
        except (TypeError, ValueError):
            raise ValueException("push_to_board requires a maestro board id", 400)
        board = MaestroBoard.objects.filter(pk=board_id, is_active=True).first()
        if board is None:
            raise ValueException("unknown or inactive maestro board", 400)
        if board.group_id and board.group_id != self.group_id:
            raise PermissionDeniedException("maestro board not available for this ticket", 403, 403)
        maestro_sync.enqueue_push(self.pk, board.pk)

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

        # Maestro board sync (DM-040): mirror REST-created notes onto linked
        # boards as comments — never sync-origin notes (metadata.origin ==
        # "maestro"), or they would echo back to the board they came from.
        if (self.metadata or {}).get("origin") != "maestro":
            from mojo.apps.incident.services import maestro_sync
            for link_id in self.parent.board_links.filter(
                    maestro_board__is_active=True,
                    maestro_board__sync_notes=True).values_list("id", flat=True):
                maestro_sync.enqueue_note(link_id, self.pk)

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
