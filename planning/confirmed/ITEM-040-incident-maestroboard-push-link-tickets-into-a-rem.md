---
# id is assigned by /scope on pickup — leave it blank
id: ITEM-040
type: feature
title: Incident MaestroBoard — push/link tickets into a remote maestro board
priority: P2
effort: L
owner: backend
opened: 2026-07-12
depends_on: []         # soft: maestro repo ships the link API first (see links)
related: []
links: ["maestro/api planning/confirmed/maestro-connect.md (contract plan-of-record)"]  # superseded by maestro's docs/web_developer/boards/linking.md once built
---

# Incident MaestroBoard — push/link tickets into a remote maestro board

## What & Why

Maestro (the hub) is growing a **board link API**: a workspace admin mints a
board-scoped link key, and a remote project can push tickets into that board and
receive signed webhooks when board-side changes happen. This item is the **client
half**, living in the incident app (every project already installs it): register a
pasted link, push `incident.Ticket` rows into the remote board, keep linked tickets
in sync both directions, and let incident rules auto-push.

Why the incident app: Ticket/TicketNote already have every attachment point the
bridge needs (`on_rest_saved` with old-value `changed_fields`, `add_note()`,
`POST_SAVE_ACTIONS` dispatch), and the rules engine is the auto-push hook.

The server half is scoped and building in the maestro repo
(`maestro/api/planning/confirmed/maestro-connect.md`). The wire contract this side
builds against is maestro's `docs/web_developer/boards/linking.md` (versioned
`{"v": 1}` payloads). Key protocol facts, decided there:

- Auth to maestro: `Authorization: linkkey <token>` — the raw key from the pasted
  link URL (`https://maestromojo.com/api/boards/link/<key>`), scoped to exactly one
  board, fail-closed.
- Endpoints: `POST /api/boards/link/register` (validates key, submits this
  project's callback URL, returns board schema), `link/item` (create),
  `link/item/<int:pk>` (update), `link/note` (comment; item id in body).
- Inbound webhooks from maestro are signed with `X-Mojo-Signature` =
  `mojo.helpers.crypto.sign.generate_signature(payload_dict, raw_link_key)`
  (canonical dict form — verify with `verify_signature(json.loads(request.body),
  sig, key)`, not raw bytes).
- Fail-open client: ticket saves never block or raise on maestro problems — all
  remote calls via the jobs app, retry-small-N then drop with a local log.

## Acceptance Criteria

- [ ] `MaestroBoard(MojoSecrets, MojoModel)` — one model per file
      (`mojo/apps/incident/models/maestro_board.py`); stores endpoint + link key
      (secret), cached board name/schema, `group`, `is_active`. Registering =
      pasting the link URL; save validates against `link/register` and submits the
      project's callback URL
- [ ] `MaestroBoardLink` (`models/maestro_board_link.py`) — `ticket` FK,
      `maestro_board` FK, `remote_item_id`, `last_synced`; unique together
      (ticket, maestro_board)
- [ ] `Ticket` gains `POST_SAVE_ACTIONS` entry `push_to_board` (value =
      MaestroBoard id) → async push via jobs; creates the link row + a ticket note
      with the remote item URL. Push is idempotent (existing link → update, not
      duplicate)
- [ ] Linked-ticket saves (title/status changes, new non-sync notes) enqueue remote
      updates/comments; failures retry small-N then drop with a local log — never
      an exception to the caller, never blocking the save
- [ ] Webhook receiver endpoint (`rest/maestro_webhook.py`, no session auth)
      verifies the signature, updates the ticket: always a ticket note describing
      the board change + comments; ticket `status` changes only when the
      MaestroBoard's optional `status_map` is configured
- [ ] Sync updates appear as system ticket notes (`user=None`,
      `metadata.origin="maestro"`) — no human identity on the link channel
- [ ] Echo suppression proven by test: a synced change does not bounce back
      (origin markers + compare-before-write on both sides)
- [ ] Incident rules integration: a rule action can create a ticket and push it to
      a configured MaestroBoard (exact hook point into the Rule/RuleSet engine
      decided in /scope)
- [ ] `rest/maestro_board.py` CRUD, `manage_security`-gated (or the perm /scope
      lands on — fail-closed either way)
- [ ] `services/maestro_sync.py` — register, push_ticket, sync_ticket_change,
      handle_board_webhook; business logic in services, handlers thin
- [ ] Docs both tracks + CHANGELOG entry

## Repro — bugs only
1.
- Expected:
- Actual:

## Plan

### Goal
Add the client half of maestro's board-link protocol to the incident app: register
a pasted board link, push tickets to the remote board (manually via a save action,
automatically on linked-ticket changes, and from rules), and apply signed inbound
webhooks to tickets — fail-open outbound, fail-closed inbound.

### Context — what exists

**Wire contract (maestro side — PLAN-OF-RECORD ONLY).** Nothing is built in the
maestro repo yet; the contract source is
`/Users/ians/Projects/mojo/maestro/api/planning/confirmed/maestro-connect.md`
(scoped 2026-07-12). `docs/web_developer/boards/linking.md` there does not exist
yet — when it lands it supersedes this summary; re-verify field names before
close. Contract:

- Paste format: single URL `https://maestromojo.com/api/boards/link/<key>` —
  nothing served at that path; client parses it into API base + raw key. Key is
  `crypto.random_string(48)`, scoped to exactly one board, revocable.
- Auth on all link endpoints: header `Authorization: linkkey <token>`. Fail-closed
  server: unknown/revoked key → 401; wrong board/link's item → 403/404; invalid
  payload → 400 `ValueException`. Per-link item ownership: only the creating link
  may update/comment an item.
- Endpoints (all POST, all payloads carry `{"v": 1}`):
  - `POST <base>/api/boards/link/register` — body: `{"v": 1, "callback_url": "<http(s) url>"}`;
    response: `{"v": 1, "label": "<link label>", "board": {"id": ..., "name": "...", "columns": [...]}}`
  - `POST <base>/api/boards/link/item` — create. Required `title` (≤255,
    truncated). Optional `description`, `values` (dict keyed by column slug,
    validated against board columns), `source` dict `{project, ticket_id, url}`.
    Response: `{"id": ..., "url": "/workspaces/board/<board>?item=<id>"}`.
  - `POST <base>/api/boards/link/item/<int:pk>` — update; same fields
    (title/description/values). 404 unknown item, 403 wrong link.
  - `POST <base>/api/boards/link/note` — body `{"v": 1, "item": <id>, "text": "<≤10k chars>"}`.
- Board columns (what `register` returns in `board.columns`): list of
  `{"slug", "name", "type", "options"?}`; seven types: text, number, category,
  person, date, checkbox, project. **There is no fixed status field on a board
  item** — "status" is whatever `category` column the board defines, stored in
  item `values`; category options are `{value, label, color}`. Our `status_map`
  must therefore name a column slug AND a value map.
- Webhooks (maestro → our `callback_url`): POSTed via maestro's jobs with
  exponential backoff; **a 4xx from our receiver is terminal (no retry)**.
  Payload: `{"v": 1, "event": "...", "board": <remote_board_id>,
  "item": {"id", "title", "values", "is_active"}, "source": {project, ticket_id, url, link},
  ...event extras}`. Events:
  - `item.updated` — extra `changes: [{column, old, new}]` (title/description
    changes appear in it; position-only moves don't dispatch)
  - `item.archived`, `item.restored`
  - `note.created` — extra `note: {id, text, author}` (author = display name or "maestro")
- Signature: header `X-Mojo-Signature` = HMAC-SHA256 hex of the **canonical JSON
  of the payload dict** (`json.dumps(data, separators=(',',':'), sort_keys=True)`,
  UTF-8) keyed by the **raw link key**. This is exactly
  `mojo/helpers/crypto/sign.py` — `generate_signature(data, secret_key)` (line 21;
  dicts are canonicalized) / `verify_signature(data, signature, secret_key)`
  (line 42; constant-time). Verify the **parsed dict**, NOT raw bytes — do not use
  `mojo.helpers.request.verify_signed_request` (that one hashes raw body bytes).
- Echo suppression maestro-side: a write arriving via the link API produces no
  webhook back to that link; human portal edits do. Revocation is immediate.

**Incident app** (`mojo/apps/incident/`):
- Layout: `models/` (ticket.py holds BOTH Ticket and TicketNote — existing
  deviation), `rest/` (`__init__.py` star-imports each module), `handlers/`
  (event_handlers.py, ticket_actions.py, llm_agent.py), `asyncjobs.py`,
  `cronjobs.py`. **No `services/` dir yet — create it.**
  `models/__init__.py` uses explicit named exports.
- `Ticket` (`models/ticket.py:28-44`): `user`/`group` FKs (null), `title`
  (Char 255), `description` (Text null), `status` (Char 50, default 'open',
  indexed), `priority` (int), `category` (Char 80), `assignee` FK, `incident` FK,
  `metadata` (JSONField default dict). RestMeta (lines 11-26):
  `VIEW_PERMS = ["view_security", "security"]`,
  `SAVE_PERMS = ["manage_security", "security"]`,
  `POST_SAVE_ACTIONS = ["enable_llm", "disable_llm"]`.
- `Ticket.on_rest_saved(self, changed_fields, created)` (ticket.py:51): existing
  logic adds a status-change note. **`changed_fields` maps field name → OLD
  value** (new value is on the instance) — set at `mojo/models/rest.py:1274-1278`.
- `Ticket.add_note(self, note, user, metadata=None)` (ticket.py:90-95):
  `TicketNote.objects.create(parent=self, note=note, group=self.group, user=user, ...)`
  — plain ORM create, does NOT fire REST hooks.
- `TicketNote` (ticket.py:97-154): `parent` FK (related_name `notes`), `user` FK
  **nullable** (SET_NULL — system notes use `user=None`), `note` Text, `media` FK,
  `metadata` JSONField. `on_rest_saved` (lines 124-150): backfills group from
  parent; on create dispatches `metadata.action_response` actions and the LLM
  reply job (skips notes starting `"[LLM Agent]"`). Notes are created via REST at
  `POST /api/incident/ticket/note` with `parent=<ticket_id>`.
- POST_SAVE_ACTIONS mechanism (`mojo/models/rest.py:1295-1368`): action keys in
  the request body are held aside (never written to the model); after
  `atomic_save()` + `on_rest_saved`, dispatch is
  `handler = getattr(self, f'on_action_{key}'); handler(value)`
  (rest.py:1360-1364). Non-None return becomes the JSON response. Actions pass
  through `check_edit_permission` (SAVE_PERMS gate). Docs:
  `docs/django_developer/core/mojo_model.md:412-474`.
- REST hook asymmetry (load-bearing for echo suppression): `on_rest_saved` /
  `on_rest_created` fire ONLY in the REST save pipeline
  (`on_rest_save`/`create_from_request`/`update_from_dict`); plain
  `Model.objects.create()` / `instance.save()` bypass them entirely.
- Rules engine: `RuleSet.run_handler` (`models/rule.py:148-200`) splits
  `handler` on `re.split(r',(?=(?:job|email|sms|notify|ticket|block|llm|resolve)://)', ...)`
  (rule.py:169) and publishes each spec to
  `mojo.apps.incident.handlers.event_handlers.execute_handler` on channel
  `incident_handlers`. `HANDLER_MAP` at `handlers/event_handlers.py:566-575`;
  **`TicketHandler` at event_handlers.py:387-441 already creates tickets** from
  `ticket://?status=open&priority=8&title=...&assignee=<uid>` specs, deduping open
  tickets per rule_set (lines 421-426). Query params arrive as kwargs to
  `handler_cls(netloc, **query_params)`.
- REST handler pattern (`rest/ticket.py:5-14`): `@md.URL('ticket')` +
  `@md.URL('ticket/<int:pk>')` → `Model.on_rest_request(request, pk)`. URL prefix
  is the app dir → `/api/incident/...`. Per `.claude/rules/rest.md` add
  `@md.uses_model_security(Model)` on new RestMeta endpoints (existing incident
  modules predate the rule and omit it).

**Framework building blocks:**
- `MojoSecrets` (`mojo/models/secrets.py:7`): inherit
  `class X(MojoSecrets, MojoModel)` (no `models.Model`). API (secrets.py:16-46):
  `set_secret(key, value)` / `get_secret(key, default=None)` — work pre-save in
  memory; encryption key is per-row, encrypted blob saved in `save()`.
  `mojo_secrets` is unconditionally excluded from serialization
  (`mojo/serializers/core/serializer.py:198`); still add `"exclude": ["mojo_secrets"]`
  to graphs (belt-and-suspenders, cf. `mojo/apps/phonehub/models/config.py:80`).
  Custom-setter routing: a REST body key `paste_url` automatically calls
  `set_paste_url(value)` if defined (`mojo/models/rest.py:1370-1374`; example
  `phonehub/models/config.py:172`).
- Jobs (`mojo/apps/jobs/__init__.py:39`):
  `jobs.publish("dotted.path.func", {"id": 42}, channel="...", max_retries=N, backoff_base=2.0)`.
  Handler = plain function `def f(job):` with `job.payload`; raising or returning
  `"failed"` retries while `attempt < max_retries`
  (`mojo/apps/jobs/job_engine.py:690-700`). Payloads: ids only, never objects.
  Incident already uses inline `from mojo.apps import jobs; jobs.publish(...)`
  (ticket.py:75-76) on channel `incident_handlers`.
- Public endpoint: `@md.public_endpoint()` (`mojo/decorators/auth.py:183`) — audit
  marker, no auth decorators. Raw body: `request.body.decode("utf-8")` →
  `json.loads` (example `mojo/apps/aws/rest/sns.py:183-192`). No CSRF middleware
  in the generated test project.
- Outbound HTTP: no framework wrapper — use `requests` directly with bounded
  `timeout`, `allow_redirects=False`, catch `requests.Timeout`/`Exception`, and do
  NOT echo remote error bodies into user-visible fields (template:
  `mojo/apps/phonehub/services/mojo_provider.py:54-80`).
- Base URL: `settings.get("BASE_URL", "")` is the established project base-URL
  setting (`from mojo.helpers.settings import settings`). Allow
  `settings.get("MAESTRO_CALLBACK_BASE")` override, falling back to `BASE_URL`.
- Logging: `from mojo.helpers import logit`; named logger
  `logger = logit.get_logger("incident", "incident.log")`.
- Random token: `mojo.helpers.crypto` `random_string(48, allow_special=False)`
  (same call maestro uses for keys — verify exact import path in
  `mojo/helpers/crypto/` when building).
- Tests: `tests/test_incident/` is `serial: True` + `requires_extra: ["slow"]` —
  opt-in, NOT in the default suite. New tests for this item must go in a **new
  module** so they run by default. Outbound-HTTP faking pattern:
  `mock.patch.object(module, "requests")` and call the service/job function
  in-process (`tests/test_jobs/test_signed_webhook.py:108-137` — remember to
  restore `mock_requests.exceptions = real_requests.exceptions`). Job-enqueue
  assertion pattern: `Job.objects.filter(func="...").order_by("-created").first()`
  (`tests/test_incident/test_analyze_action.py:35-73`). Setup must clean up
  before creating (long-lived DB).

### Changes — what to do

1. `mojo/apps/incident/models/maestro_board.py` (new) — `MaestroBoard(MojoSecrets, MojoModel)`:
   - Fields: `created`/`modified` (per `.claude/rules/models.md`), `group` FK
     (account.Group, null=True, SET_NULL), `name` (Char 200, cached remote board
     name), `api_url` (Char 255, API base parsed from paste URL), `remote_board_id`
     (int, null), `schema` (JSONField default dict — cached `board.columns`),
     `status_map` (JSONField null/blank — shape
     `{"column": "<slug>", "map": {"<ticket_status>": "<option_value>"}}`),
     `sync_notes` (bool default True), `callback_token` (Char 64, unique,
     db_index, generated on first save via crypto random string), `is_active`
     (bool default True), `metadata` (JSONField default dict).
   - Secret: raw link key via `set_secret("link_key", key)` / `get_secret("link_key")`.
   - `set_paste_url(self, value)` custom setter: parse
     `https://<host>/api/boards/link/<key>` → store `api_url = https://<host>`,
     `set_secret("link_key", key)`, flag `self.__needs_register__ = True`. Raise
     `ValueException(400)` on malformed URL.
   - `on_rest_pre_save(self, changed_fields, created)`: generate `callback_token`
     if missing; if `created` or `__needs_register__`, call
     `services.maestro_sync.register(self)` synchronously — on success cache
     `name`/`remote_board_id`/`schema`; on any failure raise `ValueException(..., 400)`
     so nothing persists (fail-closed registration).
   - `on_action_refresh_schema(self, value)`: re-run register, return refreshed
     board info dict.
   - RestMeta: `VIEW_PERMS = ["manage_security", "security"]`,
     `SAVE_PERMS = ["manage_security", "security"]`,
     `DELETE_PERMS = ["manage_security"]`, `POST_SAVE_ACTIONS = ["refresh_schema"]`,
     `GRAPHS = {"default": {"exclude": ["mojo_secrets"], "graphs": {"group": "basic"}}}`.
     Never expose the key in any graph or response.
2. `mojo/apps/incident/models/maestro_board_link.py` (new) — `MaestroBoardLink(models.Model, MojoModel)`:
   `created`/`modified`, `ticket` FK (incident.Ticket, CASCADE, related_name
   `board_links`), `maestro_board` FK (CASCADE, related_name `links`),
   `remote_item_id` (int), `remote_url` (Char 500, blank), `last_synced`
   (DateTime null). `Meta.unique_together = [("ticket", "maestro_board")]`.
   RestMeta: `VIEW_PERMS = ["view_security", "security"]`, `SAVE_PERMS = None`
   (no REST create/update), `DELETE_PERMS = ["manage_security"]` (delete =
   unlink), `CAN_DELETE = True`.
3. `mojo/apps/incident/models/__init__.py` — add
   `from .maestro_board import MaestroBoard` and
   `from .maestro_board_link import MaestroBoardLink`.
4. `mojo/apps/incident/services/__init__.py` (new, empty) +
   `mojo/apps/incident/services/maestro_sync.py` (new) — ALL wire shapes and
   business logic live here (single file to fix on contract drift):
   - `parse_paste_url(url)` → `(api_base, key)`.
   - `get_callback_url(board)` →
     `settings.get("MAESTRO_CALLBACK_BASE") or settings.get("BASE_URL", "")` +
     `/api/incident/maestro/webhook/<callback_token>`; raise if no base configured.
   - `_post(board, path, payload)` — `requests.post(f"{board.api_url}/api/boards/{path}",
     json={"v": 1, **payload}, headers={"Authorization": f"linkkey {board.get_secret('link_key')}"},
     timeout=10, allow_redirects=False)`; normalize errors, never echo remote
     bodies to users.
   - `register(board)` — POST `link/register` with callback_url; on 2xx cache
     name/remote_board_id/schema onto the instance (caller saves); else raise
     `ValueException`.
   - `build_item_payload(board, ticket)` — `title` (truncate 255), `description`,
     `values` from `status_map` if configured (skip + log unknown slug/option),
     `source = {"project": settings.get("PROJECT_NAME", ""), "ticket_id": ticket.pk,
     "url": <BASE_URL ticket url if derivable, else "">}`.
   - `push_ticket(board, ticket)` — idempotent: existing `MaestroBoardLink` →
     POST `link/item/<remote_item_id>` update; else POST `link/item`, create the
     link row (`remote_item_id`, `remote_url` from response), stamp `last_synced`,
     and `ticket.add_note(f"Pushed to maestro board '{board.name}': <remote_url>",
     user=None, metadata={"origin": "maestro", "type": "board_link"})`.
   - `sync_ticket_change(link, changed)` — POST `link/item/<pk>` with only the
     changed subset (title/description/status-mapped values); stamp `last_synced`.
   - `push_note(link, note)` — POST `link/note` `{"item": remote_item_id,
     "text": note.note[:10000]}`.
   - `handle_board_webhook(board, payload)` — dispatch by `payload["event"]`
     (see change 8). All ticket writes here use direct ORM saves
     (`save(update_fields=...)`, `objects.create`) — never the REST pipeline.
   - `enqueue_push(ticket_id, board_id)` / `enqueue_sync(link_id, changed_keys)` /
     `enqueue_note(link_id, note_id)` — thin wrappers around `jobs.publish(...)`
     with `channel="incident_handlers"`, `max_retries=3`, `backoff_base=2.0`.
5. `mojo/apps/incident/asyncjobs.py` — add job handlers `maestro_push_ticket(job)`,
   `maestro_sync_change(job)`, `maestro_push_note(job)`: load objects by id from
   `job.payload` (skip silently if deleted/inactive), call the service, re-raise
   request errors so the jobs engine retries (max 3), and on the final attempt
   log via `logit` and return `"failed"` — never propagate to any caller
   (fail-open).
6. `mojo/apps/incident/models/ticket.py` —
   - `Ticket.RestMeta.POST_SAVE_ACTIONS` → `["enable_llm", "disable_llm", "push_to_board"]`.
   - `Ticket.on_action_push_to_board(self, value)`: look up active `MaestroBoard`
     by pk=value; reject (return `{"status": False, "error": ...}`-style
     `ValueException`) if missing, inactive, or `board.group_id` is set and ≠
     `self.group_id` (fail-closed); else `enqueue_push(self.pk, board.pk)` and
     return `{"status": True, "queued": True}`.
   - `Ticket.on_rest_saved`: after existing status-note logic, if `not created`
     and `{"title", "description", "status"} & set(changed_fields)` and
     `self.board_links.exists()` → `enqueue_sync(link.pk, changed_keys)` per link
     (board must be active). REST-pipeline-only hook = webhook-applied writes
     can't re-trigger this (echo-safe).
   - `TicketNote.on_rest_saved`: on create (before/alongside LLM dispatch), if
     `(self.metadata or {}).get("origin") != "maestro"` and parent has links whose
     board is active with `sync_notes=True` → `enqueue_note(link.pk, self.pk)`.
7. `mojo/apps/incident/rest/maestro_board.py` (new) — CRUD endpoints, registered
   via `rest/__init__.py` star-import:
   ```python
   @md.URL('maestro/board')
   @md.URL('maestro/board/<int:pk>')
   @md.uses_model_security(MaestroBoard)
   def on_maestro_board(request, pk=None):
       return MaestroBoard.on_rest_request(request, pk)
   ```
   plus `maestro/link` + `maestro/link/<int:pk>` → `MaestroBoardLink.on_rest_request`.
8. `mojo/apps/incident/rest/maestro_webhook.py` (new) —
   ```python
   @md.POST('maestro/webhook/<str:token>')
   @md.public_endpoint()
   def on_maestro_webhook(request, token=None):
   ```
   Look up `MaestroBoard` by `callback_token=token, is_active=True` → 401 if
   missing. Parse `json.loads(request.body)`; read the signature header
   (`sign.get_signature_header()` → `X-Mojo-Signature`); verify with
   `verify_signature(payload, sig, board.get_secret("link_key"))` → 401 on
   mismatch (4xx is terminal to maestro — correct for bad sig). Resolve
   `MaestroBoardLink` by `(maestro_board=board, remote_item_id=payload["item"]["id"])`;
   unknown → `{"status": True, "ignored": True}` (200; keeps maestro's queue
   quiet, logged). Then in the service:
   - every event → system note via `ticket.add_note(<human-readable description
     of the change/comment>, user=None, metadata={"origin": "maestro",
     "event": <event>, ...ids})`
   - `note.created` → note text includes author display + comment text
   - `item.updated` → if `board.status_map` configured and a `changes` entry
     matches `status_map["column"]`, reverse-map the new option value to a ticket
     status; compare-before-write; `ticket.save(update_fields=["status", "modified"])`
     (direct ORM — no REST hooks, no outbound echo). Reverse-map miss → note only.
   - `item.archived` / `item.restored` → note only (v1).
   Return `{"status": True}`.
9. `mojo/apps/incident/handlers/event_handlers.py` — extend `TicketHandler`
   (lines 387-441): accept optional `board` query param; after `Ticket.objects.create`
   (create path only, not the dedupe-hit path), validate the board (exists,
   active) and `enqueue_push(ticket.pk, board_id)`; log + skip on invalid board.
   Rules opt in with e.g. `handler="ticket://?priority=9&board=3"`. No new scheme,
   no regex/HANDLER_MAP change.
10. Run `bin/create_testproject` (new models → migrations), then tests.
11. Docs + CHANGELOG (see Docs).

### Design decisions
- **Register synchronously at save; everything else async** — the admin pasting a
  bad link must see the failure immediately (fail-closed setup); ticket-path calls
  must never block or raise (fail-open sync). Rejected: async registration (silent
  broken boards).
- **`callback_token` instead of pk in the webhook URL** — solves the
  callback-URL-needed-before-pk-exists chicken-egg at registration (token
  generated in `on_rest_pre_save`), and makes the public endpoint unguessable on
  top of the signature.
- **Echo suppression via the ORM-vs-REST hook asymmetry**, not transient flags:
  webhook-applied writes use direct ORM saves which never enter the REST hook
  pipeline, so they cannot enqueue outbound sync; maestro suppresses the other
  direction (link-originated writes don't dispatch back). Compare-before-write on
  status guards repeated deliveries. Rejected: `__maestro_origin__` transient
  markers (unnecessary given the asymmetry).
- **Rules hook = `board` param on the existing `ticket://` handler** — rejected a
  new `maestroboard://` scheme (touches the split regex at rule.py:169, the scheme
  list at rule.py:174, and HANDLER_MAP for no gain).
- **`manage_security` for both VIEW and SAVE on MaestroBoard** — it holds a
  credential; a view/manage split is not warranted for an admin-only config
  object. Domain perm `security` included per `.claude/rules/models.md`.
- **Naming stays `MaestroBoard`** — protocol, signature helper, and paste format
  are maestro-specific in v1; a neutral rename later is cosmetic. (Approved.)
- **Sync scope (approved):** title + description + notes always; ticket `status`
  ↔ board category column only when `status_map` is configured. Notes→comments on
  by default with per-board `sync_notes` opt-out; sync-origin notes excluded.
- **Verify webhooks with `verify_signature` on the parsed dict** per contract —
  NOT `verify_signed_request` (raw-bytes helper); both sides hash canonical JSON
  so wire encoding is irrelevant.
- All wire shapes isolated in `services/maestro_sync.py` so contract drift (the
  maestro side isn't built yet) is a one-file fix.

### Edge cases & risks
- **Contract drift** — maestro's `linking.md` doesn't exist yet; field names
  (`text` vs `note`, response envelopes) must be re-verified against it before
  close if it has landed. Mitigation: one-file wire layer + this plan quotes the
  plan-of-record shapes.
- **Duplicate item on retried create** (create succeeded, response lost): job
  handler re-checks for an existing link row before creating (idempotent
  re-entry); residual duplicate-comment risk on note retries accepted for v1.
- **Revoked/inactive key** → maestro 401s; handler treats 4xx as terminal
  (return `"failed"` without retry), logs locally. Ticket saves unaffected.
- **Group mismatch / no group**: `push_to_board` rejects when `board.group_id` is
  set and differs from the ticket's (fail-closed); board with `group=None` accepts
  any ticket.
- **`status_map` misconfigured** (unknown slug/option): outbound — omit the value
  + log; inbound — reverse-map miss → note only, status untouched.
- **No BASE_URL configured** → registration fails with a clear 400 (callback URL
  is required by the contract).
- **Webhook for a deleted/unlinked ticket** → 200 `{"ignored": true}` + log
  (4xx would terminally kill legitimate retries; nothing sensitive is revealed).
- **LLM interplay**: maestro-origin notes have `user=None` and origin metadata but
  are created via `add_note` (ORM) → no REST hooks → cannot trigger the LLM reply
  job or action dispatch.

### Tests
New module `tests/test_maestro_board/` with
`TESTIT = {"requires_apps": ["mojo.apps.incident", "mojo.apps.jobs"]}` — runs in
the **default** suite (unlike `test_incident`, which is opt-in `--extra slow`).
Setup cleans up (delete by title/name prefix) before creating; boards created via
ORM get `set_secret("link_key", <known key>)` so tests can sign payloads.

- Service level (in-process, `mock.patch.object(maestro_sync, "requests")`):
  - `parse_paste_url` good/bad URLs → parsed / `ValueException`
  - `register` 2xx → name/remote_board_id/schema cached; non-2xx/timeout → raises
  - `push_ticket` no link → `link/item` POST + link row + system note with remote
    URL; existing link → `link/item/<pk>` update, no duplicate row (idempotency)
  - `sync_ticket_change` sends only changed fields; `status_map` maps status →
    `values[column]`; unknown option omitted
- REST fail-closed: `opts.client` POST create board with malformed paste URL →
  400 and no row persisted (registration failure path exercises the real
  pre-save gate; unreachable `api_url` → connection error → 400).
- Action: REST save on a ticket with `push_to_board=<board.pk>` → a
  `jobs.models.Job` row with `func="mojo.apps.incident.asyncjobs.maestro_push_ticket"`;
  group-mismatch or inactive board → error response, no Job row.
- Outbound sync triggers: REST title edit on a linked ticket → sync Job enqueued;
  REST note create → note-push Job; REST note with `metadata.origin="maestro"` →
  no Job.
- **Echo suppression (acceptance-criteria test)**: apply
  `handle_board_webhook` (status change + note.created) directly, then assert
  ZERO new maestro-outbound Job rows were created.
- Webhook endpoint E2E via `opts.client.post("/api/incident/maestro/webhook/<token>", ...)`
  with a real `generate_signature(payload, key)`:
  - `note.created` → TicketNote appears with `user=None`,
    `metadata.origin="maestro"`
  - bad signature → 401, no note; unknown token / inactive board → 401
  - `item.updated` with `status_map` → ticket.status changed + system note;
    without `status_map` → note only, status unchanged
  - unknown remote item id → 200 `{"ignored": true}`
- Rules: instantiate `TicketHandler` with `board=<pk>` in-process, run against a
  test event/incident → ticket created AND push Job enqueued; invalid board →
  ticket still created, no Job, logged.
- Permissions: authenticated user without `manage_security` → denied on
  `maestro/board` CRUD; `view_security`-only user can list `maestro/link` but not
  delete.

### Docs
- `docs/django_developer/` — new page (place alongside the existing incident docs;
  check the README index for the incident section): MaestroBoard model + secrets,
  registration flow, `BASE_URL`/`MAESTRO_CALLBACK_BASE` settings, `status_map`
  shape, `sync_notes`, rules `ticket://?board=` param, job/retry semantics.
  Update `docs/django_developer/README.md` index.
- `docs/web_developer/` — endpoints doc: `maestro/board` CRUD (+ `refresh_schema`
  action), `maestro/link` list/delete, `push_to_board` ticket action, webhook
  receiver contract (signature, events, responses). Update that README index.
- `CHANGELOG.md` — feature entry.

### Open questions
None — all four (naming, sync scope, notes-default, rules hook) resolved and
approved 2026-07-12; decisions recorded under Design decisions.

## Notes

Origin: maestro repo `planning/confirmed/maestro-connect.md` (scoped 2026-07-12) —
that file carries the full two-repo investigation (what exists, constraints,
endpoints, test matrix). Sequencing: this side can be scoped now, but its tests
need maestro's link API deployed (or a local maestro dev server) to run
end-to-end; the receiver + push paths can be tested against a stub first.

## Resolution
- closed: YYYY-MM-DD
- branch:
- files changed:
- tests added:
