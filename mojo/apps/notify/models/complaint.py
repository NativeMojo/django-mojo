from django.db import models
from mojo.models import MojoModel
from mojo.apps.account.models import User


class Complaint(models.Model, MojoModel):
    class RestMeta:
        CAN_SAVE = CAN_CREATE = False
        DEFAULT_SORT = "-id"
        SEARCH_FIELDS = ["address"]
        VIEW_PERMS = ["view_logs", "view_email"]
        SEARCH_TERMS = [
            ("email", "address"),
            ("to", "address"), "source", "reason", "state",
            ("user", "user__username")]

        GRAPHS = {
            "default": {
                "graphs": {
                    "user": "basic"
                }
            },
            "list": {
                "graphs": {
                    "user": "basic"
                }
            }
        }
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    user = models.ForeignKey("account.User", related_name="emails_complaints",
        null=True, blank=True, default=None, on_delete=models.CASCADE)
    address = models.CharField(max_length=255, db_index=True)
    kind = models.CharField(max_length=32, db_index=True)
    reason = models.TextField(null=True, blank=True, default=None)
    user_agent = models.CharField(max_length=255, null=True, blank=True, default=None)
    source = models.CharField(max_length=255, null=True, blank=True, default=None)
    source_ip = models.CharField(max_length=64, null=True, blank=True, default=None)

    def __str__(self):
        return f"complaint: address:{self.address} reason:{self.reason}"
