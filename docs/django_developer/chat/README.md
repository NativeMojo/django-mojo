# Chat — Django Developer Reference

Real-time chat built on top of the [realtime](../realtime/README.md) WebSocket system.

- [Models](models.md) — rooms, messages, memberships, reactions, receipts and sparse unread exceptions
- [REST Endpoints](rest.md) — Room management, message history, DMs, read state
- [WebSocket Handler](handler.md) — Real-time message sending, editing, reactions, typing
- [Services](services.md) — `send_message`, message kinds, metadata validation, the deletion hook, access-change frames
- [Cursor Read State](read_state.md) — DB-only cursors, read hooks, maintenance, activation and rollback
- [Rules & Moderation](rules.md) — Per-room content rules, content_guard integration
- [Permissions](permissions.md) — Integration with User/Group/Member permission system

Chat language moderation is advisory: sends and edits preserve real bodies at
all severities, with server-owned decision, reasons and score. Consumers choose
what to hide and may offer a local Show action. Maestro hides numeric scores
>=35 and substitutes `Hidden by moderation` in notification previews; see
[Rules & Moderation](rules.md) for the score/null and rollout contracts.
