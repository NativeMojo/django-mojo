# Rate Limits & Required Client Backoff

Every django-mojo API enforces per-identity rate limits (per user account,
per API key, and per session — not just per IP). This page is the contract
your client must honor. Clients that ignore it will find their traffic
rejected, their account throttled, and — for sustained machine-rate abuse —
their account disabled.

## The 429 contract

Any endpoint may respond:

```
HTTP/1.1 429 Too Many Requests
Retry-After: 42

{"error": "Rate limit exceeded", "code": 429, "status": false}
```

Required behavior:

1. **Stop sending immediately.** Do not retry the failed request right away.
2. **Honor `Retry-After`** (seconds). Do not send anything for that identity
   until it elapses.
3. If you retry after that and still get 429s, use **exponential backoff with
   jitter**: base 1 s, double per attempt, cap at 60 s, ±50% random jitter.
4. **Give up after ~20 attempts** and require human interaction to resume. A
   tab that can't connect after twenty tries should stop, not try harder.

Limits are keyed to your **authenticated identity and session**. Rotating
IPs, opening new tabs, or clearing cookies does not reset an account's
budget. Default budgets are generous (hundreds of requests per minute per
identity) — real interactive apps never hit them; scripts polling in a tight
loop do.

## Never report one telemetry event per failure

Error/telemetry reporting endpoints (`/api/account/bouncer/event`, etc.) are
limited per session. If your app reports client-side errors:

- **Sample and dedupe** — one report per distinct error per few minutes, not
  one per occurrence.
- **Buffer and batch** — accumulate and send one request, not a stream.
- **Never report inside a retry loop.** A telemetry POST per failed request
  turns your error handler into a traffic amplifier — this exact pattern has
  caused a 27-hour production outage.

## WebSocket rules

- **Close code `4429` means deliberately rejected** — you are connecting too
  fast. Back off exponentially (same schedule as above) before reconnecting.
  Treat it differently from a network drop.
- **Never reconnect instantly in a loop.** Every reconnect must go through
  the backoff schedule, even after a clean network blip.
- **Authenticate within 10 seconds** of connecting (the `auth_required`
  message advertises the window) or the socket is closed.
- Each account may hold a limited number of concurrent sockets (default 10).
  Share one connection per tab/app; don't open one per widget.
- If your session is disabled or revoked server-side, your socket receives a
  `disconnect` message and is closed — re-authenticate before reconnecting.

## Polling etiquette

- Prefer the realtime websocket feed over REST polling for live data.
- If you must poll, poll ≥ 5 s intervals and **never with cache-busting
  parameters in a tight loop**.
- Sustained automated access (dashboards, exports, scraping) should go
  through an issued API key with negotiated limits — ask the platform
  operator. Scraping the portal at machine rate gets an account disabled;
  an API key gets you a supported, quota'd path to the same data.

## Related

- [Security Dashboard APIs](README.md)
- [Bouncer client integration](../account/bouncer.md)
