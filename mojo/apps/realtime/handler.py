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
from mojo.helpers.request import normalize_ip
from mojo.helpers.settings import settings
from .auth import async_validate_bearer_token

logger = logit.get_logger("realtime", "realtime.log")

# Presence/connection/topic TTLs (seconds)
CONNECTION_TTL_SECONDS = 300         # connection record TTL
ONLINE_TTL_SECONDS = 300             # user online presence TTL
TOPIC_TTL_SECONDS = 300              # topic membership TTL
PRESENCE_REFRESH_MIN_INTERVAL = 30   # throttle presence refreshes
AUTH_IDLE_TIMEOUT_SECONDS = 30       # authenticated idle timeout
WS_CONNECT_WINDOW_SECONDS = 60       # fixed window for the pre-accept rate check


def resolve_scope_ip(scope):
    """Resolve the client IP from an ASGI scope. Prefer the proxy-authoritative
    X-Real-IP; never trust the client-controllable X-Forwarded-For / Forwarded.
    Falls back to the ASGI transport peer (empty over a unix socket)."""
    headers = {}
    for k, v in scope.get("headers", []):
        try:
            headers[k.decode().lower()] = v.decode()
        except Exception:
            pass

    ip = normalize_ip(headers.get("x-real-ip"))
    if ip:
        return ip

    client = scope.get("client")
    if client and client[0]:
        return normalize_ip(client[0])

    return None


def _connect_rate_check_sync(ip):
    """Fixed-window per-IP connection-rate check (DM-042). Returns True when
    the connection may proceed. Disabled with WS_CONNECT_RATE_LIMIT <= 0.
    Fail-open on Redis errors — an outage must never refuse all sockets."""
    try:
        limit = settings.get("WS_CONNECT_RATE_LIMIT", 30, kind="int")
        if limit <= 0 or not ip:
            return True
        r = get_connection()
        now = int(time.time())
        window_start = now // WS_CONNECT_WINDOW_SECONDS * WS_CONNECT_WINDOW_SECONDS
        key = f"rl:ws_connect:{ip}:{window_start}"
        count = r.incr(key)
        if count == 1:
            r.expire(key, WS_CONNECT_WINDOW_SECONDS * 2)
        if count <= limit:
            return True
        # First engagement per IP per window reports one incident event —
        # never one per refused handshake (no self-amplification).
        if r.set(f"rl:ws_connect:blocked:{ip}:{window_start}", 1, nx=True,
                 ex=WS_CONNECT_WINDOW_SECONDS * 2):
            from mojo.apps import incident
            incident.report_event(
                f"WebSocket connect storm: {ip} exceeded {limit} connects/{WS_CONNECT_WINDOW_SECONDS}s",
                category="traffic:ws_connect",
                scope="realtime",
                level=6,
                source_ip=ip,
            )
        return False
    except Exception:
        logger.exception("ws connect rate check failed — failing open")
        return True


async def check_connect_rate(scope):
    """Async pre-accept gate: one Redis INCR per connection attempt, run off
    the event loop. A refused storm costs a rejected handshake, not pub/sub +
    tasks + 30s of connection state."""
    ip = resolve_scope_ip(scope)
    return await asyncio.get_event_loop().run_in_executor(
        None, _connect_rate_check_sync, ip
    )


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

        # Redis clients - separate for pub/sub.
        # pubsub stays None until authentication succeeds (DM-042): an
        # unauthenticated socket must not hold a dedicated Redis pub/sub
        # connection — that's exactly the cost a reconnect storm multiplies.
        self.redis_client = get_connection()
        self.pubsub = None
        self._redis_task = None

        # Unauthenticated sockets get a short window to send their token.
        try:
            self.unauth_timeout = settings.get("WS_UNAUTH_TIMEOUT", 10, kind="int")
        except Exception:
            self.unauth_timeout = 10

        # Control flags
        self.running = True
        self.connected_at = time.time()
        self.last_activity = time.time()
        self.last_presence_refresh = 0

    def _log(self, message):
        try:
            rip = self.remote_ip
            if self.user and self.user_type:
                rip = f"{self.user_type}:{self.user.id} -> {rip}"
        except Exception:
            rip = None
        logger.info(f"[{self.connection_id} -> {rip}]: {message}")

    def user_online_key(self):
        return f"realtime:online:{self.user_type}:{self.user.id}"

    def resolve_remote_ip(self):
        """
        Resolve the remote IP. Prefer the proxy-authoritative X-Real-IP; never trust the
        client-controllable X-Forwarded-For / Forwarded. Fall back to the transport peer
        only when X-Real-IP is absent.
        """
        try:
            scope = getattr(self.websocket, "scope", None)
            if scope:
                ip = self.get_remote_ip(scope)
                if ip:
                    return ip
            # Fallback to wrapper-provided request headers: X-Real-IP only.
            headers = getattr(self.websocket, "request_headers", None)
            if headers:
                ip = normalize_ip(headers.get("x-real-ip") or headers.get("X-Real-IP"))
                if ip:
                    return ip
            # Final fallback to transport peername (empty over a unix socket).
            transport = getattr(self.websocket, "transport", None)
            if transport and hasattr(transport, "get_extra_info"):
                peer = transport.get_extra_info("peername")
                if peer:
                    raw = peer[0] if isinstance(peer, (tuple, list)) else str(peer)
                    return normalize_ip(raw)
        except Exception:
            self._log_exception("resolve_remote_ip")
        return None

    def get_remote_ip(self, scope):
        # Prefer the proxy-authoritative X-Real-IP (set by asgi.inc, overwriting any
        # client value). Never trust X-Forwarded-For / RFC 7239 Forwarded — both are
        # client-controllable and spoofable.
        return resolve_scope_ip(scope)

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
                "timeout": self.unauth_timeout
            })

            # Start background tasks. handle_redis_messages (the dedicated
            # pub/sub connection) starts only after successful auth — see
            # start_redis_messages() called from handle_authenticate.
            tasks = [
                asyncio.create_task(self.activity_timeout()),
                asyncio.create_task(self.handle_client_messages())
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
        self._log("authenticated")
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
        """Handle both auth and activity timeouts. Unauthenticated sockets get
        the short WS_UNAUTH_TIMEOUT window; authenticated ones the normal idle
        timeout."""
        while self.running:
            await asyncio.sleep(5)  # Check every 5 seconds

            time_since_activity = time.time() - self.last_activity
            connected_duration = time.time() - self.connected_at
            threshold = AUTH_IDLE_TIMEOUT_SECONDS if self.authenticated else self.unauth_timeout

            if time_since_activity >= threshold:
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

    async def start_redis_messages(self):
        """Create the pub/sub connection and start the delivery task.

        Called from handle_authenticate AFTER a successful auth (and before
        any topic subscription — subscribe_to_topic needs self.pubsub). The
        pub/sub connection is created synchronously here so there is no race
        between auth completing and the first topic subscribe."""
        if self.pubsub is not None:
            return

        def create_pubsub():
            pubsub = self.redis_client.pubsub()
            # Subscribe to connection-specific channel
            pubsub.subscribe(f"realtime:messages:{self.connection_id}")
            pubsub.subscribe("realtime:broadcast")
            return pubsub

        self.pubsub = await asyncio.get_event_loop().run_in_executor(
            None, create_pubsub
        )
        self._redis_task = asyncio.create_task(self.handle_redis_messages())

    async def handle_redis_messages(self):
        """Handle messages from Redis pub/sub (started post-auth)"""
        try:
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
        elif message_type == "response":
            await self.handle_response(data)
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

        # Per-identity concurrency cap (DM-042): a reconnect loop that leaks
        # sockets (or an agent opening one per scrape) is bounded here. The
        # presence set is TTL'd (300s) so a stale overcount self-heals.
        max_connections = settings.get("WS_MAX_CONNECTIONS", 10, kind="int")
        if max_connections > 0:
            def count_connections():
                try:
                    return self.redis_client.scard(f"realtime:online:{key_name}:{user.id}")
                except Exception:
                    return 0  # fail open
            current = await asyncio.get_event_loop().run_in_executor(None, count_connections)
            if current >= max_connections:
                # One incident event per identity per minute — never one per
                # rejected attempt.
                def report_once():
                    try:
                        return self.redis_client.set(
                            f"rl:ws_maxconn:{key_name}:{user.id}", 1, nx=True, ex=60)
                    except Exception:
                        return False
                if await asyncio.get_event_loop().run_in_executor(None, report_once):
                    await self.report_incident(
                        f"too many connections for {key_name}:{user.id} "
                        f"({current} >= {max_connections})",
                        "traffic:ws_maxconn", 6)
                await self.send_error("Too many connections")
                await self.close_connection()
                return

        self.user = user
        self.user_type = key_name
        self.authenticated = True

        # Update Redis state
        await self.update_connection_auth()
        await self.register_user_online()

        # Start pub/sub delivery now that the socket is authenticated —
        # must happen before any topic subscription.
        await self.start_redis_messages()

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

    async def handle_response(self, data):
        """Handle client response to a request() call from Django."""
        if not self.authenticated:
            await self.send_error("Authentication required")
            return

        request_id = data.get("request_id")
        if not request_id:
            await self.send_error("Missing request_id")
            return

        response_data = data.get("data", {})
        response_key = f"realtime:response:{request_id}"

        def push_response():
            self.redis_client.lpush(response_key, json.dumps(response_data))
            self.redis_client.expire(response_key, 120)

        await asyncio.get_event_loop().run_in_executor(None, push_response)

    async def check_waiters(self, data):
        """Check if any active wait_for_event() waiters match this message."""
        if not self.user or not self.user_type:
            return

        waiters_key = f"realtime:waiters:{self.user_type}:{self.user.id}"

        def do_check():
            if not self.redis_client.exists(waiters_key):
                return

            waiter_ids = self.redis_client.smembers(waiters_key) or set()
            for waiter_id in waiter_ids:
                if isinstance(waiter_id, (bytes, bytearray)):
                    waiter_id = waiter_id.decode()

                match_raw = self.redis_client.get(f"realtime:waiter:{waiter_id}")
                if not match_raw:
                    continue
                if isinstance(match_raw, (bytes, bytearray)):
                    match_raw = match_raw.decode()

                try:
                    match = json.loads(match_raw)
                except (json.JSONDecodeError, ValueError):
                    continue

                # All match fields must be present with equal values
                if all(data.get(k) == v for k, v in match.items()):
                    result_key = f"realtime:waiter:{waiter_id}:result"
                    self.redis_client.lpush(result_key, json.dumps(data))
                    # Remove satisfied waiter
                    self.redis_client.srem(waiters_key, waiter_id)
                    self.redis_client.delete(f"realtime:waiter:{waiter_id}")

        await asyncio.get_event_loop().run_in_executor(None, do_check)

    async def handle_custom_message(self, data):
        """Handle custom message - delegate to user's hook if available"""

        # Check if any wait_for_event() calls match this message
        await self.check_waiters(data)

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
            # Forward to client wrapped in {"type": "message", "data": ...}
            client_message = {
                "type": "message",
                "data": data.get("data", {}),
                "timestamp": data.get("timestamp")
            }

            if message_type == "topic_message":
                client_message["topic"] = data.get("topic")

            await self.send_message(client_message)
        elif message_type == "direct_event":
            # Forward payload directly — no wrapping. The payload's own
            # "type" field (e.g., "assistant_response") becomes the
            # top-level type the client sees.
            await self.send_message(data.get("data", {}))
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

        # Stop the post-auth pub/sub task if it was started (it is not in
        # handle_connection's task set, so it must be cancelled here).
        if self._redis_task is not None and not self._redis_task.done():
            self._redis_task.cancel()
            try:
                await self._redis_task
            except (asyncio.CancelledError, Exception):
                pass
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
