from mojo.models import MojoModel
from django.db import models as dm
from mojo.helpers import logit
import ujson
# logger = logit.get_logger("requests", "requests.log")


class Log(dm.Model, MojoModel):
    created = dm.DateTimeField(auto_now_add=True, db_index=True)
    level = dm.CharField(max_length=12, default="info", db_index=True)
    kind = dm.CharField(max_length=200, default=None, null=True, db_index=True)
    method = dm.CharField(max_length=200, default=None, null=True)
    path = dm.TextField(default=None, null=True, db_index=True)
    payload = dm.TextField(default=None, null=True)
    ip = dm.CharField(max_length=32, default=None, null=True, db_index=True)
    duid = dm.TextField(default=None, null=True)
    uid = dm.IntegerField(default=0, db_index=True)
    username = dm.TextField(default=None, null=True)
    user_agent = dm.TextField(default=None, null=True)
    log = dm.TextField(default=None, null=True)
    model_name = dm.TextField(default=None, null=True, db_index=True)
    model_id = dm.IntegerField(default=0, db_index=True)
    # expires = dm.DateTimeField(db_index=True)

    @classmethod
    def logit(cls, request, log, kind="log", model_name=None, model_id=0, level="info", **kwargs):
        if isinstance(log, dict):
            log = ujson.encode(log, indent=4)
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
