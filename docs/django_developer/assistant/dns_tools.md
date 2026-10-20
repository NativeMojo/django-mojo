# Assistant DNS validation setup tools

The `dns` domain exposes the supported local shared-validation flow through
`mojo/apps/dnsman/assistant_tools.py`, discovered by the Assistant app's
`autodiscover_modules("assistant_tools")`. Every handler delegates to
`dnsman.services.delegation_setup`; REST, CLI and tools return the same
[Readiness v1](../../web_developer/dnsman/README.md#readiness-v1). The tools do
not call a provider, implement a new issuer, automate the portal or distribute
private keys.

## Discovery and authority

Load `load_tools(domain="dns")`. The registry requires global `view_admin`
plus a fail-closed `authorize` predicate for DNS authority in at least one
eligible local group. Exact target authority is repeated in the read handler,
mutation preview and mutation handler. A listing predicate is never authority
over a caller-selected Domain or group. Reads honor `view_dns`, `manage_dns` or
`security`; writes require `manage_dns` or `security` for the target. Effectively
inactive groups and inaccessible targets fail closed. Key-backed overrides
remain confined to the underlying credential's authority.

The target is either an existing Domain id or an existing active group plus a
normalized apex name. No tool adopts/transfers a Domain or creates a group.
Groupless house domains are read-only and ineligible for shared mutation; the
readiness tells a deployment operator that group-managed scope is required.
The hub consumer UUID comes from the configured protected key, independent of
the local tenant UUID. Local DNS authority never grants hub controller power.

| Tool | Input | Mutation |
|---|---|---|
| `list_dns_setup_groups` | Optional bounded `id` or `search/start/size` | No; minimal authorized active local choices |
| `get_dns_validation_plan` | Target, optional `allocation_ref` | No; status/preflight, no row/ref/account/job creation or persisted demotion |
| `connect_dns_validation` | Target, `allocation_ref`, optional `restart:false` | Yes; prepare/export or replay join |
| `check_dns_validation` | `delegation` local id | Yes; persist grant/one-hop proof and bind Domain |
| `request_dns_wildcard_certificate` | `delegation` local id | Yes; idempotent exact apex-plus-wildcard request |

No grant/revoke/key/static-setting/general-DNS tool is added. Delegation model
`DENY_AI` remains enabled so generic model tools cannot mutate the durable
connection outside this boundary.

## Approval boundary

All three writes declare `mutates=True` and use the existing `PendingAction`
flow. There is no `owner_state` exemption. Calling a tool proposes an action;
only the existing operator resolution path may execute it. Summary and preview
identify the exact local target and requested operation. The bound revision
covers local Domain/group/name, consumer UUID, allocation/client ref, restart
and fixed certificate profile. Resolution refreshes the preview/permissions;
stale bindings refuse. The handler resolves authority again and the shared
service rechecks current trust before a mutation.

The same registry is projected through OAuth MCP `tools/list` and dispatched
through `agent._execute_tool`. This preserves listing/permission checks and the
PendingAction gate for remote agents. No special DNS dispatch path is added.
An agent may inspect readiness and export a public request, but cannot approve
its own action by putting confirmation text in its arguments. See
[approvals](approvals.md) and [MCP](mcp.md). A separately consented OAuth `api`
scope may call the ordinary REST API under its registered resource and the same
endpoint permissions; MCP scope alone does not widen REST access.

## Guided flow and recovery

Inspect Plan, prepare with Connect, hand the exact five-field public
`connection_request` to the allocation controller, replay Connect after its
explicit grant, Check, then Request and poll Plan. Display the apex/wildcard
scope when explaining the controller handoff. The descriptor contains no secret
and cannot authorize itself. The complete REST examples and error/state table
are in [project setup](../../web_developer/dnsman/README.md#project-shared-validation-setup).

A repeated Connect retains its immutable ref, including after a lost reply.
Explicit `restart:true` is only for a revoked binding or unjoined pending
request, settles old cleanup and exports a fresh ref requiring a new grant. It
never replaces the existing CNAME or revives a revoked ref. Missing grant,
revocation, alias failure or changed consumer identity blocks issuance before
CA/provider writes. The tools cannot fall back to a direct TXT write or
exact-host downgrade. Valid serving certificate material survives failed
renewal; request uses the existing certificate id/Jobs lineage when due.

Readiness separates `certificate.serving_valid`/`material_state` from renewal
state, retry, bounded error and selected job. It returns no PEM, key, secret or
raw job payload. API-node custody is not edge installation evidence; unknown
worker version requires deployment evidence. Hub settings remain server-only
static configuration. Deploy the coordinated hub/schema/API/worker upgrade and
restart all old issuance workers before enabling shared connections; see
[rollout and rollback](../dnsman/AcmeFederation.md#deploying-and-recovering-the-downstream).
No tool use lifts a production hold or authorizes live DNS/certificate adoption.

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
