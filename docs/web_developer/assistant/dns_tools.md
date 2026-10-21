# Assistant DNS validation setup — client reference

Load `load_tools(domain="dns")` for shared wildcard-validation setup. The
domain is listed only for a user with global `view_admin` and DNS authority
somewhere eligible. Every selected Domain/group is authorized again; seeing a
tool does not grant access to other groups. The same tools are discoverable
through the existing OAuth MCP `tools/list` projection. See [MCP](mcp.md) for
sign-in and the distinction between tool access and separately consented REST
`api` scope.

## Reads and writes

| Tool | Arguments | Result / rendering |
|---|---|---|
| `list_dns_setup_groups` | Optional `id` OR `search/start/size` | Minimal active authorized group choices; render a chooser |
| `get_dns_validation_plan` | `domain`, OR `group`+`name`; optional `allocation_ref` | Readiness v1; render serving and renewal separately |
| `connect_dns_validation` | Same target, `allocation_ref`, optional `restart:false` | Approval proposal, then Readiness v1 after execution |
| `check_dns_validation` | `delegation` local id | Approval proposal, then persisted proof/readiness |
| `request_dns_wildcard_certificate` | `delegation` local id | Approval proposal, then exact-profile request/readiness |

Target names are apex names. A group must already exist and be effectively
active. A same-name Domain must belong to that exact group. A groupless house
Domain is read-only and reports `unsupported_domain_scope`; shared mutations
currently require a group-managed domain. There is no implicit adoption,
transfer or group creation. View-only callers receive no public request and
false mutation capabilities.

The full [Readiness v1 fields and states](../dnsman/README.md#readiness-v1) are
shared with REST and CLI. Drive controls from `capabilities` and `next_action`,
not provider labels or a stored verified flag alone. A connection row can guard
renewal without any readable certificate material. A still-valid certificate
can coexist with a blocked/retrying renewal. `serving_valid:null` means custody
could not be established. This is API-node evidence, not proof that an edge
host installed the certificate. Do not infer worker upgrades from API version;
worker version remains unknown unless established telemetry proves it.

## Controller handoff

The conversational sequence is inspect, prepare, controller grant, replay
Connect, Check, Request, then poll Plan. Plan is strictly read-only. The first
Connect prepares a durable request; it cannot self-grant trust. Show/export the
returned five-field descriptor exactly, for example:

```json
{
  "version": 1,
  "consumer_project_uuid": "0123456789abcdef0123456789abcdef",
  "client_ref": "11111111-2222-4333-8444-555555555555",
  "domain": "customer.example",
  "allocation_ref": "existing-target.acme-hub.example.net"
}
```

The values are fake public references. Show scope separately as
`["customer.example","*.customer.example"]`. The consumer UUID identifies the
configured hub key's original project; it is unrelated to the local Domain
group. The allocation controller must recognize that recipient and explicitly
grant **apex and wildcard certificate issuance authority**. Knowledge of the
CNAME/descriptor grants nothing. A trusted project keeps an independent
certificate/private key and can impersonate the named domain; make that scope
clear at the controller handoff.

After the controller grants the descriptor, replay Connect with the original
target/allocation. An interrupted/lost response retains the same local ref.
Check persists current grant/one-hop proof; Request uses the existing Jobs and
renewal path for the fixed profile. `already_current` and `already_queued` are
successful idempotent outcomes. Due active material renews the same certificate
id; setup does not reset valid material, renewal dates or vhost references.

Explicit `restart:true` prepares a fresh immutable ref only for a revoked
binding or unjoined pending request, after old cleanup is settled. The new ref
requires a fresh controller grant; the old ref is never revived or reused for
new authority. No operation replaces the CNAME or allocates a competing target.

## Approval and refusal UI

Each write creates an existing server-issued `PendingAction` approval card;
a model's tool call or textual confirmation cannot execute it. Render the
fixed Approve/Cancel controls from [approvals](approvals.md), with preview and
arguments as text. The preview binds the local Domain/group, configured
consumer, allocation/client ref, restart and fixed profile. Resolution repeats
authority and preview checks; a stale selection or changed trust refuses
execution. Do not retry with a different target/ref automatically.

Render fixed copy for allowlisted readiness/error codes. Missing/revoked trust,
consumer identity change, alias mismatch/lookup failure, busy certificate,
cleanup pending or profile conflict requires the indicated next action. An old
hub means upgrade, with no allocation fallback. A failed read-only DNS
observation does not itself demote the persisted connection. Use explicit Check
when the operator wants to record the result. See the
[local error contract](../dnsman/README.md#errors-and-client-recovery).

Tools expose no hub token/key/URL input, setting editor, trust grant, key minting
or general DNS CRUD. Credentials remain in static server deployment settings;
readiness, descriptors, CLI/tool output and approval/audit content contain no
private material. All API/issuance workers require the coordinated upgrade and
restart before shared connections are enabled. After a shared row exists,
keep its routing guards and repair it; do not delete it or downgrade workers to
recover direct renewal. See [rollout](../dnsman/README.md#deployment-and-rollback).
This setup does not authorize production adoption or lift a production hold.

## Session context at preview time

A preview may opt into the engine's minimal context with the optional
`request_meta` keyword, just as a tool handler can. The engine supplies this
from the authenticated HTTP/WebSocket/OAuth session; it is never read from tool
arguments. Both proposal and approval resolution pass their own current
context, so a prior interactive proposal cannot confer authority on a later
confined session. Existing two-argument previews keep their existing behavior.

DNS tools refuse API-key overrides and group-token sessions before preview
reads or approval creation, because the Assistant context intentionally contains
no underlying confined credential. Provisioned agents use the ordinary
tenant-gated REST setup endpoints, which retain that credential. Consented OAuth
MCP sessions remain supported and keep the signed-in user's exact DNS authority.
