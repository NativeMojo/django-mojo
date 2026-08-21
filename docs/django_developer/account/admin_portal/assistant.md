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
open. Raw HTML is literal text. Input is truncated at 100 000 characters with a
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
| `file` | `filename` and `url` required; the anchor is drawn only for an absolute `https:` URL with no embedded credentials, and the **destination hostname is shown beside the filename**; anything else degrades to copyable text |
| `context` | ≤ 40 references rendered as inert chips — there is no route mapping in v1, because guessing an Admin route from a model string produces links that 404 |
| `progress` | Accepted defensively; the live tracker is fed by the `assistant_plan` / `assistant_plan_update` events, and nothing emits a `progress` block |
| `approval` | Delegated whole to `approval.js` — see [Approvals](../../assistant/approvals.md) |
| `action` | The legacy quick-reply, delegated to `approval.js`; carries no authority |

The chart palette (`--chart-1` … `--chart-6`) is scoped to `.assistant-panel`
because the platform palette lives inside the platform feature stylesheet,
which is not installed for every operator.

---

## The setup surface

`services/assistant_setup.py` is the only writer for the four assistant keys,
and `rest/admin_assistant.py` is its only boundary. See
[the assistant application docs](../../assistant/README.md#admin-setup-surface)
for the keys, the protection, and the resolution precedence.

Three capabilities ride in the Admin bootstrap:

| Capability | Source |
|---|---|
| `assistant` | `view_admin` **alone** — the WebSocket handler admits nothing else |
| `assistant_ready` | `assistant_setup.is_ready()` — the feature is on and a credential resolves |
| `assistant_setup` | `request.user.is_superuser` — the writer's own predicate |

`admin_features/assistant.py` computes `enabled` from the authority value alone;
folding installation readiness into it would mount a panel whose every message
the socket refuses.

---

## Previewing it

`bin/admin_preview --assistant-state <configured|unset|fallback|verify_failed|disabled>`
serves deterministic fixtures for the setup view. The preview has no WebSocket
bridge, so the chat body renders in its "cannot reach the realtime service"
state — itself a state worth being able to look at. Fixture key hints are
exactly four characters, so a real leak can never ship looking correct.
