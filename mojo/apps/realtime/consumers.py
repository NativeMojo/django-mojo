# consumers.py
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
import asyncio
import json
from mojo.helpers import logit
import time
import re
from django.conf import settings

from mojo.apps.realtime.auth import (
    async_validate_bearer_token,
    attach_identity_to_scope,
)
from mojo.helpers import modules

logger = logit.get_logger("realtime", "realtime.log")


# Settings-driven message handlers map: {"message_type": "path.to.function"}
REALTIME_MESSAGE_HANDLERS_MAP = getattr(settings, "REALTIME_MESSAGE_HANDLERS", {})
_RESOLVED_MESSAGE_HANDLERS = {}

def normalize_topic(topic: str) -> str:
    """
    Normalize an external topic string (e.g., 'user:123') to a Channels-safe
    group name. Channels group names should be alnum/underscore/dash/dot.
    """
    topic = str(topic or "")
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", topic)[:200] or "general_announcements"

def get_message_handler(message_type: str):
    """Resolve a message handler function from REALTIME_MESSAGE_HANDLERS."""
    if not message_type:
        return None
    if message_type in _RESOLVED_MESSAGE_HANDLERS:
        return _RESOLVED_MESSAGE_HANDLERS[message_type]
    path = REALTIME_MESSAGE_HANDLERS_MAP.get(message_type)
    if not path:
        return None
    try:
        fn = modules.load_function(path)
        _RESOLVED_MESSAGE_HANDLERS[message_type] = fn
        return fn
    except Exception:
        logger.exception("Failed to load realtime message handler for '%s' from '%s'", message_type, path)
        return None

async def call_handler_maybe_async(fn, *args, **kwargs):
    """Call a handler that may be sync or async, safely with DB access."""
    if fn is None:
        return None
    if asyncio.iscoroutinefunction(fn):
        return await fn(*args, **kwargs)
    return await database_sync_to_async(fn)(*args, **kwargs)

async def call_instance_hook(instance, hook_name: str, *args, **kwargs):
    """Safely call instance hooks like on_realtime_message/disconnected."""
    if not instance:
        return None
    fn = getattr(instance, hook_name, None)
    if not callable(fn):
        return None
    return await call_handler_maybe_async(fn, *args, **kwargs)





class AuthenticatedConsumer(AsyncWebsocketConsumer):
    """
    A simple, generic WebSocket consumer with message-based authentication.

    Protocol:
    1) Server accepts the connection and sends:
       { "type": "auth_required", "timeout_seconds": 30 }

    2) Client must respond within the timeout with split fields:
       { "type": "authenticate", "token": "<token>", "prefix": "bearer" }
       - prefix is optional and defaults to "bearer"

    3) On success, server replies with:
       {
         "type": "auth_success",
         "user_id": ...,
         "username": ...,
         "available_topics": [...],
         ...
       }

    4) After authentication, client may send:
       - { "action": "subscribe", "topic": "<topic>" }
       - { "action": "unsubscribe", "topic": "<topic>" }
       - { "action": "ping" }

    Notifications can be delivered via channel layer group_send to subscribed topics.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.authenticated = False
        self.instance = None
        self.instance_kind = None
        self.auth_timeout_task = None
        self.user_subscriptions = set()
        self.auth_deadline = None

    async def connect(self):
        # Accept connection immediately
        await self.accept()

        # Record connection time for timeout
        self.auth_deadline = time.time() + 30  # 30 seconds from now

        # Start authentication timeout task
        self.auth_timeout_task = asyncio.create_task(self.authentication_timeout())

        # Send authentication challenge
        await self.send(text_data=json.dumps({
            'type': 'auth_required',
            'message': 'Authentication required',
            'timeout_seconds': 30
        }))

        logger.info("WebSocket connection established - authentication required within 30 seconds")

    async def disconnect(self, close_code):
        # Cancel authentication timeout task
        if self.auth_timeout_task and not self.auth_timeout_task.done():
            self.auth_timeout_task.cancel()

        # Clean up subscriptions
        for subscription in list(self.user_subscriptions):
            try:
                await self.channel_layer.group_discard(subscription, self.channel_name)
            except Exception:
                # Best-effort cleanup
                pass

        # Instance-level disconnect hook
        if self.instance:
            try:
                await call_instance_hook(self.instance, "on_realtime_disconnected")
            except Exception:
                logger.exception("Error in instance.on_realtime_disconnected()")

        if self.instance:
            ident = getattr(self.instance, "username", None) or getattr(self.instance, "name", None) or str(self.instance)
            logger.info(f'Authenticated {self.instance_kind or "instance"} {ident} disconnected (code: {close_code})')
        else:
            logger.info(f'Unauthenticated connection disconnected (code: {close_code})')

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
            message_type = data.get('type')
        except json.JSONDecodeError:
            logger.error("Invalid JSON format")
            await self.send_error('Invalid JSON format')
            return

        if not self.authenticated:
            if message_type == 'authenticate':
                await self.handle_authentication(data)
            else:
                logger.error("Authentication required - only 'authenticate' messages accepted")
                await self.send_error('Authentication required - only "authenticate" messages accepted')
            return

        # Handle authenticated messages
        await self.handle_authenticated_message(data)

    async def authentication_timeout(self):
        """Force disconnect if not authenticated within 30 seconds"""
        try:
            await asyncio.sleep(30)

            if not self.authenticated:
                logger.warning("WebSocket authentication timeout - forcibly closing connection")

                # Send timeout message before closing
                await self.send(text_data=json.dumps({
                    'type': 'auth_timeout',
                    'message': 'Authentication timeout - connection closing'
                }))

                # Small delay to ensure message is sent
                await asyncio.sleep(0.1)
                logger.info("Closing connection, instance not authorized")
                # Force close with custom code for timeout
                await self.close(code=4008)  # Policy Violation (RFC 6455)

        except asyncio.CancelledError:
            # Task was cancelled because user authenticated in time
            # logger.info("Authentication timeout cancelled - user authenticated successfully")
            pass

    async def handle_authentication(self, data):
        """Handle authentication message via split fields with default prefix 'bearer'."""
        # Check if we're past the deadline
        if self.auth_deadline is not None and time.time() > self.auth_deadline:
            logger.error("Authentication timeout exceeded")
            await self.send_error('Authentication timeout exceeded', close_after=True)
            return

        # Split fields approach; default prefix is 'bearer'
        token = data.get('token')
        prefix = (data.get('prefix') or 'bearer').lower()

        if not token:
            logger.error("Token required for authentication")
            await self.send_error('Token required for authentication')
            return

        # Validate using shared bearer flow (consistent with HTTP middleware)
        logger.info(f"Validating token with prefix '{prefix}'")
        instance, err, key_name = await async_validate_bearer_token(prefix, token, request=None)
        if not instance or err:
            logger.error(f"Authentication failed: {err}")
            await self.send_error(err or 'Authentication failed', close_after=True)
            return

        # Authentication successful
        logger.info("Authentication successful")
        self.authenticated = True
        self.instance = instance
        self.instance_kind = key_name or prefix
        attach_identity_to_scope(self.scope, instance, prefix)

        # Instance-level connected hook
        try:
            await call_instance_hook(self.instance, "on_realtime_connected")
        except Exception:
            logger.exception("Error in instance.on_realtime_connected()")

        # Cancel the timeout task
        if self.auth_timeout_task and not self.auth_timeout_task.done():
            self.auth_timeout_task.cancel()

        # Auto-subscribe to instance-specific topic (external: "<kind>:<id>")
        uid = getattr(instance, "id", None)
        if uid is not None:
            topic = f'{self.instance_kind}:{uid}'
            group = normalize_topic(topic)
            await self.channel_layer.group_add(group, self.channel_name)
            self.user_subscriptions.add(group)

        # Send authentication success
        await self.send(text_data=json.dumps({
            'type': 'auth_success',
            'message': f'Authenticated as {getattr(instance, "username", None) or getattr(instance, "name", None) or str(instance)}',
            'instance_kind': self.instance_kind,
            'instance_id': getattr(instance, "id", None),
            'authenticated_at': time.time(),
            'available_topics': await self.get_available_topics(instance)
        }))

        logger.info(f'Authenticated {self.instance_kind} connected')

    async def handle_authenticated_message(self, data):
        """Handle messages from authenticated connections."""
        action = data.get('action')

        if action == 'subscribe':
            await self.handle_subscribe(data.get('topic'))
            return
        if action == 'unsubscribe':
            await self.handle_unsubscribe(data.get('topic'))
            return
        if action == 'ping':
            await self.handle_ping()
            return

        # Generic message handling via mapping, then instance hook
        message_type = data.get('message_type')
        t = data.get('type')
        reserved = {'authenticate', 'auth_required', 'auth_success', 'error', 'auth_timeout', 'pong', 'notification'}
        if not message_type and t and t not in reserved:
            message_type = t

        # Try configured handler
        if message_type:
            handler = get_message_handler(message_type)
            if handler:
                try:
                    result = await call_handler_maybe_async(
                        handler,
                        consumer=self,
                        instance=self.instance,
                        instance_kind=self.instance_kind,
                        data=data,
                    )
                    if isinstance(result, dict):
                        await self.send(text_data=json.dumps(result))
                    return
                except Exception:
                    logger.exception("Error in realtime message handler for type '%s'", message_type)
                    await self.send_error(f"Handler error for message_type '{message_type}'")
                    return

        # Fall back to instance hook
        try:
            result = await call_instance_hook(self.instance, "on_realtime_message", data)
            if isinstance(result, dict):
                await self.send(text_data=json.dumps(result))
            else:
                await self.send(text_data=json.dumps({'type': 'ack', 'timestamp': time.time()}))
        except Exception:
            logger.exception("Error in instance.on_realtime_message")
            await self.send_error("Error handling message")

    async def handle_subscribe(self, topic):
        """Subscribe the connection to a topic (group)."""
        if not topic or not isinstance(topic, str):
            await self.send_error("Invalid or missing 'topic' for subscribe")
            return

        # Basic authorization: only allow topics from available_topics (external names)
        available = set(await self.get_available_topics(self.instance))
        if topic not in available:
            await self.send_error(f"Not allowed to subscribe to topic '{topic}'")
            return

        group = normalize_topic(topic)
        await self.channel_layer.group_add(group, self.channel_name)
        self.user_subscriptions.add(group)

        await self.send(text_data=json.dumps({
            'type': 'subscribed',
            'topic': topic,
            'group': group,
            'timestamp': time.time()
        }))

    async def handle_unsubscribe(self, topic):
        """Unsubscribe the connection from a topic (group)."""
        if not topic or not isinstance(topic, str):
            await self.send_error("Invalid or missing 'topic' for unsubscribe")
            return

        group = normalize_topic(topic)
        if group in self.user_subscriptions:
            await self.channel_layer.group_discard(group, self.channel_name)
            self.user_subscriptions.discard(group)

            await self.send(text_data=json.dumps({
                'type': 'unsubscribed',
                'topic': topic,
                'group': group,
                'timestamp': time.time()
            }))
        else:
            await self.send_error(f"Not subscribed to topic '{topic}'")

    async def send_error(self, message, close_after=False):
        """Send error message and optionally close connection."""
        await self.send(text_data=json.dumps({
            'type': 'error',
            'message': message,
            'timestamp': time.time()
        }))

        if close_after:
            # Small delay to ensure error message is sent
            await asyncio.sleep(0.1)
            logger.info(f"Closing connection, user not authorized")
            await self.close(code=4001)  # Unauthorized

    async def handle_ping(self):
        """Handle ping from authenticated client."""
        await self.send(text_data=json.dumps({
            'type': 'pong',
            'timestamp': time.time(),
            'instance_kind': self.instance_kind
        }))

    async def get_available_topics(self, instance):
        """Get topics available to authenticated instance (external topic names)."""
        topics = ['general_announcements']

        if getattr(instance, "is_staff", False):
            topics.append('admin_alerts')

        uid = getattr(instance, "id", None)
        if uid is not None and self.instance_kind:
            topics.append(f'{self.instance_kind}:{uid}')

        return topics

    # Message handlers for notifications (only work when authenticated)
    async def notification_message(self, event):
        """Handle notification events for authenticated users."""
        if self.authenticated:
            return
        # Build notification message with only existing fields
        notification = {'type': 'notification'}
        if 'topic' in event:
            notification['topic'] = event['topic']
        if 'title' in event:
            notification['title'] = event['title']
        if 'message' in event:
            notification['message'] = event['message']
        if 'data' in event:
            notification['data'] = event['data']
        if 'timestamp' in event:
            notification['timestamp'] = event['timestamp']
        if 'priority' in event:
            notification['priority'] = event['priority']
        elif 'priority' not in event:
            notification['priority'] = 'normal'
        await self.send(text_data=json.dumps(notification))

    async def action(self, event):
        if not self.authenticated:
            return
        # event is a dict you sent from server; relay or handle as needed
        await self.send(text_data=json.dumps({
            "type": "action",
            "topic": event.get("topic"),
            "action": event.get("action"),
            "data": event.get("data"),
            "timestamp": event.get("timestamp"),
        }))
