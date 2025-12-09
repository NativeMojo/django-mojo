"""
WebSocket handler for individual realtime connections.

Handles the lifecycle of a single WebSocket connection including:
- Connection registration and cleanup
- Authentication flow
- Message routing between client and Redis
- Topic subscription management
- Heartbeat/ping handling

All connection state is stored in Redis for scalability.
"""

import asyncio
import json
import time
import uuid
from mojo.helpers import logit
from mojo.helpers.redis.client import get_connection
from .auth import async_validate_bearer_token

logger = logit.get_logger("realtime", "realtime.log")

# Presence/connection/topic TTLs (seconds)
CONNECTION_TTL_SECONDS = 300         # connection record TTL
ONLINE_TTL_SECONDS = 300             # user online presence TTL
TOPIC_TTL_SECONDS = 300              # topic membership TTL
PRESENCE_REFRESH_MIN_INTERVAL = 30   # throttle presence refreshes


class WebSocketHandler:
    def __init__(self, websocket, path):
        self.websocket = websocket
        self.path = path
        self.connection_id = str(uuid.uuid4())
        self.authenticated = False
        self.user = None
        self.user_type = None
        self.subscribed_topics = set()

        # Capture remote IP and User-Agent from helpers (KISS)
        self.remote_ip = self.resolve_remote_ip()
        self.user_agent = self.resolve_user_agent()

        # Redis clients - separate for pub/sub
        self.redis_client = get_connection()
        self.pubsub = None

        # Control flags
        self.running = True
        self.connected_at = time.time()
        self.last_activity = time.time()
        self.last_presence_refresh = 0

    def _log(self, message):
        try:
            rip = self.remote_ip
        except Exception:
            rip = None
        logger.info(f"[{self.connection_id} -> {rip}]: {message}")

    def user_online_key(self):
        return f"realtime:online:{self.user_type}:{self.user.id}"

    def resolve_remote_ip(self):
        """
        Resolve the remote IP using ASGI scope first, then fallbacks.
        """
        try:
            scope = getattr(self.websocket, "scope", None)
            if scope:
                ip = self.get_remote_ip(scope)
                if ip:
                    return ip
            # Fallback to wrapper-provided request headers
            headers = getattr(self.websocket, "request_headers", None)
            if headers:
                xff = headers.get("x-forwarded-for") or headers.get("X-Forwarded-For")
                xreal = headers.get("x-real-ip") or headers.get("X-Real-IP")
                if xff:
                    return xff.split(",")[0].strip()
                if xreal:
                    return xreal.strip()
            # Final fallback to transport peername
            transport = getattr(self.websocket, "transport", None)
            if transport and hasattr(transport, "get_extra_info"):
                peer = transport.get_extra_info("peername")
                if peer:
                    return peer[0] if isinstance(peer, (tuple, list)) else str(peer)
        except Exception:
            self._log_exception("resolve_remote_ip")
        return None

    def get_remote_ip(self, scope):
        # Prefer the ASGI client tuple (ip, port)
        client = scope.get("client")
        if client and client[0]:
            return client[0]

        # Build lowercase header dict for fallbacks
        headers = {}
        for k, v in scope.get("headers", []):
            try:
                headers[k.decode().lower()] = v.decode()
            except Exception:
                pass

        # RFC 7239 Forwarded header: for=...
        fwd = headers.get("forwarded")
        if fwd:
            # naive parse – good enough for common cases
            parts = [p.strip() for p in fwd.split(";")]
            for p in parts:
                if p.startswith("for="):
                    return p.split("=", 1)[1].strip('"')

        # X-Forwarded-For: first IP
        xff = headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()

        # X-Real-IP
        xri = headers.get("x-real-ip")
        if xri:
            return xri

        return None

    def resolve_user_agent(self):
        """
        Resolve the User-Agent from ASGI scope headers or request_headers.
        """
        try:
            scope = getattr(self.websocket, "scope", None)
            if scope:
                for k, v in scope.get("headers", []):
                    try:
                        if k.decode().lower() == "user-agent":
                            return v.decode()
                    except Exception:
                        pass
            headers = getattr(self.websocket, "request_headers", None)
            if headers:
                return headers.get("user-agent") or headers.get("User-Agent")
        except Exception:
            pass
        return None

    def _log_exception(self, message):
        try:
            rip = self.remote_ip
        except Exception:
            rip = None
        logger.exception(f"[{self.connection_id} -> {rip}]: {message}")

    async def handle_connection(self):
        """Main connection handler - manages entire connection lifecycle"""
        self._log("connected")

        try:
            # Register connection in Redis
            await self.register_connection()

            # Send auth required message
            await self.send_message({
                "type": "auth_required",
                "timeout": 30
            })

            # Start background tasks
            tasks = [
                asyncio.create_task(self.activity_timeout()),
                asyncio.create_task(self.handle_client_messages()),
                asyncio.create_task(self.handle_redis_messages())
            ]

            # Wait for any task to complete (usually means connection ended)
            done, pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED
            )

            # Cancel remaining tasks
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        except Exception as e:
            self._log_exception("connection error")
        finally:
            await self.cleanup_connection()

    async def register_connection(self):
        """Register connection in Redis with TTL"""
        connection_data = {
            "connection_id": self.connection_id,
            "authenticated": False,
            "connected_at": time.time(),
            "last_ping": time.time(),
            "topics": [],
            "remote_ip": self.remote_ip
        }

        key = f"realtime:connections:{self.connection_id}"
        try:
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self.redis_client.setex(key, CONNECTION_TTL_SECONDS, json.dumps(connection_data))
            )
        except Exception as e:
            self._log_exception("registration failed")

    async def update_connection_auth(self):
        """Update connection with authentication info"""
        connection_data = {
            "connection_id": self.connection_id,
            "user_id": self.user.id if self.user else None,
            "user_type": self.user_type,
            "authenticated": True,
            "connected_at": time.time(),
            "last_ping": time.time(),
            "topics": list(self.subscribed_topics),
            "remote_ip": self.remote_ip,
            "user_agent": self.user_agent
        }

        key = f"realtime:connections:{self.connection_id}"
        try:
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self.redis_client.setex(key, CONNECTION_TTL_SECONDS, json.dumps(connection_data))
            )
        except Exception as e:
            self._log_exception("update failed")

    async def register_user_online(self):
        """Register user as online in Redis"""
        if not self.user or not self.user_type:
            return

        key = self.user_online_key()

        def get_and_update():
            try:
                # Add this connection to the user's online set and refresh TTL
                self.redis_client.sadd(key, self.connection_id)
                self.redis_client.expire(key, ONLINE_TTL_SECONDS)
            except Exception:
                self._log_exception("Failed to register user online")

        await asyncio.get_event_loop().run_in_executor(None, get_and_update)

    async def activity_timeout(self):
        """Handle both auth and activity timeouts"""
        while self.running:
            await asyncio.sleep(5)  # Check every 5 seconds

            time_since_activity = time.time() - self.last_activity
            connected_duration = time.time() - self.connected_at

            if time_since_activity >= 30:
                if not self.authenticated:
                    await self.report_incident("auth timeout", "auth", 6)
                    await self.send_error("Authentication timeout")
                else:
                    self._log(f"timeout due to no activity for {time_since_activity:.2f} seconds, connected for {connected_duration:.2f} seconds")
                await self.close_connection()
                break

    async def handle_client_messages(self):
        """Handle messages from WebSocket client"""
        try:
            async for message in self.websocket:
                if not self.running:
                    break

                try:
                    data = json.loads(message)
                    await self.process_client_message(data)
                except json.JSONDecodeError:
                    await self.send_error("Invalid JSON")
                except Exception as e:
                    self._log_exception("message processing error")
                    await self.send_error("Message processing error")

        except Exception as e:
            if "closed" in str(e).lower():
                self._log("disconnected")
            else:
                self._log_exception("client message handler error")
        finally:
            self.running = False

    async def handle_redis_messages(self):
        """Handle messages from Redis pub/sub"""
        try:
            # Create pubsub connection
            def create_pubsub():
                pubsub = self.redis_client.pubsub()
                # Subscribe to connection-specific channel
                pubsub.subscribe(f"realtime:messages:{self.connection_id}")
                pubsub.subscribe("realtime:broadcast")
                return pubsub

            self.pubsub = await asyncio.get_event_loop().run_in_executor(
                None, create_pubsub
            )

            # Listen for messages
            while self.running:
                def get_message():
                    return self.pubsub.get_message(timeout=1.0)

                message = await asyncio.get_event_loop().run_in_executor(
                    None, get_message
                )

                if message and message['type'] == 'message':
                    try:
                        data = json.loads(message['data'])
                        await self.process_redis_message(data)
                    except Exception as e:
                        self._log(f"Error processing Redis message: {e}")

        except Exception as e:
            self._log_exception(f"Error in Redis message handler: {e}")
        finally:
            if self.pubsub:
                await asyncio.get_event_loop().run_in_executor(
                    None, self.pubsub.close
                )



    async def process_client_message(self, data):
        """Process message from client"""
        # Reset activity timeout on any incoming message
        self.last_activity = time.time()

        # Support both "type" and "action" fields for backward compatibility
        message_type = data.get("type") or data.get("action")

        if message_type == "authenticate":
            await self.handle_authenticate(data)
        elif message_type == "subscribe":
            await self.handle_subscribe(data)
        elif message_type == "unsubscribe":
            await self.handle_unsubscribe(data)
        elif message_type == "ping":
            await self.handle_ping(data)
        else:
            # Handle custom messages if authenticated
            if self.authenticated:
                await self.handle_custom_message(data)
            else:
                await self.send_error("Authentication required")

    async def handle_authenticate(self, data):
        """Handle authentication request"""
        if self.authenticated:
            await self.send_error("Already authenticated")
            return

        token = data.get("token")
        prefix = data.get("prefix", "bearer")

        if not token:
            await self.report_incident("auth with no token", "auth", 8)
            await self.send_error("Missing token")
            return

        # Use existing auth logic
        user, error, key_name = await async_validate_bearer_token(prefix, token)

        if error or not user:
            await self.report_incident("auth failed", "auth", 4)
            await self.send_error(f"Authentication failed: {error}")
            return

        self.user = user
        self.user_type = key_name
        self.authenticated = True

        # Update Redis state
        await self.update_connection_auth()
        await self.register_user_online()

        # Auto-subscribe to user's own topic
        user_topic = f"{self.user_type}:{self.user.id}"
        await self.subscribe_to_topic(user_topic)

        # Call user's connected hook if available
        if hasattr(self.user, 'on_realtime_connection'):
            connection_data = {
                "connection_id": self.connection_id,
                "remote_ip": self.remote_ip,
                "user_agent": self.user_agent
            }
            def call_hook():
                return self.user.on_realtime_connection(connection_data)
            result = await asyncio.get_event_loop().run_in_executor(None, call_hook)
            # Process hook response
            if result:
                await self._process_hook_response(result)
        elif hasattr(self.user, 'on_realtime_connected'):
            def call_hook():
                return self.user.on_realtime_connected()
            result = await asyncio.get_event_loop().run_in_executor(None, call_hook)

            # Process hook response
            if result:
                await self._process_hook_response(result)

        await self.send_message({
            "type": "auth_success",
            "user_type": self.user_type,
            "user_id": self.user.id
        })

    async def handle_subscribe(self, data):
        """Handle topic subscription"""
        if not self.authenticated:
            await self.send_error("Authentication required")
            return

        topic = data.get("topic")
        if not topic:
            await self.send_error("Missing topic")
            return

        # Topic authorization check
        if hasattr(self.user, 'on_realtime_can_subscribe'):
            def check_permission():
                return self.user.on_realtime_can_subscribe(topic)

            try:
                can_subscribe = await asyncio.get_event_loop().run_in_executor(
                    None, check_permission
                )
                if not can_subscribe:
                    await self.report_incident(f"access denied for topic {topic}", "permission_denied", 4)
                    await self.send_error(f"Access denied to topic: {topic}")
                    return
            except Exception as e:
                self._log_exception(f"Error checking topic permission for {topic}: {e}")
                await self.send_error("Authorization check failed")
                return

        await self.subscribe_to_topic(topic)

        await self.send_message({
            "type": "subscribed",
            "topic": topic
        })

    async def handle_unsubscribe(self, data):
        """Handle topic unsubscription"""
        if not self.authenticated:
            await self.send_error("Authentication required")
            return

        topic = data.get("topic")
        if not topic:
            await self.send_error("Missing topic")
            return

        await self.unsubscribe_from_topic(topic)

        await self.send_message({
            "type": "unsubscribed",
            "topic": topic
        })

    async def handle_ping(self, data):
        """Handle ping request"""
        if not self.authenticated:
            await self.send_error("Authentication required")
            return

        # Refresh presence TTLs on ping (throttled)
        await self.refresh_presence()

        await self.send_message({
            "type": "pong",
            "user_type": self.user_type,
            "user_id": self.user.id if self.user else None
        })

    async def handle_custom_message(self, data):
        """Handle custom message - delegate to user's hook if available"""


        if hasattr(self.user, 'on_realtime_message'):
            def call_hook():
                return self.user.on_realtime_message(data)

            try:
                response = await asyncio.get_event_loop().run_in_executor(
                    None, call_hook
                )

                if response:
                    await self._process_hook_response(response)
                else:
                    self._log("No response from user hook")
            except Exception as e:
                self._log_exception(f"Error in user message hook: {e}")
                await self.send_error("Message processing error")
        else:

            await self.send_error("Unsupported message type")

    async def _process_hook_response(self, response):
        """Process unified response from user hooks"""


        if isinstance(response, dict):
            # Send response message to client
            if "response" in response:

                await self.send_message(response["response"])

            # Process subscription requests
            if "subscriptions" in response:

                for topic in response["subscriptions"]:
                    if topic and isinstance(topic, str):
                        try:
                            await self.subscribe_to_topic(topic)
                        except Exception as e:
                            self._log(f"Failed to subscribe to topic {topic}: {e}")
        else:
            # Backward compatibility - treat non-dict as direct response

            await self.send_message(response)

    async def subscribe_to_topic(self, topic):
        """Subscribe connection to a topic"""
        if topic in self.subscribed_topics:
            return

        def subscribe():
            try:
                # Add to topic subscribers
                self.redis_client.sadd(f"realtime:topic:{topic}", self.connection_id)
                self.redis_client.expire(f"realtime:topic:{topic}", TOPIC_TTL_SECONDS)

                # Subscribe to Redis channel
                self.pubsub.subscribe(f"realtime:topic:{topic}")
            except Exception as e:
                self._log(f"Failed to subscribe to topic {topic}: {e}")
                raise

        await asyncio.get_event_loop().run_in_executor(None, subscribe)
        self.subscribed_topics.add(topic)

    async def unsubscribe_from_topic(self, topic):
        """Unsubscribe connection from a topic"""
        if topic not in self.subscribed_topics:
            return

        def unsubscribe():
            try:
                # Remove from topic subscribers
                self.redis_client.srem(f"realtime:topic:{topic}", self.connection_id)

                # Unsubscribe from Redis channel
                self.pubsub.unsubscribe(f"realtime:topic:{topic}")
            except Exception as e:
                self._log(f"Failed to unsubscribe from topic {topic}: {e}")

        await asyncio.get_event_loop().run_in_executor(None, unsubscribe)
        self.subscribed_topics.discard(topic)

    async def process_redis_message(self, data):
        """Process message from Redis pub/sub"""
        message_type = data.get("type")

        if message_type in ["broadcast", "topic_message", "direct_message"]:
            # Forward to client
            client_message = {
                "type": "message",
                "data": data.get("data", {}),
                "timestamp": data.get("timestamp")
            }

            if message_type == "topic_message":
                client_message["topic"] = data.get("topic")

            await self.send_message(client_message)
        elif message_type == "disconnect":
            await self.send_message(data)
            await self.close_connection()

    async def send_message(self, message):
        """Send message to WebSocket client"""
        # logger.debug(f"Sending WebSocket message to {self.connection_id}: {message}")
        try:
            await self.websocket.send(json.dumps(message))
        except Exception as e:
            if "closed" in str(e).lower():
                self.running = False
            else:
                self._log_exception(f"Error sending message: {e}")
                self.running = False

    async def send_error(self, error_message):
        """Send error message to client"""
        await self.send_message({
            "type": "error",
            "message": error_message
        })

    async def refresh_presence(self, force=False):
        """
        Refresh connection and online presence TTLs without blocking the event loop.
        Throttled by PRESENCE_REFRESH_MIN_INTERVAL unless force=True.
        """
        now = time.time()
        if not force and (now - getattr(self, "last_presence_refresh", 0)) < PRESENCE_REFRESH_MIN_INTERVAL:
            return

        self.last_presence_refresh = now
        conn_key = f"realtime:connections:{self.connection_id}"

        def do_refresh():
            try:
                # Extend connection record TTL
                self.redis_client.expire(conn_key, CONNECTION_TTL_SECONDS)
                # Extend user online presence TTL, if authenticated
                if self.user and self.user_type:
                    online_key = f"realtime:online:{self.user_type}:{self.user.id}"
                    self.redis_client.expire(online_key, ONLINE_TTL_SECONDS)
            except Exception:
                # Keep presence refresh best-effort
                pass

        await asyncio.get_event_loop().run_in_executor(None, do_refresh)

    async def report_incident(self, details, event_type="info", level=1, scope="realtime", **context):
        """
        Report an incident (audit/event) from any websocket event without blocking the event loop.
        Captures connection/user context and executes the synchronous reporter in a thread pool.
        """
        try:
            payload = dict(context or {})
            payload.setdefault("connection_id", self.connection_id)
            payload.setdefault("user_type", self.user_type)
            payload.setdefault("source_ip", self.remote_ip)
            payload.setdefault("request_ip", self.remote_ip)
            payload.setdefault("http_protocol", "websocket")
            payload.setdefault("http_user_agent", self.user_agent)
            if self.subscribed_topics:
                payload.setdefault("topics", list(self.subscribed_topics))
            if self.user and "uid" not in payload:
                payload["uid"] = self.user.id

            # Local import to avoid top-level dependency changes
            from mojo.apps import incident

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: incident.report_event(
                    details,
                    title=details[:80],
                    category=event_type,
                    level=level,
                    request=None,   # no HTTP request in websocket context
                    scope=scope,
                    **payload
                )
            )
        except Exception as e:
            self._log_exception("failed to report incident")

    async def close_connection(self):
        """Close WebSocket connection"""
        self.running = False
        try:
            await self.websocket.close()
        except:
            pass

    async def cleanup_connection(self):
        """Clean up connection state in Redis"""
        self._log("disconnected")
        def cleanup():
            try:
                # Remove connection record
                self.redis_client.delete(f"realtime:connections:{self.connection_id}")

                # Remove from all subscribed topics
                for topic in self.subscribed_topics:
                    self.redis_client.srem(f"realtime:topic:{topic}", self.connection_id)

                # Update user online status
                if self.user and self.user_type:
                    key = self.user_online_key()
                    # Remove this connection from the online set
                    self.redis_client.srem(key, self.connection_id)
                    # If set is empty, delete; otherwise refresh TTL
                    if self.redis_client.scard(key) == 0:
                        self.redis_client.delete(key)
                    else:
                        self.redis_client.expire(key, ONLINE_TTL_SECONDS)
            except Exception as e:
                self._log_exception("redis cleanup failed")

        await asyncio.get_event_loop().run_in_executor(None, cleanup)

        # Call user's disconnected hook if available
        if self.authenticated and hasattr(self.user, 'on_realtime_disconnected'):
            def call_hook():
                self.user.on_realtime_disconnected()
            try:
                await asyncio.get_event_loop().run_in_executor(None, call_hook)
            except Exception as e:
                self._log_exception("user disconnect hook failed")

        # Close pubsub
        if self.pubsub:
            try:
                await asyncio.get_event_loop().run_in_executor(None, self.pubsub.close)
            except Exception as e:
                self._log(f"Failed to close pubsub: {e}")
