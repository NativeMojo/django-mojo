# routing.py
"""
WebSocket routing for the realtime app.

Exposes `websocket_urlpatterns` for inclusion in the project's root ASGI routing.
"""

from django.urls import path
from .consumers import AuthenticatedConsumer

websocket_urlpatterns = [
    path("ws/realtime/", AuthenticatedConsumer.as_asgi()),
]
