# Helpers — Django Developer Reference

Helpers live in `mojo/helpers/`. Import directly — no registration required.

- [objict](objict.md) — Attribute-access dict used throughout the framework (`request.DATA`, metadata, etc.)
- [logit](logit.md) — Structured logging
- [dates](dates.md) — Timezone-aware datetime utilities
- [settings](settings.md) — Settings access with defaults
- [settings reference](settings_reference.md) — Framework-recognized setting keys (names only)
- [content_guard](content_guard.md) — Content moderation for usernames and text
- [crypto](crypto.md) — Encryption, hashing, signing
- [request](request.md) — Request parsing and client info
- [response](response.md) — JSON response helpers
- [redis](redis.md) — Redis client and caching
- [other](other.md) — stats, qrcode, filetypes, domain, geoip, sysinfo
