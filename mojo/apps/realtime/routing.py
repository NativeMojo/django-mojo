"""
ASGI routing system for Mojo realtime WebSockets.

Provides ProtocolTypeRouter and WebSocketRouter classes similar to Django Channels
for clean, familiar integration with Django projects.

Example usage:
    from django.core.asgi import get_asgi_application
    from mojo.apps.realtime.routing import ProtocolTypeRouter, WebSocketRouter
    from mojo.apps.realtime.asgi import get_asgi_application as get_realtime_asgi

    application = ProtocolTypeRouter({
        "http": get_asgi_application(),
        "websocket": WebSocketRouter([
            (r"^ws/realtime/$", get_realtime_asgi()),
        ]),
    })
"""

import re


class ProtocolTypeRouter:
    """
    ASGI application that routes by protocol type.

    Similar to Channels' ProtocolTypeRouter - routes different protocol types
    to different ASGI applications.
    """

    def __init__(self, application_mapping):
        """
        Args:
            application_mapping: Dict mapping protocol types to ASGI applications
                                Example: {"http": django_app, "websocket": ws_router}
        """
        self.application_mapping = application_mapping

    async def __call__(self, scope, receive, send):
        """ASGI application entry point"""
        protocol_type = scope.get("type")

        if protocol_type in self.application_mapping:
            application = self.application_mapping[protocol_type]
            await application(scope, receive, send)
        elif protocol_type == "lifespan":
            # Route lifespan events to HTTP application (Django handles startup/shutdown)
            http_app = self.application_mapping.get("http")
            if http_app:
                await http_app(scope, receive, send)
            else:
                # No HTTP app to handle lifespan, just acknowledge
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
        else:
            # Unsupported protocol

            print(f"Unsupported protocol type: {protocol_type}")
            if protocol_type == "websocket":
                await send({"type": "websocket.close", "code": 403})
            else:
                # For HTTP-like protocols, send a basic response
                await send({
                    "type": "http.response.start",
                    "status": 400,
                    "headers": [[b"content-type", b"text/plain"]],
                })
                await send({
                    "type": "http.response.body",
                    "body": b"Unsupported protocol",
                })


class WebSocketRouter:
    """
    ASGI application that routes WebSocket connections by path pattern.

    Similar to Django URL routing - matches WebSocket paths against
    regex patterns and routes to appropriate applications.
    """

    def __init__(self, routes):
        """
        Args:
            routes: List of (pattern, application) tuples
                   Example: [(r"^ws/realtime/$", realtime_app)]
        """
        self.routes = []
        for pattern, application in routes:
            compiled_pattern = re.compile(pattern)
            self.routes.append((compiled_pattern, application))

    async def __call__(self, scope, receive, send):
        """ASGI application entry point"""
        if scope["type"] != "websocket":

            print(f"WebSocketRouter received non-websocket scope: {scope['type']}")
            return

        path = scope["path"]

        # Try to match against each route pattern
        for pattern, application in self.routes:
            match = pattern.match(path)
            if match:
                # Add match groups to scope for use by application
                scope["path_match"] = match
                await application(scope, receive, send)
                return

        # No route matched - reject connection
        print(f"No WebSocket route matched path: {path}")
        await send({"type": "websocket.close", "code": 404})


class URLRouter:
    """
    Alias for WebSocketRouter for Channels compatibility.

    In Channels, URLRouter is used for both HTTP and WebSocket routing.
    Since we only need WebSocket routing, this is just an alias.
    """

    def __init__(self, routes):
        self.router = WebSocketRouter(routes)

    async def __call__(self, scope, receive, send):
        await self.router(scope, receive, send)


def path(route, application):
    """
    Convenience function to create WebSocket routes with simpler syntax.

    Similar to Django's path() - converts simple path patterns to regex.

    Args:
        route: Simple path pattern (e.g., "ws/realtime/")
        application: ASGI application to handle this route

    Returns:
        (regex_pattern, application) tuple for use in WebSocketRouter

    Example:
        path("ws/realtime/", get_realtime_asgi())
        # Equivalent to: (r"^ws/realtime/$", get_realtime_asgi())
    """
    # Convert simple path to regex
    # Escape special regex characters except for named groups
    escaped_route = re.escape(route)

    # Add anchors for exact matching
    if not escaped_route.startswith("^"):
        escaped_route = "^" + escaped_route
    if not escaped_route.endswith("$"):
        escaped_route = escaped_route + "$"

    return (escaped_route, application)


def create_application(websocket_routes=None):
    """
    Convenience function to create a complete ASGI application.

    Args:
        websocket_routes: List of WebSocket routes (optional)
                         If None, includes default realtime route

    Returns:
        ProtocolTypeRouter instance ready to use as ASGI application
    """
    from django.core.asgi import get_asgi_application
    from .asgi import get_asgi_application as get_realtime_asgi

    # Default WebSocket routes
    if websocket_routes is None:
        websocket_routes = [
            ("/ws/realtime/", get_realtime_asgi()),
        ]

    return ProtocolTypeRouter({
        "http": get_asgi_application(),
        "websocket": WebSocketRouter(websocket_routes),
    })
