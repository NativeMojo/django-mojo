from django.db import models
from mojo.models import MojoModel
from mojo.helpers import logit
from mojo.helpers.request import is_request_user


class VaultAccessLog(models.Model, MojoModel):
    """Append-only record of who reached a vault secret, when, and from where.

    For a secrets vault, `VaultFile.unlocked_by` is not an audit trail — it
    holds only the LAST unlocker and is overwritten on every unlock. This model
    records each access attempt individually, successful or denied, so an owner
    can answer "who opened this, and who tried".

    Rows are never updated or deleted through REST (CAN_CREATE / CAN_UPDATE /
    CAN_DELETE are all False); the only writer is `record()` below.

    Deliberately NOT `mojo.apps.logit.Log`, despite the overlap: `Log.gid` is a
    plain IntegerField rather than an FK, so `is_group_scoped` is False and
    `on_rest_list`'s tenant filter never fires for it, and its VIEW_PERMS are a
    global-admin audience. A vault owner could not read their own file's trail.
    This model carries a real `group` FK so the standard group scoping applies.
    """

    ACTION_UNLOCK = "unlock"
    ACTION_DOWNLOAD = "download"
    ACTION_RETRIEVE = "retrieve"
    ACTION_PASSWORD = "password"

    RESULT_GRANTED = "granted"
    RESULT_DENIED = "denied"

    REASON_PERMISSION_DENIED = "permission_denied"
    REASON_INVALID_PASSWORD = "invalid_password"
    REASON_PASSWORD_REQUIRED = "password_required"
    REASON_INVALID_TOKEN = "invalid_token"

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    # Denormalized from the target row at write time, never taken from
    # request.group — rest_check_permission_or_raise REBINDS request.group to
    # the instance's group as a side effect, and a denial path can run before
    # that happens. This FK is also what makes the trail tenant-scoped.
    group = models.ForeignKey(
        "account.Group", related_name="vault_access_logs", on_delete=models.CASCADE)

    # NULL for anonymous token downloads — the public download endpoint has no
    # authenticated user at all.
    user = models.ForeignKey(
        "account.User", null=True, blank=True, default=None,
        on_delete=models.SET_NULL, related_name="vault_access_logs")

    # SET_NULL, not CASCADE: deleting the secret must not erase the record of
    # who read it, which is exactly when the trail matters most. target_name
    # keeps the row readable afterwards.
    vault_file = models.ForeignKey(
        "filevault.VaultFile", null=True, blank=True, default=None,
        on_delete=models.SET_NULL, related_name="access_logs")
    vault_data = models.ForeignKey(
        "filevault.VaultData", null=True, blank=True, default=None,
        on_delete=models.SET_NULL, related_name="access_logs")

    target_name = models.CharField(max_length=200, blank=True, default="")
    action = models.CharField(max_length=16, db_index=True)
    result = models.CharField(max_length=16, db_index=True)
    reason = models.CharField(max_length=64, blank=True, default="")

    # CharField(45), not GenericIPAddressField — full IPv6 including
    # ::ffff:-mapped IPv4. Nullable because get_remote_ip can return None.
    ip = models.CharField(max_length=45, null=True, blank=True, default=None, db_index=True)
    user_agent = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-created"]
        indexes = [
            models.Index(fields=["group", "created"]),
            models.Index(fields=["vault_file", "created"]),
            models.Index(fields=["vault_data", "created"]),
            models.Index(fields=["user", "created"]),
        ]

    class RestMeta:
        CAN_CREATE = False
        CAN_UPDATE = False
        CAN_DELETE = False
        DEFAULT_SORT = "-created"
        # Manager tier. Deliberately NO "owner": OWNER_FIELD defaults to `user`,
        # which on this model is the ACCESSOR — so "owner" would let a
        # cross-tenant prober read back the very denial row they generated,
        # including the victim's target_name.
        VIEW_PERMS = ["manage_vault", "files"]
        SAVE_PERMS = ["manage_vault", "files"]
        SEARCH_FIELDS = ["target_name", "ip", "action", "result"]
        SEARCH_TERMS = ["action", "result", "ip", ("group", "group__name")]

        GRAPHS = {
            "list": {
                "fields": [
                    "id", "created", "target_name", "action", "result",
                    "reason", "ip"],
                "graphs": {"user": "basic"},
            },
            "default": {
                "fields": [
                    "id", "created", "target_name", "action", "result",
                    "reason", "ip", "user_agent"],
                "graphs": {
                    "user": "basic",
                    "group": "basic",
                },
            },
        }

    def __str__(self):
        return f"{self.action}/{self.result} {self.target_name}"

    @classmethod
    def record(cls, action, result, vault_file=None, vault_data=None,
               request=None, reason=""):
        """Write one access row. Never raises.

        An audit-write failure must not take down a legitimate vault read, so
        this swallows exceptions — but loudly, via logit.error. A bare
        `except: pass` would be metrics behavior, not audit behavior.
        """
        try:
            target = vault_file or vault_data
            if target is None:
                return None

            user = None
            if request is not None:
                # request.acting_user is the member an ApiKey acts as; it is
                # the right attribution for a key-authenticated access. Fall
                # back to request.user only when it is a real User —
                # ANONYMOUS_USER is an objict and an unlinked ApiKey is not a
                # User, and assigning either to the FK would raise.
                user = getattr(request, "acting_user", None)
                if user is None and is_request_user(request):
                    user = request.user

            return cls.objects.create(
                group=target.group,
                user=user,
                vault_file=vault_file,
                vault_data=vault_data,
                target_name=(getattr(target, "name", "") or "")[:200],
                action=action,
                result=result,
                reason=reason or "",
                ip=getattr(request, "ip", None) if request is not None else None,
                user_agent=(getattr(request, "user_agent", "") or "") if request is not None else "",
            )
        except Exception:
            logit.exception("filevault: failed to write access log")
            return None
