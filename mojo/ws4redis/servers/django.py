#-*- coding: utf-8 -*-
import base64
import select
from hashlib import sha1
from wsgiref import util
from django.core.handlers import wsgi as django_wsgi
from django.core.wsgi import get_wsgi_application
from django.core.servers.basehttp import WSGIServer, WSGIRequestHandler, ServerHandler

from django.conf import settings
from django.core.management.commands import runserver
import socketserver
from django.utils.encoding import force_str
from mojo.ws4redis.websocket import WebSocket
from mojo.ws4redis.servers.base import WebsocketServerBase, HandshakeError, UpgradeRequiredError

from io import IOBase


from mojo.helpers.logit import get_logger
logger = get_logger("async", filename="async.log")

logger.info("YES")

util._hoppish = {}.__contains__


class LimitedStreamPatched(IOBase):
    """
    Wrap another stream to disallow reading it past a number of bytes.

    Based on the implementation from werkzeug.wsgi.LimitedStream
    See https://github.com/pallets/werkzeug/blob/dbf78f67/src/werkzeug/wsgi.py#L828
    """

    def __init__(self, stream, limit):
        self.stream = stream
        self._read = stream.read
        self._readline = stream.readline
        self._pos = 0
        self.limit = limit

    def read(self, size=-1, /):
        _pos = self._pos
        limit = self.limit
        if _pos >= limit:
            return b""
        if size == -1 or size is None:
            size = limit - _pos
        else:
            size = min(size, limit - _pos)
        data = self._read(size)
        self._pos += len(data)
        return data

    def readline(self, size=-1, /):
        _pos = self._pos
        limit = self.limit
        if _pos >= limit:
            return b""
        if size == -1 or size is None:
            size = limit - _pos
        else:
            size = min(size, limit - _pos)
        line = self._readline(size)
        self._pos += len(line)
        return line


def patchLimitedStream():
    django_wsgi.LimitedStream = LimitedStreamPatched


class WSGIRequestHandlerRunServer(WSGIRequestHandler):
    def handle(self):
        self.raw_requestline = self.rfile.readline(65537)
        if len(self.raw_requestline) > 65536:
            self.requestline = ''
            self.request_version = ''
            self.command = ''
            self.send_error(414)
            return

        if not self.parse_request():  # An error code has been sent, just exit
            return

        handler = ServerHandler(
            self.rfile, self.wfile, self.get_stderr(), self.get_environ()
        )
        handler.request_handler = self      # backpointer for logging
        handler.http_version = '1.1'
        handler.run(self.server.get_app())


class WebsocketRunServer(WebsocketServerBase):
    WS_GUID = b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
    WS_VERSIONS = ('13', '8', '7')

    def upgrade_websocket(self, environ, start_response):
        """
        Attempt to upgrade the socket environ['wsgi.input'] into a websocket enabled connection.
        """
        websocket_version = environ.get('HTTP_SEC_WEBSOCKET_VERSION', '')
        if not websocket_version:
            raise UpgradeRequiredError
        elif websocket_version not in self.WS_VERSIONS:
            raise HandshakeError('Unsupported WebSocket Version: {0}'.format(websocket_version))

        key = environ.get('HTTP_SEC_WEBSOCKET_KEY', '').strip()
        if not key:
            raise HandshakeError('Sec-WebSocket-Key header is missing/empty')
        try:
            key_len = len(base64.b64decode(key))
        except TypeError:
            raise HandshakeError('Invalid key: {0}'.format(key))
        if key_len != 16:
            # 5.2.1 (3)
            raise HandshakeError('Invalid key: {0}'.format(key))

        sec_ws_accept = base64.b64encode(sha1(key.encode('utf-8') + self.WS_GUID).digest())
        sec_ws_accept = sec_ws_accept.decode('ascii')
        headers = [
            ('Upgrade', 'websocket'),
            ('Connection', 'Upgrade'),
            ('Sec-WebSocket-Accept', sec_ws_accept),
            ('Sec-WebSocket-Version', str(websocket_version)),
        ]
        if environ.get('HTTP_SEC_WEBSOCKET_PROTOCOL') is not None:
            headers.append(('Sec-WebSocket-Protocol', environ.get('HTTP_SEC_WEBSOCKET_PROTOCOL')))

        logger.debug('WebSocket request accepted, switching protocols')
        start_response(force_str('101 Switching Protocols'), headers)
        start_response.__self__.finish_content()
        winput = environ['wsgi.input']
        if not hasattr(winput, "stream"):
            # hack to get the stream back in later DJANGO
            winput.stream = winput._read.__self__
        return WebSocket(winput.stream)

    def select(self, rlist, wlist, xlist, timeout=None):
        return select.select(rlist, wlist, xlist, timeout)


def run(addr, port, wsgi_handler, ipv6=False, threading=False, server_cls=None, **kwargs):
    """
    Function to monkey patch the internal Django command: manage.py runserver
    """
    server_address = (addr, port)
    logger.info('Websocket support is enabled', server_address)
    if not threading:
        raise Exception("Django's Websocket server must run with threading enabled")
    httpd_cls = type('WSGIServer', (socketserver.ThreadingMixIn, WSGIServer), {'daemon_threads': True})
    httpd = httpd_cls(server_address, WSGIRequestHandlerRunServer, ipv6=ipv6)
    httpd.set_app(wsgi_handler)
    httpd.serve_forever()


patchLimitedStream()

runserver.run = run

_django_app = get_wsgi_application()
_websocket_app = WebsocketRunServer()
_websocket_url = getattr(settings, 'WEBSOCKET_URL')


def application(environ, start_response):
    if _websocket_url and environ.get('PATH_INFO').startswith(_websocket_url):
        # logger.info("environ", environ)
        return _websocket_app(environ, start_response)
    return _django_app(environ, start_response)
