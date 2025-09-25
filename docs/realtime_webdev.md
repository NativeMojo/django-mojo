# Realtime Web Developer Guide (JS)

This guide shows how to interface with the Mojo realtime WebSocket API from JavaScript apps (Vanilla JS, React, etc.). It covers authentication, reconnection, subscriptions, heartbeats, and custom messages with robust, production-ready patterns.

Contents
- Protocol recap
- Quick start (Vanilla JS)
- Robust client with auto-reconnect + backoff
- Subscribing to topics (user:{id}, general_announcements)
- Heartbeats (ping/pong) and visibility handling
- Token refresh (short-lived JWTs)
- Custom messages (message_type), example: echo and set_meta
- React hook example (useRealtime)
- TypeScript types (optional)
- Security best practices
- Troubleshooting

---

## Protocol Recap

Endpoint
- WebSocket URL: ws(s)://<host>/ws/realtime/

Message-based authentication (split fields)
- Server accepts the socket and sends:
  - { "type": "auth_required", "timeout_seconds": 30 }
- Client must authenticate within the timeout:
  - { "type": "authenticate", "token": "<token>", "prefix": "bearer" }
  - prefix is optional; defaults to "bearer"
- On success, server returns:
  - { "type": "auth_success", "instance_kind": "user", "instance_id": 123, "available_topics": [...] }

Built-in actions
- Subscribe: { "action": "subscribe", "topic": "<external-topic>" }
- Unsubscribe: { "action": "unsubscribe", "topic": "<external-topic>" }
- Ping: { "action": "ping" } (application-level heartbeat)

Notifications (server → client)
- { "type": "notification", "topic": "<external-topic>", "title": "...", "message": "...", "timestamp": ... }

Custom messages
- { "message_type": "<your_type>", ... }
- If no central handler is configured, the message is routed to instance.on_realtime_message(data)

Topics
- Use external names like "user:{id}" or "general_announcements"
- Authorization is enforced; available topics are advertised in auth_success

---

## Quick Start (Vanilla JS)

```html
<script>
  (() => {
    const httpBase = location.origin; // e.g., https://app.example.com
    const wsBase = httpBase.replace(/^http/, 'ws');
    const wsUrl = `${wsBase}/ws/realtime/`;

    // Replace with your access token retrieval (e.g., from localStorage)
    function getAccessToken() {
      return localStorage.getItem("access_token");
    }

    let ws;

    function connect() {
      ws = new WebSocket(wsUrl);

      ws.addEventListener("open", () => {
        console.log("[ws] open");
        const token = getAccessToken();
        ws.send(JSON.stringify({
          type: "authenticate",
          token // prefix omitted → defaults to "bearer"
        }));
      });

      ws.addEventListener("message", (ev) => {
        const msg = safeParse(ev.data);
        if (!msg) return;

        switch (msg.type) {
          case "auth_required":
            console.log("[ws] auth_required", msg);
            break;
          case "auth_success":
            console.log("[ws] auth_success", msg);
            // Example: subscribe to user topic
            if (msg.instance_id && msg.instance_kind) {
              const topic = `${msg.instance_kind}:${msg.instance_id}`;
              ws.send(JSON.stringify({ action: "subscribe", topic }));
            }
            break;
          case "subscribed":
            console.log("[ws] subscribed:", msg.topic);
            break;
          case "notification":
            console.log("[ws] notification:", msg);
            break;
          case "pong":
            console.log("[ws] pong", msg);
            break;
          case "error":
            console.warn("[ws] error", msg.message);
            break;
          default:
            console.log("[ws] message", msg);
        }
      });

      ws.addEventListener("close", (ev) => {
        console.log("[ws] close", ev.code, ev.reason);
      });

      ws.addEventListener("error", (err) => {
        console.error("[ws] error", err);
      });
    }

    function safeParse(text) {
      try { return JSON.parse(text); } catch { return null; }
    }

    // Initiate
    connect();
  })();
</script>
```

---

## Robust Client: Auto-Reconnect + Backoff

Use exponential backoff to reconnect after network issues or server restarts.

```js
class RealtimeClient {
  constructor({ wsUrl, getToken, onMessage, onOpen, onClose, onError }) {
    this.wsUrl = wsUrl;
    this.getToken = getToken;
    this.onMessage = onMessage || (() => {});
    this.onOpen = onOpen || (() => {});
    this.onClose = onClose || (() => {});
    this.onError = onError || (() => {});
    this.ws = null;
    this._shouldReconnect = true;

    // Backoff config
    this._reconnectDelay = 1000; // 1s
    this._maxReconnectDelay = 15000; // 15s
    this._backoffFactor = 1.7;

    // Heartbeat
    this._pingIntervalMs = 30_000; // 30s
    this._pingTimer = null;
  }

  connect() {
    this._shouldReconnect = true;
    this._open();
  }

  disconnect() {
    this._shouldReconnect = false;
    this._clearHeartbeat();
    if (this.ws && this.ws.readyState <= 1) {
      this.ws.close();
    }
  }

  send(obj) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(obj));
    }
  }

  subscribe(topic) {
    this.send({ action: "subscribe", topic });
  }

  unsubscribe(topic) {
    this.send({ action: "unsubscribe", topic });
  }

  ping() {
    this.send({ action: "ping" });
  }

  _open() {
    this.ws = new WebSocket(this.wsUrl);

    this.ws.addEventListener("open", () => {
      this._onOpen();
    });

    this.ws.addEventListener("message", (ev) => {
      const data = this._safeParse(ev.data);
      if (data) this._onMessage(data);
    });

    this.ws.addEventListener("close", (ev) => {
      this._onClose(ev);
      this._attemptReconnect();
    });

    this.ws.addEventListener("error", (err) => {
      this.onError(err);
    });
  }

  _onOpen() {
    // Reset backoff on successful connect
    this._reconnectDelay = 1000;

    // Authenticate
    const token = this.getToken?.();
    if (!token) {
      this.onError(new Error("Missing access token"));
      return;
    }
    this.send({ type: "authenticate", token });

    // Start heartbeat
    this._startHeartbeat();

    this.onOpen();
  }

  _onMessage(msg) {
    if (msg.type === "auth_success") {
      // Auto-subscribe to own topic
      const { instance_kind, instance_id } = msg;
      if (instance_kind && instance_id != null) {
        this.subscribe(`${instance_kind}:${instance_id}`);
      }
    } else if (msg.type === "auth_timeout") {
      this.onError(new Error("Authentication timeout"));
      this.ws?.close();
    } else if (msg.type === "error") {
      // Optionally close or re-auth / refresh token here
      console.warn("[ws] server error:", msg.message);
    } else if (msg.type === "pong") {
      // no-op
    }

    this.onMessage(msg);
  }

  _onClose(ev) {
    this._clearHeartbeat();
    this.onClose(ev);
  }

  _attemptReconnect() {
    if (!this._shouldReconnect) return;

    setTimeout(() => {
      this._open();
      // Exponential backoff
      this._reconnectDelay = Math.min(
        this._maxReconnectDelay,
        Math.floor(this._reconnectDelay * this._backoffFactor)
      );
    }, this._reconnectDelay);
  }

  _startHeartbeat() {
    this._clearHeartbeat();
    this._pingTimer = setInterval(() => {
      this.ping();
    }, this._pingIntervalMs);
  }

  _clearHeartbeat() {
    if (this._pingTimer) {
      clearInterval(this._pingTimer);
      this._pingTimer = null;
    }
  }

  _safeParse(text) {
    try { return JSON.parse(text); } catch { return null; }
  }
}

// Usage
const wsUrl = `${location.origin.replace(/^http/, 'ws')}/ws/realtime/`;
const client = new RealtimeClient({
  wsUrl,
  getToken: () => localStorage.getItem("access_token"),
  onMessage: (msg) => console.log("[ws] message", msg),
  onError: (err) => console.error("[ws] error", err),
});

client.connect();

// Subscribe to a global topic
client.subscribe("general_announcements");
```

---

## Visibility Handling (Reduce Noise and Save Power)

Pause heartbeat when the tab is hidden; resume on visible.

```js
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    // Optionally pause ping; keep the socket open
  } else {
    // Optionally send a ping or re-auth if token was refreshed
    client.ping();
  }
});
```

---

## Token Refresh (Short-Lived JWTs)

If your access token expires:
- Listen for server error payloads like "Token expired" (your server’s message).
- Refresh token via HTTP.
- Close and reconnect with the new token, or send a new "authenticate" (if your server supports re-auth on the same connection — current design expects auth once per connection).

Example:
```js
async function refreshToken() {
  // fetch a new token from your REST API
  const res = await fetch("/api/refresh_token", { method: "POST" });
  const data = await res.json();
  localStorage.setItem("access_token", data.access_token);
  return data.access_token;
}

// On auth error:
client.onMessage = async (msg) => {
  if (msg.type === "error" && /token/i.test(msg.message || "")) {
    await refreshToken();
    client.disconnect();
    client.connect();
    return;
  }
  // ... other handling
};
```

---

## Custom Messages (message_type)

Send application-level messages with a `message_type`. If not handled centrally, they are forwarded to instance.on_realtime_message on the server.

Echo example (the server’s User model includes a test echo handler):
```js
client.send({ message_type: "echo", payload: { hello: "world" } });
```

Set metadata example:
```js
client.send({ message_type: "set_meta", key: "theme", value: "dark" });
```

You’ll receive:
- Echo: { "type": "echo", "user_id": <id>, "payload": {...} }
- Set meta: { "type": "ack", "key": "theme", "value": "dark" }

---

## React Hook Example (useRealtime)

```jsx
import { useEffect, useRef, useState, useCallback } from "react";

function useRealtime({ onNotification }) {
  const [status, setStatus] = useState("disconnected"); // "connecting" | "connected" | "disconnected"
  const wsRef = useRef(null);

  const wsUrl = `${location.origin.replace(/^http/, 'ws')}/ws/realtime/`;
  const getToken = () => localStorage.getItem("access_token");

  const connect = useCallback(() => {
    setStatus("connecting");
    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;

    ws.addEventListener("open", () => {
      setStatus("connected");
      const token = getToken();
      ws.send(JSON.stringify({ type: "authenticate", token }));
    });

    ws.addEventListener("message", (ev) => {
      const msg = safeParse(ev.data);
      if (!msg) return;
      if (msg.type === "auth_success") {
        const { instance_kind, instance_id } = msg;
        if (instance_kind && instance_id != null) {
          ws.send(JSON.stringify({ action: "subscribe", topic: `${instance_kind}:${instance_id}` }));
        }
      } else if (msg.type === "notification") {
        onNotification?.(msg);
      }
    });

    ws.addEventListener("close", () => {
      setStatus("disconnected");
    });

    ws.addEventListener("error", (err) => {
      console.error("[ws] error", err);
    });
  }, [wsUrl, onNotification]);

  const disconnect = useCallback(() => {
    wsRef.current?.close();
  }, []);

  useEffect(() => {
    connect();
    return () => disconnect();
  }, [connect, disconnect]);

  const send = useCallback((obj) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(obj));
    }
  }, []);

  function safeParse(text) {
    try { return JSON.parse(text); } catch { return null; }
  }

  return { status, send };
}

// Usage in a component
// function Notifications() {
//   const { status, send } = useRealtime({
//     onNotification: (msg) => console.log("Notification:", msg),
//   });
//   return <div>WS Status: {status}</div>;
// }
```

---

## TypeScript Types (Optional)

```ts
type WsServerMessage =
  | { type: "auth_required"; timeout_seconds: number }
  | { type: "auth_success"; instance_kind: string; instance_id: number | string; available_topics?: string[] }
  | { type: "subscribed"; topic: string; group: string }
  | { type: "unsubscribed"; topic: string; group: string }
  | { type: "notification"; topic: string; title?: string; message?: string; timestamp?: number; priority?: string }
  | { type: "pong"; instance_kind?: string; instance?: string }
  | { type: "error"; message: string }
  | { type: string; [k: string]: any }; // custom cases

type WsClientMessage =
  | { type: "authenticate"; token: string; prefix?: string }
  | { action: "subscribe"; topic: string }
  | { action: "unsubscribe"; topic: string }
  | { action: "ping" }
  | { message_type: string; [k: string]: any };
```

---

## Security Best Practices

- Always use WSS (TLS) in production.
- Do not pass JWTs as query parameters (use message-based auth as shown).
- Rotate and refresh tokens; handle expiration with a clean reconnect.
- Limit subscriptions per connection and validate topics on the server (already enforced).
- Avoid sending sensitive data in notifications; rely on server-side authorization checks.

---

## Troubleshooting

- 404 on /ws/realtime/:
  - Ensure your ASGI routing includes the realtime websocket URL.
- “Unsupported upgrade request”:
  - Run an ASGI server with WebSocket support and the necessary WS libs.
- Auth times out:
  - Send the authenticate message right after open.
- No notifications:
  - Make sure you subscribed to the correct external topic (e.g., "user:123").
  - Verify the server publishes to the same external topic.
- Random disconnects:
  - Add heartbeat (ping/pong) and reconnection with backoff.
  - Consider network proxies—some aggressively close idle connections.

---

## Minimal Patterns to Remember

Authenticate immediately after open:
```js
ws.addEventListener("open", () => {
  ws.send(JSON.stringify({ type: "authenticate", token: getAccessToken() }));
});
```

Subscribe to your own topic after auth_success:
```js
if (msg.type === "auth_success") {
  ws.send(JSON.stringify({ action: "subscribe", topic: `${msg.instance_kind}:${msg.instance_id}` }));
}
```

Handle notifications:
```js
if (msg.type === "notification") {
  // show toast/banner
}
```

Send heartbeat:
```js
setInterval(() => ws.send(JSON.stringify({ action: "ping" })), 30000);
```

Reconnect on close with backoff:
```js
ws.addEventListener("close", () => {
  setTimeout(connect, backoffDelay);
  backoffDelay = Math.min(15000, Math.floor(backoffDelay * 1.7));
});
```

With these patterns, you’ll have a resilient, secure integration with the realtime WebSocket API.