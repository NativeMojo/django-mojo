import time
from objict import objict
from django.core.handlers.wsgi import WSGIRequest
from django.apps import apps

from mojo.ws4redis import settings as private_settings
from mojo.ws4redis.redis import RedisStore, RedisMessage

from mojo.apps.account.utils.jwtoken import JWToken
from mojo.helpers import request as rhelper
from mojo.helpers.logit import get_logger
logger = get_logger("async", filename="async.log")

MODEL_CACHE = dict()  # caching of app.Model for faster access

ALLOW_ANY_FACILITY = not private_settings.WS4REDIS_FACILITIES
DISCONNECT_AFTER_NO_CREDS = 30



class WebsocketConnection():
    def __init__(self, server, environ, start_response):
        self.server = server
        self.request = WSGIRequest(environ)
        self.ip = rhelper.get_remote_ip(self.request)
        self.ua = rhelper.get_user_agent(self.request)
        self.facility = self.request.path_info.replace(private_settings.WEBSOCKET_URL, '', 1)
        self.credentials = objict()
        self.listening_fds = None
        self.redis = RedisStore(server._redis_connection)
        self.websocket = server.upgrade_websocket(environ, start_response)
        self.last_beat = time.time()
        self.conneted_time = time.time()
        self.last_msg = None
        self._heart_beat = private_settings.WS4REDIS_HEARTBEAT
        self.debug = private_settings.WS4REDIS_LOG_DEBUG

    @property
    def elapsed_time(self):
        return time.time() - self.conneted_time

    def refreshFDs(self):
        if len(self.listening_fds) == 1:
            sub_sd = self.redis.get_file_descriptor()
            if sub_sd:
                self.listening_fds.append(sub_sd)

    def on_auth(self, msg):
        if msg.kind == "jwt":
            self.on_auth_jwt(msg)
            return
        # check our other auth mechanisms
        auther = self.getAuthenticator(msg.kind)
        if auther is None or not hasattr(auther, "authWS4RedisConnection"):
            logger.error(f"{self.ip} invalid auth", msg)
            self.sendToWS(msg.channel, dict(error="invalid auth kind", code=500))
            return

        # logger.info(f"{self.ip} authenticating")
        self.credentials = auther.authWS4RedisConnection(msg)
        if self.credentials is None or self.credentials.pk is None:
            logger.error(f"{self.ip} invalid credentials for", msg, self.credentials)
            self.sendToWS(msg.channel, dict(error="invalid credentials", code=401))
            return
        self.on_authenticated()

    def on_auth_jwt(self, msg):
        # logger.info(f"{self.ip} authenticating with JWT")
        User = apps.get_model("account", "User")
        user, error = User.validate_jwt(msg.token)
        if error is not None:
            raise Exception(error)
        self.credentials = objict(kind="user", instance=user, pk=user.pk, uuid=user.username)
        self.on_authenticated()

    def on_authenticated(self):
        if self.debug:
            logger.info(F"authenticated {self.credentials.kind}: {self.credentials.uuid}")
        if self.credentials.kind == "user":
            channel_key = self.redis.subscribe("user", self.facility, self.credentials.uuid)
            self.forwardPending(channel_key)
        else:
            channel_key = self.redis.subscribe(self.credentials.kind, self.facility, self.credentials.uuid)
            self.forwardPending(channel_key)

        self.redis.publishModelOnline(self.credentials.kind, self.credentials.pk, only_one=self.credentials.only_one)
        self.refreshFDs()
        if self.credentials and self.credentials.instance:
            if hasattr(self.credentials.instance, "on_ws_online"):
                self.credentials.instance.on_ws_online()

    def forwardPending(self, channel_key):
        msg = self.redis.getPendingMessage(channel_key)
        if msg:
            if self.debug:
                logger.info("pending messages", repr(msg))
            channel, pk = self.parseChannel(channel_key)
            self.sendToWS(channel, msg, pk)

    def on_resubscribe(self, msg):
        for ch in msg.channels:
            self.redis.subscribe(ch.channel, self.facility, ch.pk)

    def getAppModel(self, app_model):
        if app_model not in MODEL_CACHE:
            app_label, model_name = app_model.split('.')
            MODEL_CACHE[app_model] = apps.get_model(app_label, model_name)
        return MODEL_CACHE[app_model]

    def getAuthenticator(self, channel):
        auther_path = private_settings.WS4REDIS_AUTHENTICATORS.get(channel, None)
        if auther_path is not None:
            return self.getAppModel(auther_path)
        return None

    def on_subscribe(self, msg):
        pks = self.canSubscribeTo(msg)
        if bool(pks):
            for pk in pks:
                channel_key = self.redis.subscribe(msg.channel, self.facility, pk)
        else:
            logger.warning("subscribe permission denied", msg)
            self.sendToWS(msg.channel, dict(error="subscribe permission denied"))

    def canSubscribeTo(self, msg):
        ch_model = private_settings.WS4REDIS_CHANNELS.get(msg.channel, None)
        if not ch_model:
            return None
        if ch_model == "any":
            # this allows anything to be sent
            return [msg.pk]
        Model = self.getAppModel(ch_model)
        if not hasattr(Model, "can_ws_subscribe_to"):
            return None
        return Model.can_ws_subscribe_to(self.credentials, msg)

    def on_unsubscribe(self, msg):
        self.redis.unsubscribe(msg.channel, self.facility, msg.pk)

    def on_publish(self, msg):
        if self.canPublishTo(msg):
            self.redis.publish(msg.message, channel=msg.channel, pk=msg.pk)
        else:
            logger.warning("publish permission denied", msg)
            self.sendToWS(msg.channel, dict(error="publish permission denied"))

    def canPublishTo(self, msg):
        ch_model = private_settings.WS4REDIS_CHANNELS.get(msg.channel, None)
        if not ch_model:
            # you can publish to any channels you may create
            return True
        Model = self.getAppModel(ch_model)
        if not hasattr(Model, "canPublishTo"):
            logger.warning("canPublishTo", "no canPublishTo")
            return False
        return Model.can_ws_publish_to(self.credentials, msg)

    def on_ws_msg(self, raw_data):
        if isinstance(raw_data, bytes):
            raw_data = raw_data.decode()
        if raw_data == self._heart_beat:
            # echo the heartbeat
            self.websocket.send(self._heart_beat)
            return
        if self.debug:
            logger.info("on_ws_msg", repr(raw_data))
        dmsg = objict.from_json(raw_data, ignore_errors=True)
        if dmsg.action:
            # this is a special message that we want to handle directly
            try:
                if dmsg.action == "auth":
                    self.on_auth(dmsg)
                elif dmsg.action == "subscribe":
                    self.on_subscribe(dmsg)
                elif dmsg.action == "unsubscribe":
                    self.on_unsubscribe(dmsg)
                elif dmsg.action == "resubscribe":
                    self.on_resubscribe(dmsg)
                elif dmsg.action == "publish":
                    self.on_publish(dmsg)
                elif dmsg.action == "save":
                    self.on_save(dmsg)
                elif dmsg.channel:
                    self.on_channel_msg(dmsg)
            except Exception as err:
                self.sendToWS(dmsg.get("channel", "system"), dict(error=str(err)))
                logger.exception(self.ip)

    def on_save(self, msg):
        if self.credentials and self.credentials.instance:
            if hasattr(self.credentials.instance, "on_ws_save"):
                resp = objict(id=msg.id, name="save")
                if msg.echo:
                    resp.data = msg.data
                resp.status = self.credentials.instance.on_ws_save(msg)
                self.sendToWS(self.credentials.kind, resp)

    def on_channel_msg(self, msg):
        if not self.canPublishTo(msg):
            logger.warning("on_channel_msg permission denied", msg, private_settings.WS4REDIS_CHANNELS)
            self.sendToWS(msg.channel, dict(error="cannot publish to channel"))
            return None
        ch_model = private_settings.WS4REDIS_CHANNELS.get(msg.channel, None)
        if ch_model is None:
            return None
        Model = self.getAppModel(ch_model)
        if not hasattr(Model, "on_ws_message"):
            logger.warning(f"{msg.channel} does not support on_ws_message")
            self.sendToWS(msg.channel, dict(error="channel does not support on_ws_message"))
            return None
        Model.on_ws_message(self.credentials, msg, self)

    def on_redis_pending(self):
        sub_resp = self.redis.getSubMessage()
        if sub_resp:
            if self.debug:
                logger.info("incoming redis msg", sub_resp)
            self.on_redis_msg(sub_resp)

    def sendToWS(self, channel, message, pk=None):
        msg = objict(channel=channel)
        if pk is not None:
            msg.pk = pk
        if isinstance(message, (str, bytes)):
            msg.message = objict.from_json(message, ignore_errors=True)
        if not msg.message:
            msg.message = message
        elif msg.message.name == "logout" and msg.message.pk == self.credentials.pk:
            raise Exception("websocket is being logged out")
        self.websocket.send(msg.toJSON(as_string=True))

    def on_redis_msg(self, sub_resp):
        if isinstance(sub_resp, list):
            if sub_resp[0] == b'subscribe':
                # this is succesfull subscriptions
                # notify ws
                channel, pk = self.parseChannel(sub_resp[1].decode())
                msg = objict(name="subscribed", channel=channel, status=sub_resp[2])
                if pk is not None:
                    msg.pk = pk
                self.websocket.send(msg.toJSON(as_string=True))
                return
            elif sub_resp[0] == b'unsubscribe':
                # this is succesfull subscriptions
                # notify ws
                channel, pk = self.parseChannel(sub_resp[1].decode())
                msg = objict(name="unsubscribed", channel=channel, status=sub_resp[2])
                if pk is not None:
                    msg.pk = pk
                self.websocket.send(msg.toJSON(as_string=True))
                return
            elif sub_resp[0] == b'message':
                channel, pk = self.parseChannel(sub_resp[1].decode())
                self.sendToWS(channel, sub_resp[2].decode(), pk)
                return
        sendmsg = RedisMessage(sub_resp)
        if self.debug:
            logger.info(sub_resp)
        if sendmsg:
            if self.debug:
                logger.info("pushing to websocket", sendmsg)
            self.websocket.send(sendmsg)

    def parseChannel(self, channel):
        fields = channel.split(":")
        fields.pop(0)
        fields.pop()
        if len(fields) == 1:
            return fields[0], None
        return fields[0], fields[1]

    def on_ws_pending(self):
        try:
            self.last_msg = self.websocket.receive()
            if bool(self.last_msg):
                self.on_ws_msg(self.last_msg)
        except Exception:
            # logger.exception()
            logger.error(f"{self.ip}: unable to recv on ws... flushing")
            self.websocket.flush()

    def checkHeartbeat(self):
        beat_delta = time.time() - self.last_beat
        if beat_delta > 30.0:
            self.last_beat = time.time()
            if private_settings.WS4REDIS_HEARTBEAT and not self.websocket.closed:
                # logger.info("send heartbeat")
                self.websocket.send(private_settings.WS4REDIS_HEARTBEAT)

    def release(self):
        self.redis.release()
        self.listening_fds = None
        if self.websocket:
            self.websocket.close(code=1001, message='Websocket Closed')
        if self.credentials and self.credentials.instance:
            if hasattr(self.credentials.instance, "on_ws_offline"):
                self.credentials.instance.on_ws_offline()

    def handleComs(self):
        # check if token is in url
        if not ALLOW_ANY_FACILITY and self.facility not in private_settings.WS4REDIS_FACILITIES:
            self.sendToWS("facility", dict(error=f"facility name not allowed: '{self.facility}'.  Please check your connection URL."))
            return

        websocket_fd = self.websocket.get_file_descriptor()
        self.listening_fds = [websocket_fd]
        if callable(private_settings.URL_AUTHENTICATOR):
            private_settings.URL_AUTHENTICATOR(self)

        while self.websocket and not self.websocket.closed:
            ready = self.server.select(self.listening_fds, [], [], 4.0)[0]
            if not ready:
                self.websocket.flush()
            if not self.credentials:
                if self.elapsed_time > 8 and self.elapsed_time < 30:
                    logger.error(f"{self.ip} has sent no credentials", self.ua)
                    self.sendToWS("user", dict(error="no credentials received"))
                elif private_settings.WS4REDIS_NOAUTH_CLOSE and self.elapsed_time > 30:
                    self.release()

            for fd in ready:
                if fd == websocket_fd:
                    self.on_ws_pending()
                else:
                    self.on_redis_pending()
