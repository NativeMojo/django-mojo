# Assistant does not handle 529 overloaded errors from Anthropic API

**Type**: bug
**Status**: open
**Date**: 2026-04-06
**Severity**: high

## Description
When the Anthropic API returns a 529 (overloaded/service unavailable) error, the assistant agent loop treats it as a generic crash instead of a transient API issue. The user sees a raw error message like `Assistant error: Error code: 529 - {'type': 'error', 'error': {'type': 'overloaded_error', ...}}` and a level-7 critical incident is filed, even though this is a temporary service condition similar to rate limiting.

## Context
529 errors are transient — the API is temporarily overloaded but will recover. This differs from real crashes (bad code) and config errors (wrong key/model). Treating it as a critical crash creates noisy incident reports and gives users no useful guidance. The 429 rate-limit path already handles a similar transient condition correctly (level-5 event, friendly message).

## Acceptance Criteria
- 529/overloaded errors are detected in both `run_assistant()` and `run_assistant_ws()` error handlers
- User receives a friendly message like "The AI service is temporarily overloaded. Please try again in a moment."
- Incident is reported at level 5 (transient), not level 7 (critical)
- `llm.call()` retries once on 529 before raising (optional — discuss with user)
- No retry loop that blocks the request for a long time

## Investigation
**Likely root cause**: The error handler in `agent.py` checks for 404, 401, and 429 by string-matching the exception message, but has no check for 529 or "overloaded". The 529 falls through to the generic `else` clause.

**Confidence**: confirmed

**Code path**:
- `mojo/helpers/llm.py:271` — `client.messages.create()` raises on 529, no retry
- `mojo/apps/assistant/services/agent.py:999-1019` — `run_assistant()` catch-all, missing 529 check
- `mojo/apps/assistant/services/agent.py:1143-1162` — `run_assistant_ws()` catch-all, same gap

**Regression test**: not feasible — requires mocking the Anthropic client, which is in a separate server process during testit runs

**Related files**:
- `mojo/helpers/llm.py` — add optional retry for 529
- `mojo/apps/assistant/services/agent.py` — add 529/overloaded detection in both error handlers
