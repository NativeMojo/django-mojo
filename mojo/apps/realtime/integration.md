# Realtime WebSocket Integration Guide

This guide shows how to integrate the mojo realtime WebSocket system into your Django project.

## 1. ASGI Setup

Create or update your project's `asgi.py` file:

**⚠️ Important: Import routing utilities directly (no Django setup needed):**

```python
# your_project/asgi.py
import os
from django.core.asgi import get_asgi_application
from mojo.apps.realtime.routing import ProtocolTypeRouter, WebSocketRouter
from mojo.apps.realtime.asgi import get_asgi_application as get_realtime_asgi

# Set Django settings
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'your_project.settings')

# Create the ASGI application with routing
application = ProtocolTypeRouter({
    "http": get_asgi_application(),
    "websocket": WebSocketRouter([
        (r"^ws/realtime/$", get_realtime_asgi()),
    ]),
})
```

**Using path() for cleaner syntax:**
```python
# your_project/asgi.py
import os
from django.core.asgi import get_asgi_application
from mojo.apps.realtime.routing import ProtocolTypeRouter, WebSocketRouter, path
from mojo.apps.realtime.asgi import get_asgi_application as get_realtime_asgi

# Set Django settings
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'your_project.settings')

# Create the ASGI application with cleaner path syntax
application = ProtocolTypeRouter({
    "http": get_asgi_application(),
    "websocket": WebSocketRouter([
        path("ws/realtime/", get_realtime_asgi()),
    ]),
})
```

**Alternative - Using the convenience function:**
```python
# your_project/asgi.py  
import os
from mojo.apps.realtime.routing import create_application

# Set Django settings
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'your_project.settings')

# Create application with default realtime route (cleanest option!)
application = create_application()
```

**Custom WebSocket routes:**
```python
# your_project/asgi.py
import os
from mojo.apps.realtime.routing import create_application, path
from mojo.apps.realtime.asgi import get_asgi_application as get_realtime_asgi

# Set Django settings
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'your_project.settings')

# Custom WebSocket routes using path() for cleaner syntax
websocket_routes = [
    path("ws/realtime/", get_realtime_asgi()),
    path("ws/admin/realtime/", get_realtime_asgi()),  # Admin-only endpoint
]

application = create_application(websocket_routes=websocket_routes)
```

**Using raw regex patterns (alternative):**
```python
# your_project/asgi.py
import os
from mojo.apps.realtime.routing import create_application
from mojo.apps.realtime.asgi import get_asgi_application as get_realtime_asgi

# Set Django settings
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'your_project.settings')

# Custom WebSocket routes with regex patterns
websocket_routes = [
    (r"^ws/realtime/$", get_realtime_asgi()),
    (r"^ws/admin/realtime/$", get_realtime_asgi()),  # Admin-only endpoint
]

application = create_application(websocket_routes=websocket_routes)
```

## 2. Server Configuration

Make sure your ASGI server supports WebSockets:

### Uvicorn
```bash
pip install uvicorn[standard]
uvicorn your_project.asgi:application --host 0.0.0.0 --port 8000
```

### Daphne  
```bash
pip install daphne
daphne -b 0.0.0.0 -p 8000 your_project.asgi:application
```

### Gunicorn + Uvicorn
```bash
pip install gunicorn uvicorn[standard]
gunicorn your_project.asgi:application -w 4 -k uvicorn.workers.UvicornWorker
```

## 3. Django Usage Examples

**⚠️ Important: Import realtime functions from mojo.apps (requires Django setup):**

### In Views
```python
from django.http import JsonResponse
from mojo.apps import realtime

def notify_user(request):
    user_id = request.POST.get('user_id')
    message = request.POST.get('message')
    
    # Check if user is online
    if realtime.is_online("user", user_id):
        # Send realtime notification
        realtime.send_to_user("user", user_id, {
            "title": "New Notification",
            "body": message,
            "type": "notification"
        })
        return JsonResponse({"sent": "realtime"})
    else:
        # User offline, send email instead
        send_email_notification(user_id, message)
        return JsonResponse({"sent": "email"})

def broadcast_announcement(request):
    message = request.POST.get('message')
    
    # Broadcast to all connected users
    realtime.broadcast({
        "title": "System Announcement", 
        "body": message,
        "type": "announcement"
    })
    
    return JsonResponse({"status": "sent"})
```

### In Models
```python
from django.db import models
from mojo.apps import realtime

class ChatMessage(models.Model):
    room = models.CharField(max_length=100)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    
    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        
        # Send to room subscribers
        realtime.publish_topic(f"chat:{self.room}", {
            "type": "new_message",
            "message_id": self.id,
            "user": self.user.username,
            "message": self.message,
            "timestamp": self.created_at.isoformat()
        })

class User(AbstractUser):
    # Your existing user model
    
    def on_realtime_connected(self):
        """Called when user connects via WebSocket"""
        # Update online status
        self.last_seen = timezone.now()
        self.save(update_fields=['last_seen'])
        
    def on_realtime_disconnected(self):
        """Called when user disconnects"""
        self.last_seen = timezone.now()
        self.save(update_fields=['last_seen'])
        
    def on_realtime_message(self, data):
        """Handle custom realtime messages"""
        message_type = data.get('message_type')
        
        if message_type == 'typing':
            # Broadcast typing indicator
            room = data.get('room')
            realtime.publish_topic(f"chat:{room}", {
                "type": "typing",
                "user": self.username,
                "typing": data.get('typing', True)
            })
            return {"type": "ack"}
            
        elif message_type == 'get_stats':
            # Return user stats
            return {
                "type": "stats",
                "online_users": realtime.get_auth_count("user"),
                "your_connections": len(realtime.get_user_connections("user", self.id))
            }
```

### In Celery Tasks
```python
from celery import shared_task
from mojo.apps import realtime

@shared_task
def process_order(order_id):
    # Process order...
    order = Order.objects.get(id=order_id)
    
    # Notify user of order status
    realtime.send_to_user("user", order.user_id, {
        "title": "Order Update",
        "body": f"Your order #{order_id} is being processed",
        "order_id": order_id,
        "status": "processing"
    })
    
    # Do processing...
    
    # Notify completion
    realtime.send_to_user("user", order.user_id, {
        "title": "Order Complete", 
        "body": f"Your order #{order_id} is ready!",
        "order_id": order_id,
        "status": "complete"
    })
```

## 4. Frontend JavaScript

### Basic Connection
```javascript
class RealtimeClient {
    constructor(token) {
        this.token = token;
        this.ws = null;
        this.authenticated = false;
    }
    
    connect() {
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${protocol}//${window.location.host}/ws/realtime/`;
        
        this.ws = new WebSocket(wsUrl);
        
        this.ws.onopen = () => {
            console.log('WebSocket connected');
            this.authenticate();
        };
        
        this.ws.onmessage = (event) => {
            const data = JSON.parse(event.data);
            this.handleMessage(data);
        };
        
        this.ws.onclose = () => {
            console.log('WebSocket disconnected');
            // Implement reconnection logic
        };
    }
    
    authenticate() {
        this.send({
            type: 'authenticate',
            token: this.token,
            prefix: 'bearer'
        });
    }
    
    subscribe(topic) {
        this.send({
            type: 'subscribe', 
            topic: topic
        });
    }
    
    send(data) {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify(data));
        }
    }
    
    handleMessage(data) {
        switch(data.type) {
            case 'auth_success':
                this.authenticated = true;
                console.log('Authenticated as:', data.user_type, data.user_id);
                // Auto-subscribe to user topic
                this.subscribe(`user:${data.user_id}`);
                break;
                
            case 'message':
                // Handle incoming messages
                if (data.topic) {
                    console.log('Topic message:', data.topic, data.data);
                } else {
                    console.log('Direct message:', data.data);
                }
                this.onMessage(data);
                break;
                
            case 'error':
                console.error('WebSocket error:', data.message);
                break;
        }
    }
    
    onMessage(data) {
        // Override this method to handle messages
        const messageData = data.data;
        
        if (messageData.type === 'notification') {
            this.showNotification(messageData.title, messageData.body);
        }
    }
    
    showNotification(title, body) {
        if (Notification.permission === 'granted') {
            new Notification(title, { body: body });
        }
    }
}

// Usage
const client = new RealtimeClient(localStorage.getItem('access_token'));
client.connect();
```

## 5. Admin/Monitoring

### Get Online Users
```python
from mojo.apps import realtime

# Get count of online users
online_count = realtime.get_auth_count("user")

# Get all online users  
online_users = realtime.get_online_users("user")
for user_type, user_id in online_users:
    print(f"{user_type}:{user_id} is online")
    
# Check specific user
if realtime.is_online("user", 123):
    print("User 123 is online")
    
# Force disconnect user
realtime.disconnect_user("user", 123)
```

### Management Command Example
```python
# management/commands/realtime_stats.py
from django.core.management.base import BaseCommand
from mojo.apps import realtime

class Command(BaseCommand):
    help = 'Show realtime connection statistics'
    
    def handle(self, *args, **options):
        total_connections = realtime.get_auth_count()
        user_connections = realtime.get_auth_count("user")
        
        self.stdout.write(f"Total connections: {total_connections}")
        self.stdout.write(f"User connections: {user_connections}")
        
        # Show online users
        online_users = realtime.get_online_users("user")
        self.stdout.write(f"Online users: {len(online_users)}")
        
        for user_type, user_id in online_users[:10]:  # Show first 10
            connections = realtime.get_user_connections(user_type, user_id)
            self.stdout.write(f"  {user_type}:{user_id} - {len(connections)} connections")
```

## 6. Import Patterns Summary

**ASGI Setup (works before Django fully configured):**
```python
# asgi.py - No Django dependencies
from mojo.apps.realtime.routing import create_application, ProtocolTypeRouter
```

**Django Usage (requires Django setup):**
```python 
# views.py, models.py, tasks.py - After Django is configured
from mojo.apps import realtime
```

## 7. Settings

Add to your Django settings if needed:

```python
# Redis configuration (if not already set)
REDIS_URL = "redis://localhost:6379/0"  # or your Redis URL
REDIS_MAX_CONN = 500

# Optional: Configure authentication handlers if using custom auth
AUTH_BEARER_HANDLERS_MAP = {
    'bearer': 'your_app.auth.validate_jwt_token',
}

AUTH_BEARER_NAME_MAP = {
    'bearer': 'user',
}
```

## 8. Deployment Considerations

1. **Redis**: Ensure Redis is available and properly configured
2. **ASGI Server**: Use uvicorn, daphne, or gunicorn with uvicorn workers  
3. **Load Balancing**: WebSocket connections work with standard load balancing
4. **Monitoring**: Monitor Redis keys with `realtime:*` pattern
5. **Scaling**: Add more ASGI worker processes as needed

The system is stateless and scales horizontally through Redis pub/sub.