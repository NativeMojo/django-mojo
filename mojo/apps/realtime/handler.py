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


class WebSocketHandler:
    def __init__(self, websocket, path):
        self.websocket = websocket
        self.path = path
        self.connection_id = str(uuid.uuid4())
        self.authenticated = False
        self.user = None
        self.user_type = None
        self.subscribed_topics = set()

        # Redis clients - separate for pub/sub
        self.redis_client = get_connection()
        self.pubsub = None

        # Control flags
        self.running = True
        self.last_activity = time.time()

    async def handle_connection(self):
        """Main connection handler - manages entire connection lifecycle"""
        logger.info(f"New WebSocket connection: {self.connection_id}")

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
            logger.exception(f"Error in connection {self.connection_id}: {e}")
        finally:
            await self.cleanup_connection()

    async def register_connection(self):
        """Register connection in Redis with TTL"""
        connection_data = {
            "connection_id": self.connection_id,
            "authenticated": False,
            "connected_at": time.time(),
            "last_ping": time.time(),
            "topics": []
        }

        key = f"realtime:connections:{self.connection_id}"
        try:
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self.redis_client.setex(key, 3600, json.dumps(connection_data))
            )
        except Exception as e:
            logger.warning(f"Failed to register connection {self.connection_id} in Redis: {e}")

    async def update_connection_auth(self):
        """Update connection with authentication info"""
        connection_data = {
            "connection_id": self.connection_id,
            "user_id": self.user.id if self.user else None,
            "user_type": self.user_type,
            "authenticated": True,
            "connected_at": time.time(),
            "last_ping": time.time(),
            "topics": list(self.subscribed_topics)
        }

        key = f"realtime:connections:{self.connection_id}"
        try:
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self.redis_client.setex(key, 3600, json.dumps(connection_data))
            )
        except Exception as e:
            logger.warning(f"Failed to update connection auth {self.connection_id} in Redis: {e}")

    async def register_user_online(self):
        """Register user as online in Redis"""
        if not self.user or not self.user_type:
            return

        key = f"realtime:online:{self.user_type}:{self.user.id}"

        # Get existing data or create new
        def get_and_update():
            try:
                existing = self.redis_client.get(key)
                if existing:
                    user_data = json.loads(existing)
                    connection_ids = set(user_data.get("connection_ids", []))
                else:
                    connection_ids = set()

                connection_ids.add(self.connection_id)

                user_data = {
                    "connection_ids": list(connection_ids),
                    "last_seen": time.time()
                }

                self.redis_client.setex(key, 3600, json.dumps(user_data))
            except Exception as e:
                logger.warning(f"Failed to register user online for {self.connection_id}: {e}")

        await asyncio.get_event_loop().run_in_executor(None, get_and_update)

    async def activity_timeout(self):
        """Handle both auth and activity timeouts"""
        while self.running:
            await asyncio.sleep(5)  # Check every 5 seconds

            time_since_activity = time.time() - self.last_activity

            if time_since_activity >= 30:
                if not self.authenticated:
                    await self.send_error("Authentication timeout")
                else:
                    logger.info(f"Connection {self.connection_id} timed out due to inactivity")
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
                    logger.exception(f"Error processing client message: {e}")
                    await self.send_error("Message processing error")

        except Exception as e:
            if "closed" in str(e).lower():
                logger.info(f"Client connection closed: {self.connection_id}")
            else:
                logger.exception(f"Error in client message handler: {e}")
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
                        logger.exception(f"Error processing Redis message: {e}")

        except Exception as e:
            logger.exception(f"Error in Redis message handler: {e}")
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
            await self.send_error("Missing token")
            return

        # Use existing auth logic
        user, error, key_name = await async_validate_bearer_token(prefix, token)

        if error or not user:
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
        if hasattr(self.user, 'on_realtime_connected'):
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
                    await self.send_error(f"Access denied to topic: {topic}")
                    return
            except Exception as e:
                logger.exception(f"Error checking topic permission for {topic}: {e}")
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

        await self.send_message({
            "type": "pong",
            "user_type": self.user_type,
            "user_id": self.user.id if self.user else None
        })

    async def handle_custom_message(self, data):
        """Handle custom message - delegate to user's hook if available"""
        logger.debug(f"Processing custom message for {self.connection_id}: {data}")

        if hasattr(self.user, 'on_realtime_message'):
            def call_hook():
                return self.user.on_realtime_message(data)

            try:
                response = await asyncio.get_event_loop().run_in_executor(
                    None, call_hook
                )
                logger.debug(f"User hook returned for {self.connection_id}: {response}")
                if response:
                    await self._process_hook_response(response)
                else:
                    logger.debug(f"No response from user hook for {self.connection_id}")
            except Exception as e:
                logger.exception(f"Error in user message hook: {e}")
                await self.send_error("Message processing error")
        else:
            logger.debug(f"No on_realtime_message hook for user {self.user}")
            await self.send_error("Unsupported message type")

    async def _process_hook_response(self, response):
        """Process unified response from user hooks"""
        logger.debug(f"Processing hook response for {self.connection_id}: {response}")

        if isinstance(response, dict):
            # Send response message to client
            if "response" in response:
                logger.debug(f"Sending response to client {self.connection_id}: {response['response']}")
                await self.send_message(response["response"])

            # Process subscription requests
            if "subscriptions" in response:
                logger.debug(f"Processing subscriptions for {self.connection_id}: {response['subscriptions']}")
                for topic in response["subscriptions"]:
                    if topic and isinstance(topic, str):
                        try:
                            await self.subscribe_to_topic(topic)
                        except Exception as e:
                            logger.warning(f"Failed to subscribe to topic {topic}: {e}")
        else:
            # Backward compatibility - treat non-dict as direct response
            logger.debug(f"Sending direct response to client {self.connection_id}: {response}")
            await self.send_message(response)

    async def subscribe_to_topic(self, topic):
        """Subscribe connection to a topic"""
        if topic in self.subscribed_topics:
            return

        def subscribe():
            try:
                # Add to topic subscribers
                self.redis_client.sadd(f"realtime:topic:{topic}", self.connection_id)
                self.redis_client.expire(f"realtime:topic:{topic}", 3600)

                # Subscribe to Redis channel
                self.pubsub.subscribe(f"realtime:topic:{topic}")
            except Exception as e:
                logger.warning(f"Failed to subscribe {self.connection_id} to topic {topic}: {e}")
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
                logger.warning(f"Failed to unsubscribe {self.connection_id} from topic {topic}: {e}")

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
        logger.debug(f"Sending WebSocket message to {self.connection_id}: {message}")
        try:
            await self.websocket.send(json.dumps(message))
        except Exception as e:
            if "closed" in str(e).lower():
                self.running = False
            else:
                logger.exception(f"Error sending message: {e}")
                self.running = False

    async def send_error(self, error_message):
        """Send error message to client"""
        await self.send_message({
            "type": "error",
            "message": error_message
        })

    async def close_connection(self):
        """Close WebSocket connection"""
        self.running = False
        try:
            await self.websocket.close()
        except:
            pass

    async def cleanup_connection(self):
        """Clean up connection state in Redis"""
        logger.info(f"Cleaning up connection: {self.connection_id}")

        def cleanup():
            try:
                # Remove connection record
                self.redis_client.delete(f"realtime:connections:{self.connection_id}")

                # Remove from all subscribed topics
                for topic in self.subscribed_topics:
                    self.redis_client.srem(f"realtime:topic:{topic}", self.connection_id)

                # Update user online status
                if self.user and self.user_type:
                    key = f"realtime:online:{self.user_type}:{self.user.id}"
                    existing = self.redis_client.get(key)
                    if existing:
                        user_data = json.loads(existing)
                        connection_ids = set(user_data.get("connection_ids", []))
                        connection_ids.discard(self.connection_id)

                        if connection_ids:
                            # Still has other connections
                            user_data["connection_ids"] = list(connection_ids)
                            user_data["last_seen"] = time.time()
                            self.redis_client.setex(key, 3600, json.dumps(user_data))
                        else:
                            # No more connections, remove online status
                            self.redis_client.delete(key)
            except Exception as e:
                logger.warning(f"Failed to cleanup connection {self.connection_id} in Redis: {e}")

        await asyncio.get_event_loop().run_in_executor(None, cleanup)

        # Call user's disconnected hook if available
        if self.authenticated and hasattr(self.user, 'on_realtime_disconnected'):
            def call_hook():
                self.user.on_realtime_disconnected()
            try:
                await asyncio.get_event_loop().run_in_executor(None, call_hook)
            except Exception as e:
                logger.exception(f"Error in user disconnect hook: {e}")

        # Close pubsub
        if self.pubsub:
            try:
                await asyncio.get_event_loop().run_in_executor(None, self.pubsub.close)
            except Exception as e:
                logger.warning(f"Failed to close pubsub for {self.connection_id}: {e}")
