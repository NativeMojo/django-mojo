# LLM Safety Boundary

Every external framework LLM request passes through a provider-neutral,
fail-closed boundary before network I/O. `mojo.helpers.llm.call()` and `ask()`
remain stable; Anthropic-specific credentials, SDK calls, model discovery,
prompt-cache arguments, response/usage conversion, and errors live behind the
Anthropic provider adapter. No second production adapter ships in this change.

## Deployment requirements

Each django-mojo installation must use its own provider credential. Database
and Redis controls coordinate only one installation; provider-organization
spend caps are the final backstop across installations. Never share a project
credential or rely on automatic provider/credential failover: billing,
authentication, budget, and breaker failures stop the call.

`LLM_SAFETY_POLICY` is file-owned, required, versioned, and exact-schema. An
absent or invalid policy denies every external request. It contains `version:
1`, `routes`, an installation-wide `shared` envelope, per-feature `features`
envelopes, and positive `breaker` thresholds. Routes have exactly `provider`,
`model`, `credential` (`admin` or `handler`), and `capabilities`; the provider
is never inferred from the model string.

Every envelope has positive integer `requests_minute`, `requests_hour`,
`requests_day`, `tokens_minute`, `tokens_hour`, `tokens_day`, `concurrency`,
`max_input_bytes`, `max_output_tokens`, `timeout_seconds`, and
`max_loop_calls`. Minute limits cannot exceed hour limits, and hour limits
cannot exceed day limits. Fixed features are `assistant`, `incident_triage`,
`incident_analysis`, `incident_ticket`, `scheduled_task`, `memory`,
`file_analysis`, `configuration`, `model_discovery`, and transitional
`unattributed`.

## Ordering and evidence

The boundary validates policy and primary controls, checks the
provider-and-credential-fingerprinted circuit, atomically reserves Redis
request/token/concurrency budgets, creates `LLMRequest(status="started")`, and
only then invokes the provider. The reservation includes serialized input bytes
and allowed output tokens, then reconciles downward from provider usage.
Pre-network failures release or expire the reservation. Post-network ledger
uncertainty never fabricates success; the started row remains repairable.

Ledger rows store scalar context ids, provider/model, a one-way credential
fingerprint, provider request id, token classes, duration, status, and a fixed
error code. They never store prompts, responses, credentials, provider bodies,
or raw exceptions. `LLMCircuitBreaker` uses a generation and half-open owner
token so old successes and non-owners cannot close or release newer state.

## Autonomous incident work

`LLM_AUTONOMOUS_INCIDENT_TRIAGE_ENABLED` defaults off. Enabling it in the
fresh-auth owner editor stamps `LLM_AUTONOMOUS_INCIDENT_TRIAGE_ACTIVATED_AT`.
The 09:00/18:00 sweep selects only post-watermark incidents, oldest-first, 20
per run; overflow remains `new`. Historical work requires the bounded owner
`historical_triage` action.

`IncidentLLMAttempt` and its jobs-system `Job` outbox row commit in one
transaction. Active-attempt and logical-key constraints make duplicate sweep or
handler delivery harmless. Worker leases, retry/terminal states, prior status,
safe errors, and `repair_attempts()` keep failures recoverable.

## Emergency response and recovery

1. Set deployment `LLM_EMERGENCY_STOP=True`, or switch on the protected
   database stop from either Admin bundle. Deployment true cannot be overridden.
2. Inspect aggregate state in Assistant setup or
   `GET /api/account/admin/llm-safety`; fingerprints are never returned.
3. Correct the provider account, credential, or operator-approved envelope.
4. Run the owner-only fixed candidate-key check. It is the only stopped-state
   provider exception and consumes one single-flight `configuration` permit.
5. Reset circuits with the fresh-auth owner action, then clear the database
   stop. Keep provider organization caps enabled throughout.

Production cleanup remains separate operational work. Migrations create only
`LLMRequest`, `LLMCircuitBreaker`, and `IncidentLLMAttempt`; they do not mutate
historical incidents, jobs, credits, or provider configuration.
