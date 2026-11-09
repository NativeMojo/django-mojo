# LLM Safety Boundary

Every external framework LLM request passes through a provider-neutral,
fail-closed boundary before network I/O. `mojo.helpers.llm.call()` and `ask()`
remain the public facade. Anthropic credentials, SDK calls, bounded one-page
model discovery, prompt-cache arguments, response conversion, and provider
error reduction live in the Anthropic adapter. A deliberately configured AWS
Bedrock route supports bounded Nova 2 Lite text reasoning and, when the route
admits `images`, validated image input. Neither adapter
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
    "max_input_bytes": 150000,
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
`capabilities` of `["text"]`, optionally plus `"tools"`, `"images"`, or both,
in any order. Adding `"tools"` lets
callers declare tools (see [Tool use on Bedrock](../helpers/llm.md#tool-use-on-bedrock));
adding `"images"` admits image input (see [Image input](#image-input)). Either
changes the policy hash, so activate the deployed policy again. A tool
loop of N rounds makes N + 1 calls under one operation id, so give the
feature envelope a `max_loop_calls` of at least that. Allowed models are exactly
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
The adapter supports text, image blocks in user messages on an image route,
caller-declared tools, and streamed answers. It
suppresses reasoning content from the final answer and from the stream, and
retains billed reasoning within `usage.output_tokens`. It sends no explicit
cache controls, guardrail configuration, or application content filter.
Streaming needs no extra policy capability, but `ConverseStream` needs the
IAM action `bedrock:InvokeModelWithResponseStream` on the same inference
profile and destination model resources that `bedrock:InvokeModel` covers.
Anthropic tool routes, key probes, and model discovery retain their existing
contracts; streaming is Bedrock-only.

## Image input

A caller passes an image as validated bytes, never a URL. Build the block with
the facade helper and send it in a user message:

```python
from mojo.helpers import llm

block = llm.image_block(png_bytes, "image/png")  # raises llm.ImageInputError
response = llm.call(
    [{"role": "user", "content": [block, {"type": "text", "text": "Describe this screenshot."}]}],
    feature="my_maestro_reasoning", operation="screenshot_describe",
    context={"operation_id": operation_id}, max_tokens=1024)
```

The block is `{"type": "image", "source": {"type": "base64", "media_type": ...,
"data": ...}}`, the same shape the Anthropic adapter takes. To admit it on a
Bedrock route, add `"images"` to its capabilities: `["text", "images"]`, or
`["text", "tools", "images"]` for a tool route. That changes the policy
hash, so update `LLM_SAFETY_POLICY_EXPECTED_HASH` with it; an existing
policy without `images` parses and hashes exactly as before.

The same block works in `llm.stream()` and inside a tool loop. In a loop the
image stays in its user turn and is resent, and revalidated, on every round;
Nova bills its 230 tokens again each round. A tool result's content stays
text-only: an image inside a `tool_result` is `image_invalid`. Document blocks
(PDF) are not supported.

Before any byte cap, permit, ledger row or adapter, the guard checks every image
in the request, for any provider:

- Media types are `image/png`, `image/jpeg`, `image/gif` and `image/webp`. The
  data must be strict base64, and its magic bytes and parsed header must match
  the media type. Only the header is parsed; pixels are not decoded here.
- At most 5 images, 5 MiB each (raw) and 20 MiB in total, each side at most
  8000 px. These are framework constants in `mojo.helpers.llm_images`.
- Images belong only as top-level blocks of a user message. An image in an
  assistant message, `system`, a tool definition or nested inside another
  block is refused.
- A route without `images` refuses an image with `capability_unsupported`. A
  malformed, mismatched or misplaced image is `image_invalid`; one over a count,
  size or side limit is `input_too_large`.

The operator's `max_input_bytes` still bounds the request text: image data is
blanked when the guard sizes the request, and each image adds 1600 tokens to
the permit reservation. Actual billed usage reconciles the permit as for text.
Nova bills a flat 230 input tokens per image, included in `input_tokens`.
GIF and WebP animations are accepted; Nova reads the first frame.

Nothing fetches an image from a URL or storage, and no log line carries image
data. A caller may say it inspected an image only after `call` returns; every
refusal is raised before dispatch or recorded on the ledger as failed.

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

`llm.stream()` passes exactly the same steps before it dispatches
`converse_stream`, so a refused stream raises before any event. Its extra
pre-dispatch checks are the route's adapter supporting streaming (only
Bedrock does) and, for every Bedrock call, the tool-loop rules: tool turns
without declared tools are `capability_unsupported`, declared tools without
an `operation_id` are `operation_invalid`, and a transcript holding more
answered tool rounds than `max_tool_rounds` (Bedrock default 8) is
`loop_limit`. The Bedrock adapter then checks the whole request shape
(messages, tool declarations and tool turns) without any SDK work; a
malformed request is `provider_rejected`. All of these refuse before the
operation counter, circuit, permit, or ledger, for plain and streamed calls.

A stream settles exactly once: on its final record (actual usage,
`succeeded`), on a provider failure (as below), or on a cancel
(`failed`/`cancelled`, full reservation kept unless the final usage report
had arrived). A cancel releases the circuit as healthy only after the stream
delivered provider events, so cancels cannot hide a provider that accepts
and then hangs. A timer ends a stream at the policy timeout, so a trickling
stream cannot outlive its admission lease. A deadline that passes while the
caller holds the stream between reads still fails as `provider_timeout`, but
does not count against the circuit; only a read left blocked by AWS does.

Bedrock success and failed answers with validated token evidence reconcile
the reservation using all four usage classes, including billed reasoning and
implicit cache usage. If spend is unknown, the failure releases concurrency
but retains the full token reservation for its budget windows; missing usage
is never treated as zero. Existing Anthropic failure accounting is unchanged.
Bedrock authentication, throttling, timeout, unavailable, and server failures
count toward the circuit, including connection resets and read timeouts in
the middle of a stream. Unusable output (`provider_rejected`, including an
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

## How token budgets are charged

Every `tokens_*` limit counts tokens: input, output, cache read and cache
write, all at full weight. Each call reserves its input estimate plus the full
`max_tokens` before it runs, and settles against the usage the provider
reports when it finishes.

- **Anthropic text and tool calls.** The guard asks Anthropic's free
  token-count endpoint for the exact request (messages, system prompt and
  tools) on the resolved model, with a 3-second timeout. It reserves
  `min(bytes, ceil(count × 1.10) + 64)` for the input, computed in integers
  (count 50 gives exactly 119), where `bytes` is the length of the
  serialized request. The count never raises a reservation
  above today's byte bound. It is not counted as a request in the budget
  windows and never touches the circuit breaker.
- **Byte fallback.** Bedrock routes (Nova 2 Lite has no count endpoint),
  requests containing images, and any count that fails, times out or returns
  something other than a positive integer reserve the serialized byte length,
  exactly as before.
- **Output headroom** is always the full `max_tokens`.
- **Settling.** Usage below the reservation is refunded. Usage above it is
  charged at once to every token window that is still live. The one corner:
  a call that outlives its minute window charges the excess to the hour and
  day windows only, because an expired window is never recreated.
- **Windows** are per provider and credential fingerprint, for the shared
  envelope and the feature, so two features on one key share the shared
  windows.
- **Two budget codes.** A reservation above the smaller `tokens_minute`
  (shared or feature) can never fit: it is refused with
  `request_exceeds_budget`, permanently, before anything is written. A
  reservation that would overfill a current window is refused with
  `budget_exhausted` and `retry_after`: the seconds until the first refusing
  window rolls over. `concurrency_exhausted` carries no hint.

Each ledger row records `estimated_input_tokens` (the provider count, 0 for
the byte fallback) and `estimate_source` (`count` or `bytes`). To compare the
estimate with billed input on real traffic:

```python
from django.db.models import Avg, Count, F, Max, Min
from mojo.apps.account.models import LLMRequest

(LLMRequest.objects
    .filter(status="succeeded", estimate_source="count", input_tokens__gt=0)
    .annotate(billed=F("input_tokens") + F("cache_read_input_tokens")
              + F("cache_creation_input_tokens"))
    .values("feature", "model")
    .annotate(calls=Count("id"),
              mean_ratio=Avg(F("estimated_input_tokens") * 1.0 / F("billed")),
              low=Min(F("estimated_input_tokens") * 1.0 / F("billed")),
              high=Max(F("estimated_input_tokens") * 1.0 / F("billed"))))
```

A ratio near 1.0 means the count matches billed input; the 10% + 64 margin
sits on top of it.

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

- Ledger: `started`, `succeeded`, `failed`, `blocked`, `unknown`. A cancelled
  stream is `failed` with the ledger-only code `cancelled`; it is never
  raised to a caller.
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

`llm.call()`, `ask()` and `stream()` (from the call or from iteration) raise
`LLMExecutionError(code, retry_after=None)`.
Provider text is discarded. `retry_after` may be populated for a bounded
provider rate-limit response, and for `budget_exhausted` (the seconds until the
first refusing budget window rolls over). Stable codes are:

- Controls/policy: `policy_invalid`, `policy_mixed`, `route_missing`,
  `emergency_stopped`, `control_state_unknown`, `credential_missing`,
  `provider_unsupported`, `capability_unsupported`, `model_mismatch`,
  `context_invalid`, `operation_invalid`, `triage_incomplete`, `credential_mismatch`,
  `catalogue_unavailable`, `catalogue_incomplete`, `no_compatible_model`,
  `refresh_in_progress`, `refresh_throttled`.
- Image input: `image_invalid`.
- Limits/persistence: `input_too_large`, `output_too_large`, `loop_limit`,
  `request_exceeds_budget`, `budget_exhausted`, `concurrency_exhausted`,
  `permit_unavailable`, `ledger_unavailable`, `ledger_persistence_unknown`.

`request_exceeds_budget` is permanent: the call alone is larger than the
smallest `tokens_minute` limit, so retrying it can never succeed and incident
work does not retry it. `budget_exhausted` is retryable after `retry_after`.
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
