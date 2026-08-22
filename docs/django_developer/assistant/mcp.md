# Assistant MCP transport

The Assistant's tools are reachable by a remote AI client over **MCP (Model
Context Protocol)** at one endpoint:

```
POST /api/assistant/mcp
```

It is a **stateless Streamable HTTP resource server**: JSON-RPC 2.0 in,
`application/json` out, no `Mcp-Session-Id` ever issued, no server-initiated
SSE, no process or Redis state between requests. That is what makes it ordinary
web-application traffic — every node behind the load balancer serves it, nothing
extra runs, and no sticky sessions are needed.

It is invisible until an operator turns it on, it accepts only the credentials
this installation's own OAuth 2.1 server issues **for this resource**, and it
leaves the in-Admin chat and the WebSocket transport completely untouched.

The client-facing wire reference is
[web_developer/assistant/mcp.md](../../web_developer/assistant/mcp.md). Token
issuance, audience confinement and the discovery documents belong to
[account/oauth_server.md](../account/oauth_server.md).

---

## The request path

```
POST /api/assistant/mcp
  → AuthenticationMiddleware
      Bearer present? → User.validate_jwt → the `mcp` branch
        (oauth_server.tokens.validate_access): audience path == request path,
        resource registered AND enabled, grant live, signature/expiry verified
        against the USER's auth_key → stamps request.oauth_grant.
        Any failure here is a 401 the view never sees, carrying the RFC 9728
        challenge when the path is a live resource.
  → rest/mcp.py :: on_assistant_mcp
      ASSISTANT_MCP_ENABLED off → the PROJECT's own handler404. Stop.
  → _serve  (@md.rate_limit("assistant_mcp", ip_limit=120))
      method != POST                 → 405 + Allow: POST
      mcp/auth.py :: refusal()       → 401 / 403 (see below)
      MCP-Protocol-Version unknown   → 400
      request.body                   → mcp/server.py :: handle()
  → mcp/protocol.py  parse + classify
  → mcp/server.py    _dispatch → initialize | ping | tools/list | tools/call
  → tools/call → agent._execute_tool(block, PROJECTED registry, …)
      unknown / hidden name → "Unknown tool: <name>"  + assistant:permission_denied (6)
      permission miss       → "Permission denied. …"  + assistant:permission_denied (5)
      mutates=True          → approvals.propose() → an approval CARD, no side effect
      otherwise             → the handler runs
```

Nothing in that chain is MCP-specific except the projection and the per-grant
conversation. **There is no second dispatch path**: a registry tool called over
MCP runs through the same `agent._execute_tool` both agent loops use, so the
approval gate, the permission gate, the incident events and the result
serialization are the chat path's behaviour rather than a re-implementation that
can drift from it.

### Ordering is the design

Two orderings are load-bearing and must not be rearranged:

* **The enabled gate runs before the rate limiter.** `@md.rate_limit` answers
  429 and files a level-5 incident on its block path. In front of the gate that
  would make a disabled door distinguishable from a non-existent route at the
  121st probe, and would turn a scanner into an incident generator for a feature
  nobody enabled. The limiter therefore decorates the inner `_serve`, which the
  gate calls.
* **The 404 is the project's, and it is called, not raised.** The view invokes
  `get_resolver().resolve_error_handler(404)(request, exception=Http404())` —
  the exact callable a genuinely unresolved path reaches, HTML/JSON negotiation
  included. Raising `Http404` inside a dispatched view would become a 500
  (`mojo/decorators/http.py`), and hand-building an imitation would drift from
  the project's own page the first time someone customized it.

---

## Settings

| Setting | Default | Plane | Description |
|---|---|---|---|
| `ASSISTANT_MCP_ENABLED` | `False` | database or deployment file | Whether the door accepts anything at all. Read with `settings.get(..., kind="bool")` on **every** request — never cached, so the switch is immediate in both directions. |
| `ASSISTANT_MCP_PATH` | `api/assistant/mcp` | deployment file only (`get_static`) | The request path. One value derives the route, the OAuth resource registration and the sensitive-body label, so they cannot disagree within a process. Changing it needs a restart. |

All three consumers of the path — the `@md.URL` route, the OAuth resource
registration in `apps.py`, and the challenge's `resource_metadata` — call
`mcp_auth.configured_path()`, because `validate_access` compares the token
audience's path to `request.path` **exactly**: a registration that disagreed
with the route by one trailing slash would refuse every token this server had
just minted. The helper appends the slash itself when `MOJO_APPEND_SLASH` is
set (which also sidesteps `_register_route`'s own append step, since the pattern
then already ends in one), and falls back to the default with a `logit.error`
for an empty or `/`-only setting — honouring that would mount the endpoint at
the site root. Its `raw` / `append_slash` parameters exist so tests can drive it
without touching the shared settings singleton.

> `MOJO_APPEND_SLASH` is read here with `get_static`, while
> `mojo/decorators/http.py` reads it with `settings.get`. A deployment that sets
> it **only** through a database row would make the two disagree; set it in the
> deployment file, as every other consumer of it assumes.

Both appear in the Admin settings catalog (`assistant/apps.py ::
register_settings_descriptors`).

**`ASSISTANT_MCP_ENABLED` is catalog-protected.** It is in
`admin_settings.ASSISTANT_KEYS`, so `is_catalog_protected()` is true and the
generic settings surface (`Setting.set`, `POST /api/settings`, whose
`SAVE_PERMS` are `manage_settings`/`groups`) refuses it. That matters because a
global database row **outranks the deployment file**: without protection, any
`manage_settings` holder could open a remote-agent door on every node. Note that
protection and writability are separate sets — the key is deliberately NOT in
`ASSISTANT_WRITABLE_KEYS`, whose owner editor is a separate concern.

`LLM_ADMIN_ENABLED` is **not** consulted. It gates `run_assistant`, the
provider-key chat loop. The whole point of remote access is to work without a
platform LLM credential, so the door has its own switch.

### While the switch is off

* Anonymous, session-authenticated and API-key requests, any method → the
  project's `handler404` response, and **no rate-limit key is touched**.
* A presented MCP token → the framework chokepoint's generic 401, with **no**
  `WWW-Authenticate`. The resource is dormant, not a live door, and must not
  advertise itself.
* The RFC 9728 protected-resource document 404s for the same reason (owned by
  `oauth_server`).

Grants are **dormant, never revoked**, by a switch-off: re-enabling brings every
existing connection back.

---

## Who refuses what

| Situation | Refused by | Status | `WWW-Authenticate` |
|---|---|---|---|
| Bad, expired, revoked or wrong-audience token | `AuthenticationMiddleware` + `validate_access` | 401 | yes (live resource only) |
| No credential at all | `mcp/auth.refusal` | 401 `invalid_token` | yes |
| A session JWT, a `user_api_key`, an `ApiKey`, a group token | `mcp/auth.refusal` | 401 `invalid_token` | yes |
| An MCP grant without the `mcp` scope | `mcp/auth.refusal` | 403 `insufficient_scope` | yes, with `scope="mcp"` |
| A grant user holding neither `view_admin` nor `assistant` | `mcp/auth.refusal` | 403 `permission_denied` | **no** |

The split is deliberate. A bad bearer never reaches a view, so only the
middleware can attach a challenge to it; `mcp/auth.py` makes exactly the checks
the chokepoint cannot make. The permission 403 carries no challenge because
re-authenticating cannot fix a permission.

`is_key_backed_session()` and `is_request_user()` are checked even though an MCP
grant can never be key-backed: a custom `AUTH_BEARER_HANDLERS` identity that
happened to carry an `oauth_grant` attribute must not open this door.

The permission predicate is exactly the chat endpoint's —
`has_permission(["view_admin", "assistant"])`, ANY-of, the same rule
`@md.requires_global_perms('view_admin', 'assistant')` applies to
`POST /api/assistant`. An operator who can use the chat can use the door, and
nobody else can.

---

## The projection

`tools/list` and `tools/call` both read `mcp/server.projected_registry()`, which
is `get_registry()` minus:

* **`HIDDEN_TOOLS`**, three named sets in `mcp/server.py`, each with one reason.
  A tool is hidden for what it **is**, never for who is calling:

  | Set | Names | Why |
  |---|---|---|
  | `_META_TOOLS` | `agent.META_TOOLS` (`load_tools`, `create_plan`, `update_plan`) + `list_tools` + `add_context` | The meta-tools steer an agent loop that does not exist here — the client runs its own model. `list_tools` is a discovery tool that reads the **raw** registry, so it would hand back exactly the names this projection exists to withhold, and MCP already has `tools/list`. `add_context` is a chat-context builder whose per-pk validation makes it a model-row existence oracle, and the context it builds has nowhere to go on a stateless transport. Deriving from `agent.META_TOOLS` means a new meta-tool is hidden the day it is added. |
  | `_CHAT_STATE_WRITERS` | `write_memory`, `delete_memory`, `save_skill`, `update_skill`, `delete_skill` | They shape the **chat** assistant's own state; exposing them would put approval cards for the operator's own memory and skills in front of an external client for no benefit. The readers — `read_memory`, `find_skill`, `list_skills` — stay. |
  | `_LLM_SPENDING_READS` | `analyze_image` | **A read tool that spends the platform LLM credential is not exposed over MCP.** `analyze_image` sends an image plus a client-chosen free-text prompt through `helpers.llm` on `LLM_HANDLER_API_KEY`, and read-only tools carry no approval gate — over MCP that is an unmetered spend of the installation's own credential, driven by a remote client. Re-run `grep -rn "helpers.llm\|LLM_HANDLER_API_KEY" mojo/apps/assistant/services/tools/` when adding tools and add anything it finds to this set. |

* **`requires_managed_infrastructure` tools when `infrastructure.is_external()`.**

`tools/list` additionally filters by `user_can_use_tool(user, entry)` and
renders each entry as `{name, description, inputSchema, annotations}` with
`annotations.readOnlyHint = not mutates` and `annotations.destructiveHint =
mutates`. Registry order; no pagination cursor.

`projected_registry` is deliberately **not** permission-filtered:
`_execute_tool` keeps its own permission gate and its own denial event, and a
duplicate check here could only disagree with it.

**Why a projection rather than a pre-check.** `_execute_tool(block, registry, …)`
already takes the registry as a parameter, so handing it the projection makes a
hidden name fail as `Unknown tool: <name>` with the chat path's own string and
its own `assistant:permission_denied` level-6 event — indistinguishable from a
name that was never registered. And the filter is load-bearing rather than
cosmetic: a **read-only** `requires_managed_infrastructure` tool is not refused
by `_execute_tool` at all (only `approvals.propose` checks that gate), so the
projection is the only thing keeping such a tool off an external installation.

**An external app's tools appear automatically.** Anything registered with
`register_tool()` / `@tool` from an `assistant_tools.py` is in the registry, so
it is offered over MCP under exactly these filters — no MCP-specific
registration exists.

---

## The per-grant conversation

The first **registry** tool call on a connection lazily creates one
`Conversation`:

```python
Conversation(user=<grant user>,
             title=f"MCP: {client.client_name or client.client_id}"[:255],
             metadata={"transport": "mcp", "grant": <grant pk>})
```

No migration was needed — the flag rides in the existing `metadata` JSONField.
It does three jobs:

1. gives `approvals.propose()` its dedupe and supersede scope, so calling the
   same tool with the same arguments twice is one card, not two;
2. gives the Admin somewhere to see what the client proposed
   (`GET /api/assistant/conversation/<id>?graph=detail`);
3. **bounds what the client may read back** — see the MCP-only tools below.

`ping`, `tools/list` and the two MCP-only tools never create one.

**Creation is serialized on the grant row.** The fast path is an unlocked read;
only when it misses does `conversation_for_grant` open a transaction, take
`select_for_update` on the grant the caller already authenticated with, and
look again. Without it two concurrent first calls could each create a row —
harmless for reads (the lowest pk wins every later lookup) but not for
approvals, because a card proposed into the losing conversation is visible to
the operator in the Admin and unreachable by the client forever.

**One conversation per GRANT, not per user.** A second grant for the same
operator gets its own conversation and therefore its own card scope: neither
connection can poll the other's approvals.

**No `Message` rows are written for MCP calls.** The transport is stateless and
carries no conversation history; the audit trail is the existing `assistant:*`
incident events plus `approvals._audit`'s `logit.Log` rows.
`_write_outcome_message` still records the *resolution* of a card into that
conversation, because that happens on the operator's side.

---

## The two MCP-only tools

| Tool | Returns |
|---|---|
| `list_pending_actions()` | `{"actions": [...]}` — `approvals.states_for_conversation(<this grant's conversation>)`, oldest first, 50 max. `[]` when no conversation exists yet. |
| `get_pending_action(action_id)` | One `approval` block, via `approvals.state_for_action(user, action_id, conversation_id=<this grant's conversation>)`. |

They exist because a stateless client needs to learn the outcome of a card it
proposed, and they are **scoped to the grant's own conversation** because
`render_block` ships redacted `args` and up to 8 KB of `preview`. A card the
operator has pending in the Admin chat panel belongs to a different conversation
and is invisible here, even though it belongs to the same user.

Every unresolvable case — a malformed id, an unknown id, another user's id, the
same user's chat-conversation id, and "no conversation on this connection yet" —
returns the identical `{"error": "This action is no longer available."}`, and
`approvals._load`'s `_deny` budget reports an id-guessing loop exactly as it does
for a REST caller.

**Resolution is never available over MCP.** `POST /api/assistant/action` refuses
an MCP token (its audience is the MCP path and nothing else), so the operator
resolves in the Admin over REST or the WebSocket. See
[approvals.md](approvals.md).

---

## Logging

`sensitive_body_label()` labels `POST <ASSISTANT_MCP_PATH>` as
`assistant_mcp`. This is not optional decoration: the request body carries tool
**arguments** and the response carries tool **results**, neither of which the
chat path ever puts on the wire. With the label, `mojo/middleware/mojo.py` skips
`_raw_body` entirely and `mojo/middleware/logging.py` writes
`{"sensitive_body": "assistant_mcp"}` for both directions.

The label does one more job: `MojoMiddleware` also sets `request.DATA =
objict()` for it, the same treatment the mojosec batch endpoint gets. The view
reads `request.body` itself, so nothing is lost — and it closes two holes at
once. A parsed envelope would put `params.arguments` into the incident that
`dispatch_error_handler` files on an unhandled 500, and a top-level `"group":
<id>` sitting beside `"jsonrpc"` would let the dispatcher resolve and `touch()`
an arbitrary `Group` before the view ever ran.

The view body is additionally wrapped: anything raised **outside**
`server.handle` (reading the body, building the request meta, serializing)
answers a generic JSON-RPC `-32603` with the detail going only to
`assistant.log`. Otherwise it would reach `dispatch_error_handler`, which
returns `str(err)` to an unauthenticated caller while `LOGIT_RETURN_REAL_ERROR`
is on.

`mcp/server.py` writes one `assistant.log` line per `tools/call` with the tool
name, the user pk and the grant pk — never the arguments, never the result. The
name is safe to log because `_dispatch` refuses anything that is not
`^[A-Za-z0-9_.\-]{1,128}$` with `-32602` **before** dispatch: a client-chosen
name reaches a log line, an incident title and an incident body, so an
unbounded or newline-bearing one is log forging.

### Incident volume is bounded per operator

Everything an MCP dispatch reports goes through
`server.suppressed_reporter(user)`, which wraps `report_event_suppressed` with
`key="mcp:<user pk>"`, a one-hour window and `fail_open=False`. The chat path is
untouched and stays unsuppressed — a person picks the tool names there. Here the
client picks them, and `_execute_tool` files a level-6
`assistant:permission_denied` for every unregistered name, so without this a
machine could mint one incident row per request indefinitely. Fail-closed is
deliberate: a Redis outage must drop these, not remove the ceiling.

---

## Rate limiting and CORS

* One IP bucket, `@md.rate_limit("assistant_mcp", ip_limit=120)` — twice the
  chat bucket, because a client issues `initialize` + `tools/list` + several
  calls per task. Per-grant budgets can be added later without a contract
  change.
* `MCP-Protocol-Version` is in `Access-Control-Allow-Headers`
  (`mojo/middleware/cors.py`). A browser-hosted 2025-06-18 client sends that
  header on every request; without the entry its preflight fails and the door is
  unreachable from a browser.
* **No `Origin` validation.** The spec's DNS-rebinding concern is about ambient
  credentials on localhost servers. This door authenticates only by an explicit
  `Authorization` header, which a rebinding page cannot attach, and rejecting
  foreign origins would break legitimate browser-hosted clients.

---

## Protocol surface

| Method | Behaviour |
|---|---|
| `initialize` | Negotiates `2025-06-18`, accepts `2025-03-26` (the tools subset is identical). Advertises `capabilities.tools` only. `serverInfo` is `{"<MOJO_INSTALLATION_SLUG or "mojo">-assistant", <framework version>}`. Carries `instructions` explaining the approval hand-off. |
| `notifications/*` | Accepted, 202, no-op. |
| `ping` | `{}`. |
| `tools/list` | The projection. No cursor. |
| `tools/call` | Dispatch (above). |
| anything else | JSON-RPC `-32601`. |
| `GET` / `DELETE` / `HEAD` | 405 with `Allow: POST`. |

**HTTP status vs JSON-RPC error.** A body that is not a JSON-RPC message at all
(`-32700` unparsable, `-32600` a scalar / empty array / oversized batch /
malformed envelope) is a **transport** failure and answers HTTP 400 carrying the
error object. A well-formed request whose method or params are wrong is a
**protocol** answer: HTTP 200 carrying a JSON-RPC error response. Auth and
routing keep their own codes (401/403/404/405/429).

**Batches are still supported.** Revision 2025-06-18 dropped JSON-RPC batching,
but 2025-03-26 clients still send them and accepting an array costs one loop.
Each element is answered independently, a bad element gets `-32600` in its own
slot with a null id, and a batch of only notifications is a 202. `MAX_BATCH` is
20 — it is a work-amplification budget, not a convenience limit.

**Two deliberate spec deviations**, both recorded in the client-facing doc:

* an unknown tool name is an `isError: true` tool result rather than `-32602`,
  because the result form is what carries `_execute_tool`'s
  `assistant:permission_denied` event;
* the no-credential 401 carries `error="invalid_token"`, where RFC 6750 §3.1
  would prefer no error code on a credential-less request. The epic fixes this
  form and `oauth_server.www_authenticate` always emits an `error`.

---

## Key files

| File | Role |
|---|---|
| `mojo/apps/assistant/mcp/protocol.py` | JSON-RPC 2.0 framing. No Django imports. |
| `mojo/apps/assistant/mcp/server.py` | Methods, projection, per-grant conversation, dispatch. |
| `mojo/apps/assistant/mcp/auth.py` | The door's acceptance checks and the raw JSON response builder. |
| `mojo/apps/assistant/rest/mcp.py` | The route, the enabled gate, the rate bucket. |
| `mojo/apps/assistant/apps.py` | The two descriptors and the OAuth resource registration. |
| `mojo/apps/account/services/oauth_server/` | Token issuance, the resource registry, `validate_access`, `www_authenticate`. |

## Tests

```bash
./bin/run_tests -t test_assistant
./bin/run_tests --extra extended -t test_assistant_extended_serial.44_test_mcp_wire
```

- `tests/test_assistant/44_test_mcp_server.py` — framing and classification,
  `initialize` negotiation, ping/notifications/batches/unknown methods, the
  projection and its annotations, the lazy per-grant conversation, a mutating
  call yielding a card that never runs, the conversation scope of the two
  MCP-only tools, and every refusal string and incident level.
- `tests/test_assistant/45_test_mcp_gate.py` — the token-kind gate and its
  challenges, the disabled door over the wire (including that the rate-limit
  bucket is never touched), the sensitive-body label, and the descriptors,
  catalog protection, OAuth registration and route wiring.
- `tests/test_assistant_extended_serial/44_test_mcp_wire.py` — the enabled door
  end to end: transport, challenges, protocol shapes, a real `block_ip`
  proposal that only the operator's own session can resolve, and the switch
  taking effect with no reload.
