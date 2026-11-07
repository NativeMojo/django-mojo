"""
Live check of the #9598 input reservation: Anthropic `count_tokens` + margin.

Three small live requests, each through the guarded path (llm_safety), each
leaving LLMRequest ledger rows:

  1. plain chat: one short user message, in-process through llm.call;
  2. one Assistant turn that uses two cheap read-only core tools
     (describe_model, then query_model: three provider calls), over REST
     exactly as 3_test_live_assistant does;
  3. one call with a long (~28 KB, ~7k token) system prompt, in-process.

For every provider call the band (#9598 plan step 6, Q5) is:
  * estimated_input_tokens within +/-5% of billed input;
  * reserved input >= billed input and <= 1.20 x billed input + 64.

"Billed input" is input_tokens + cache_read_input_tokens +
cache_creation_input_tokens: Anthropic's input_tokens excludes the cached
prefix, while count_tokens counts the whole prompt, and the guard releases
its permit against that same sum (llm_safety._execute).

"Reserved input" is reserved_tokens - max_tokens: the ledger's
reserved_tokens is reserved_input + max_tokens (llm_safety._execute), and
the ledger has no max_tokens column, so each scenario uses the max_tokens it
knows: 16 for the in-process calls, llm.call's default for the Assistant.

The test brings its own tight LLM_SAFETY_POLICY (one `assistant` route on
the `handler` credential) and its protected agreement row, as
test_assistant_extended_serial/34_test_rest_attachments does. The policy only
caps this throwaway test project; it raises no real limit. The server sees
it through th.server_settings (a reload, legal in this serial package); the
in-process calls see it through override_settings, because llm_safety reads
the policy with settings.get_static, which reads django.conf.settings live.

Needs a real LLM_HANDLER_API_KEY (settings or environment); skips without
one, before any request.

    bin/run_tests --agent --all -t test_assistant_slow.4_test_live_token_estimate
"""
import inspect
import json
import os
import time
import uuid

from testit import TestitSkip
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TEST_EMAIL = 't9598-live-estimate@example.com'
TEST_PASSWORD = 'TestPass1!'
FEATURE = "assistant"
SMALL_MAX_TOKENS = 16
KEY_NAME = "LLM_HANDLER_API_KEY"  # the key the policy's "handler" credential reads

# Long enough that a one- or two-token count/bill difference stays inside
# the +/-5% band, still a single short message.
PLAIN_MESSAGE = (
    "This is a short connectivity check for an input token estimate. "
    "Please reply with the single word OK and nothing else. Do not explain, "
    "do not add punctuation, do not repeat this request, and do not use any "
    "formatting. The reply is discarded; only the usage numbers are read."
)
# Both tools are core, so a new conversation has them without load_tools:
# three provider calls in all (two tool turns and the answer).
TOOL_MESSAGE = (
    "Call describe_model with app_name 'incident' and model_name 'Incident'. "
    "After you have its result, call query_model for the same model with "
    "limit 1. Make the calls one after the other, not in parallel. Then answer "
    "in one short sentence."
)


def _assistant_max_tokens():
    # The Assistant calls llm.call without max_tokens (agent.py), so its
    # reservation carries llm.call's default output headroom (4096).
    from mojo.helpers import llm
    return inspect.signature(llm.call).parameters["max_tokens"].default


def _limits():
    # Sized to the three scenarios. Request bytes, measured: the long prompt
    # is ~28.5 KB, the Assistant's first call ~26.4 KB and its last ~29 KB.
    # Tokens: three Assistant calls at ~9k reserved input + 4096 output each,
    # the long prompt at ~8k reserved, the plain chat at ~0.1k. Permits are
    # trued up to billed usage on release, so this also leaves room for one
    # call that falls back to its byte-length reservation.
    return {
        "requests_minute": 10, "requests_hour": 10, "requests_day": 10,
        "tokens_minute": 64000, "tokens_hour": 64000, "tokens_day": 64000,
        "concurrency": 1, "max_input_bytes": 57344,
        "max_output_tokens": _assistant_max_tokens(), "timeout_seconds": 60,
        "max_loop_calls": 4,
    }


def _policy():
    from mojo.helpers import llm
    return {
        "version": 1,
        "routes": {FEATURE: {
            # The framework's pinned Sonnet snapshot (llm._FALLBACKS), the same
            # fixed id the documented policy gives its Sonnet routes.
            "provider": "anthropic", "model": llm._FALLBACKS["general"],
            "credential": "handler",
            "capabilities": ["text", "tools", "prompt_cache"],
        }},
        "shared": _limits(),
        "features": {FEATURE: _limits()},
        "breaker": {
            "auth_failures": 2, "rate_failures": 2,
            "server_failures": 2, "open_seconds": 60,
        },
    }


def _clear_policy_agreement():
    from mojo.apps.account.models import Setting
    from mojo.apps.account.services import llm_safety

    Setting.objects.filter(
        key=llm_safety.EXPECTED_POLICY_HASH_KEY, group=None).delete()
    redis = Setting._redis()
    if redis:
        redis.hdel(Setting._redis_key(), llm_safety.EXPECTED_POLICY_HASH_KEY)


def _install_policy_agreement():
    from mojo.apps.account.models import Setting
    from mojo.apps.account.services import llm_safety

    _clear_policy_agreement()
    parsed = llm_safety.parse_policy(_policy())
    row = Setting(
        key=llm_safety.EXPECTED_POLICY_HASH_KEY,
        group=None, is_secret=False)
    row.set_value(parsed["hash"])
    row.save(_protected_writer=llm_safety.EXPECTED_POLICY_HASH_KEY)


def _env_key():
    return {KEY_NAME: os.environ[KEY_NAME]} if os.environ.get(KEY_NAME) else {}


def _has_api_key():
    """The handler key in settings or in the environment."""
    from mojo.helpers.settings import settings
    return bool(settings.get(KEY_NAME, None)) or bool(_env_key())


def _local_policy():
    """The policy (and an environment key) for the in-process calls."""
    from django.test.utils import override_settings
    return override_settings(LLM_SAFETY_POLICY=_policy(), **_env_key())


def _margin(count):
    # llm_safety._input_estimate: -(-count * 110 // 100) + 64
    return -(-count * 110 // 100) + 64


def _billed_input(row):
    return row.input_tokens + row.cache_read_input_tokens + row.cache_creation_input_tokens


def _report_and_check(scenario, rows, max_tokens):
    """Print one line per provider call, then assert the band on each."""
    assert_true(rows, f"{scenario}: no LLMRequest rows were written")
    failures = []
    for row in rows:
        billed = _billed_input(row)
        reserved_input = row.reserved_tokens - max_tokens
        est = row.estimated_input_tokens
        ratio = (est / billed) if billed else float("nan")
        est_ok = billed > 0 and abs(est - billed) * 100 <= 5 * billed
        reserve_ok = billed > 0 and reserved_input >= billed \
            and reserved_input * 100 <= 120 * billed + 6400
        capped = " (byte-capped)" if reserved_input < _margin(est) else ""
        cache = ""
        if row.cache_read_input_tokens or row.cache_creation_input_tokens:
            cache = (f" [input={row.input_tokens} cache_read={row.cache_read_input_tokens}"
                     f" cache_write={row.cache_creation_input_tokens}]")
        print(f"    t9598 {scenario} row={row.pk} status={row.status} "
              f"source={row.estimate_source} est={est} reserved_input={reserved_input}"
              f"{capped} billed_input={billed}{cache} duration_ms={row.duration_ms} "
              f"est/billed={ratio:.3f} "
              f"band={'IN' if est_ok and reserve_ok else 'OUT'}")
        if row.status != "succeeded":
            failures.append(f"row {row.pk} status {row.status} {row.error_code}")
        elif row.estimate_source != "count":
            failures.append(f"row {row.pk} estimate_source {row.estimate_source!r}, not 'count'")
        elif not est_ok:
            failures.append(f"row {row.pk} estimate {est} not within 5% of billed {billed}")
        elif not reserve_ok:
            failures.append(f"row {row.pk} reserved input {reserved_input} outside "
                            f"[{billed}, 1.20 x {billed} + 64]")
    assert_true(not failures, f"{scenario}: " + "; ".join(failures))


def _filler_system_prompt():
    """About 7k tokens of deterministic, varied filler."""
    words = ("harbor", "lantern", "meadow", "copper", "signal", "orchard",
             "granite", "willow", "beacon", "river", "summit", "thistle")
    lines = []
    for i in range(420):
        a, b, c = words[i % 12], words[(i * 5) % 12], words[(i * 7) % 12]
        lines.append(f"Note {i}: the {a} near the {b} keeps the {c} record {i * 37 % 1000}.")
    return ("You are a terse assistant. The reference notes below are filler "
            "for a token-count check; ignore them.\n" + "\n".join(lines))


def _guarded_call(scenario, messages, system=None):
    from mojo.helpers import llm
    from mojo.apps.account.models import LLMRequest
    operation = f"t9598_{scenario}_{uuid.uuid4().hex[:8]}"
    with _local_policy():
        _install_policy_agreement()
        try:
            llm.call(messages, system=system, max_tokens=SMALL_MAX_TOKENS,
                     feature=FEATURE, operation=operation)
        finally:
            _clear_policy_agreement()
    return list(LLMRequest.objects.filter(
        feature=FEATURE, operation=operation).order_by("created", "pk"))


@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
@th.requires_extra("slow")
def setup_live_estimate(opts):
    from mojo.apps.account.models import User
    from mojo.apps.assistant.models import Conversation

    _clear_policy_agreement()
    opts.skip_reason = None
    if not _has_api_key():
        opts.skip_reason = f"no LLM API key ({KEY_NAME})"
        return

    User.objects.filter(email=TEST_EMAIL).delete()
    user = User.objects.create_user(
        username=TEST_EMAIL, email=TEST_EMAIL, password=TEST_PASSWORD)
    user.is_email_verified = True
    user.save()
    for perm in ("view_admin", "view_security"):
        user.add_permission(perm)
    Conversation.objects.filter(user=user).delete()


def _skip_if_unavailable(opts):
    if getattr(opts, "skip_reason", None):
        raise TestitSkip(opts.skip_reason)


@th.django_unit_test("t9598 live estimate: plain chat")
@th.requires_extra("slow")
def test_live_estimate_plain_chat(opts):
    _skip_if_unavailable(opts)
    rows = _guarded_call("plain", [{"role": "user", "content": PLAIN_MESSAGE}])
    assert_eq(len(rows), 1, f"plain chat should be one provider call, got {len(rows)}")
    _report_and_check("plain", rows, SMALL_MAX_TOKENS)


@th.django_unit_test("t9598 live estimate: tool-heavy assistant turn")
@th.requires_extra("slow")
def test_live_estimate_assistant_tools(opts):
    _skip_if_unavailable(opts)
    from mojo.apps.account.models import LLMRequest

    with th.server_settings(LLM_ADMIN_ENABLED=True, LLM_SAFETY_POLICY=_policy(),
                            **_env_key()):
        _install_policy_agreement()
        try:
            assert_true(opts.client.login(TEST_EMAIL, TEST_PASSWORD), "REST login failed")
            resp = opts.client.post('/api/assistant', {"message": TOOL_MESSAGE})
            opts.client.logout()
        finally:
            _clear_policy_agreement()
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}: {resp.json}")
    conv_id = resp.json.data.conversation_id
    assert_true(conv_id, "Expected conversation_id")
    print(f"    t9598 tools: tool_calls_made={resp.json.data.tool_calls_made}")

    rows = list(LLMRequest.objects.filter(
        feature=FEATURE, conversation_id=conv_id).order_by("created", "pk"))
    assert_true(len(rows) >= 2,
                f"a tool turn needs at least two provider calls, got {len(rows)}")
    _report_and_check("tools", rows, _assistant_max_tokens())


@th.django_unit_test("t9598 live estimate: long system prompt")
@th.requires_extra("slow")
def test_live_estimate_long_system(opts):
    _skip_if_unavailable(opts)
    system = _filler_system_prompt()
    messages = [{"role": "user", "content": "Reply with the single word OK."}]
    size = len(json.dumps({"messages": messages, "system": system, "tools": None},
                          separators=(",", ":")).encode("utf-8"))
    assert_true(size <= _limits()["max_input_bytes"],
                f"the long prompt ({size} bytes) outgrew this test's max_input_bytes")
    started = time.time()
    rows = _guarded_call("longsys", messages, system=system)
    assert_eq(len(rows), 1, f"long system prompt should be one provider call, got {len(rows)}")
    print(f"    t9598 longsys: {size} bytes, {time.time() - started:.1f}s")
    _report_and_check("longsys", rows, SMALL_MAX_TOKENS)
