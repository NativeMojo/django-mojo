"""
ASGI application for realtime WebSocket handling.

Provides the ASGI interface for WebSocket connections at /ws/realtime/.
Integrates with Django's existing ASGI application for HTTP requests.
"""


class ASGIApplication:
    """ASGI application for WebSocket realtime connections"""

    def __init__(self):
        pass

    async def __call__(self, scope, receive, send):
        """ASGI application entry point"""
        if scope["type"] == "websocket":
            await self.websocket_application(scope, receive, send)
        else:
            # Reject non-WebSocket connections
            await send({
                "type": "websocket.close",
                "code": 403
            })

    async def websocket_application(self, scope, receive, send):
        """Handle WebSocket connections"""
        from .handler import WebSocketHandler

        path = scope["path"]

        if path != "/ws/realtime/":
            # Reject connections to wrong path
            await send({
                "type": "websocket.close",
                "code": 404
            })
            return

        # Accept the WebSocket connection
        await send({"type": "websocket.accept"})

        # Create WebSocket wrapper (include ASGI scope for headers/client info)
        websocket = ASGIWebSocketWrapper(scope, receive, send)

        # Handle the connection
        handler = WebSocketHandler(websocket, path)
        try:
            await handler.handle_connection()
        except Exception as e:
            from mojo.helpers import logit
            logit.exception(f"Error in WebSocket handler: {e}")
        finally:
            # Ensure connection is closed
            try:
                await send({"type": "websocket.close", "code": 1000})
            except:
                pass


class ASGIWebSocketWrapper:
    """Wrapper to make ASGI WebSocket interface compatible with websockets library"""

    def __init__(self, scope, receive, send):
        self.scope = scope
        self.receive = receive
        self._send = send
        self._closed = False
        # Normalize headers to a simple case-insensitive dict (lower-cased keys)
        headers = {}
        try:
            for k, v in (scope.get("headers") or []):
                headers[k.decode("latin1").lower()] = v.decode("latin1")
        except Exception:
            pass
        self.request_headers = headers
        # Expose client address via a transport-like object for compatibility
        self.transport = _ASGITransport(scope)

    async def __aiter__(self):
        """Async iterator for receiving messages"""
        while not self._closed:
            message = await self.receive()

            if message["type"] == "websocket.receive":
                if "text" in message:
                    yield message["text"]
                elif "bytes" in message:
                    yield message["bytes"].decode("utf-8")
            elif message["type"] == "websocket.disconnect":
                self._closed = True
                break

    async def send(self, message):
        """Send message to client"""
        if self._closed:
            return

        try:
            await self._send({
                "type": "websocket.send",
                "text": message
            })
        except Exception:
            self._closed = True

    async def close(self, code=1000):
        """Close the WebSocket connection"""
        if not self._closed:
            self._closed = True
            try:
                await self._send({
                    "type": "websocket.close",
                    "code": code
                })
            except:
                pass


class _ASGITransport:
    """Minimal transport shim exposing peername from ASGI scope client tuple."""
    def __init__(self, scope):
        self._client = scope.get("client")
    def get_extra_info(self, name, default=None):
        if name == "peername":
            return self._client
        return default
def get_asgi_application():
    """Get the realtime ASGI application instance"""
    return ASGIApplication()
