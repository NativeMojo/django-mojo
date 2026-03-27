# Chat — Django Developer Reference

Real-time chat built on top of the [realtime](../realtime/README.md) WebSocket system.

- [Models](models.md) — ChatRoom, ChatMessage, ChatMembership, ChatReaction, ChatReadReceipt
- [REST Endpoints](rest.md) — Room management, message history, DMs, read state
- [WebSocket Handler](handler.md) — Real-time message sending, editing, reactions, typing
- [Rules & Moderation](rules.md) — Per-room content rules, content_guard integration
- [Permissions](permissions.md) — Integration with User/Group/Member permission system
