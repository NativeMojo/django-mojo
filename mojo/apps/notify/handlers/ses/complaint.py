from mojo.apps.mailmain.models import Complaint
from mojo.models import User


def record(kind, address, reason, user_agent=None, code=None, source=None, source_ip=None, user=None):
    complaint = Complaint(
        kind=kind,
        address=address,
        reason=reason,
        user_agent=user_agent,
        code=code,
        source=source,
        source_ip=source_ip
    )

    if user is None:
        user = User.objects.filter(email=address).last()
        if user:
            user.is_email_verified = False
            user.save()
            user.log(f"email complaint: {kind} to {address} from {source_ip}", kind="complaint", level="warning")
            user.log("Email notifications have been disabled because of complaints.", kind="email", level="warning")

    complaint.user = user
    complaint.save()
