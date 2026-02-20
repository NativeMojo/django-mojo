# Realtime WebSocket Scaling Architecture

This document outlines the scaling optimizations needed to handle 10,000+ concurrent WebSocket connections per server in the mojo realtime system.

## Current System Limitations

### Redis Connection Bottleneck
The current implementation creates individual Redis connections per WebSocket:
- Each WebSocket handler: 1 Redis client connection
- Each WebSocket handler: 1 Redis pub/sub connection  
- **Total: 20,000 Redis connections for 10k WebSockets**

**Problem**: Most Redis servers max out at 10,000 connections. The system will fail around 5,000 concurrent WebSockets.

### Memory and File Descriptor Limits
- Each WebSocket connection: ~8-16KB memory
- Each Redis connection: ~8KB memory
- File descriptors: 2 per WebSocket + 2 per Redis connection = 40,000 FDs needed
- Default system limit: 1,024 FDs

### Pub/Sub Message Storm
- 10,000 individual pub/sub subscriptions
- Each published message multiplied across all subscribers
- Redis pub/sub becomes inefficient at scale

## Proposed Scaling Architecture

### 1. Shared Redis Connection Pool

```python
# mojo/apps/realtime/redis_pool.py
import asyncio
import threading
from typing import Dict, Set, Optional, List
from mojo.helpers.redis.client import get_connection
from mojo.helpers import logit

logger = logit.get_logger("realtime", "realtime.log")

class SharedRedisPool:
    """
    Shared Redis connection pool for all WebSocket connections.
    
    Reduces Redis connections from 20k to ~50 for 10k WebSocket connections.
    Thread-safe for mixed async/sync usage.
    """
    
    def __init__(self, max_connections=50, max_pubsub_connections=10):
        self.max_connections = max_connections
        self.max_pubsub_connections = max_pubsub_connections
        
        # Separate pools for different usage patterns
        self.general_pool: List = []  # For manager operations
        self.general_in_use: Set = set()
        
        self.pubsub_pool: List = []   # Dedicated pub/sub connections
        self.pubsub_in_use: Set = set()
        
        self.lock = asyncio.Lock()
        self._stats = {
            'total_requests': 0,
            'pool_exhausted_count': 0,
            'connections_created': 0,
            'connections_returned': 0
        }
        
    async def get_connection(self, connection_type='general'):
        """
        Get Redis connection from appropriate pool.
        
        Args:
            connection_type: 'general' or 'pubsub'
        """
        async with self.lock:
            self._stats['total_requests'] += 1
            
            if connection_type == 'pubsub':
                return await self._get_from_pool(
                    self.pubsub_pool, 
                    self.pubsub_in_use, 
                    self.max_pubsub_connections,
                    'pub/sub'
                )
            else:
                return await self._get_from_pool(
                    self.general_pool,
                    self.general_in_use,
                    self.max_connections,
                    'general'
                )
    
    async def _get_from_pool(self, pool, in_use, max_size, pool_name):
        """Get connection from specific pool"""
        # Try to reuse available connection
        for conn in pool:
            if conn not in in_use:
                in_use.add(conn)
                return conn
        
        # Create new connection if under limit
        if len(pool) < max_size:
            conn = await asyncio.get_event_loop().run_in_executor(
                None, get_connection
            )
            pool.append(conn)
            in_use.add(conn)
            self._stats['connections_created'] += 1
            logger.info(f"Created Redis {pool_name} connection {len(pool)}/{max_size}")
            return conn
        
        # Pool exhausted
        self._stats['pool_exhausted_count'] += 1
        logger.error(f"Redis {pool_name} pool exhausted! ({max_size} connections)")
        raise Exception(f"Redis {pool_name} connection pool exhausted")
    
    async def return_connection(self, conn, connection_type='general'):
        """Return connection to appropriate pool"""
        async with self.lock:
            self._stats['connections_returned'] += 1
            
            if connection_type == 'pubsub':
                self.pubsub_in_use.discard(conn)
            else:
                self.general_in_use.discard(conn)
    
    def get_stats(self):
        """Get connection pool statistics"""
        return {
            **self._stats,
            'general_pool_size': len(self.general_pool),
            'general_in_use': len(self.general_in_use),
            'pubsub_pool_size': len(self.pubsub_pool),
            'pubsub_in_use': len(self.pubsub_in_use),
        }

# Global shared pools
redis_pool = SharedRedisPool(max_connections=50, max_pubsub_connections=10)
```

### 2. Shared Pub/Sub Manager

Replace individual pub/sub connections with a single shared manager:

```python
# mojo/apps/realtime/pubsub_manager.py
import asyncio
import json
import time
from typing import Dict, Set, Optional
from .redis_pool import redis_pool
from mojo.helpers import logit

logger = logit.get_logger("realtime", "realtime.log")

class SharedPubSubManager:
    """
    Single pub/sub connection shared across all WebSocket connections.
    
    Eliminates the 1:1 pub/sub connection per WebSocket pattern.
    Handles message routing to appropriate connections.
    """
    
    def __init__(self):
        # Connection registry
        self.connections: Dict[str, 'WebSocketHandler'] = {}
        
        # Topic subscriptions: topic -> set of connection_ids
        self.subscriptions: Dict[str, Set[str]] = {}
        
        # Redis pub/sub connection (single shared instance)
        self.pubsub = None
        self.redis_conn = None
        
        # Control flags
        self.running = False
        self.message_loop_task = None
        
        # Performance metrics
        self.stats = {
            'messages_processed': 0,
            'messages_routed': 0,
            'routing_errors': 0,
            'dead_connections_cleaned': 0,
            'redis_reconnects': 0
        }
        
    async def start(self):
        """Start the shared pub/sub listener (called once globally)"""
        if self.running:
            return
            
        try:
            # Get dedicated pub/sub Redis connection
            self.redis_conn = await redis_pool.get_connection('pubsub')
            
            def create_pubsub():
                pubsub = self.redis_conn.pubsub()
                # Subscribe to global broadcast channel
                pubsub.subscribe("realtime:broadcast")
                return pubsub
                
            self.pubsub = await asyncio.get_event_loop().run_in_executor(
                None, create_pubsub
            )
            
            self.running = True
            
            # Start message processing loop
            self.message_loop_task = asyncio.create_task(self._message_loop())
            
            logger.info("SharedPubSubManager started successfully")
            
        except Exception as e:
            logger.exception(f"Failed to start SharedPubSubManager: {e}")
            raise
    
    async def stop(self):
        """Stop the pub/sub manager"""
        self.running = False
        
        if self.message_loop_task:
            self.message_loop_task.cancel()
            
        if self.pubsub:
            await asyncio.get_event_loop().run_in_executor(
                None, self.pubsub.close
            )
            
        if self.redis_conn:
            await redis_pool.return_connection(self.redis_conn, 'pubsub')
            
        logger.info("SharedPubSubManager stopped")
    
    async def register_connection(self, connection_id: str, handler: 'WebSocketHandler'):
        """Register a WebSocket connection for message routing"""
        self.connections[connection_id] = handler
        logger.debug(f"Registered connection {connection_id} (total: {len(self.connections)})")
        
    async def unregister_connection(self, connection_id: str):
        """
        Unregister and cleanup WebSocket connection.
        Critical for preventing memory leaks and Redis subscription buildup.
        """
        if connection_id in self.connections:
            del self.connections[connection_id]
            
            # Remove from all topic subscriptions
            topics_to_cleanup = []
            for topic, conn_ids in list(self.subscriptions.items()):
                conn_ids.discard(connection_id)
                if not conn_ids:
                    # No connections left for this topic
                    topics_to_cleanup.append(topic)
            
            # Unsubscribe from Redis for unused topics
            for topic in topics_to_cleanup:
                await self._redis_unsubscribe(topic)
                del self.subscriptions[topic]
                logger.debug(f"Unsubscribed from unused topic: {topic}")
                    
            logger.info(f"Cleaned up connection {connection_id} (remaining: {len(self.connections)})")
    
    async def subscribe_connection(self, connection_id: str, topic: str):
        """Subscribe connection to topic"""
        if connection_id not in self.connections:
            logger.warning(f"Attempting to subscribe unknown connection: {connection_id}")
            return False
            
        # Create topic subscription if first subscriber
        if topic not in self.subscriptions:
            self.subscriptions[topic] = set()
            await self._redis_subscribe(topic)
            logger.debug(f"Created new topic subscription: {topic}")
            
        self.subscriptions[topic].add(connection_id)
        logger.debug(f"Subscribed {connection_id} to {topic}")
        return True
        
    async def unsubscribe_connection(self, connection_id: str, topic: str):
        """Unsubscribe connection from topic"""
        if topic in self.subscriptions:
            self.subscriptions[topic].discard(connection_id)
            
            # Clean up empty topic subscriptions
            if not self.subscriptions[topic]:
                await self._redis_unsubscribe(topic)
                del self.subscriptions[topic]
                logger.debug(f"Removed empty topic subscription: {topic}")
        
    async def _redis_subscribe(self, topic: str):
        """Subscribe to Redis channel for topic"""
        if self.pubsub:
            try:
                def subscribe():
                    self.pubsub.subscribe(f"realtime:topic:{topic}")
                    
                await asyncio.get_event_loop().run_in_executor(None, subscribe)
                logger.debug(f"Subscribed to Redis channel: realtime:topic:{topic}")
                
            except Exception as e:
                logger.error(f"Failed to subscribe to Redis topic {topic}: {e}")
                
    async def _redis_unsubscribe(self, topic: str):
        """Unsubscribe from Redis channel for topic"""
        if self.pubsub:
            try:
                def unsubscribe():
                    self.pubsub.unsubscribe(f"realtime:topic:{topic}")
                    
                await asyncio.get_event_loop().run_in_executor(None, unsubscribe)
                logger.debug(f"Unsubscribed from Redis channel: realtime:topic:{topic}")
                
            except Exception as e:
                logger.error(f"Failed to unsubscribe from Redis topic {topic}: {e}")
    
    async def _message_loop(self):
        """
        Process incoming Redis messages and route to WebSocket connections.
        This is the core message routing logic.
        """
        logger.info("SharedPubSubManager message loop started")
        
        while self.running:
            try:
                def get_message():
                    return self.pubsub.get_message(timeout=1.0)
                
                message = await asyncio.get_event_loop().run_in_executor(
                    None, get_message
                )
                
                if message and message['type'] == 'message':
                    self.stats['messages_processed'] += 1
                    await self._route_message(message)
                    
            except Exception as e:
                logger.exception(f"Error in pub/sub message loop: {e}")
                self.stats['redis_reconnects'] += 1
                
                # Attempt to reconnect
                await asyncio.sleep(1)
                try:
                    await self._reconnect_pubsub()
                except Exception as reconnect_error:
                    logger.error(f"Failed to reconnect pub/sub: {reconnect_error}")
                    await asyncio.sleep(5)
                    
        logger.info("SharedPubSubManager message loop stopped")
    
    async def _route_message(self, redis_message):
        """
        Route Redis message to appropriate WebSocket connections.
        Handles broadcast, topic, and direct messages.
        """
        try:
            channel = redis_message['channel']
            data = json.loads(redis_message['data'])
            
            target_connections = set()
            message_type = None
            
            if channel == "realtime:broadcast":
                # Broadcast to all active connections
                target_connections = set(self.connections.keys())
                message_type = "broadcast"
                
            elif channel.startswith("realtime:topic:"):
                # Topic message - route to subscribers
                topic = channel[15:]  # Remove "realtime:topic:" prefix
                target_connections = self.subscriptions.get(topic, set()).copy()
                message_type = "topic"
                
            elif channel.startswith("realtime:messages:"):
                # Direct message to specific connection
                connection_id = channel[18:]  # Remove "realtime:messages:" prefix
                if connection_id in self.connections:
                    target_connections = {connection_id}
                    message_type = "direct"
            
            # Route message to target connections
            successful_sends = 0
            for conn_id in target_connections.copy():
                handler = self.connections.get(conn_id)
                
                if handler and handler.running:
                    try:
                        client_message = {
                            "type": "message",
                            "data": data.get("data", {}),
                            "timestamp": data.get("timestamp"),
                        }
                        
                        # Add topic for topic messages
                        if message_type == "topic" and "topic" in data:
                            client_message["topic"] = data["topic"]
                        
                        await handler.send_message(client_message)
                        successful_sends += 1
                        
                    except Exception as send_error:
                        logger.warning(f"Failed to send to {conn_id}: {send_error}")
                        # Connection appears dead, mark for cleanup
                        asyncio.create_task(self._cleanup_dead_connection(conn_id))
                else:
                    # Handler is missing or not running, clean it up
                    asyncio.create_task(self._cleanup_dead_connection(conn_id))
            
            self.stats['messages_routed'] += successful_sends
            
            if successful_sends == 0 and target_connections:
                logger.warning(f"Message routed to 0/{len(target_connections)} connections")
                
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in Redis message: {e}")
            self.stats['routing_errors'] += 1
        except Exception as e:
            logger.exception(f"Error routing message: {e}")
            self.stats['routing_errors'] += 1
    
    async def _cleanup_dead_connection(self, connection_id: str):
        """Clean up a dead connection"""
        try:
            await self.unregister_connection(connection_id)
            self.stats['dead_connections_cleaned'] += 1
        except Exception as e:
            logger.error(f"Error cleaning up dead connection {connection_id}: {e}")
    
    async def _reconnect_pubsub(self):
        """Reconnect pub/sub after connection failure"""
        logger.info("Attempting to reconnect pub/sub...")
        
        # Close existing connection
        if self.pubsub:
            try:
                await asyncio.get_event_loop().run_in_executor(None, self.pubsub.close)
            except:
                pass
                
        # Return connection to pool
        if self.redis_conn:
            await redis_pool.return_connection(self.redis_conn, 'pubsub')
        
        # Get new connection and recreate pub/sub
        self.redis_conn = await redis_pool.get_connection('pubsub')
        
        def recreate_pubsub():
            pubsub = self.redis_conn.pubsub()
            # Re-subscribe to all active channels
            pubsub.subscribe("realtime:broadcast")
            for topic in self.subscriptions.keys():
                pubsub.subscribe(f"realtime:topic:{topic}")
            return pubsub
        
        self.pubsub = await asyncio.get_event_loop().run_in_executor(None, recreate_pubsub)
        logger.info("Pub/sub reconnected successfully")
    
    def get_stats(self):
        """Get pub/sub manager statistics"""
        return {
            **self.stats,
            'active_connections': len(self.connections),
            'active_subscriptions': len(self.subscriptions),
            'total_subscribers': sum(len(subs) for subs in self.subscriptions.values())
        }

# Global shared pub/sub manager
pubsub_manager = SharedPubSubManager()
```

### 3. Heartbeat and Dead Connection Cleanup

Critical for preventing resource leaks at scale:

```python
# mojo/apps/realtime/heartbeat.py
import asyncio
import time
from typing import Dict, Optional
from .pubsub_manager import pubsub_manager
from .redis_pool import redis_pool
from mojo.helpers import logit

logger = logit.get_logger("realtime", "realtime.log")

class HeartbeatManager:
    """
    Manages heartbeat/ping for all connections and aggressive cleanup of dead ones.
    
    Essential for 10k+ connections to prevent:
    - Redis connection leaks
    - Memory leaks from dead WebSocket handlers
    - Stale pub/sub subscriptions
    """
    
    def __init__(self):
        # Heartbeat tracking: connection_id -> last_ping_time
        self.connections: Dict[str, float] = {}
        
        # Configuration
        self.ping_interval = 30          # Send ping every 30s
        self.timeout_threshold = 90      # Consider dead after 90s no response
        self.cleanup_interval = 30       # Check for dead connections every 30s
        self.aggressive_cleanup = True   # Force cleanup dead connections
        
        # Control flags
        self.running = False
        self.heartbeat_task = None
        self.cleanup_task = None
        
        # Statistics
        self.stats = {
            'pings_sent': 0,
            'pings_responded': 0,
            'connections_timed_out': 0,
            'cleanup_cycles': 0,
            'redis_cleanup_operations': 0
        }
        
    async def start(self):
        """Start heartbeat monitoring (called once globally)"""
        if self.running:
            return
            
        self.running = True
        self.heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self.cleanup_task = asyncio.create_task(self._cleanup_loop())
        
        logger.info("HeartbeatManager started")
        
    async def stop(self):
        """Stop heartbeat monitoring"""
        self.running = False
        
        if self.heartbeat_task:
            self.heartbeat_task.cancel()
        if self.cleanup_task:
            self.cleanup_task.cancel()
            
        logger.info("HeartbeatManager stopped")
        
    async def register_connection(self, connection_id: str):
        """Register connection for heartbeat monitoring"""
        self.connections[connection_id] = time.time()
        logger.debug(f"Registered heartbeat for {connection_id}")
        
    async def update_heartbeat(self, connection_id: str):
        """Update last seen time for connection (called on pong response)"""
        if connection_id in self.connections:
            self.connections[connection_id] = time.time()
            self.stats['pings_responded'] += 1
            logger.debug(f"Updated heartbeat for {connection_id}")
            
    async def unregister_connection(self, connection_id: str):
        """Remove connection from heartbeat monitoring"""
        self.connections.pop(connection_id, None)
        logger.debug(f"Unregistered heartbeat for {connection_id}")
        
    async def _heartbeat_loop(self):
        """Send periodic pings to all active connections"""
        logger.info("Heartbeat loop started")
        
        while self.running:
            try:
                current_time = time.time()
                ping_message = {
                    "type": "heartbeat",
                    "timestamp": current_time
                }
                
                # Send pings to all registered connections
                active_connections = list(self.connections.keys())
                successful_pings = 0
                
                for conn_id in active_connections:
                    handler = pubsub_manager.connections.get(conn_id)
                    
                    if handler and handler.running:
                        try:
                            await handler.send_message(ping_message)
                            successful_pings += 1
                            self.stats['pings_sent'] += 1
                        except Exception as e:
                            logger.debug(f"Failed to ping {conn_id}: {e}")
                            # Will be cleaned up by cleanup loop
                    else:
                        # Handler is gone, remove from our tracking
                        await self.unregister_connection(conn_id)
                
                logger.debug(f"Sent pings to {successful_pings}/{len(active_connections)} connections")
                
                await asyncio.sleep(self.ping_interval)
                
            except Exception as e:
                logger.exception(f"Error in heartbeat loop: {e}")
                await asyncio.sleep(5)
                
        logger.info("Heartbeat loop stopped")
    
    async def _cleanup_loop(self):
        """
        Aggressive cleanup of dead connections.
        This is critical for preventing resource leaks at scale.
        """
        logger.info("Cleanup loop started")
        
        while self.running:
            try:
                current_time = time.time()
                dead_connections = []
                
                # Find connections that haven't responded to pings
                for conn_id, last_ping in list(self.connections.items()):
                    time_since_ping = current_time - last_ping
                    
                    if time_since_ping > self.timeout_threshold:
                        dead_connections.append((conn_id, time_since_ping))
                
                # Clean up dead connections
                for conn_id, time_since_ping in dead_connections:
                    logger.warning(f"Connection {conn_id} timed out ({time_since_ping:.1f}s), cleaning up")
                    
                    await self._cleanup_dead_connection(conn_id)
                    self.stats['connections_timed_out'] += 1
                
                # Additional cleanup: verify handler states
                if self.aggressive_cleanup:
                    await self._aggressive_cleanup_check()
                
                self.stats['cleanup_cycles'] += 1
                
                if dead_connections:
                    logger.info(f"Cleaned up {len(dead_connections)} dead connections")
                
                await asyncio.sleep(self.cleanup_interval)
                
            except Exception as e:
                logger.exception(f"Error in cleanup loop: {e}")
                await asyncio.sleep(10)
                
        logger.info("Cleanup loop stopped")
    
    async def _cleanup_dead_connection(self, connection_id: str):
        """
        Comprehensive cleanup of a dead connection.
        Ensures no resource leaks.
        """
        try:
            # 1. Clean up WebSocket handler
            handler = pubsub_manager.connections.get(connection_id)
            if handler:
                try:
                    await handler.cleanup_connection()
                except Exception as e:
                    logger.error(f"Error in handler cleanup for {connection_id}: {e}")
            
            # 2. Clean up from pub/sub manager
            await pubsub_manager.unregister_connection(connection_id)
            
            # 3. Clean up from heartbeat tracking
            await self.unregister_connection(connection_id)
            
            # 4. Clean up Redis state directly (failsafe)
            await self._redis_cleanup_connection(connection_id)
            
            logger.debug(f"Comprehensive cleanup completed for {connection_id}")
            
        except Exception as e:
            logger.error(f"Error in comprehensive cleanup for {connection_id}: {e}")
    
    async def _aggressive_cleanup_check(self):
        """
        Additional cleanup pass to catch any missed dead connections.
        Checks handler states directly.
        """
        try:
            registered_connections = set(self.connections.keys())
            active_connections = set(pubsub_manager.connections.keys())
            
            # Find connections registered for heartbeat but missing from pub/sub manager
            orphaned_heartbeats = registered_connections - active_connections
            for conn_id in orphaned_heartbeats:
                logger.warning(f"Found orphaned heartbeat registration: {conn_id}")
                await self.unregister_connection(conn_id)
            
            # Find connections in pub/sub manager with dead handlers
            dead_handlers = []
            for conn_id, handler in pubsub_manager.connections.items():
                if not handler.running or handler.websocket is None:
                    dead_handlers.append(conn_id)
            
            for conn_id in dead_handlers:
                logger.warning(f"Found dead handler in pub/sub manager: {conn_id}")
                await self._cleanup_dead_connection(conn_id)
                
        except Exception as e:
            logger.exception(f"Error in aggressive cleanup check: {e}")
    
    async def _redis_cleanup_connection(self, connection_id: str):
        """
        Direct Redis cleanup for connection state.
        Failsafe to prevent Redis key buildup.
        """
        try:
            redis_conn = await redis_pool.get_connection()
            try:
                def cleanup_redis():
                    # Remove connection record
                    redis_conn.delete(f"realtime:connections:{connection_id}")
                    
                    # Remove from online status (scan for user keys containing this connection)
                    for key in redis_conn.scan_iter(match="realtime:online:*"):
                        try:
                            data = redis_conn.get(key)
                            if data:
                                user_data = json.loads(data)
                                connection_ids = set(user_data.get("connection_ids", []))
                                if connection_id in connection_ids:
                                    connection_ids.remove(connection_id)
                                    if connection_ids:
                                        user_data["connection_ids"] = list(connection_ids)
                                        user_data["last_seen"] = time.time()
                                        redis_conn.setex(key, 3600, json.dumps(user_data))
                                    else:
                                        redis_conn.delete(key)
                        except:
                            # Skip corrupted entries
                            pass
                    
                    # Remove from topic subscriptions (scan for topic keys)
                    for key in redis_conn.scan_iter(match="realtime:topic:*"):
                        try:
                            redis_conn.srem(key, connection_id)
                        except:
                            pass
                
                await asyncio.get_event_loop().run_in_executor(None, cleanup_redis)
                self.stats['redis_cleanup_operations'] += 1
                
            finally:
                await redis_pool.return_connection(redis_conn)
                
        except Exception as e:
            logger.error(f"Error in Redis cleanup for {connection_id}: {e}")
    
    def get_stats(self):
        """Get heartbeat manager statistics"""
        return {
            **self.stats,
            'active_heartbeats': len(self.connections),
            'ping_response_rate': (
                self.stats['pings_responded'] / max(1, self.stats['pings_sent']) * 100
            )
        }

# Global heartbeat manager
heartbeat_manager = HeartbeatManager()
```

### 4. Updated WebSocket Handler Integration

```python
# Updated WebSocketHandler methods for scaling
async def handle_connection(self):
    """Main connection handler with shared resource management"""
    logger.info(f"New WebSocket connection: {self.connection_id}")
    
    try:
        # 1. Register with shared managers (order matters)
        await pubsub_manager.register_connection(self.connection_id, self)
        await heartbeat_manager.register_connection(self.connection_id)
        
        # 2. Start global managers if not running (first connection starts them)
        await pubsub_manager.start()
        await heartbeat_manager.start()
        
        # 3. Register connection in Redis using shared pool
        await self.register_connection()
        
        # 4. Send auth required and start message handling
        await self.send_message({"type": "auth_required", "timeout": 30})
        
        # 5. Handle messages (no individual pub/sub - uses shared manager)
        await self.handle_client_messages()
        
    except Exception as e:
        logger.exception(f"Error in connection {self.connection_id}: {e}")
    finally:
        await self.cleanup_connection()

async def register_connection(self):
    """Register connection in Redis using shared pool"""
    redis_conn = await redis_pool.get_connection()
    try:
        connection_data = {
            "connection_id": self.connection_id,
            "authenticated": False,
            "connected_at": time.time(),
            "last_ping": time.time(),
            "topics": []
        }
        
        key = f"realtime:connections:{self.connection_id}"
        
        def set_connection():
            redis_conn.setex(key, 3600, json.dumps(connection_data))
        
        await asyncio.get_event_loop().run_in_executor(None, set_connection)
        
    finally:
        await redis_pool.return_connection(redis_conn)

async def handle_ping(self, data):
    """Handle ping and update heartbeat"""
    # Update heartbeat tracking
    await heartbeat_manager.update_heartbeat(self.connection_id)
    
    await self.send_message({
        "type": "pong",
        "user_type": self.user_type,
        "user_id": self.user.id if self.user else None
    })

async def subscribe_to_topic(self, topic):
    """Subscribe to topic using shared pub/sub manager"""
    if topic in self.subscribed_topics:
        return
    
    success = await pubsub_manager.subscribe_connection(self.connection_id, topic)
    if success:
        self.subscribed_topics.add(topic)
        
        # Update Redis connection record
        await self._update_connection_topics()

async def