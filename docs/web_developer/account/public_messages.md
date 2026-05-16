# Public Messages — Web Developer Reference

Bouncer-gated contact / support form intake. Unauthenticated visitors submit a
message through a server-rendered HTML page; the backend stores the record and
fires an admin notification email. Admins read and resolve submissions through
a standard RestMeta endpoint.

See also: [Bouncer](bouncer.md) for the underlying bot-gate flow.

---

## Flow

```
1. Visitor loads /contact?kind=<kind>
      ↓
   Same bouncer pipeline as /auth:
     signature cache → pass cookie → pre-screen → decoy / challenge / page
      ↓
2. Challenge (if shown)
      mojo-bouncer.js runs, user clicks target
      POST /api/account/bouncer/assess → token + pass cookie
      page reloads with bouncer_token in localStorage
      ↓
3. Full contact page renders fields driven by ?kind=
      Form POSTs → /api/account/bouncer/message with bouncer_token
      ↓
4. Server validates the (single-use) token + fields, saves PublicMessage,
   emails flagged admins, returns {status: true, data: {id}}
```

`kind` is taken from the query string on the page; if missing or unknown, the
page falls back to `contact_us`.

---

## Page URL

```
GET /contact?kind=contact_us
GET /contact?kind=support
```

Both are bouncer-gated identically to `/auth` and `/register`. Response is HTML
(rendered by the server) — either the form, a challenge page, or a decoy.

The path is configurable server-side via `BOUNCER_CONTACT_PATH` (default
`contact`). If your backend has customized it, use that value here.

White-label: pass `?group_uuid=<uuid>` to scope the page to a specific group.
The resulting message will be attached to that group and only admins with
notify_public_messages access in that group are emailed.

---

## Submit Endpoint

**POST** `/api/account/bouncer/message`

No user authentication required. Requires a valid single-use bouncer token and
is rate-limited (5 submits per IP per 5 minutes).

### Common fields (every kind)

| Field | Required | Notes |
|---|---|---|
| `kind` | yes | `contact_us` or `support` |
| `name` | yes | ≤ 120 chars |
| `email` | yes | Valid email, ≤ 254 chars |
| `message` | yes | ≤ 4000 chars (server cap) |
| `bouncer_token` | yes* | Issued by the assess endpoint; single-use |

`*` With `BOUNCER_REQUIRE_TOKEN=False` (the default rollout mode), a missing
token is logged but the submit proceeds. In production you should assume the
token is required.

### Per-kind fields

**`contact_us`**:

| Field | Required | Notes |
|---|---|---|
| `company` | no | Free text, ≤ 120 chars, stored in `metadata.company` |

**`support`**:

| Field | Required | Notes |
|---|---|---|
| `category` | yes | One of `billing`, `account`, `bug`, `other` |
| `severity` | yes | One of `low`, `normal`, `high` |

### Request

```json
POST /api/account/bouncer/message
{
  "kind": "support",
  "name": "Jane Doe",
  "email": "jane@example.com",
  "category": "bug",
  "severity": "high",
  "message": "I cannot log in since the last deploy.",
  "bouncer_token": "<bouncer token from assess>",
  "metadata": {
    "utm_source": "google",
    "utm_campaign": "spring-2026",
    "referrer": "https://example.com/blog/post",
    "landing_page": "/pricing"
  }
}
```

### Free-form `metadata`

A marketing or landing page can attach arbitrary tracking payload under the
top-level `metadata` key. Rules:

- Flat object only — primitive values (`string`, `number`, `boolean`, `null`).
  Arrays and nested objects are silently dropped.
- Keys must match `[A-Za-z0-9_.-]+` and be ≤ 64 chars. Others are dropped.
- String values are capped at 500 chars (trimmed silently).
- Up to 25 keys; extras are ignored.
- Kind-schema keys (e.g. `category`, `severity`, `company`) **cannot** be
  spoofed through `metadata` — the kind-provided value always wins.
- Values are stored verbatim and not run through content moderation, so do
  not put submitter free-text in here. Use the `message` field for that.

The stored `metadata` column on the row is the merged blob — kind-specific
fields plus whatever client extras survived the cleanse.

### Response

**200 OK**

```json
{
  "status": true,
  "data": {"id": 1234}
}
```

**400 Bad Request** — validation error

```json
{
  "status": false,
  "error": "category:invalid",
  "code": 400
}
```

Error strings are `<field>:<reason>` where `<reason>` is one of:
- `required` — the field was missing or blank
- `invalid` — email format, enum value, or kind not recognized
- `too_long` — exceeded the per-field max length
- `blocked` — content moderation flagged the submission

**403 Forbidden** — bouncer token missing / invalid / replayed (when
`BOUNCER_REQUIRE_TOKEN=True`). Let the user refresh the page to re-run the
challenge and obtain a fresh token.

**429 Too Many Requests** — IP rate-limited (5 submits per 5 minutes).

### Single-use token

Every bouncer token is single-use. After a successful submit, the token's
nonce is consumed; a second submit with the same token returns 403. Users who
want to send another message must reload the contact page to get a new token.

---

## Admin Endpoint

**GET / POST** `/api/account/public_message`
**GET / POST / DELETE** `/api/account/public_message/<id>`

Standard RestMeta surface. Requires one of:

| Permission | What it grants |
|---|---|
| `view_support` / `security` / `support` | Read list + detail |
| `manage_support` / `security` / `support` | Update status |
| `manage_support` | Delete |

Group-scoped admins (members with one of the view/manage perms on a specific
Group) automatically see only messages attached to that group. System-level
admins see everything.

### Typical admin workflow

```
GET /api/account/public_message?status=open&sort=-created&size=50
GET /api/account/public_message/123
POST /api/account/public_message/123    { "status": "closed" }
```

### Status values

- `open` — default on new submission
- `closed` — resolved / acknowledged

There is no reply-to-submitter workflow in v1. Contact the submitter through
your normal support channel using the `email` on the record.

---

## Rate Limits

| Surface | Limit |
|---|---|
| `POST /api/account/bouncer/message` | 5 requests per IP per 5 minutes (sliding window) |
| `GET /contact` | subject to the bouncer pre-screen; no fixed rate limit |

---

## Integrating from an External Marketing Site

Host the bouncer-gated contact page on the same origin or a trusted subdomain.
For a fully separated marketing site:

1. Embed `/contact?kind=<kind>` in an iframe, OR
2. Redirect users from your marketing contact link to `/contact`, OR
3. Reuse `mojo-bouncer.js` directly — call the assess endpoint to get a token,
   then POST to `/api/account/bouncer/message` with your own form.

Option 1 is the least work and inherits all bouncer protections. Option 3
requires you to render the same challenge flow that the built-in page does.
