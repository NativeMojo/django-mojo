# LLM Safety Boundary

Every external framework LLM request passes through a provider-neutral,
fail-closed boundary before network I/O. `mojo.helpers.llm.call()` and `ask()`
remain the public facade. Anthropic credentials, SDK calls, bounded one-page
model discovery, prompt-cache arguments, response conversion, and provider
error reduction live in the Anthropic adapter. This release deliberately has
no second production provider adapter and no automatic provider or credential
failover.

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
        "assistant": {"provider": "anthropic", "model": "claude-sonnet-5",
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
                            "max_output_tokens": 16, "max_loop_calls": 1},
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
exactly matches the route. `credential: "admin"` requires
`LLM_ADMIN_API_KEY`; it never silently falls back to the handler credential.

Each installation needs its own provider credential. Provider-organization
caps remain the final cross-installation backstop. Central billing, dynamic
provider-credit changes, price tables, a second provider, automatic failover,
and production-data cleanup are non-goals.

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
4. Verify candidate credentials if needed, reset breakers only after their
   cause is corrected, then remove the static `LLM_EMERGENCY_STOP=True` from
   deployment configuration and redeploy. Clearing only the database checkbox
   cannot override a static true value.

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

Redis uses owner-token leases for shared and per-feature concurrency. Request
and token keys include their minute/hour/day epoch. Only the current owner can
release a lease or reconcile its exact epoch, and reconciliation saturates at
zero. A late release cannot free newer work or decrement a newer epoch.

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
loop-exhaustion failures become a queued retry or terminal state; terminal
incident work restores its prior status. Transient exhaustion retains a safe
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

Omit `provider` to reset every breaker. A reset increments its generation and
does not change either emergency stop.

```json
{"action": "historical_triage", "before": "2026-08-31T00:00:00Z", "limit": 20}
```

`before` must be an ISO timestamp and `limit` must be an integer from 1 through
100. The response reports `queued` and fresh state. These exact actions are
available from both Admin bundles and cannot be performed through generic
settings or model REST.

## Candidate and stored verification

Only a supplied, not-yet-stored candidate may make a provider request while
stopped. That private configuration probe is fixed to one `Reply OK` text
request, the configuration route/model, four output tokens, no system prompt,
tools, images, cache, caller context, or pagination. Its concurrency lease is
installation-wide across every candidate fingerprint. Owner/fresh-auth and
audit remain at the Assistant REST/service boundary.

Checking a stored credential targets exactly `admin` or `handler`, never
fallback resolution. It is an ordinary guarded call, is refused while stopped,
uses the configuration route/model and accounting, and forces prompt caching
off. Candidate probes require owner authority in the service as well as the
fresh-auth owner REST boundary. Model discovery is one page of at most 100 models with the policy
timeout and one permit/ledger; it does not paginate invisibly.

## Stable failures and retry semantics

`llm.call()` and `ask()` raise `LLMExecutionError(code, retry_after=None)`.
Provider text is discarded. `retry_after` may be populated for a bounded
provider rate-limit response. Stable codes are:

- Controls/policy: `policy_invalid`, `policy_mixed`, `route_missing`,
  `emergency_stopped`, `control_state_unknown`, `credential_missing`,
  `provider_unsupported`, `capability_unsupported`, `model_mismatch`,
  `context_invalid`, `operation_invalid`.
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
