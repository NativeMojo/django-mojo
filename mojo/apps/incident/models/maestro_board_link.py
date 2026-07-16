from django.db import models
from mojo.models import MojoModel


class MaestroBoardLink(models.Model, MojoModel):
    """
    A ticket that has been pushed into a maestro board (DM-040) — one row per
    (ticket, board) pair. Created by the sync service, never via REST; REST
    exposure is read-only plus delete (= unlink).
    """
    class Meta:
        ordering = ['-created']
        unique_together = [("ticket", "maestro_board")]

    class RestMeta:
        VIEW_PERMS = ["view_security", "security"]
        SAVE_PERMS = ["manage_security", "security"]
        DELETE_PERMS = ["manage_security"]
        CAN_CREATE = False
        CAN_UPDATE = False
        CAN_DELETE = True
        GRAPHS = {
            "default": {
                "graphs": {
                    "maestro_board": "basic"
                }
            },
        }

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    ticket = models.ForeignKey("incident.Ticket", related_name="board_links", on_delete=models.CASCADE)
    maestro_board = models.ForeignKey("incident.MaestroBoard", related_name="links", on_delete=models.CASCADE)

    remote_item_id = models.IntegerField(db_index=True)
    remote_url = models.CharField(max_length=500, blank=True, default="")
    last_synced = models.DateTimeField(blank=True, null=True, default=None)

    def __str__(self):
        return f"MaestroBoardLink(ticket={self.ticket_id}, board={self.maestro_board_id}, item={self.remote_item_id})"
