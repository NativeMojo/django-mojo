# LLM Safety Boundary

Every external framework LLM request passes through a provider-neutral,
fail-closed boundary before network I/O. `mojo.helpers.llm.call()` and `ask()`
remain the public facade. Anthropic credentials, SDK calls, bounded one-page
model discovery, prompt-cache arguments, response conversion, and provider
error reduction live in the Anthropic adapter. A deliberately configured AWS
Bedrock route supports bounded Nova 2 Lite text reasoning. Neither adapter
provides automatic provider or credential failover.

## Complete policy example

The deployment must define an exact version-1 policy. This Python settings
example is copyable; the temporary `_LLM_LIMITS` name is only used to avoid
repeating the eleven required keys:

```python
_LLM_LIMITS = {
    "requests_minute": 30,
    "requests_hour": 600,
    "requests_day": 5000,
    "tokens_minute": 200000,
    "tokens_hour": 2000000,
    "tokens_day": 10000000,
    "concurrency": 4,
    "max_input_bytes": 200000,
    "max_output_tokens": 8192,
    "timeout_seconds": 60,
    "max_loop_calls": 25,
}

LLM_SAFETY_POLICY = {
    "version": 1,
    "routes": {
        "assistant": {"provider": "anthropic", "model": "auto:sonnet",
                      "credential": "admin",
                      "capabilities": ["text", "tools", "images", "prompt_cache"]},
        "incident_triage": {"provider": "anthropic", "model": "claude-sonnet-5",
                            "credential": "handler",
                            "capabilities": ["text", "tools", "prompt_cache"]},
        "incident_analysis": {"provider": "anthropic", "model": "claude-sonnet-5",
                              "credential": "handler",
                              "capabilities": ["text", "tools", "prompt_cache"]},
        "incident_ticket": {"provider": "anthropic", "model": "claude-sonnet-5",
                            "credential": "handler",
                            "capabilities": ["text", "tools", "prompt_cache"]},
        "scheduled_task": {"provider": "anthropic", "model": "claude-sonnet-5",
                           "credential": "handler",
                           "capabilities": ["text", "prompt_cache"]},
        "memory": {"provider": "anthropic", "model": "claude-sonnet-5",
                   "credential": "admin", "capabilities": ["text", "prompt_cache"]},
        "file_analysis": {"provider": "anthropic", "model": "claude-sonnet-5",
                          "credential": "admin",
                          "capabilities": ["text", "images", "prompt_cache"]},
        "configuration": {"provider": "anthropic", "model": "claude-haiku-4-5",
                          "credential": "admin", "capabilities": ["text"]},
        "model_discovery": {"provider": "anthropic", "model": "claude-haiku-4-5",
                            "credential": "admin", "capabilities": ["models"]},
        "unattributed": {"provider": "anthropic", "model": "claude-sonnet-5",
                         "credential": "handler",
                         "capabilities": ["text", "prompt_cache"]},
    },
    "shared": {**_LLM_LIMITS, "concurrency": 8, "max_loop_calls": 30},
    "features": {
        "assistant": {**_LLM_LIMITS},
        "incident_triage": {**_LLM_LIMITS, "concurrency": 2},
        "incident_analysis": {**_LLM_LIMITS, "concurrency": 2},
        "incident_ticket": {**_LLM_LIMITS, "concurrency": 2},
        "scheduled_task": {**_LLM_LIMITS, "max_loop_calls": 1},
        "memory": {**_LLM_LIMITS, "max_loop_calls": 1},
        "file_analysis": {**_LLM_LIMITS, "max_loop_calls": 1},
        "configuration": {**_LLM_LIMITS, "concurrency": 1,
                          "max_output_tokens": 16, "max_loop_calls": 1},
        "model_discovery": {**_LLM_LIMITS, "concurrency": 1,
                            "max_output_tokens": 16, "max_loop_calls": 5},
        "unattributed": {**_LLM_LIMITS, "concurrency": 1,
                         "max_loop_calls": 1},
    },
    "breaker": {
        "auth_failures": 2,
        "rate_failures": 3,
        "server_failures": 5,
        "open_seconds": 300,
    },
}
```

Routes and feature envelopes must have the same feature keys. The shared and
feature caps are both enforced for request windows, token windows,
concurrency, input bytes, output tokens, timeout, and loop calls. A request's
tools, images, and enabled prompt cache must be declared by its route. The
route owns the guarded model; an explicit `model=` is accepted only when it
exactly matches the resolved runtime ID. Only `assistant` on Anthropic may
use `model="auto:sonnet"`; other feature/provider routes reject delegation.
Literal policy routes remain fixed. The delegated priority is owner-stored
`LLM_ADMIN_MODEL`, file-owned `LLM_ADMIN_MODEL`, then the newest compatible
active Sonnet from metadata. No handler model/key fallback is allowed.
Inspect/clear old legacy pins before opting in, deploy the policy change, and
activate the new hash normally. No installed policy is rewritten automatically.
`credential: "admin"` requires
`LLM_ADMIN_API_KEY`; it never silently falls back to the handler credential.

Each installation supplies its exact Anthropic credential or AWS role/profile
chain. Provider-account caps remain the final cross-installation backstop.
Central billing, dynamic provider-credit changes, price tables, automatic
failover, and production-data cleanup are non-goals.

## Host features

A host app registers its own feature names in the static, file-only setting
`LLM_HOST_FEATURES`, a list of slugs. The default is empty, which leaves the
framework's feature set unchanged.

```python
LLM_HOST_FEATURES = ["support_responder", "support_staff"]
```

Each slug is a lowercase letter followed by up to 31 lowercase letters, digits
or underscores (32 characters is the ledger's `feature` column). A slug may not
repeat, copy a framework feature (`assistant`, `unattributed`, ...) or use a
name the guard keeps for itself: `shared`, `breaker` or `unknown`. Any bad
entry, or a value that is not a list or tuple, stops startup with
`ImproperlyConfigured`; `llm.call` re-checks the setting on every call.

A listed slug works exactly like a framework feature. It needs its own route
in `routes` and its own envelope in `features`; a listed slug with no route
fails with `route_missing`, and a route for a slug that is not listed fails
with `policy_invalid`. Its calls write ledger rows and metrics under the slug
and obey the shared envelope, the emergency stop and the per-credential
breaker, which host features share with the framework's own features: a burst
of failing host calls can open the breaker for every feature on that
credential.

Listing a slug alone does not change the policy hash. Adding its route and
envelope does, so activate the deployed policy again (step 3 below) after the
deploy. The allowed `context` keys are `job_id`, `incident_id`,
`conversation_id`, `file_id` and `operation_id`; `conversation_id` must be an
integer.

## Bedrock routes

Add the host feature, route, and envelope to the complete static policy above:

```python
LLM_HOST_FEATURES = ["my_maestro_reasoning"]
LLM_SAFETY_POLICY["routes"]["my_maestro_reasoning"] = {
    "provider": "bedrock",
    "model": "us.amazon.nova-2-lite-v1:0",
    "credential": "aws",
    "capabilities": ["text"],
    "region": "us-east-1",
    "reasoning_efforts": ["off", "low", "medium"],
}
LLM_SAFETY_POLICY["features"]["my_maestro_reasoning"] = {
    **_LLM_LIMITS, "max_loop_calls": 1,
}
```

Bedrock requires exactly these six route keys, `credential="aws"`, and
`capabilities=["text"]`. Allowed models are exactly
`us.amazon.nova-2-lite-v1:0`, `eu.amazon.nova-2-lite-v1:0`,
`jp.amazon.nova-2-lite-v1:0`, and `global.amazon.nova-2-lite-v1:0`.
`reasoning_efforts` must be a nonempty, unique list drawn from `off`, `low`,
and `medium`. Region syntax is checked locally; AWS authorizes the actual
profile/region combination. Choose a source region supported by that exact
profile in the [AWS Nova 2 Lite region table](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-amazon-nova-2-lite.html).
The framework does not enable Bedrock or activate this example policy by default.

The adapter uses `mojo.helpers.aws.client.get_client` with the existing default
AWS role/profile chain, including an operator's `AWS_PROFILE`. It pins SigV4,
the policy region, AWS endpoint resolution, and one SDK attempt (no SDK
retries). No AWS key, profile, endpoint, or arbitrary provider options come
from callers. Unattended deployments need a working non-interactive role or
profile; interactive MFA and credential-process workflows are outside this
cloud route's supported deployment contract.

Converse requires `bedrock:InvokeModel`. Authorize the selected inference
profile resource **and** its destination foundation-model resources, with
organizational region controls permitting that profile's routing. For the US
profile called from `us-east-1`, destinations currently include `us-east-1`,
`us-east-2`, and `us-west-2`; other profiles/source regions differ. Follow
[AWS inference-profile permissions](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-prereq.html)
and the linked region table when granting access.

`route_state()` is declarative: `credential_configured=True` for a valid AWS
route means the installation chain was selected. Neither that nor `ready=True`
resolves credentials, constructs an AWS client, checks IAM/model access, or
makes a billed request. Only an authorized guarded invocation proves access.
All Bedrock routes use one installation-chain accounting identity; refreshing
role credentials does not reset budgets or the circuit. The two Anthropic key
settings are never read to resolve a Bedrock credential.

See [the call and response contract](../helpers/llm.md#bedrock-reasoning).
The adapter supports text only, suppresses reasoning content from the final
answer, and retains billed reasoning within `usage.output_tokens`. It sends
no explicit cache controls, guardrail configuration, or application content
filter. Anthropic tool routes, key probes, and model discovery retain their
existing contracts.

## Policy agreement and rollout

Calls require the static policy hash to equal the single authoritative primary
database row `LLM_SAFETY_POLICY_EXPECTED_HASH`. Redis activity and recent node
traffic do not choose the winner, so an idle or stale node cannot become the
accepted policy after a timeout.

Use this rollout sequence:

1. Deploy `LLM_EMERGENCY_STOP=True` and wait until every application/worker
   node has restarted.
2. Deploy the new `LLM_SAFETY_POLICY` to every node while the static stop
   remains true.
3. In either Admin bundle, fresh-auth as the installation owner and choose
   **Activate deployed policy**. This writes and audits the expected hash.
4. Verify Anthropic candidate credentials if needed, reset breakers only after
   their cause is corrected, then remove the static `LLM_EMERGENCY_STOP=True`
   from deployment configuration and redeploy. Clearing only the database
   checkbox cannot override a static true value.

If nodes disagree, calls fail with `policy_mixed`. Restore one identical policy
on every node, keep the stop on, activate that deployed policy again, and then
perform step 4. Do not repair agreement by editing the protected setting
directly.

## Guard ordering, permits, and evidence

The guard validates policy and authoritative primary controls, resolves the
exact credential route, binds request capabilities/model, atomically counts a
stable operation id, checks the provider-and-credential circuit, obtains Redis
permits, creates `LLMRequest(status="started")`, and only then invokes the
provider. Multi-call agents use one operation id for the entire loop; callers
cannot provide a trusted ordinal to reset the limit.

The operation counter TTL is the smaller timeout times the smaller loop cap,
plus 60 seconds (`timeout_seconds * max_loop_calls + 60`). A stable id binds
calls within that window; it is not durable idempotency across delayed job
retries. Application jobs must bound their durable attempts separately.

Redis uses owner-token leases for shared and per-feature concurrency. Request
and token keys include their minute/hour/day epoch. Only the current owner can
release a lease or reconcile its exact epoch, and reconciliation saturates at
zero. A late release cannot free newer work or decrement a newer epoch.

Admission leases expire after the smaller timeout plus 30 seconds. Bedrock
client creation and credential resolution occur after the permit and started
ledger. Its connect/read socket timeouts are not a hard elapsed deadline or
remote cancellation: credential refresh and I/O may outlive the admission
lease. These concurrency caps bound admission leases, not every still-running
AWS request.

Bedrock success and failed answers with validated token evidence reconcile
the reservation using all four usage classes, including billed reasoning and
implicit cache usage. If spend is unknown, the failure releases concurrency
but retains the full token reservation for its budget windows; missing usage
is never treated as zero. Existing Anthropic failure accounting is unchanged.
Bedrock authentication, throttling, timeout, unavailable, and server failures
count toward the circuit. Unusable output (`provider_rejected`, including an
AWS `content_filtered` result) does not open it and releases a half-open probe
as healthy. This does not add content filtering to the application.

Ledger rows store scalar context ids, provider/model, one-way credential
fingerprint, provider request id, token classes, duration, state, and safe code.
They never store prompts, responses, credentials, provider bodies, or raw
exceptions. `LLMCircuitBreaker` uses a generation and half-open owner token.
Bounded metrics cover succeeded/failed/blocked requests, tokens, breaker opens,
and shared/feature reservation burn rate. Operational guard/breaker events are
rate-limited and carry only provider, feature, and safe code—never a
fingerprint or raw error.

`LLMRequest`, `LLMCircuitBreaker`, and `IncidentLLMAttempt` have no generic
per-row REST surface. Operators receive aggregates only.

## Autonomous and explicit incident work

`LLM_AUTONOMOUS_INCIDENT_TRIAGE_ENABLED` is only the catch-all gate for event
pickup and the scheduled sweep. Its value and activation watermark are read
from unambiguous primary-database rows; any read failure, duplicate, or invalid
watermark disables catch-all work. Enabling it from the fresh-auth owner editor
stamps `LLM_AUTONOMOUS_INCIDENT_TRIAGE_ACTIVATED_AT`. The 09:00/18:00 sweep
selects only post-watermark incidents, oldest-first, 20 per run. Historical
work requires the bounded owner `historical_triage` action.

Explicit/manual incident analysis and ticket-linked LLM work remain opt-in and
do not depend on the catch-all switch. They still pass the same emergency stop,
policy, budget, circuit, capability, and credential guard.

`IncidentLLMAttempt` and its jobs `Job` outbox row commit together. Incident
and standalone-ticket active constraints make duplicate delivery converge.
Running attempts carry an owner-token lease sized for the policy loop and
heartbeat it around provider/tool work. Guard, provider, missing-input, and
loop-exhaustion failures become a queued retry or terminal state. Only triage
owns the `investigating` transition and restores its prior status on terminal
failure; ticket work never restores another workflow's status, and manual
analysis is status-neutral. Analysis progress stays true across retryable
delivery failures and clears only on success or terminal failure. A triage
`end_turn` with neither a new ticket nor a durable assessment fails as
`triage_incomplete`; ticket-created/investigating and assessed outcomes succeed.
Transient exhaustion retains a safe
cooldown (including bounded `retry_after`) and a later sweep may re-arm the
same logical attempt after it expires; permanent context errors remain
terminal. Every repair publication uses a new delivery generation, so a
failed/canceled/expired Job key cannot suppress its replacement. Repair never
requeues an unexpired worker.

States are:

- Ledger: `started`, `succeeded`, `failed`, `blocked`, `unknown`.
- Circuit: `closed`, `open`, `half_open`.
- Incident attempt: `claimed`, `queued`, `running`, `retryable`, `succeeded`,
  `terminal` (a retryable attempt normally transitions immediately back to
  `queued` when its retry job is published).

## Operator API

All routes deny key-backed sessions. The security-reader aggregate is:

```http
GET /api/account/admin/llm-safety?hours=24
```

`hours` is an integer clamped to 1–168. The response is
`{"schema_version": 2, "safety": {"hours": 24, "requests": [...],
"breakers": [...]}}`; rows are grouped aggregates, never per-row ledger,
circuit, attempt, credential fingerprint, or policy hash data.

Fresh-auth literal-owner actions use `POST /api/account/admin/assistant`:

```json
{"action": "activate_policy"}
```

```json
{"action": "reset_breaker", "provider": "anthropic"}
```

`provider` may be `anthropic` or `bedrock`; omit it to reset every breaker.
A reset increments its generation and does not change either emergency stop.

```json
{"action": "historical_triage", "before": "2026-08-31T00:00:00Z", "limit": 20}
```

`before` must be an ISO timestamp and `limit` must be an integer from 1 through
100. The response reports `queued` and fresh state. These exact actions are
available from both Admin bundles and cannot be performed through generic
settings or model REST.

## Candidate and stored verification

Only a supplied, not-yet-stored candidate may make a provider request while
stopped. Candidate/stored key verification and catalogue discovery remain
Anthropic-only; a Bedrock configuration or discovery route is refused before
SDK construction. The candidate exception never probes the AWS credential
chain. That private configuration probe is fixed to one `Reply OK` text
request, the configuration route/model, four output tokens, no system prompt,
tools, images, cache, caller context, or pagination. Its concurrency lease is
installation-wide across every candidate fingerprint. Owner/fresh-auth and
audit remain at the Assistant REST/service boundary.

Checking a stored credential targets exactly `admin` or `handler`, never
fallback resolution. It is an ordinary guarded call, is refused while stopped,
uses the configuration route/model and accounting, and forces prompt caching
off. Candidate probes require owner authority in the service as well as the
fresh-auth owner REST boundary. Model discovery traverses at most five pages
of 100 entries under a total deadline of 20 seconds or the smaller configured
shared/discovery timeout. Each page re-proves current controls/credential,
counts the same operation ID, and obtains its own breaker lease, permit and
ledger. A smaller installed `max_loop_calls` stops pagination safely; this
feature never increases deployment budgets. Partial traversal is a failure.

The shared catalogue is namespaced by version, policy hash, provider and exact
credential fingerprint. Assistant and discovery credentials must match.
Metadata explicitly declares active Sonnet line, release instant, adequate
output cap and image support when required; no family/version guessing or
invented client-tool metadata is used. Complete success with no compatible
model invalidates old selection. Freshness is 24 hours, bounded last-good use
is seven days from success after failed/incomplete refresh, and expired data
cannot make an automatic route ready. A 30-second Redis NX owner-token lock
and 60-second attempt cooldown bound refresh work; unknown coordination
cannot issue a request. The process-local fallback shares these deadlines.
Ordinary setup/bootstrap reads are passive. First authorized calls after
expiry or explicit owner refresh perform refreshes, with no background job or
SDK retry. The response reports the actual refresh outcome independently of
usable stale data. See [selection/cache details](../helpers/llm.md#catalogue-cache)
and the [additive schema-2 owner API](../../web_developer/account/admin_portal/assistant.md).

## Stable failures and retry semantics

`llm.call()` and `ask()` raise `LLMExecutionError(code, retry_after=None)`.
Provider text is discarded. `retry_after` may be populated for a bounded
provider rate-limit response. Stable codes are:

- Controls/policy: `policy_invalid`, `policy_mixed`, `route_missing`,
  `emergency_stopped`, `control_state_unknown`, `credential_missing`,
  `provider_unsupported`, `capability_unsupported`, `model_mismatch`,
  `context_invalid`, `operation_invalid`, `triage_incomplete`, `credential_mismatch`,
  `catalogue_unavailable`, `catalogue_incomplete`, `no_compatible_model`,
  `refresh_in_progress`, `refresh_throttled`.
- Limits/persistence: `input_too_large`, `output_too_large`, `loop_limit`,
  `budget_exhausted`, `concurrency_exhausted`, `permit_unavailable`,
  `ledger_unavailable`, `ledger_persistence_unknown`.
- Circuit/provider: `breaker_open`, `breaker_half_open`,
  `provider_authentication`, `provider_billing_exhausted`,
  `provider_rate_limited`, `provider_timeout`, `provider_unavailable`,
  `provider_rejected`, `provider_failed`, `safety_unavailable`.

Callers may retry only through their bounded workflow. `retry_after` is a hint,
not permission to bypass budgets or breakers. Authentication, billing/spend,
budget, and emergency-stop signals never trigger provider or credential
failover. Assistant messages, scheduled task results, incidents, ledgers, and
attempts persist only these safe codes or fixed messages.

For Bedrock, every failure after dispatch uses a `provider_*` code, including
unusable final output or uncertain persistence. An ambiguous provider result
may still be billed and must not trigger an automatic retry, even when a
socket timeout or `retry_after` is reported. AWS credential/access failures
reduce to `provider_authentication`; throttling to `provider_rate_limited`;
timeouts to `provider_timeout`; service/5xx failures to `provider_unavailable`;
validation or unusable output to `provider_rejected`; other failures to
`provider_failed`. Raw SDK messages and response bodies never cross the boundary.
