# Realtime (WebSocket) — Django Developer Reference

- [Architecture & Setup](architecture.md) — Django Channels setup, configuration, connection/rate limits (DM-042), idle timeout and server keepalive
- [Publishing Messages](publishing.md) — Server-side publish helpers
- [Presence Checks](presence.md) — `is_online` and `count_online` (exact bulk online count)
- [Instance Hooks](hooks.md) — Model hooks for WebSocket events, and the `realtime_connection_changed` signal

See also [Authenticated-Abuse Hardening](../security/abuse_hardening.md) for the
websocket connect-rate gate, per-identity concurrency cap, and unauth timeout.
