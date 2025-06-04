from django.db import models
from mojo.models import MojoModel
from mojo.helpers import dates
from mojo.apps.account.models import User


class Bounce(models.Model, MojoModel):
    class RestMeta:
        CAN_SAVE = CAN_CREATE = False
        DEFAULT_SORT = "-id"
        VIEW_PERMS = ["view_logs", "view_email"]
        SEARCH_FIELDS = ["address"]
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
    user = models.ForeignKey("account.User", related_name="emails_bounced",
        null=True, blank=True, default=None, on_delete=models.CASCADE)
    address = models.CharField(max_length=255, db_index=True)
    kind = models.CharField(max_length=32, db_index=True)
    reason = models.TextField(null=True, blank=True, default=None)
    reporter = models.CharField(max_length=255, null=True, blank=True, default=None)
    code = models.CharField(max_length=32, null=True, blank=True, default=None)
    source = models.CharField(max_length=255, null=True, blank=True, default=None)
    source_ip = models.CharField(max_length=64, null=True, blank=True, default=None)

    def __str__(self):
        return f"bounce: address:{self.address} reason:{self.reason}"

    @staticmethod
    def record(kind, address, reason, reporter=None, code=None, source=None, source_ip=None, user=None):
        bounce = Bounce(
            kind=kind,
            address=address,
            reason=reason,
            reporter=reporter,
            code=code,
            source=source,
            source_ip=source_ip,
            user=None
        )

        if user is None:
            user = User.objects.filter(email=address).last()
            if user:
                user.log("bounced", f"{kind} bounced to {address} from {source_ip}", method=kind)
                recent_bounce_count = Bounce.objects.filter(user=user, created__gte=dates.add(dates.utcnow(), days=14)).count()
                if recent_bounce_count > 2:
                    user.is_email_verified = False
                    user.save()
                    user.log("Email notifications have been disabled because of repeated bounces.", kind="email", level="warning")

        bounce.user = user
        bounce.save()
