# LLM Safety Boundary

Every external framework LLM request passes through a provider-neutral,
fail-closed boundary before network I/O. `mojo.helpers.llm.call()` and `ask()`
remain the public facade. Anthropic credentials, SDK calls, bounded one-page
model discovery, prompt-cache arguments, response conversion, and provider
error reduction live in the Anthropic adapter. A deliberately configured AWS
Bedrock route supports bounded Nova 2 Lite or Claude (Haiku 5.5, Sonnet 5.5,
Opus 5.5) text reasoning and, when the route admits `images`, validated image
input. Neither adapter
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
adding `"images"` admits image input (see [Image input](#image-input)). A
Claude route may also add `"prompt_cache"` (see
[Prompt caching on Claude routes](#prompt-caching-on-claude-routes)); on a Nova
route it is `policy_invalid`. Each of these
changes the policy hash, so activate the deployed policy again. A tool
loop of N rounds makes N + 1 calls under one operation id, so give the
feature envelope a `max_loop_calls` of at least that. `model` must be one of
the ids in [the Bedrock model allow-list](#bedrock-model-allow-list), spelled
exactly. `reasoning_efforts` must be a nonempty, unique list drawn from `off`,
`low`, and `medium`. Region syntax is checked locally; AWS authorizes the actual
profile/region combination. Choose a source region supported by that exact
profile in the model's AWS model card (linked in the allow-list).
The framework does not enable Bedrock or activate this example policy by default.

### Bedrock model allow-list

The adapter and the policy check read one list,
`mojo.helpers.llm_providers.bedrock.BEDROCK_MODELS`. A route's `model` must be
one of these ids exactly; anything else, including a bare model id, an ARN, a
`-v1:0` suffix on a Claude id or a region prefix the model does not publish, is
`policy_invalid`. There is no prefix matching.

| Model | Inference-profile ids | AWS model card |
|---|---|---|
| Nova 2 Lite | `us.amazon.nova-2-lite-v1:0`, `eu.amazon.nova-2-lite-v1:0`, `jp.amazon.nova-2-lite-v1:0`, `global.amazon.nova-2-lite-v1:0` | [Nova 2 Lite](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-amazon-nova-2-lite.html) |
| Claude Haiku 5.5 (fast) | `us.anthropic.claude-haiku-5-5`, `ca.anthropic.claude-haiku-5-5`, `eu.anthropic.claude-haiku-5-5`, `au.anthropic.claude-haiku-5-5`, `jp.anthropic.claude-haiku-5-5`, `global.anthropic.claude-haiku-5-5` | [Claude Haiku 5.5](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-haiku-5-5.html) |
| Claude Sonnet 5.5 (general) | `us.anthropic.claude-sonnet-5-5`, `ca.anthropic.claude-sonnet-5-5`, `eu.anthropic.claude-sonnet-5-5`, `au.anthropic.claude-sonnet-5-5`, `global.anthropic.claude-sonnet-5-5` | [Claude Sonnet 5.5](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-sonnet-5-5.html) |
| Claude Opus 5.5 (powerful) | `us.anthropic.claude-opus-5-5`, `eu.anthropic.claude-opus-5-5`, `au.anthropic.claude-opus-5-5`, `jp.anthropic.claude-opus-5-5`, `global.anthropic.claude-opus-5-5` | [Claude Opus 5.5](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-opus-5-5.html) |

Sonnet 5.5 has no `jp.` profile and Opus 5.5 has no `ca.` profile.

### Claude routes

A Claude route is a Bedrock route with a Claude id. Everything else about the
route is the same as for Nova:

```python
LLM_SAFETY_POLICY["routes"]["my_maestro_mail"] = {
    "provider": "bedrock",
    "model": "us.anthropic.claude-sonnet-5-5",
    "credential": "aws",
    "capabilities": ["text", "tools", "images"],
    "region": "us-east-1",
    "reasoning_efforts": ["off", "low", "medium"],
}
```

Claude models think by default on Bedrock, so the adapter always tells a Claude
model how much to think. The effort a caller passes maps to this
`additionalModelRequestFields` value:

| Effort | Haiku 5.5 | Sonnet 5.5 | Opus 5.5 |
|---|---|---|---|
| `off` | `{"thinking": {"type": "disabled"}, "output_config": {"effort": "low"}}` | `{"thinking": {"type": "between_tools"}, "output_config": {"effort": "low"}}` | `{"thinking": {"type": "adaptive"}, "output_config": {"effort": "low"}}` |
| `low` | `{"thinking": {"type": "adaptive"}, "output_config": {"effort": "low"}}` | same | same |
| `medium` | `{"thinking": {"type": "adaptive"}, "output_config": {"effort": "medium"}}` | same | same |

`off` is the least thinking the model allows: Haiku does not think, Sonnet does
not think before it answers, and Opus cannot stop thinking, so on Opus `off`
and `low` send the same request. Nova's `reasoningConfig` is never sent to a
Claude model. `high` and above are not supported on either family.

Claude thinking is billed as output tokens and counts inside `max_tokens`. Size
`max_tokens` for the thinking as well as the answer: a reply that reaches
`max_tokens` before any answer text is `provider_rejected`, its usage is
charged, and the ledger row's `error_detail` says
`max_tokens reached before any answer text`. Thinking is never returned to the
caller or echoed back on a later tool round.

Claude routes have their own circuit: one `LLMCircuitBreaker` row shared by all
three Claude models, separate from the Nova row, so a Claude access, IAM or
throttling fault does not stop Nova calls, and the reverse. Permits, token
budgets, the operation counter and the ledger's `credential_fingerprint` stay
on the one installation identity, so Claude and Nova calls draw on the same
Bedrock token budget. `reset_breakers(actor, provider="bedrock")` resets both
circuits. Owner diagnostics and `reset_breakers("bedrock")` report and reset
the two Bedrock circuits together and do not label which is which. A half-open
probe that a budget or concurrency limit refuses gives its turn back at once,
so the next call may probe; this holds for Anthropic routes too.

Claude image limits: an image over 3,750,000 raw bytes (Bedrock's 5 MB base64
limit for Claude) is `provider_rejected` before any reservation, and each
image reserves 4800 tokens instead of Nova's 1600.

### Prompt caching on Claude routes

A Claude route that lists `"prompt_cache"` lets callers mark where the cacheable
part of a prompt ends, so a repeated question reads that part from Bedrock's
cache instead of paying for it again:

```python
LLM_SAFETY_POLICY["routes"]["my_maestro_call"] = {
    "provider": "bedrock",
    "model": "us.anthropic.claude-sonnet-5-5",
    "credential": "aws",
    "capabilities": ["text", "prompt_cache"],
    "region": "us-east-1",
    "reasoning_efforts": ["off", "low"],
}
```

Adding `"prompt_cache"` is an ordinary policy change: the hash rule, the
emergency stop and activation are the same as for any other capability. The
caller's marker and the Converse request it becomes are described in
[Prompt caching on Bedrock](../helpers/llm.md#prompt-caching-on-bedrock).
Nothing is cached without a marker: the adapter never sends Converse's
automatic-caching field (`additionalModelRequestFields.cache_control`), so a
caching route sends the same request as before until a caller marks one.

- Only Claude routes may declare it; Nova with `"prompt_cache"` is
  `policy_invalid`.
- On a route that does not declare it, a marker is `provider_rejected` before
  any reservation, exactly as before, and the request is unchanged.
- `LLM_ADMIN_PROMPT_CACHE_ENABLED` governs Anthropic's automatic caching only.
  It never turns Bedrock caching on or off; remove `"prompt_cache"` from the
  route to stop it.
- Cache reads and writes still count against every `tokens_*` budget (see
  [How token budgets are charged](#how-token-budgets-are-charged)). Budgets
  count tokens, not price: a 1-hour cache write costs more per token on the
  AWS bill than a 5-minute one, and a cache read costs less.

### Enabling Anthropic models in the AWS account

Each Anthropic model is enabled once per AWS account, by an administrator:

1. In the Bedrock console, open **Model catalog**, choose the model, and open it
   in the playground. The first invocation of a model in the account starts an
   AWS Marketplace subscription for it.
2. The person doing this needs `aws-marketplace:Subscribe`,
   `aws-marketplace:Unsubscribe` and `aws-marketplace:ViewSubscriptions`, and
   the account needs a valid payment method. Calls may succeed for up to 15
   minutes while the subscription finalises; if a prerequisite is missing the
   subscription fails and later calls return `AccessDeniedException`
   (`provider_authentication`). Some accounts also need extra account-level
   access from AWS Sales.
3. Repeat for each Claude model a route names.

The server role is never given Marketplace permissions. Once a model is
enabled it needs only `bedrock:InvokeModel` and
`bedrock:InvokeModelWithResponseStream` on the Claude inference profile and its
destination foundation-model resources, as for Nova below. Claude is a
third-party model billed through AWS Marketplace: its charges appear on the AWS
bill under the model provider, not under Amazon Bedrock.

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
role credentials does not reset budgets or the circuits. Claude routes use a
separate circuit row (see [Claude routes](#claude-routes)). The two Anthropic key
settings are never read to resolve a Bedrock credential.

See [the call and response contract](../helpers/llm.md#bedrock-reasoning).
The adapter supports text, image blocks in user messages on an image route,
caller-declared tools, and streamed answers. It
suppresses reasoning content from the final answer and from the stream, and
retains billed reasoning within `usage.output_tokens`. It sends cache
checkpoints only where a caller marks them on a `"prompt_cache"` Claude route,
and never a guardrail configuration or application content filter.
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
- On a Claude Bedrock route only, an image over 3,750,000 raw bytes but within
  the 5 MiB limit above is `provider_rejected`, still before any reservation
  or ledger row (see [Claude routes](#claude-routes)).

The operator's `max_input_bytes` still bounds the request text: image data is
blanked when the guard sizes the request, and each image adds 1600 tokens to
the permit reservation (4800 on a Claude Bedrock route, where an image over
3,750,000 raw bytes is also refused first). Actual billed usage reconciles the
permit as for text.
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
3. Activate the deployed policy: an agent with an MCP execution grant calls
   the assistant tool `activate_llm_policy`, or an owner chooses **Activate
   deployed policy** in either Admin bundle (fresh-auth). Either one writes and
   audits the expected hash, records the policy it was taken from
   (`LLM_SAFETY_POLICY_ACTIVATED`), and returns a `tldr` plus a `changes`
   list saying what the activation changed compared with the last activated
   policy: features added or removed, models and settings that moved, limits
   raised or lowered. The agent tells the owner that TL;DR.
   `get_llm_policy_change` (or `llm_safety.policy_change`) gives the same
   summary without activating, plus the deployed `hash`; the tool must be
   given that hash and refuses `policy_changed` when the deployed policy is no
   longer that one. The recorded policy is a baseline only when its own hash is
   the activated one; otherwise the summary says it could not compare. A
   policy activated by a release older than this record reports
   `record_needed`: activate it once (nothing changes) to record it.
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
- **Byte fallback.** Bedrock routes (the guard does not count Bedrock input),
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

The answer carries `tldr` and `changes`, the plain-words summary of what the
activation changed (`llm_policy_change.describe`).

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

One exception, for diagnosis: when a provider answers with an error, the
ledger row's `error_detail` keeps the provider's own reason, cut to 512
characters. For Anthropic that is the error type and message from the API's
error body, such as `invalid_request_error: messages.1.content.0: ...`, else
`HTTP <status>` when the body has no usable error object (the SDK's own
message would render the whole response body). For Bedrock it is the AWS error code and message, such as
`ValidationException: ...`. The same text goes to `llm.log` as one warning
with the ledger id, provider, code and provider request id. Callers still see
only the safe code, and `str()` of the provider error is still the code.
Failures with no provider answer (timeouts, connection and credential errors,
guard refusals such as budgets and breakers) leave `error_detail` blank.

When the framework's own checks refuse an answer the provider did return,
`error_detail` names the check in the framework's own words, for example
`the answer asks for a tool that was not declared`,
`unexpected stop reason content_filtered`, `output tokens exceed max_tokens`
or `the stream ended without a stop reason`. An unclassified failure after
dispatch records only the exception's class, `unexpected <ClassName>`. A
request the adapter refuses before dispatch has no ledger row; its check is
logged once to `llm.log` instead.

What the adapters read (#11908): the fields an answer needs are read and
checked, and everything else is ignored. Extra keys at any level, unknown
stream event kinds, unknown delta kinds and unknown content block kinds are
skipped and never forwarded to the caller; a tool the provider runs itself
(a Bedrock `toolUse` whose `type` is not `tool_use`) is skipped with all of
its deltas. A Bedrock stream block whose start kind is unknown is read by
its deltas: text deltas are answer text, any other kind is skipped. An
Anthropic answer is normalized like a Bedrock one: `id`, `model`, `role`,
`stop_reason`, `stop_sequence`, the four usage counters, and `text`,
`tool_use`, `thinking` and `redacted_thinking` blocks with their own fields
only.

On our own requests a key the adapter does not know is dropped, never sent.
On Bedrock, keys that change meaning or cost are still refused:
`cache_control` anywhere but a text block on a caching route, a tool `type`
other than `custom`, and a tool's `strict`. On Anthropic, a block kind or
tool `type` the adapter does not know passes unchanged for the provider to
judge, and on the blocks and tools it knows `cache_control` is kept only
while prompt caching is on. The
safety checks stand: a tool request must name a declared tool, its input
must be a JSON object without NaN or Infinity, ids match their patterns,
every tool request is answered by exactly the next user message, usage stays
within the caps, the stop reason must match the content, and image and
cache-checkpoint limits are unchanged. Image blocks stay exact: an extra key
on one is refused.

The rule for this text:

- The framework never adds request content (messages, system text, tool input
  or tool definitions) to `error_detail` or the log line. It reads only the
  error the provider returns.
- It stores the provider's own error text verbatim, cut to 512 characters.
  Nothing is redacted.
- That text may quote a fragment of the request when the provider echoes one
  in its error.
- Only people with database or server-log access can read it: `LLMRequest`
  has no REST surface.

For Bedrock, every failure after dispatch uses a `provider_*` code, including
unusable final output or uncertain persistence. An ambiguous provider result
may still be billed and must not trigger an automatic retry, even when a
socket timeout or `retry_after` is reported. AWS credential/access failures
reduce to `provider_authentication`; throttling to `provider_rate_limited`;
timeouts to `provider_timeout`; service/5xx failures to `provider_unavailable`;
validation or unusable output to `provider_rejected`; other failures to
`provider_failed`. Raw SDK messages and response bodies never cross the boundary.
