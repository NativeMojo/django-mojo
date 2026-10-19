# LLM Helper — Django Developer Reference

Provider-neutral LLM facade with Anthropic and a text-only AWS Bedrock Nova 2
Lite adapter. Production requests use the mandatory
[LLM safety boundary](../security/llm_safety.md).

```python
from mojo.helpers import llm
```

## API Key

These key helpers, verification, and model discovery remain Anthropic-only.
Bedrock uses the server's AWS role/profile chain; see
[Bedrock reasoning](#bedrock-reasoning).

```python
key = llm.get_api_key()
# Resolution: LLM_ADMIN_API_KEY -> LLM_HANDLER_API_KEY -> None

ok, error = llm.verify_api_key("admin")
# Returns (True, None) or (False, "error message")
# Pass "handler" to check that exact stored target. Candidate material is
# accepted only by the fresh-owner Assistant setup service.
```

## Model Selection

```python
model = llm.get_model("general")   # latest Sonnet (balanced)
model = llm.get_model("powerful")  # latest Opus (max intelligence)
model = llm.get_model("fast")      # latest Haiku (quick/cheap)
```

This resolution order supplies picker suggestions and legacy non-guarded
configuration reads:
1. Explicit setting pin (`LLM_ADMIN_MODEL` or `LLM_HANDLER_MODEL`) — if set, returned as-is
2. Auto-detect from Anthropic `/v1/models` endpoint (cached 24h in Redis, in-memory fallback)
3. Hardcoded fallback if API is unreachable

### Tiers

| Use case | Family |
|---|---|
| `"powerful"` | Opus |
| `"general"` | Sonnet |
| `"fast"` | Haiku |

Guarded calls use the exact literal model from their policy, except for the
explicit Anthropic Assistant delegation `routes.assistant.model="auto:sonnet"`.
Only that token enables automatic selection. Existing literal policies remain
fixed until a deployment change and normal owner policy-hash activation.
Inspect or clear any existing `LLM_ADMIN_MODEL` before opting in: it becomes
an active pin on the delegated route.

On a delegated route the priority is owner-stored `LLM_ADMIN_MODEL`, then
file-owned `LLM_ADMIN_MODEL`, then discovery. `LLM_HANDLER_MODEL` and the
handler credential never substitute for this route. Literal policy and
Bedrock models remain authoritative over legacy pins. `model=` can only
repeat the resolved ID; it cannot override selection. One invocation snapshots
that ID for mismatch checks, ledger, adapter and returned `model`, including
calls from the streamed Assistant loop.

Automatic selection requires explicit provider metadata: `line="sonnet"`,
`lifecycle="active"`, a real timezone-aware `created_at` release timestamp,
adequate positive `max_tokens` for the effective configured output cap, and
`capabilities.image_input.supported=true` if the route delegates images.
Missing/unknown metadata and conflicting duplicate IDs are excluded. Newest
release instant wins, with opaque ID as the deterministic tie breaker; names
and version numbers do not choose the family. Client tools and prompt caching
remain controlled by the existing policy and adapter capability checks; the
Models API does not advertise a general client-tool support flag.

Modern dateless IDs are pinned release snapshots, not floating aliases.
`get_model(use)` retains its older naming/alias preference and fallback IDs
for legacy configuration callers only. It never selects the guarded automatic
Assistant model; use `llm_safety.model_selection_state()` for passive runtime
diagnostics.

### Catalogue cache

All catalogue consumers share a versioned cache scoped by policy hash,
provider and exact discovery credential fingerprint. Discovery must use the
same credential identity as the automatic Assistant. Normal owner/bootstrap
GETs inspect cached state and never contact a provider.

A complete catalogue is fresh for 24 hours. On expiry the first authorized
invocation may refresh; an explicit owner refresh can bypass freshness. A
failed or incomplete attempt may retain last-good data until seven days from
its original success. Failures never extend that deadline. Expired data gives
no effective automatic model. A successful complete catalogue that contains
no compatible active Sonnet immediately invalidates the old selection.

Redis coordinates a 30-second owner-token `SET NX EX` lock and 60-second
attempt cooldown. Losers use usable cached data or return unavailable without
waiting. A bounded process-local last-good cache follows the same age bounds;
unknown Redis coordination never authorizes an uncontrolled network request.
Refreshes use at most five pages of 100 models/500 total and a total deadline
of the smaller of 20 seconds, discovery timeout and shared timeout. Each page
passes safety controls, operation counting, breaker, permit and durable ledger
independently, with one operation ID across pages. Missing/repeated cursors,
a page failure, deadline or smaller installed loop budget make the refresh
incomplete. SDK retries stay disabled; there is no background scheduler.

```python
models = llm.get_models()  # shared cache; coordinated refresh on expiry
models = llm.get_models(force_refresh=True)  # still obeys lock/cooldown/safety
```

Owner refresh reports that request's result in `model.refresh`: a usable
last-good model never turns a failed attempt into success. See the exact
[owner API](../../web_developer/account/admin_portal/assistant.md).

## Quick Calls

### `ask()` — One-shot question

```python
answer = llm.ask("Summarize this text: ...", feature="scheduled_task")
answer = llm.ask("Classify this: ...", feature="file_analysis")
```

Returns a string. No tools, no conversation. Good for summarization, classification, text generation.

### `call()` — Full messages API

```python
response = llm.call(
    messages=[{"role": "user", "content": "Hello"}],
    system="You are a helpful assistant.",
    tools=[...],           # optional tool definitions
    # model omitted: policy literal or delegated resolved ID owns selection
    max_tokens=4096,       # optional
    reasoning_effort="off",  # default; Anthropic accepts only off
    feature="assistant",  # required for framework callers
    context={"conversation_id": 42, "operation_id": "stable-loop-uuid"},
)
# Returns a normalized response dict, including content and usage
```

Raises `LLMExecutionError(code, retry_after=None)` when policy, controls,
budgets, the circuit, persistence, or provider denies the request. Raw
SDK/provider text does not cross the adapter. Multi-call loops must reuse one
unguessable `operation_id`; the guard increments it atomically. Omitted
`feature` temporarily becomes separately budgeted `unattributed`; unknown
explicit features are refused.
A host app adds its own feature names with `LLM_HOST_FEATURES`; see
[Host features](../security/llm_safety.md#host-features).

## Bedrock reasoning

A deployment may deliberately add a Bedrock text route for a host feature;
see the [policy and AWS requirements](../security/llm_safety.md#bedrock-routes).
There is no default Bedrock route or automatic provider/credential fallback.
The installation supplies AWS access, so end users need no individual AI
subscription or computer setup.

```python
response = llm.call(
    messages=[{"role": "user", "content": "Compare these two designs: ..."}],
    system="Return a concise final answer.",
    feature="my_maestro_reasoning",
    operation="reason",
    context={"job_id": queue_job.id, "operation_id": str(personal_job.uid)},
    max_tokens=4096,
    reasoning_effort="low",
)
answer = "\n".join(block["text"] for block in response["content"])
usage = response["usage"]
```

`ask(..., reasoning_effort="low")` also returns final text. The keyword-only
effort defaults to `off`; Bedrock permits only the route's declared subset of
`off`, `low`, and `medium`. Off omits extended thinking; low/medium send the
fixed reasoning configuration. High effort is unsupported because this
adapter always sends an output cap. `max_tokens` must be a positive integer
within both policy caps and the adapter ceiling of 65000, including billed
reasoning.

Messages have exactly `role` (`user` or `assistant`) and `content`; content is
a nonblank string or a nonempty list of `{"type": "text", "text": "..."}`
blocks with nonblank text.
System content accepts the same text forms. Tools, images, streaming,
arbitrary model fields, and explicit cache controls are unsupported. The
adapter sends no Bedrock guardrail configuration or application content filter.

The response has `provider="bedrock"`, the pinned `model`, AWS request `id`,
`stop_reason`, final `content` text blocks, and these usage classes:

| Field | Meaning |
|---|---|
| `input_tokens` | Input outside the cache classes |
| `cache_read_input_tokens` | Implicit cache reads |
| `cache_creation_input_tokens` | Implicit cache writes |
| `output_tokens` | All billed output, including reasoning |

Total input is the sum of the three input classes. Reasoning/redacted blocks
never become answer text; there is no separate exact reasoning-token count
to add to output. `end_turn` is complete; a text-bearing `max_tokens` result
is partial. Empty, reasoning-only, filtered, malformed, over-cap, or over-1-MiB
final output raises a safe provider error, retaining any validated billed
usage in the ledger. No explicit cache checkpoints are sent, but implicit
cache usage is still accounted for.

An application-owned handler published with the existing `jobs.publish` can
make this call; keep AWS credentials out of browser input, job payloads, and
logs. Reuse one stable operation id for one durable application job;
different jobs from the same spoken turn need different ids. The operation
counter expires, so durable attempts need their own bounds. Socket timeouts
do not guarantee cancellation, and ambiguous `provider_*` failures must not
be automatically replayed; see [retry semantics](../security/llm_safety.md#stable-failures-and-retry-semantics).

## `model_choices()` — picker suggestions

```python
from mojo.helpers import llm

llm.model_choices()               # from the shared 24h cache, no network call
llm.model_choices(refresh=True)   # explicit operator-driven refresh
```

Returns `[{"id", "label"}]`, capped at 40, filtered to the opus/sonnet/haiku
families and sorted newest-first by the same `_rank_key` `get_model()` uses. The
label is the API's `display_name`, or the id when it has none.

**It never returns an empty list.** When the catalogue is unavailable — no key
yet, or the API is unreachable — it returns the three legacy `_FALLBACKS` IDs,
for legacy use-case callers only. The owner setup contract uses known
metadata choices (which may be empty), and automatic runtime never uses
hardcoded fallback IDs.

It reads the cache by default and does **not** fetch: a settings page that
fetched on every render would spend a provider round trip to draw a dropdown.
It also never accepts a candidate key, because fetching under an unsaved
credential would write the shared 24-hour cache from a key the installation is
not running.

These are suggestions only. Nothing validates a saved pin against this list: the
list is network-dependent, so validating against it would refuse a perfectly
good re-save whenever the cache has lapsed and the API is down.

## Settings

| Setting | Purpose |
|---|---|
| `LLM_ADMIN_API_KEY` | Checked first by `get_api_key()` |
| `LLM_HANDLER_API_KEY` | Fallback — the platform key, settable from the built-in Admin's Assistant setup |
| `LLM_ADMIN_MODEL` | Owner/file manual pin on a delegated Assistant route; legacy `get_model()` pin |
| `LLM_HANDLER_MODEL` | Second-tier pin |
| `LLM_SAFETY_POLICY` | Required file-owned provider routes and cost envelopes |
| `LLM_SAFETY_POLICY_EXPECTED_HASH` | Protected owner-activated DB agreement; never edit directly |
| `LLM_EMERGENCY_STOP` | Static OR protected database stop; unknown database state denies |
| `LLM_AUTONOMOUS_INCIDENT_TRIAGE_ENABLED` | Protected owner switch, default off |

If no model setting is pinned, `get_model()` auto-detects from the API.
