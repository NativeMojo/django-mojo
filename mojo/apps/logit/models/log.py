from mojo.models import MojoModel
from django.db import models as dm
from mojo.helpers import logit
import ujson
# logger = logit.get_logger("requests", "requests.log")


class Log(dm.Model, MojoModel):
    created = dm.DateTimeField(auto_now_add=True)
    level = dm.CharField(max_length=12, default="info")
    kind = dm.CharField(max_length=200, default=None, null=True)
    method = dm.CharField(max_length=200, default=None, null=True)
    path = dm.TextField(default=None, null=True)
    payload = dm.TextField(default=None, null=True)
    ip = dm.CharField(max_length=32, default=None, null=True)
    duid = dm.TextField(default=None, null=True)
    uid = dm.IntegerField(default=0)
    username = dm.TextField(default=None, null=True)
    user_agent = dm.TextField(default=None, null=True)
    log = dm.TextField(default=None, null=True)
    model_name = dm.TextField(default=None, null=True)
    model_id = dm.IntegerField(default=0)
    # expires = dm.DateTimeField(db_index=True)

    class Meta:
        indexes = [
            dm.Index(fields=['created']),
            dm.Index(fields=['level']),
            dm.Index(fields=['kind']),
            dm.Index(fields=['path']),
            dm.Index(fields=['ip']),
            dm.Index(fields=['uid']),
            dm.Index(fields=['model_name', 'model_id']),  # composite index for fields searched together
            dm.Index(fields=['created', 'kind']),  # composite index for common search/order combination
        ]

    class RestMeta:
        VIEW_PERMS = ["manage_logs", "view_logs", "admin"]
        SAVE_PERMS = ["admin"]  # Only admins should create/edit logs manually
        DELETE_PERMS = ["admin"]  # Only admins can delete logs
        CAN_DELETE = True  # Allow deletion for cleanup purposes

        GRAPHS = {
            "basic": {
                "fields": [
                    "id", "created", "level", "kind", "method", "path",
                    "ip", "uid", "username", "model_name", "model_id"
                ],
            },
            "default": {
                "fields": [
                    "id", "created", "level", "kind", "method", "path", "payload",
                    "ip", "duid", "uid", "username", "user_agent", "log",
                    "model_name", "model_id"
                ],
            },
        }

    @classmethod
    def logit(cls, request, log, kind="log", model_name=None, model_id=0, level="info", **kwargs):
        if isinstance(log, dict):
            # Convert unsupported types in dict values
            def convert_value(value):
                if isinstance(value, bytes):
                    return value.decode('utf-8', errors='replace')
                elif hasattr(value, 'isoformat'):  # datetime objects
                    return value.isoformat()
                elif isinstance(value, str):
                    return value
                else:
                    return str(value)

            converted_log = {}
            for key, value in log.items():
                if isinstance(value, dict):
                    # Recursively handle nested dicts
                    converted_log[key] = {k: convert_value(v) for k, v in value.items()}
                elif isinstance(value, (list, tuple)):
                    # Handle lists/tuples of values
                    converted_log[key] = [convert_value(item) for item in value]
                else:
                    converted_log[key] = convert_value(value)

            log = ujson.encode(converted_log, indent=4)
        if not isinstance(log, (bytes, str)):
            log = f"INVALID LOG TYPE: attempting to log type: {type(log)}"
        log = log.decode("utf-8") if isinstance(log, bytes) else log
        log = logit.mask_sensitive_data(log)

        uid, username, ip_address, path, method, duid = 0, None, None, None, None, None
        user_agent = "system"
        if request:
            username = request.user.username if request.user.is_authenticated else None
            uid = request.user.pk if request.user.is_authenticated else 0
            path = request.path
            duid = request.duid
            ip_address = request.ip
            method = request.method
            user_agent = request.user_agent

        path = kwargs.get("path", path)
        method = kwargs.get("method", method)
        duid = kwargs.get("duid", duid)

        return cls.objects.create(
            level=level,
            kind=kind,
            method=method,
            path=path,
            payload=kwargs.get("payload", None),
            ip=ip_address,
            uid=uid,
            duid=duid,
            username=username,
            log=log,
            user_agent=user_agent,
            model_name=model_name,
            model_id=model_id
        )
