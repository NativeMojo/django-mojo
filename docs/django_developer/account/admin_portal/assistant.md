# Admin Assistant panel

The built-in Admin carries a docked Assistant panel on the right of every page.
It is a **shell slot**, not a navigation lane: no route, no registry
descriptor, no sidebar entry. This page is the backend developer's view of how
it is wired and what its client modules guarantee.

The operator-facing API contract lives in
[web_developer/account/admin_portal/assistant.md](../../../web_developer/account/admin_portal/assistant.md).

---

## Why it is not a registry feature

`assets/features/registry.js` validates a fixed descriptor shape — `routes`,
`navigation`, `title`, `render`, all required — and
`services/admin_assets.FEATURES` enumerates the lane directories under
`assets/features/`. Both exist to serve *navigation lanes*. Satisfying them
with an empty-route descriptor would be a lie told to two validators and would
rewrite the asserted lane order.

Instead the panel is appended as a **third grid child of `#app`**:

```
#app  ->  aside.sidebar | main | aside#assistant-panel
```

`app.js render()` replaces only the `content` node inside `main`, so a third
child survives every route change for free. It is deliberately not built on
`components/overlays.js` either: `render()` calls `closeAllOverlays()` on every
navigation, so an overlay-based panel would be destroyed by the first sidebar
click.

The nine files under `mojo/apps/account/admin_portal/assets/assistant/` are
declared in the **base** `manifest.json`, never a feature manifest.
`admin_assets.PRIVATE_ASSETS = load_manifest()` runs at import and raises on a
declared-but-missing file, so **a manifest entry and the file it names must land
in the same commit** or `account.rest` stops importing.

---

## Module layout

| Module | Owns |
|---|---|
| `panel.js` | The aside, the launcher, docked/sheet modes, the focus trap, open state |
| `conversation.js` | History, the live turn, the composer |
| `transport.js` | The one WebSocket. No DOM |
| `blocks.js` | Block validators and renderers |
| `markdown.js` | The bounded markdown subset |
| `plan.js` | The plan tracker |
| `approval.js` | The whole approval seam |
| `setup.js` | The owner-only setup view |
| `assistant.css` | Every style, from the shell's own tokens |

`assistant.css` is linked from `index.html` beside the foundation sheets.
Feature stylesheets load through `registry.js:installFeatureStyles`, which the
panel does not use.

### Docked is not a dialog; the narrow sheet is

Above 1100px the aside is `role="complementary"` and is **not** focus-trapped:
it is a peer region, and trapping focus in a docked panel strands keyboard users
away from the page they are working on. At or below 1100px it becomes
`role="dialog" aria-modal="true"`, focus-trapped, Escape closes and returns
focus to the launcher. A `matchMedia` listener switches roles, the trap and the
grid together.

`z-index: 60` puts the panel below the busy scrim (90), the modal scrim (100),
the overlay/inspector scrims (110) and the modal scrim and toasts (120). A
fresh-auth prompt is a modal, so it is never obscured.

`#app` is `aria-live="polite"`, so the aside and the thread both carry
`aria-live="off"` — otherwise a screen reader narrates every streamed token.
Turn boundaries are announced once each through `announce()`.

---

## Transport rules

* **One correlation owner.** `send_event_to_user` fans an event out to *every*
  socket the user holds, so a second Admin tab's turn arrives here too. Every
  inbound `assistant_*` event whose `request_id` this transport did not mint is
  dropped.
* **One terminal outcome.** `assistant_response` or `assistant_error` resolves a
  turn. Nothing else does.
* **Keep-alive is mandatory.** The realtime consumer closes an authenticated
  socket after `AUTH_IDLE_TIMEOUT_SECONDS = 30` of *client* silence, and
  `last_activity` is stamped on inbound messages only — server→client events do
  not count. The transport sends `{"action": "ping"}` every 12s; two consecutive
  missing pongs trigger a reconnect.
* **Watchdog, not a verdict.** After 240s with no event of any kind the turn is
  resolved as an error saying it *may still be running*, with a reload control.
  A late terminal event is still accepted and appended, because the server did
  finish.
* **Backoff.** 1, 2, 4, 8, 16, 30s while the panel is open, stopping after 8
  consecutive failures. Close code `4429` starts at the top of the ladder: it is
  a deliberate pre-accept rejection, not a network blip. An authentication
  failure is terminal and removes the panel.
* **No cancel.** The server exposes no cancellation and the agent runs on a
  detached daemon thread. The composer is re-enabled by a terminal outcome and
  by nothing else; no control claims to stop a turn. Closing the panel mid-turn
  keeps the socket open until that turn resolves.

There is no REST fallback for a turn. `POST /api/assistant` runs the entire
multi-turn agent loop inside one request, and a second transport would be a
second correlation owner.

---

## Rendering

### The markdown subset

Fenced code, inline code, `**bold**`, `*`/`_` italic, `#`–`######` headings
clamped to `<h4>`–`<h6>` (so a panel heading never outranks the page `<h1>`),
`-`/`*` and `1.` lists nested at most two deep, `>` blockquote, `---` rule,
paragraphs and line breaks.

**Links and images are deliberately excluded** and render as their literal
text. Model prose is influenced by tool output the model read, so an
assistant-authored clickable URL is an injection surface this panel does not
open. The `file` block is held to the same rule — see its row below. Raw HTML is literal text. Input is truncated at 100 000 characters with a
visible note, and any line over 4 000 characters is emitted as plain text with
no inline scanning.

Every node is built with `createElement` + `textContent`. **No module under
`assets/assistant/` may contain `innerHTML`**, and a default-tier test enforces
that.

### Block validators

Each renderer validates first and returns `null` on a malformed block; the
caller then draws one muted line naming the type it could not read, so a bad
block is visible rather than silently dropped.

| Type | Bounds |
|---|---|
| `table` | ≤ 12 columns, ≤ 200 rows with a "showing the first 200 of N" note, inside the shared `.table-wrap` scroller |
| `chart` | `chart_type` in `line\|bar\|pie\|area`; ≤ 60 labels; every series length must equal the label count; ≤ 8 series; non-finite values are gaps, never zeros; a `color`/`colors` entry is honoured only when it matches `/^#[0-9a-f]{3,8}$/i` |
| `stat` | ≤ 12 `{label, value}` items |
| `list` | ≤ 60 `{label, value}` items |
| `alert` | `level` in `info\|success\|warning\|error`, non-empty `message`; `role="status"` for info/success, `role="alert"` for warning/error |
| `file` | `filename` and `url` required; the anchor is drawn **only for a same-origin `https:` URL** with no embedded credentials. Every other host — a storage backend, a shortlink on another domain — renders as copyable text with its hostname named. A `file` block is model-emittable (`file` is in the server's `VALID_BLOCK_TYPES` and `_validate_block` only checks truthiness), so a URL the model read out of a tool result must never become a click target in a superuser's console |
| `context` | ≤ 40 references rendered as inert chips — there is no route mapping in v1, because guessing an Admin route from a model string produces links that 404 |
| `progress` | Accepted defensively; the live tracker is fed by the `assistant_plan` / `assistant_plan_update` events, and nothing emits a `progress` block |
| `approval` | Delegated whole to `approval.js` — see [Approvals](../../assistant/approvals.md) |
| `action` | The legacy quick-reply, delegated to `approval.js`; carries no authority |

The chart palette (`--chart-1` … `--chart-6`) is scoped to `.assistant-panel`
because the platform palette lives inside the platform feature stylesheet,
which is not installed for every operator.

---

## The setup surface

`services/assistant_setup.py` is the only writer for the seven Assistant keys —
the Assistant's own flag, model and key, the platform key every LLM feature
uses, and the remote agent access switch —
and `rest/admin_assistant.py` is its only boundary. See
[the assistant application docs](../../assistant/README.md#admin-setup-surface)
for the keys, the protection, and the resolution precedence.

Four capabilities ride in the Admin bootstrap:

| Capability | Source |
|---|---|
| `assistant` | `view_admin` **alone** — the WebSocket handler admits nothing else |
| `assistant_ready` | `assistant_setup.is_ready()` — the feature is on and a credential resolves |
| `assistant_setup` | `request.user.is_superuser` — the writer's own predicate |
| `assistant_mcp` | `assistant_setup.mcp_ready()` — remote agent access is on, the assistant app is installed, and a public address resolves |

`admin_features/assistant.py` computes `enabled` from the authority value alone;
folding installation readiness into it would mount a panel whose every message
the socket refuses. `assistant_ready` and `assistant_mcp` both travel on as
namespace capabilities (`ready`, `mcp`) for the panel to render with, and
neither feeds `enabled`.

---

## Remote agent access

The **switch** is `ASSISTANT_MCP_ENABLED`. It ships off, is written only by
`assistant_setup.save(..., mcp_enabled=...)` through the `_protected_writer`
path, and is read live on every request — so flipping it takes effect on every
node without a restart, in both directions. `None` (an absent field, or a JSON
`null`) leaves it alone: the shipped page sends the field on every save, but a
tab that outlived a deploy must not be able to close the door by accident. Any
other non-boolean is refused before the transaction opens.

It is deliberately independent of `LLM_ADMIN_ENABLED` and of every API key — a
remote client brings its own model — and switching it off makes existing grants
**dormant, not revoked**: `oauth_server` refuses them while the resource is
disabled, the Admin still lists them, and turning the switch back on brings them
back.

### The self-check

`assistant_setup.check_discovery()` fetches this installation's **own**
`<BASE_URL>/.well-known/oauth-protected-resource<path>` through
`mojo.helpers.safe_fetch.safe_fetch`:

```python
safe_fetch(resources.prm_url(origin, path),
           timeout=5, max_bytes=65536, max_redirects=0,
           headers={"Accept": "application/json"},
           allow_hosts=[urlsplit(origin).hostname], schemes=("https",),
           resolver=resolver, transport=transport)
```

* `allow_hosts` is safe here because it covers the **initial URL only** — a
  redirect an attacker chose never inherits the exemption — and because
  `system_settings.validate_base_url` already refuses private literals and
  `localhost`. The exemption therefore only ever matters for a public name that
  resolves privately from *inside* the deployment (split-horizon DNS, the load
  balancer's internal address), which is exactly the self-probe case.
* `max_redirects=0`, and any redirect error is reported as `redirected`.
  Following a hop from a self-probe would be a port-scan primitive.
* `ok` requires the **document**, not merely a 200: a front door that serves the
  SPA's `index.html` for unknown paths answers 200 with HTML, so the verdict
  compares the body's `resource` against `canonical_url(origin, path)`.
* `origin`, `transport` and `resolver` are test seams. The REST layer passes
  none of them, so the probe can only ever target `public_origin()`.

Only **network** verdicts are cached (Redis, `assistant:mcp:discovery`, 60s).
That cache is the rate limit: `check_discovery()` serves it before making a
request. The record carries the resource URL it was probed for, and
`discovery_cached(expected)` discards a mismatch — so a `BASE_URL` changed in
System Setup (a different writer entirely) can never surface the old address's
verdict. `save()` drops the cache on commit whenever the switch is written, and
`mcp_state()` returns the unchecked record while the switch is off, so a flip
made from the deployment file or on another node never shows a stale answer.
Redis being down costs the throttle, not the verdict.

### nginx

The framework does not own the consumer's front door — which is precisely why
the self-check exists. Both discovery documents are served by the application
and must be forwarded to it:

```nginx
# Forward the OAuth discovery documents to the application. Only the
# path-suffixed forms exist; the root forms stay free for another product.
location /.well-known/oauth-authorization-server/ {
    proxy_pass http://<the same upstream as /api/>;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}
location /.well-known/oauth-protected-resource/ {
    proxy_pass http://<the same upstream as /api/>;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}
```

If the server already has `location ^~ /.well-known/ { root …; }` — some ACME
layouts do — that prefix wins over a plain one, and these two must be declared
`^~` as well.

### Scoped to these two resources, by path

Remote agent access is one switch with two doors — the Assistant's MCP endpoint
and the REST API root — so the connected-agents list, its count and the
disconnect-all sweep all pass `resource_path=grant_paths()`
(`[mcp_path(), api_root()]`) to `oauth_server.list_grants` / `count_grants` /
`revoke_all_grants`. A list becomes ONE `OR` predicate, so the page still costs
one query, one honest `COUNT(*)` and one bulk `UPDATE`. This surface owns remote
agent access, not every OAuth resource the installation may protect, so a grant
issued for some other registered resource is neither shown here nor swept by
"Disconnect all".

Each row carries an **`access`** field derived from the grant's scopes —
`tools` (`mcp`), `api`, or `both` — computed by `assistant_setup._access_kind`
rather than by `list_grants`. "Tools" / "Full API" is this page's vocabulary;
the generic Admin API keeps returning raw `scopes`. The table renders it as an
**Access** column, with a note under the section: a Full API row can call every
API as that person, and the approval step does not apply to those calls.

Scoping is by **path**, never by the full resource URL: the URL embeds
`BASE_URL`, so matching on it would make a public-address change silently hide
grants that are still perfectly valid at the same endpoint (#2613's rule). The
SQL filter is a suffix match, which is a superset — `https://x/nested/api/…`
also ends with `/api/…` — so every caller re-confirms the parsed path in Python
before listing or revoking a row.

The list is bounded in SQL (`limit=MAX_GRANT_ROWS`, 200) rather than sliced in
Python, and `grant_count` is a separate `COUNT(*)` on the same predicate, so a
large grant table is never loaded to draw one page and the number stays honest
past the slice.

### Revocation

`assistant_setup.revoke_grant(actor, grant_id)` and `revoke_all_grants(actor)`
ride the existing POST boundary (fresh auth, same-Origin, and a live literal
superuser re-proven inside `system_settings.require_system_admin`) rather than a
new endpoint.

`revoke_grant_by_id` is the single-row path and randomises every column a live
credential resolves through, so the access token is refused at the door and the
refresh token at the token service on their next use.

`revoke_all_grants` is one bulk `UPDATE` over the scoped active set instead of a
per-row loop inside the request. Deactivating the row is what every credential
check actually reads — `validate_access` filters `is_active=True` and
`_check_refreshable` refuses an inactive grant — so the column rotation is not
needed for the sweep. Only ids and owner ids are read, never whole rows, and the
audit is **one `oauth:grant_revoked` line per affected user carrying that user's
count**: bounded by operators rather than by connections.

Both answer a count, never a 404: an unknown or already-dead id is `0`, and the
page repaints from the fresh state either way. The `_audit` incident event is
keyed **per grant id** so `report_event_suppressed`'s hourly `(category, key)`
dedupe cannot swallow a second grant's revocation, and carries `budget=50`
because a distinct key per id is exactly the unbounded-key case the budget
exists for.

The switch write is audited the same way, with the **direction in the key**
(`mcp_enabled:on` / `mcp_enabled:off`) plus an unsuppressed
`assistant:mcp_switch` line on the actor — otherwise on → off → on inside one
hour would file a single event that cannot say which way the door moved.

### Accepted properties

Two things this design deliberately keeps, recorded so a later reader does not
re-litigate them:

* **The self-check is a reachability oracle, and that is accepted.** It performs
  an HTTPS `GET` against exactly one host — the operator's own `BASE_URL`, never
  a caller-supplied address — and reports the status it answered with. The only
  caller who can reach it is a live literal superuser, who already owns the
  System Setup surface that sets `BASE_URL` in the first place. `allow_hosts`
  covers that one initial URL and no redirect hop, `schemes=("https",)`, and no
  response body or hostname ever reaches the operator-visible `detail`.
* **`assistant_mcp` is installation state published to every Admin reader.** It
  rides in the bootstrap capabilities exactly like `assistant_ready`, so any
  caller who can open the Admin learns whether remote agent access is on and
  reachable. It carries no address, no grant and no credential, and the panel
  needs it to render the chip; this is the same disclosure the existing
  readiness bit already makes.

---

## Previewing it

`bin/admin_preview --assistant-state <configured|unset|fallback|verify_failed|disabled>`
`--assistant-mcp-state <off|reachable|unreachable|connected>`
serves deterministic fixtures for the setup view. The preview has no WebSocket
bridge, so the chat body renders in its "cannot reach the realtime service"
state — itself a state worth being able to look at. Fixture key hints are
exactly four characters and fixture grants carry no jti, hash or token, so a
real leak can never ship looking correct.
