# WebSocket Protocol — REST API Reference

## Connection

Connect to the WebSocket endpoint:

```
ws://api.example.com/ws/realtime/
wss://api.example.com/ws/realtime/   (production — always use TLS)
```

## Authentication Flow

### Step 1: Server sends auth challenge

```json
{"type": "auth_required", "timeout_seconds": 30}
```

### Step 2: Client authenticates

```json
{
  "type": "authenticate",
  "token": "eyJhbGci...",
  "prefix": "bearer"
}
```

`prefix` is optional and defaults to `"bearer"`. Use the same JWT token as HTTP requests.

### Step 3: Server confirms

```json
{
  "type": "auth_success",
  "instance_kind": "user",
  "instance_id": 42,
  "available_topics": [
    "user:42",
    "general_announcements"
  ]
}
```

You are automatically subscribed to `user:<your_id>`.

### Authentication Failure

```json
{"type": "error", "message": "Invalid token"}
```

Connection is closed after failure or timeout.

---

## Subscribing to Topics

```json
{"action": "subscribe", "topic": "user:42"}
```

**Response:**

```json
{"type": "subscribed", "topic": "user:42", "group": "user_42"}
```

You can only subscribe to topics in your `available_topics` list.

## Unsubscribing

```json
{"action": "unsubscribe", "topic": "user:42"}
```

**Response:**

```json
{"type": "unsubscribed", "topic": "user:42", "group": "user_42"}
```

## Ping / Keep-Alive

```json
{"action": "ping"}
```

**Response:**

```json
{"type": "pong", "instance_kind": "user", "instance": "alice@example.com"}
```

---

## Receiving Messages

Notifications are delivered as:

```json
{
  "type": "notification",
  "topic": "user:42",
  "title": "Your order shipped",
  "message": "Order #123 is on its way!",
  "priority": "normal",
  "timestamp": 1712345678.9
}
```

Custom message types depend on your app's implementation.

---

## Sending Custom Messages

Send any JSON payload with a `message_type` field:

```json
{"message_type": "echo", "payload": {"foo": "bar"}}
```

The server routes it to a registered handler or the model's `on_realtime_message` hook. The response (if any) is sent back to your connection.

---

## Standard Message Reference

### Server → Client

| Type | Trigger |
|---|---|
| `auth_required` | Connection established |
| `auth_success` | Authentication succeeded |
| `auth_timeout` | Authentication not received in time |
| `error` | Auth failed or invalid operation |
| `subscribed` | Subscribe action succeeded |
| `unsubscribed` | Unsubscribe action succeeded |
| `notification` | Push message from server |
| `pong` | Response to ping |

### Client → Server

| Field | Description |
|---|---|
| `type: "authenticate"` | Authentication with token |
| `action: "subscribe"` | Subscribe to a topic |
| `action: "unsubscribe"` | Unsubscribe from a topic |
| `action: "ping"` | Keep-alive ping |
| `message_type: "<custom>"` | App-specific message |

---

## JavaScript Example

```javascript
const ws = new WebSocket("wss://api.example.com/ws/realtime/");
let token = "<your-jwt-token>";

ws.onmessage = (event) => {
  const msg = JSON.parse(event.data);

  if (msg.type === "auth_required") {
    ws.send(JSON.stringify({type: "authenticate", token: token}));

  } else if (msg.type === "auth_success") {
    console.log("Connected as", msg.instance_kind, msg.instance_id);

  } else if (msg.type === "notification") {
    console.log("Notification:", msg.title, msg.message);
  }
};

ws.onclose = () => console.log("Disconnected");
```
