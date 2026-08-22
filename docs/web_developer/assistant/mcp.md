# Connecting an AI client over MCP

The Admin Assistant's tools are available to a remote AI client — Claude Code,
Claude Desktop, a claude.ai custom connector, ChatGPT developer mode, or anything
else that speaks **MCP (Model Context Protocol)** — at one endpoint:

```
POST https://<host>/api/assistant/mcp
```

Three things to know before you start:

1. **You sign in through the installation's own pages.** No API key is copied
   anywhere. The connection is authorized with OAuth 2.1 + PKCE; see
   [Connecting an app with OAuth 2.1](../account/oauth_server.md) for the full
   flow, which most MCP clients perform for you.
2. **Tools run with the connected operator's own permissions.** You see exactly
   the tools that operator can use, and nothing else.
3. **A tool that changes SHARED data never executes.** It returns an approval
   card, and a human resolves it in the Admin. There is no way to approve over
   MCP. The exception is the operator's own assistant state — a `user`-tier
   memory, or a skill they own — which is written immediately. See
   [Approvals](approvals.md).

The transport is **stateless Streamable HTTP**: plain `application/json`
responses, no SSE stream, and **no `Mcp-Session-Id`** — never send one and never
expect one.

---

## Connecting

**Claude Code**

```bash
claude mcp add --transport http assistant https://<host>/api/assistant/mcp
```

The first tool call opens a browser at this installation's sign-in and consent
pages. Approve, and the client stores the tokens.

**Claude Desktop / claude.ai custom connector, ChatGPT developer mode**

Add a remote MCP server and paste the same URL:

```
https://<host>/api/assistant/mcp
```

The client discovers the authorization server from the `WWW-Authenticate`
header on the first `401` (`resource_metadata` points at
`/.well-known/oauth-protected-resource/api/assistant/mcp`), registers itself,
and runs the consent flow.

**Anything else.** Any client that supports HTTP MCP with OAuth works. If it
does not do discovery for you, the sequence is: fetch the protected-resource
metadata, fetch the authorization-server metadata it names, register (RFC 7591)
or publish a client-ID metadata document, send the user to `/authorize` with a
PKCE challenge and `resource=https://<host>/api/assistant/mcp`, exchange the
code, then send `Authorization: Bearer <access token>`.

> **Nothing is reachable until an operator enables it.** While the feature is
> off, every request to the endpoint answers the site's ordinary **404** — the
> same body an address that does not exist answers. A 404 here means "not
> switched on (or not this host)", not "wrong URL".

---

## Requesting full API access

The sign-in above grants the **tool door** and nothing else: the token works at
`/api/assistant/mcp` and returns `401` everywhere else. The same authorization
server can also grant **full API access as that person** — every endpoint their
account can already reach through the REST API — under a second scope, `api`.

A client asks for it by naming the **API root** as the resource:

```
GET /api/account/oauth/authorize
      ?…
      &resource=https://<host>/api
      &scope=api            # or `mcp api` for one credential that does both
```

The consent screen states it in plain words, including that the Assistant's
approval step does **not** apply to direct API calls. Everything else — PKCE,
the code exchange, refresh rotation, the 30-day ceiling, Admin revocation — is
identical.

Three things to know:

- **An MCP client's built-in sign-in cannot obtain `api`.** It names the MCP
  endpoint as its resource, and the server echoes the requested resource rather
  than upgrading it. Asking for `api` there answers `invalid_scope`. Drive the
  flow yourself if you want full API access.
- **`api` alone does not open the tool door.** It authenticates at
  `/api/assistant/mcp` but the door answers `403 insufficient_scope`. Ask for
  `scope=mcp api`.
- **`scopes_supported` in the authorization-server metadata is the truth.** An
  installation that has not registered the API root never lists `api`, and
  asking for it is `invalid_scope`.

Full details, including exactly what an `api` token cannot do, are in
[account/oauth_server.md](../account/oauth_server.md).

---

## The wire

Every request is `POST`, `Content-Type: application/json`, with the bearer token.
`GET`, `DELETE` and `HEAD` answer `405` with `Allow: POST`.

Send `MCP-Protocol-Version: 2025-06-18` (or `2025-03-26`) on every request after
initialization. Omitting it is fine; sending an unsupported value is a `400`.

### initialize

```json
{"jsonrpc":"2.0","id":1,"method":"initialize",
 "params":{"protocolVersion":"2025-06-18","capabilities":{},
           "clientInfo":{"name":"my-client","version":"1.0"}}}
```

```json
{"jsonrpc":"2.0","id":1,"result":{
  "protocolVersion":"2025-06-18",
  "capabilities":{"tools":{"listChanged":false}},
  "serverInfo":{"name":"acme-assistant","version":"1.15.18"},
  "instructions":"Tools run with the connected operator's own permissions. …"}}
```

Only `tools` is advertised — there are no resources, prompts, sampling or
elicitation. Follow with the usual `notifications/initialized`, which answers
**`202` with an empty body**.

### tools/list

```json
{"jsonrpc":"2.0","id":2,"method":"tools/list"}
```

```json
{"jsonrpc":"2.0","id":2,"result":{"tools":[
  {"name":"query_incidents",
   "description":"…",
   "inputSchema":{"type":"object","properties":{…}},
   "annotations":{"readOnlyHint":true,"destructiveHint":false}},
  …
  {"name":"list_pending_actions","inputSchema":{"type":"object","properties":{}},
   "annotations":{"readOnlyHint":true,"destructiveHint":false}},
  {"name":"get_pending_action","inputSchema":{"type":"object",
   "properties":{"action_id":{"type":"string"}},"required":["action_id"]},
   "annotations":{"readOnlyHint":true,"destructiveHint":false}}]}}
```

The list is not paginated — there is no cursor. `destructiveHint: true` means
"this tool changes data" — on this server, that means an approval card rather
than anything happening, *unless* the call touches only the operator's own
memory or a skill they own.

The list reflects the operator's permissions and this installation's
configuration, so two operators may legitimately see different tools. A handful
of the Admin's own conversation tools (`load_tools`, `create_plan`,
`update_plan`, `list_tools`, `add_context`) are deliberately not offered here,
and neither is `analyze_image`.

The memory and skill tools ARE offered — both the readers (`read_memory`,
`find_skill`, `list_skills`) and the five writers (`write_memory`,
`delete_memory`, `save_skill`, `update_skill`, `delete_skill`). A writer call in
the operator's own `user` tier, or on a user-tier skill they own, runs
immediately and returns the handler's result; a `global` or `group` tier, or
anyone else's skill, returns an approval card. `initialize` says the same thing
in its `instructions`:

> Tools run with the connected operator's own permissions. A tool that changes
> shared data never executes here: it returns an approval card (status
> approval_required) that the operator resolves in the Admin; poll
> get_pending_action with the action_id to learn the outcome. Writes to the
> operator's own memory and skills (tier "user", or a skill they own) run
> immediately and return the result.

Know what that last sentence means before you use it: a memory or skill your
client writes feeds the **operator's own chat assistant** — user-tier memories
and skills are injected into its system prompt. That is by design, because your
grant acts as that operator and they could type the same thing into chat
themselves, and it reaches nobody else's context. It is still their assistant,
so write what they asked you to write. A skill you save is a suggestion, not a
capability: replaying one cannot execute a mutating tool without the operator
approving a card.

### tools/call

```json
{"jsonrpc":"2.0","id":3,"method":"tools/call",
 "params":{"name":"query_incidents","arguments":{"limit":5}}}
```

```json
{"jsonrpc":"2.0","id":3,"result":{
  "content":[{"type":"text","text":"{\"incidents\": [...]}"}],
  "structuredContent":{"incidents":[…]},
  "isError":false}}
```

Every result is a JSON object, sent both as text and as `structuredContent` so
you can skip re-parsing. No `outputSchema` is declared, so nothing about the
shape is promised.

`isError: true` means the tool refused or failed; the text is a JSON object with
an `error` key. Unknown tool names, permission refusals and handler errors all
arrive this way.

### Batches

An array body is accepted (up to 20 elements) and answered as an array, in
order. A batch of only notifications answers `202`. Revision 2025-06-18 dropped
batching from the spec — sending one element at a time is always fine.

---

## The approval hand-off

Calling a tool that changes data does **not** change anything:

```json
{"jsonrpc":"2.0","id":4,"result":{
  "content":[{"type":"text","text":"{\"status\": \"approval_required\", …}"}],
  "structuredContent":{
    "status":"approval_required",
    "action_id":"1f0c…",
    "tool":"block_ip",
    "summary":"Block 203.0.113.10",
    "expires_at":"2026-08-22T18:41:00+00:00",
    "message":"NOT EXECUTED. This operation requires operator approval. …"},
  "isError":false}}
```

`isError` is `false` because nothing went wrong — the proposal succeeded. **Do
not call the tool again for the same request, and do not report the work as
done.** Tell the operator an approval is waiting, then stop.

An approval card appears in the Admin. Only the operator it is bound to can
resolve it, and only over an interactive session — presenting your MCP token at
`POST /api/assistant/action` answers `401`, by design.

To learn the outcome, poll:

```json
{"jsonrpc":"2.0","id":5,"method":"tools/call",
 "params":{"name":"get_pending_action","arguments":{"action_id":"1f0c…"}}}
```

The result is an `approval` block whose `state` moves `pending` →
`completed` / `failed` / `canceled` / `expired` / `superseded`; `result` is
populated once it is terminal. `list_pending_actions` returns the same blocks
for **this connection's** cards (oldest first, 50 max).

Both tools see only the cards this connection proposed. Cards the operator has
pending in the Admin chat are not yours to read, and asking for one returns the
same "no longer available" refusal as an id that never existed.

Cards expire (10 minutes by default). Calling the same tool with the same
arguments while a card is still pending returns that same `action_id` rather
than proposing a second one. Some actions additionally require the operator to
have signed in recently, or to be a superuser — that is resolved on their side
and is not something you can satisfy.

---

## Errors

| What you get | Meaning | What to do |
|---|---|---|
| `404` (site's ordinary not-found body) | The feature is switched off, or this host does not serve it. | Ask an operator to enable it. Nothing about the request will change this. |
| `405` + `Allow: POST` | You used `GET`, `DELETE` or `HEAD`. | Use `POST`. |
| `401` + `WWW-Authenticate: Bearer error="invalid_token", resource_metadata="…"` | No credential, an expired/revoked/invalid token, or a credential that is not an MCP token for this resource (a browser session, an API key, a group token). | Follow `resource_metadata` and run the OAuth flow, or refresh. |
| `401` with **no** `WWW-Authenticate` | The resource is currently switched off, so it is not advertising itself. | Wait for the operator. Your grant is dormant, not destroyed — it works again when the feature comes back. |
| `403` + `WWW-Authenticate: … error="insufficient_scope", scope="mcp"` | The grant does not carry the `mcp` scope — including an `api`-only grant, which reaches this path but may not use the tools. | Re-run consent requesting `mcp` (or `mcp api`). |
| `403` `{"error":"permission_denied"}`, no challenge | The signed-in operator does not have Assistant access. | Nothing a client can fix; the operator needs the permission. |
| `400` `{"error":"unsupported_protocol_version", …}` | `MCP-Protocol-Version` named a revision this server does not implement. | Send a listed one, or omit the header. |
| `400` + JSON-RPC `-32700` / `-32600` | The body was not a JSON-RPC message: unparsable, a scalar, an empty array, an array over 20 elements, or a malformed envelope. A `null` request id is refused. | Fix the envelope. |
| `429` + `Retry-After` | 120 requests per minute per IP. | Back off. |
| `200` + JSON-RPC `-32601` | Unknown method. | Only `initialize`, `ping`, `tools/list`, `tools/call` and notifications exist. |
| `200` + JSON-RPC `-32602` | `tools/call` had a missing or non-string `name`, or `arguments` that were not an object. | Fix the params. |
| `200` + `isError: true` | The tool refused or failed: unknown tool name, a permission you do not have, or a handler error. | Read the `error` text. Retrying identically will not help. |

Responses carry `Cache-Control: no-store` and are never cached.

Two places where this server deviates from the MCP spec, deliberately:

* an **unknown tool name** is returned as an `isError: true` tool result, not a
  `-32602` protocol error — the result form is what lets the server record the
  refusal against the operator's account;
* the **no-credential `401`** carries `error="invalid_token"`, where RFC 6750
  would prefer no error code on a request that presented nothing.

---

## Operator runbook

If you are the person being asked to turn this on:

1. **Set `BASE_URL`** in System Setup. Without the installation's canonical
   public origin the authorization server does not run at all, and no discovery
   document is served.
2. **Turn on `ASSISTANT_MCP_ENABLED`.** Until then the endpoint is a plain 404
   and no token can be minted for it. The key is protected from the generic
   settings API on purpose — it is set through its own owner surface or the
   deployment file.
3. **Check the reverse proxy passes `/.well-known/`.** If it serves that prefix
   from disk for ACME, discovery returns the proxy's 404 and no client can
   connect. See the deployment note in
   [OAuth 2.1](../account/oauth_server.md).
4. **Grant the operator `view_admin` or `assistant`.** The MCP door applies the
   same permission check as the in-Admin chat.
5. **Watch the approvals.** Everything an external client tries to change lands
   as a card in the Admin, addressed to the operator whose account authorized
   the connection. Nothing runs until they approve it, and a connection can be
   cut at any time by revoking its grant.
