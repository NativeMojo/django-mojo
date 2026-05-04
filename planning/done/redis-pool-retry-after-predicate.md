# Redis Pool — Retry-After Predicate for Cooldown-Aware Checkout

**Type**: request
**Status**: resolved
**Date**: 2026-05-03
**Priority**: medium

## Description

Extend the `skip_predicate` hook on `RedisBasePool` / `RedisModelPool` so it can signal *temporarily ineligible — retry after N seconds* in addition to *eligible* and *skip this round*. When all candidates are temporarily ineligible, the pool maintains a min-heap of "available again at T" entries and sleeps until the soonest retry time (bounded by the caller's `timeout`), then re-evaluates. Today the predicate is a strict bool: `False` (eligible) or `True` (skip). When all members are skipped, the pool returns `None` immediately — even if the caller passed a generous timeout and the cooldowns would have expired well within that budget.

## Motivation — Pool Size Should Not Affect Correctness

The current bool-only predicate has a leaky behaviour that depends on pool size:

- **Multi-member pool, one member cooling**: predicate skips the cooling member, the next candidate is eligible, request is served immediately. Works as intended.
- **Single-member pool, the only member cooling**: predicate skips, retry budget = `scard(pool) = 1` is exhausted on the first iteration, `get_next_*` returns `None` instantly. Caller sees "no instance available" and typically surfaces an error to the end user.
- **Multi-member pool, all members cooling at the same instant**: same failure mode as the single-member case — bounded sweep gives up immediately.

These three cases are conceptually identical (one or more pool members are temporarily busy), but the outcome differs based on whether at least one member happens to be eligible *right now*. That is the leak. The pool's caller has no way to express "I am willing to wait up to N seconds for any member to become available, including waiting through a cooldown window." The `timeout` parameter today only controls how long `brpop` blocks waiting for a checked-out member to be checked back in — it does not cover the case where members are physically in the pool but the consumer-supplied predicate has marked them ineligible.

The result is that consumers using the predicate to enforce a per-member cooldown end up reimplementing the wait themselves outside the pool (inline `sleep`, retry loops, manual TTL inspection), which:

- Defeats the point of the predicate (the consumer ends up doing the work the pool was supposed to do).
- Splits scheduling across two layers — the pool's `brpop` timeout and the consumer's sleep — making the customer-visible total latency hard to reason about.
- Forces every consumer to invent the same waiting logic.

## Proposal — Predicate Returns When, Not Just Whether

Today the predicate returns a `bool`. Extend the contract so the predicate may also return a non-negative integer / float meaning *retry after this many seconds*. Three legal return shapes:

| Return value     | Meaning                                                                 |
|------------------|-------------------------------------------------------------------------|
| `False` / `0`    | Eligible — return this candidate now.                                   |
| `True` / `None`  | Skip — same as today's "skip this round, no retry-after info".          |
| `int` / `float` (> 0) | Temporarily ineligible — pool may retry this candidate after N s. |

The pool keeps the existing skip-bounded-by-pool-size behaviour for `True` returns (back-compat). For numeric returns the pool maintains a per-call min-heap of `(retry_at_monotonic, str_id)` entries and, when the current pop yields no eligible candidate, sleeps until the soonest retry time (bounded by the caller's `timeout`), then re-queues that id and tries again.

### Why the pool is the right layer

- The pool already owns "wait for an available item" semantics — `brpop` with a timeout. *Available later* is the same shape as *available eventually*; it is just a different signal.
- Consumer-supplied state already exists (typically a Redis TTL key or a DB column with an expires-at). The consumer just needs a way to say "ask me again at this time"; it already knows when.
- Removes the asymmetry: pool size stops affecting whether a consumer is rejected vs. served.
- Removes the inline-sleep workaround on the consumer side. Caller code stays unaware of cooldown details — the pool encodes "wait until something is ready".
- Generally useful primitive — anyone modelling rate-limited resources, scheduled-availability windows, or maintenance/backoff windows benefits.

## Acceptance Criteria

- `skip_predicate` may return `bool`, `int`, or `float`. Numeric > 0 means "retry after N seconds"; `0` / `False` means eligible; `True` / non-numeric truthy means "skip this round" (current semantics).
- `get_next_available` / `get_next_instance`:
  - Maintain a per-call deferred heap of `(retry_at_monotonic, str_id)` entries seeded by predicate returns.
  - When the current pop yields no eligible candidate AND the deferred heap is non-empty AND the caller's `timeout` budget allows, sleep until `min(soonest_retry_at, deadline)` and try again.
  - When the deferred heap is empty and `brpop` returns `None` (existing empty-pool behaviour), return `None`.
  - When the caller's `timeout` budget is exhausted, return `None` regardless of heap state.
- Bool-only predicates behave exactly as today (back-compat). The new code path activates only when the predicate returns a numeric value.
- Predicate exceptions remain caught and logged via `logit.exception(...)`, treated as `True` (skip this round) — same as today.
- `get_specific_instance` / `checkout_specific_instance` continue to bypass the predicate.
- Caller's `timeout` parameter becomes the real cap on customer-visible wait. Document this clearly: with a numeric predicate, `timeout` may include a wait through a cooldown window, not just `brpop` blocking.
- New tests cover: numeric predicate served after the configured wait; deferred heap drains in correct order; `timeout` cap honored when heap retry-at exceeds deadline; mixed bool / numeric returns coexist; back-compat with bool-only predicates is byte-identical.
- No regression in existing pool tests.
- CHANGELOG entry under the next release.

## Investigation

### What exists today

- **`RedisBasePool.get_next_available`** (`mojo/helpers/redis/pool.py:169-210`) — `brpop` to pop a candidate, then if `skip_predicate` is set call it; on truthy return, `lpush` back and recurse with `timeout=0`, bounded by `_max_skip_retries = max(scard(pool), 1)`.
- **`RedisModelPool.get_next_instance`** (`mojo/helpers/redis/pool.py:339-398`) — same pattern with the instance-level predicate (`instance_skip_predicate`), separate counter from the stale-row retry path.

The skip-and-recurse loop is exactly where the deferred heap belongs. Today the loop's only termination condition for the predicate path is "we've examined every member once and they all said skip". The proposed change adds a second termination condition for the numeric path: "we've waited as long as the caller permitted".

### Implementation sketch

A small helper to normalize predicate returns:

```python
def _classify_predicate_result(result):
    """
    Returns one of:
      ('eligible', None)
      ('skip', None)
      ('retry_after', float)  # seconds, > 0
    """
    if result is None or result is True:
        return ('skip', None)
    if result is False or result == 0:
        return ('eligible', None)
    if isinstance(result, (int, float)) and result > 0:
        return ('retry_after', float(result))
    return ('skip', None)  # unknown truthy → conservative skip
```

In `get_next_available` (and the equivalent block in `get_next_instance`), restructure the loop to be iterative around a deadline:

```python
def get_next_available(self, timeout=None):
    timeout = timeout or self.default_timeout
    deadline = time.monotonic() + timeout
    deferred = []  # min-heap of (retry_at_monotonic, str_id)
    seen_skip = set()  # bool-skip dedupe within this call (existing cap behaviour)

    while True:
        remaining = max(0.0, deadline - time.monotonic())
        # Try to grab a candidate from the available list.
        if remaining > 0 and not deferred:
            # Blocking pop with the remaining budget — same as today's hot path.
            result = self.redis_client.brpop(self.available_list_key, timeout=int(remaining) or 1)
            str_id = result[1] if result else None
        else:
            # Non-blocking — we either have deferred entries to wait on, or budget is gone.
            result = self.redis_client.rpop(self.available_list_key)
            str_id = result if result else None

        if str_id is not None:
            if self.skip_predicate is None:
                return str_id
            try:
                verdict, retry_after = _classify_predicate_result(self.skip_predicate(str_id))
            except Exception:
                logit.exception("skip_predicate failed", str_id)
                verdict, retry_after = ('skip', None)

            if verdict == 'eligible':
                return str_id
            if verdict == 'skip':
                # Existing back-compat path: lpush and bound by pool size to avoid spin.
                self.redis_client.lpush(self.available_list_key, str_id)
                if str_id in seen_skip and not deferred:
                    return None
                seen_skip.add(str_id)
                continue
            # 'retry_after'
            heapq.heappush(deferred, (time.monotonic() + retry_after, str_id))
            self.redis_client.lpush(self.available_list_key, str_id)
            continue

        # No candidate from Redis. If we have deferred entries, sleep until the soonest.
        if not deferred:
            return None
        retry_at, _ = deferred[0]
        sleep_for = min(retry_at - time.monotonic(), max(0.0, deadline - time.monotonic()))
        if sleep_for <= 0:
            heapq.heappop(deferred)  # this entry's wait is up — loop and try again
            continue
        time.sleep(sleep_for)
```

`get_next_instance` follows the same shape with the instance-level predicate. Both keep the existing stale-row retry counter intact.

### Edge cases

- **Predicate returns negative number** → treated as `'skip'` (conservative, matches "unknown truthy → skip" rule).
- **Predicate returns `True` then later `int`** during the same call → both branches coexist; `True` items live in `seen_skip` (bounded by pool size), numeric items live in the deferred heap (bounded by deadline).
- **Predicate is non-deterministic** (e.g., reads a TTL that expires mid-call) → at most one wasted iteration per stale read; benign.
- **Caller passes `timeout=0`** → no waiting; bool-skip path runs as today, numeric returns are observed but never waited on (return `None` if no eligible candidate in one sweep).
- **Caller passes a very large `timeout`** with a cooling pool → pool sleeps in increments, will return as soon as a member is eligible. Caller's `timeout` is the only cap.
- **Pool is empty AND deferred is empty** → `brpop` blocks for full timeout, returns `None`. Same as today.
- **All deferred entries' `retry_at` exceed `deadline`** → sleep until deadline expires, then return `None`.
- **Concurrent worker checks the same id back in mid-loop** → the `lpush`-back puts it at head; another worker's `brpop` may pop it next; safe but a member may be evaluated twice across workers within a deadline window.

## Open Questions

- **Maximum sleep granularity** — should the pool sleep in fine-grained increments (e.g., never more than 1s at a time) so a fresh checkin via another worker can be picked up promptly? Recommendation: yes, cap each sleep at `min(retry_at - now, 1.0)` so a peer checkin via `brpop` resumes the wait quickly. Adds at most one extra wakeup per second of waiting.
- **Should `RedisBasePool.get_next_available` accept `(retry_at_monotonic)` directly** as an alternative to `(retry_after_seconds)` from the predicate? Recommendation: no — seconds-from-now is the natural unit consumers think in, and absolute monotonic times are awkward to compose.
- **Per-call timeout override on the predicate** — should consumers be able to say "but cap my retry-after at K seconds globally"? Recommendation: defer. The caller's `timeout` already caps total wait; predicate-side cap would be redundant.
- **Metric for time-spent-waiting** — useful for tuning cooldown windows in production. Recommendation: defer to a follow-up; pool internals stay simple.

## Plan

**Status**: planned
**Planned**: 2026-05-03

### Objective
Make `timeout` a true wallclock budget for `get_next_available` / `get_next_instance` when a `skip_predicate` is configured. Extend the predicate to optionally return a numeric retry-after (seconds); the loop holds deferred items out of the available list and sleeps within the budget until the soonest matures. Predicate-less callers and bool-only predicates keep their current behaviour exactly.

### Core Principle

`timeout=N` means *"I am willing to wait up to N wallclock seconds for you to return."* A single `deadline = monotonic() + timeout` is computed once; every blocking operation (`brpop`, `time.sleep`) is bounded by `deadline - monotonic()`. Slow predicate calls, slow Redis round-trips, and accumulated sleep all consume the same budget.

### Steps

1. `mojo/helpers/redis/pool.py` — add `import heapq` and `import time` at the top. Add module-private helper:
   ```python
   def _classify_predicate_result(result):
       if result is False or result == 0:
           return ('eligible', None)
       if result is True or result is None:
           return ('skip', None)
       if isinstance(result, (int, float)) and result > 0:
           return ('retry_after', float(result))
       return ('skip', None)  # negative / unknown / weird → conservative skip
   ```

2. `mojo/helpers/redis/pool.py` — `RedisBasePool.get_next_available` (replaces lines 169-210). Drop internal `_skip_retries` / `_max_skip_retries` recursion params.
   - **Fast path**: if `self.skip_predicate is None`, run today's exact code (single `brpop(timeout=timeout)`, return result or `None`). Byte-identical.
   - **Predicate path**: deadline-driven loop:
     ```
     deadline = monotonic() + timeout
     deferred = []     # min-heap of (mature_at, str_id), held OUT of list
     examined = set()  # bool-skipped this call (already lpush'd back)
     pool_size = max(scard(all_items_set_key), 1)

     try:
         while True:
             remaining = deadline - monotonic()
             if remaining <= 0: return None

             # Republish matured deferred items to the list head
             now = monotonic()
             while deferred and deferred[0][0] <= now:
                 _, mature_id = heappop(deferred)
                 lpush(available_list_key, mature_id)
                 examined.discard(mature_id)  # fresh look after maturity

             # Pop one item — blocking only when nothing is deferred
             if deferred:
                 str_id = rpop(available_list_key)
             else:
                 blk = max(1, int(remaining))  # brpop is integer seconds, min 1
                 result = brpop(available_list_key, timeout=blk)
                 str_id = result[1] if result else None
                 if str_id is None: return None

             # List empty but heap non-empty → sleep until soonest, capped at 1s
             if str_id is None:
                 soonest = deferred[0][0]
                 sleep_for = min(soonest - monotonic(), deadline - monotonic(), 1.0)
                 if sleep_for > 0: time.sleep(sleep_for)
                 continue

             # Already bool-skipped this call? Don't re-eval; sleep if fully cycled
             if str_id in examined:
                 lpush(available_list_key, str_id)
                 if len(examined) + len(deferred) >= pool_size:
                     if not deferred:
                         return None  # bool-only sweep exhausted (today's behaviour)
                     soonest = deferred[0][0]
                     sleep_for = min(soonest - monotonic(), deadline - monotonic(), 1.0)
                     if sleep_for > 0: time.sleep(sleep_for)
                 continue

             # Evaluate predicate
             try:
                 verdict, retry_after = _classify_predicate_result(skip_predicate(str_id))
             except Exception:
                 logit.exception("skip_predicate failed", str_id)
                 verdict, retry_after = ('skip', None)

             if verdict == 'eligible':
                 return str_id
             if verdict == 'retry_after':
                 heappush(deferred, (monotonic() + retry_after, str_id))
                 # held OUT of list — no lpush
                 continue
             # 'skip'
             lpush(available_list_key, str_id)
             examined.add(str_id)
             if len(examined) >= pool_size and not deferred:
                 return None  # bool-only sweep exhausted

     finally:
         # Always republish remaining deferred items so peers aren't starved
         for _, str_id in deferred:
             lpush(available_list_key, str_id)
     ```

3. `mojo/helpers/redis/pool.py` — `RedisModelPool.get_next_instance` (replaces lines 339-398). Same deadline-driven loop, with two adjustments:
   - After rpop, fetch the model instance and run the existing `query_dict` validation. If it fails or `DoesNotExist`, `srem` and `continue` (counts against `_retries`/`_max_retries=100`, not against `examined`).
   - Run `instance_skip_predicate(instance)` for the verdict.
   - Drop internal `_skip_retries` / `_max_skip_retries` params; keep `_retries` / `_max_retries=100` for the stale-row path.
   - The deadline bounds the whole call, including stale-row retries.

4. No changes to `get_specific_instance`, `get_specific_*`, `checkout_*` context managers, `checkin`, `return_instance`, `add`, `remove`, `init_pool`, or any other public method.

5. `tests/test_helpers/redis_pools.py`:
   - Extend setup `test_patterns` cleanup list with `'test_retry_after*'`.
   - `test_retry_after_eligible_zero_returns_immediately` — `0` → ~0ms.
   - `test_retry_after_served_after_wait` — 1-item pool, predicate returns `0.5` once then `False`; returned within ~0.6s with `timeout=2`.
   - `test_retry_after_respects_timeout` — predicate returns `5.0` always; `timeout=1` returns `None` within ~1.2s; item back in available list.
   - `test_retry_after_picks_soonest` — 3-item pool with `(2.0, 0.3, 5.0)`; the 0.3 member returned first within ~0.5s.
   - `test_retry_after_mixed_bool_and_numeric` — items returning `True`, `0.4`, `False`; eligible served immediately; bool-skip never selected; numeric served after wait if eligible removed.
   - `test_retry_after_negative_treated_as_skip` — `-1` → conservative skip.
   - `test_retry_after_predicate_raises_treated_as_skip` — exception → caught, logged, skip.
   - `test_retry_after_deferred_republished_on_none` — `timeout=1` with retry-after `60`; assert all items back in available list after `None` return.
   - `test_retry_after_deferred_republished_on_eligible` — mixed; assert deferred items back in list after eligible returned.
   - `test_retry_after_back_compat_bool_only_sweep` — pure bool predicate, all `True`; returns `None` after one sweep, items in list.
   - `test_retry_after_no_predicate_byte_identical` — explicit regression guard for the no-predicate fast path.
   - Equivalent set against `RedisModelPool.get_next_instance` using `instance_skip_predicate`.

6. `docs/django_developer/helpers/redis.md` — extend the `skip_predicate` subsections under both pool classes:
   - Document the three return shapes (`False`/`0` eligible, `True`/`None` skip, numeric > 0 retry-after seconds).
   - State the wallclock contract for numeric returns: *"`timeout` is the wallclock cap on the call. The pool will sleep through cooldown windows within the budget."*
   - State the bool-only carve-out: *"`True` carries no retry signal; the pool returns `None` after one sweep regardless of `timeout`. Return a numeric retry-after (seconds) to opt into waiting."*
   - Note the 1s sleep granularity for peer-checkin responsiveness.

7. `CHANGELOG.md` — under next release: `redis.pool: skip_predicate may now return a retry-after duration (int/float seconds). get_next_available / get_next_instance honour the caller's timeout as a true wallclock budget — they hold deferred items out of the available list and sleep until the soonest retry within the budget. Bool-only predicates and predicate-less callers behave exactly as before.`

### Design Decisions

- **Single deadline, checked every iteration.** Slow predicates, slow Redis, accumulated sleep all draw from the same budget. No separate timers to keep in sync.
- **Hold retry-after items OUT of the available list.** Avoids the spin where `lpush`-back + non-blocking `rpop` keeps surfacing the same just-deferred id. Items are still in `all_items_set_key`, so a worker crash mid-call leaves them recoverable via existing pool-rebuild paths.
- **`finally` republishes deferred items on every exit path.** Eligible return, `None` return, exception — peers aren't starved.
- **`brpop` blocks only when no deferred items exist.** When the heap is non-empty we use non-blocking `rpop` and let `time.sleep` consume the wait — `brpop` would just re-pop bool-skipped items off the head. Heap-driven sleep + 1s cap give peer-checkin responsiveness.
- **1s sleep cap.** A peer checkin via `lpush` is observed within at most one second. Adds at most one extra `rpop` per second of waiting.
- **Bool-only carve-out.** `True` carries no "when" signal, so we cannot honour the wallclock cap meaningfully. Returning `None` after one sweep matches today's behaviour exactly. Documented escape hatch: return numeric to opt into waiting.
- **`examined` dedupe + `pool_size` bound.** Prevents infinite cycling on the bool path (today's `_max_skip_retries` semantics), and enables mixed bool/numeric pools to know when to stop polling and sleep instead.
- **`examined.discard(id)` on maturity.** A deferred item that matures gets a fresh predicate evaluation — if its state changed during the wait, we honour it.
- **Numeric = seconds-from-now, not absolute time.** Matches how consumers naturally model cooldown (Redis `TTL`, `expires_in`).
- **Conservative on weird returns.** Negative numeric, unknown truthy → skip. Predicate exceptions → caught, logged, skip. Pool never returns a member that should have been ineligible.
- **Predicate-less fast path is untouched.** The `if self.skip_predicate is None` early return runs today's exact two-line code — zero risk of regression for the most common case.

### User Cases

- **Cooldown after use** (rate-limited resource with a Redis TTL key): `lambda i: redis.ttl(key(i)) or False` — TTL while alive, `False` when expired. Pool waits for the soonest cooldown to clear within the caller's budget.
- **Maintenance window** (member offline until a known time): predicate returns `(scheduled_end - now).total_seconds()` while inside the window. Pool serves other members; if none, sleeps until the window closes (within budget).
- **Mixed steady-state + warm-up**: warming members return `30.0` for the first half-minute, healthy members return `False`. Healthy members serve immediately; warming members are folded in as they mature.
- **Single-member pool, member cooling**: today returns `None` in milliseconds. New behaviour with numeric predicate: waits within budget until the cooldown clears, then serves. The original asymmetry is gone.
- **Multi-member pool, all cooling at once**: today returns `None` in milliseconds. New behaviour: waits for the soonest to mature within budget.
- **Back-compat**: predicate-less consumers and bool-only consumers see no behaviour change.

### Edge Cases

- **`timeout=0`**: `deadline = now`. First iteration: `remaining ≤ 0` → return `None`. (For the predicate-less fast path, `timeout = timeout or default_timeout` keeps today's semantics.)
- **0 items in pool, predicate set**: heap empty, `brpop` blocks for full budget waiting for a peer checkin. If something arrives, evaluated; if not, `None`.
- **1 item in pool**: covered by walked-through cases above (eligible / skip / retry-after / cooling-then-eligible).
- **Many items, all retry-after with different delays**: acquire phase pops them all, all held; sleep until soonest matures (1s cap); republish; re-evaluate; loop until eligible or deadline.
- **Predicate is slow (e.g., 10s)**: deadline check at top of next iteration absorbs it. If a 30s budget had a 10s predicate call, only 20s remain for further work.
- **Predicate raises every call**: each treated as skip; bool sweep terminates within `pool_size` iterations; returns `None`. No spin.
- **Concurrent worker checks in a fresh item**: observed within at most 1s on the next sleep wake, then `rpop`'d and evaluated.
- **Worker crash mid-call with deferred items**: items stranded out of available list but still in `all_items_set_key`. Same recovery profile as today's "checked out but no holder" case (existing `init_pool` / `add_to_pool` rebuild paths cover it).
- **Mixed bool-skip + numeric in same call**: `examined` set bounds the bool side at `pool_size`; `deferred` heap drives the numeric side; combined check `len(examined) + len(deferred) >= pool_size` knows when there's nothing left to do but wait.
- **Pool size changes mid-call**: `pool_size` is sampled once at start. New items added by peers will be popped and evaluated normally on the next iteration. Removed items just won't be popped. Benign.

### Testing
Mapped above; all in `tests/test_helpers/redis_pools.py` using the existing setup pattern (cleanup list extended).

### Docs
- `docs/django_developer/helpers/redis.md` — both pool sections.
- `CHANGELOG.md` — next release entry.

## Resolution

**Status**: resolved
**Date**: 2026-05-03

### What Was Built
`skip_predicate` now accepts a third return shape — positive `int`/`float` meaning *retry after N seconds*. When any predicate returns numeric, `get_next_available` / `get_next_instance` honour `timeout` as a true wallclock budget: a single `deadline = monotonic() + timeout` is computed once, deferred candidates are held OUT of the available list (in a per-call min-heap), and the loop sleeps until the soonest matures (capped at 1s per sleep so peer checkins are observed quickly). All deferred items are republished via `try/finally` on every exit path so peers are never starved. The predicate-less fast path is byte-identical to the previous implementation; the bool-only path keeps today's behaviour exactly (one sweep bounded by pool size, returns `None` if every member skips — documented carve-out since `True` carries no retry signal).

### Files Changed
- `mojo/helpers/redis/pool.py` — added `_classify_predicate_result` helper; restructured `get_next_available` and `get_next_instance` with deadline-driven loops behind a `skip_predicate is None` fast-path early return.
- `tests/test_helpers/redis_pools.py` — extended cleanup `test_patterns` with `'test_retry_after*'`; added 16 new tests covering eligible-zero, served-after-wait, respects-timeout, picks-soonest, mixed bool/numeric, negative-treated-as-skip, predicate-raises, deferred-republished-on-none, deferred-republished-on-eligible, back-compat bool sweep, no-predicate byte-identical regression, plus matching cases on the model pool.
- `docs/django_developer/helpers/redis.md` — documented the three return shapes, the wallclock-budget contract for numeric returns, the bool-only carve-out, and the 1s sleep granularity for peer-checkin responsiveness.
- `CHANGELOG.md` — entry under v1.1.0.

### Tests
- `tests/test_helpers/redis_pools.py` — 16 new retry-after tests + all existing skip_predicate and pool tests pass.
- Run: `bin/run_tests --agent -t test_helpers.redis_pools` — 34/34 pass in 9.17s.

### Docs Updated
- `docs/django_developer/helpers/redis.md` — both `skip_predicate` subsections rewritten with the three return shapes table, wallclock contract, bool-only carve-out, and a `cooldown` example using `r.ttl(...)`.
- `CHANGELOG.md` — v1.1.0 entry.

### Security Review
Pending — security-review agent will run post-commit.

### Follow-up
- The deferred-checkin proposal lives in `planning/requests/redis-pool-delayed-checkin.md` — independent feature, separate request file.
