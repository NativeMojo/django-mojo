# Rate Limits & Required Client Backoff

django-mojo hard-limits human identities and security-sensitive endpoints.
Ordinary ApiKey traffic is unlimited by default so a trusted integration can
fan out work for many end users, but it is still counted, observed, and can be
given a positive per-key hard ceiling. This page is the 429 contract every
client must honor whenever a hard limit applies.

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

Hard identity limits are keyed to the **individual User or ApiKey**, never the
ApiKey's group. Rotating IPs, opening tabs, or clearing cookies does not reset
that budget. Strict endpoint IP/device gates are separate and remain active
for every caller.

## ApiKey defaults and explicit limits

On an ordinary endpoint, `Authorization: apikey <token>` skips consumer
IP/device fairness gates automatically. No endpoint-specific bypass flag is
required. The request can still receive 429 when any of these apply:

- the endpoint is a credential, expensive-work, or write-amplification
  boundary using `strict_rate_limit`;
- that ApiKey has a positive `limits[endpoint_key]` entry;
- the endpoint declares a positive hard ApiKey fallback; or
- while global throttle enforcement is enabled and the route is not exempt,
  the key has a positive global `limits["api"]` entry or the deployment has
  deliberately enabled its legacy global ApiKey ceiling.

Unlimited does not mean unobserved. The server records a bounded threshold
event and five-minute concentration data by the individual key id. These are
signals for operator review, not proof of abuse and not automatic revocation.
Malformed or non-positive per-key values are not a kill switch; deactivate or
delete the key to revoke it.

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
- Sustained automated access (dashboards, exports, scraping) should use an
  issued API key. Ask the operator whether that key has an explicit hard
  ceiling; otherwise ordinary routes are pass-through but monitored. Scraping
  through a human portal session at machine rate can get the account disabled.

## Related

- [Security Dashboard APIs](README.md)
- [Bouncer client integration](../account/bouncer.md)
