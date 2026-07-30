"""
The platform-admin gate for dnsman.

This app has a handful of surfaces that are not tenant operations at all —
adopting a zone out of the house AWS account, listing that account, assigning a
house domain to a tenant, releasing key material for a house certificate. They
all need the same answer to the same question, and getting it subtly wrong in
one place is how a cross-tenant primitive ships. So it is written once, here.

Two things this does that a bare `request.user.is_superuser` does not:

1. It refuses a KEY-BACKED session. For an `ApiKey` in override mode
   (`ApiKey.override_user`) `request.user` really IS a `User` — the framework
   warns about exactly this read in `mojo/helpers/request.py` — so a
   group-scoped key that happens to be linked to a member who is a platform
   superuser would sail through an attribute check and inherit platform
   authority the key was never issued for.

2. It is checked BEFORE the model permission call, not after. A model check like
   `Domain.rest_check_permission_or_raise(request, [...])` with no instance
   consults `request.group.user_has_permission(...)` whenever the caller
   supplied `?group=`, and that honors GroupMember permissions — so any tenant
   admin passes it by naming their own group. The model check is defence in
   depth on these endpoints; THIS is the boundary.
"""

from mojo import errors as me
from mojo.helpers.request import is_key_backed_session


def require_platform_admin(request, what):
    """
    Raise unless the caller is a real, interactive platform superuser.

    `what` names the operation and is echoed in the refusal, so a denied caller
    learns which surface refused them and nothing about what exists behind it.
    """
    if is_key_backed_session(request):
        raise me.PermissionDeniedException(
            f"{what} is restricted to platform administrators "
            f"and cannot be performed with an API key")
    if not getattr(request.user, "is_superuser", False):
        raise me.PermissionDeniedException(
            f"{what} is restricted to platform administrators")
    return True
