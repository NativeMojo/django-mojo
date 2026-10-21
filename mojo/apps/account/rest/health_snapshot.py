"""Protected fast cached health report; inputs never select a collector."""
import mojo.decorators as md
from mojo import errors as me
from mojo.apps.account.services import health_snapshot


@md.GET("account/admin/health/snapshot")
@md.requires_global_perms("view_platform", "manage_platform", "admin")
def on_health_snapshot(request):
    if set(request.DATA) - {"evidence_cursor"}:
        raise me.ValueException("Only evidence_cursor is accepted")
    try:
        return health_snapshot.read_snapshot(request.user, request.DATA.get("evidence_cursor"))
    except health_snapshot.SnapshotError as exc:
        if exc.error_code == "permission_denied":
            raise me.PermissionDeniedException("Platform permission required") from None
        raise me.ValueException(exc.error_code) from None
