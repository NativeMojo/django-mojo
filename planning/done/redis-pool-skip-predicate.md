# Redis Pool — Skip Predicate for Conditional Checkout

**Type**: request
**Status**: planned
**Date**: 2026-05-01
**Priority**: medium

## Description

Add an optional `skip_predicate` hook to `RedisModelPool` (and `RedisBasePool` if useful) so consumers can mark a pool member as temporarily ineligible without removing it from the pool. When `get_next_instance` (or `get_next_available`) pulls a candidate, the predicate decides whether the candidate is currently eligible; if not, the candidate is returned to the pool and the next candidate is tried, up to the existing `_max_retries` budget.

The motivating use case is a pool of rate-limited resources where each member needs a randomized cooldown window between checkouts to avoid predictable cadence. Today there is no clean way to express "this pool member is in the pool but temporarily ineligible." Workarounds (inline `time.sleep`, deferred async-job re-checkin, sweepers) all add latency, infrastructure, or crash-recovery hazards.

A predicate hook is the smallest change that solves it generically: the consumer stores cooldown state wherever it likes (a Redis TTL key, a DB column, an in-memory cache) and the pool stays oblivious to the policy.

## Context

The consumer-side pattern is "set a TTL key after each checkout; have the pool skip members whose key is alive." That pattern needs this library hook to land first.

`RedisModelPool.get_next_instance` already has the right shape — it loops with a `_retries` counter and falls back through stale records. Extending the same loop to honor an eligibility predicate is a natural fit.

## Acceptance Criteria

- `RedisModelPool.__init__` accepts an optional `skip_predicate` kwarg: a callable `(instance) -> bool`. Returning `True` means "skip this one for now."
- `get_next_instance` honors the predicate: when it returns `True`, the instance is returned to the pool (back of the available list, so other waiters can try it) and the loop advances to the next candidate, decrementing the existing retry budget.
- Predicate exceptions do not poison the pool — a raised exception is caught, logged, and treated as "skip" (conservative default) so a buggy predicate cannot wedge a worker.
- `get_next_available` on `RedisBasePool` gains the same hook for non-Django consumers (string-id predicate).
- `checkout_instance` / `checkout_item` context managers honor the predicate (they already delegate to `get_next_instance` / `get_next_available`).
- `get_specific_instance` and `checkout_specific_item` are **not** gated by the predicate — explicit checkouts of a known id bypass the policy. Admin / non-customer paths need this.
- When all pool members are temporarily ineligible, `get_next_instance(timeout=N)` blocks via `brpop` for the timeout duration, then returns `None` (matches today's empty-pool behavior).
- No regression in existing pool tests; new tests cover predicate skip, predicate-raises, all-ineligible-with-timeout, and explicit-checkout-bypasses-predicate.
- CHANGELOG entry under the next release.

## Investigation

### What exists

- **`RedisBasePool.get_next_available`** (`mojo/helpers/redis/pool.py:162-172`) — `brpop` on the available list with a timeout. Returns the raw id or `None`. No retry loop.
- **`RedisModelPool.get_next_instance`** (`mojo/helpers/redis/pool.py:293-324`) — wraps `get_next_available`, then validates the instance still matches `query_dict` and retries up to `_max_retries=100` if the model row was deleted or no longer matches. The retry path already calls itself recursively with `timeout=0` to avoid double-blocking.
- **`return_instance` / `checkin`** (`mojo/helpers/redis/pool.py:93-118`, `335-357`) — `lpush` to the head of the available list, with optional duplicate-protection via `lrange` membership check.

The retry loop in `get_next_instance` is exactly where the predicate hook belongs. The recursion pattern (drop, retry with `timeout=0`) maps cleanly onto "predicate said skip, try the next one."

### Implementation sketch

In `RedisModelPool.__init__`:
```python
def __init__(self, model_cls, query_dict, pool_key=None, default_timeout=30, skip_predicate=None):
    ...
    self.skip_predicate = skip_predicate
```

In `get_next_instance`:
```python
def get_next_instance(self, timeout=None, _retries=0, _max_retries=100):
    ...
    pk = self.get_next_available(timeout)
    if pk:
        try:
            instance = self.model_cls.objects.get(pk=pk)
            for key, value in self.query_dict.items():
                if getattr(instance, key) != value:
                    self.redis_client.srem(self.all_items_set_key, pk)
                    return self.get_next_instance(timeout=0, _retries=_retries+1, _max_retries=_max_retries)

            # New: eligibility check
            if self.skip_predicate is not None:
                try:
                    skip = bool(self.skip_predicate(instance))
                except Exception:
                    logit.exception("skip_predicate failed")
                    skip = True  # conservative: treat buggy predicate as skip
                if skip:
                    # Return to back of list (rpush-equivalent) so other waiters can try it
                    self.redis_client.rpush(self.available_list_key, pk)
                    return self.get_next_instance(timeout=0, _retries=_retries+1, _max_retries=_max_retries)

            return instance
        except self.model_cls.DoesNotExist:
            ...
```

Note the use of `rpush` for the skipped instance: `return_instance` uses `lpush` (head) which would put the skipped instance back at the front and we'd re-pick it on the very next iteration. Tail-push (`rpush`) lets every other available member be tried before we cycle back.

For `RedisBasePool.get_next_available`, the predicate signature would be `(str_id) -> bool`. Same skip-and-rpush logic, with a small loop in `get_next_available` itself (currently it's a single `brpop` — we'd wrap it in a retry loop only if a predicate is configured).

### Edge cases

- **Predicate raises** → catch, log, treat as skip. Caller never sees the exception. Buggy predicate still drains retry budget but doesn't return a poisoned instance.
- **All members skipped** → exhaust `_max_retries`, return `None`. Caller sees the same "no instance available" outcome as an empty pool.
- **Predicate has side effects** (e.g., refreshing a cached value) → caller's responsibility. Document that the predicate may be called multiple times per `get_next_instance` call.
- **Race during cooldown TTL expiry** → if predicate returns `True` (skip) a microsecond before the TTL key expires, instance gets re-queued and tried again on next iteration; net effect is at-most-one-loop wasted iteration. Benign.
- **Predicate cost** → predicate is called once per candidate examined. If it hits Redis (typical), that's one extra round-trip per candidate. For a small pool with a few members cooling, overhead is negligible.

## Open Questions

- Should `skip_predicate` also be a parameter to `get_next_instance` itself (per-call override), or only on the pool? Recommendation: pool-level only for v1 — keeps the API surface small and matches the consumer use case.
- Should we expose a metric for "skipped due to predicate" vs "skipped due to stale row"? Useful for observability if cooldown windows ever look wrong; defer to a follow-up if not needed immediately.
- Re-queuing skipped instances changes ordering: a skipped member is placed so other members are tried first. If a pool consumer cares about strict FIFO ordering of returned items, this is a small behavior shift. Document the new ordering for consumers.

## Plan

**Status**: planned
**Planned**: 2026-05-01

### Objective
Add an optional `skip_predicate` hook to `RedisBasePool` and `RedisModelPool` so consumers can mark pool members as temporarily ineligible without removing them; the next-available loop honors the predicate and falls through to the next candidate.

### Steps

1. `mojo/helpers/redis/pool.py` — `RedisBasePool.__init__` (line 12)
   Add `skip_predicate=None` kwarg. Signature: `(str_id) -> bool`. `True` = skip.

2. `mojo/helpers/redis/pool.py` — `RedisBasePool.get_next_available` (line 162)
   Add keyword-only `_skip_retries=0, _max_skip_retries=None` params. After `brpop` returns an id, if `self.skip_predicate` is set:
   - On first entry, set `_max_skip_retries = max(self.redis_client.scard(self.all_items_set_key), 1)` if not already provided.
   - Call predicate inside `try/except`. Exception → `logit.exception(...)`, treat as skip.
   - On skip: `lpush` the id back to head, then recurse with `timeout=0, _skip_retries=_skip_retries+1, _max_skip_retries=_max_skip_retries`.
   - When `_skip_retries >= _max_skip_retries`, return `None`.
   - When `skip_predicate is None`: zero added work, identical to today.

3. `mojo/helpers/redis/pool.py` — `RedisModelPool.__init__` (line 207)
   Add `skip_predicate=None` kwarg. Signature: `(instance) -> bool`. Stored on instance; **not forwarded** to `RedisBasePool`.

4. `mojo/helpers/redis/pool.py` — `RedisModelPool.get_next_instance` (line 293)
   Add keyword-only `_skip_retries=0, _max_skip_retries=None` (separate from existing `_retries`/`_max_retries=100`). After the `query_dict` recheck and before `return instance`, if `self.skip_predicate` is set:
   - On first entry, set `_max_skip_retries = max(self.redis_client.scard(self.all_items_set_key), 1)` if not already provided.
   - Call predicate inside `try/except`. Exception → `logit.exception(...)`, treat as skip.
   - On skip: `lpush` pk back to `available_list_key`, recurse with `timeout=0, _skip_retries=_skip_retries+1, _max_skip_retries=_max_skip_retries` (preserve `_retries` and `_max_retries` unchanged).
   - When `_skip_retries >= _max_skip_retries`, return `None`.

5. No changes to `get_specific_instance` / `checkout_specific_item` / `checkout_specific_instance` — explicit checkouts bypass the predicate by design.

6. No changes to `checkout_item` / `checkout_instance` context managers — they delegate to `get_next_*`.

7. `tests/test_helpers/redis_pools.py`
   - Extend setup `test_patterns` cleanup list with `'test_skip_predicate*'`.
   - Add `test_redis_base_pool_skip_predicate` — predicate skips one item; verify others returned first; flip predicate, skipped item returns.
   - Add `test_redis_base_pool_skip_predicate_raises` — predicate raises; treated as skip; budget exhausted → `None`; pool not corrupted.
   - Add `test_redis_model_pool_skip_predicate_blocks_all` — all members skipped; `get_next_instance(timeout=1)` returns `None` after one full sweep (bounded by `scard`).
   - Add `test_redis_model_pool_skip_predicate_specific_bypasses` — `get_specific_instance` and `checkout_specific_instance` ignore the predicate.
   - Add `test_redis_model_pool_skip_predicate_default_none` — pool with no predicate behaves exactly as today (regression guard).

8. `docs/django_developer/helpers/redis.md`
   - Section "RedisBasePool" (line 233): add `skip_predicate` to the parameter list with a short generic example (cooldown TTL key pattern). Note ordering side effect: skipped items go to head, tried last after others.
   - Section "RedisModelPool" (line 279): add `skip_predicate` with instance-callable example. Note that `get_specific_instance` / `checkout_specific_instance` bypass the predicate.

9. `CHANGELOG.md` — under next release: "redis.pool: optional `skip_predicate` for conditional checkout in `RedisBasePool` and `RedisModelPool`."

### Design Decisions

- **lpush on skip, not rpush.** Queue uses `lpush` to add and `brpop` (tail) to remove → FIFO. Skipped items must go to head so other tail items are tried first; `rpush` would re-pick the same id immediately.
- **Per-class predicates, no chaining.** `RedisModelPool` does not forward to `RedisBasePool`'s predicate. Avoids double-evaluation and keeps signatures cleanly separated (`str_id` vs `instance`).
- **Two independent retry counters in `get_next_instance`.** Existing `_retries`/`_max_retries=100` (stale-row path) untouched. New `_skip_retries`/`_max_skip_retries` (predicate path) bounded by `scard(pool)`. Stale-row is transient inconsistency that may need slack during `init_pool` churn; predicate skip is steady-state policy where one full sweep is sufficient.
- **Conservative on exception.** Buggy predicate ⇒ treat as skip ⇒ drains skip budget but never returns a member that should have been ineligible.
- **No per-call predicate override.** Pool-level only for v1. Smaller API surface; can add per-call later if a real need surfaces.
- **Skipped-due-to-predicate metric deferred.** Add a follow-up if observability gaps appear; pool internals stay simple now.
- **Backward compatibility.** All new kwargs default to `None`/`0`. Behavior with `skip_predicate=None` is byte-identical to today: no extra Redis calls, no extra branches taken.

### User Cases

- Cooldown after use (rate-limited resource): consumer sets a TTL key on checkin; predicate is `lambda i: redis.exists(f"cooldown:{i.pk}")`.
- Maintenance flag: predicate checks an in-memory or DB flag and skips members under maintenance without removing them from the pool.
- Mixed steady/explicit access: customer-facing path uses `checkout_instance` (predicate-gated); admin tooling uses `checkout_specific_instance` (bypasses predicate to force access).
- No predicate configured: existing consumers unchanged.

### Edge Cases

- All members ineligible + nonzero timeout → first `brpop` blocks for full timeout; recursive retries use `timeout=0`; budget bounded by `scard`; returns `None`.
- All members ineligible + `timeout=0` → at most `scard` round-trips, returns `None`. Cannot wedge.
- Predicate raises every time → caught and logged each iteration; budget drained; returns `None`. Worker never sees exception.
- Cooldown TTL expires mid-loop → at most one wasted iteration; instance picked up on the next `get_next_*` call. Benign.
- Concurrent worker returns/skips same id → both `lpush` to head; `brpop` from tail still drains FIFO; safe but a member may be tried twice across workers (within budget).
- `init_pool` re-entry while a predicate is active → predicate state lives outside the pool (consumer's TTL keys); fresh checkout re-evaluates correctly.
- Pool empty (`scard == 0`) with predicate set → `_max_skip_retries` floor of `1` ensures we don't compute a 0-budget loop; `brpop` blocks/returns `None` as today.
- FIFO ordering shift → skipped items move to head, tried last; documented in redis.md as a behavior note.

### Testing
Mapped above; all in `tests/test_helpers/redis_pools.py` using existing setup pattern (cleanup list extended).

### Docs
- `docs/django_developer/helpers/redis.md` — both pool sections.
- `CHANGELOG.md` — next release entry.

## Resolution

**Status**: resolved
**Date**: 2026-05-01

### What Was Built
Added optional `skip_predicate` hook to `RedisBasePool` and `RedisModelPool`. When set, candidates returning `True` (or raising) are returned to the head of the available list and the next candidate is fetched, bounded by `scard(pool)` skip retries. Explicit checkouts (`get_specific_instance`, `checkout_specific_instance`, `checkout_specific_item`) bypass the predicate by design. Predicate exceptions are caught and logged via `logit.exception` then treated as skip.

The base predicate (`(str_id) -> bool`) and the model predicate (`(instance) -> bool`) are stored under separate internal attributes (`skip_predicate` on the base, `instance_skip_predicate` on the model) so the inherited base method does not invoke the model-level predicate with a string id.

### Files Changed
- `mojo/helpers/redis/pool.py` — `skip_predicate` kwarg on both pool classes; `get_next_available` and `get_next_instance` apply the predicate, lpush-back on skip, and bound retries by current pool size; predicate exceptions caught and logged.
- `tests/test_helpers/redis_pools.py` — extended cleanup pattern list and added 8 tests for both pool classes.
- `docs/django_developer/helpers/redis.md` — documented `skip_predicate` for both pool classes (signature, ordering note, predicate-exception behaviour, explicit-checkout bypass).
- `CHANGELOG.md` — entry under v1.1.0 unreleased.

### Tests
- `tests/test_helpers/redis_pools.py`:
  - `test_redis_base_pool_skip_predicate` — predicate hides eligible items until flipped.
  - `test_redis_base_pool_skip_predicate_all_skipped_returns_none` — bounded sweep returns `None`, items remain in pool.
  - `test_redis_base_pool_skip_predicate_raises` — raising predicate treated as skip; pool not corrupted.
  - `test_redis_base_pool_skip_predicate_default_none_no_change` — default `None` preserves FIFO behaviour.
  - `test_redis_model_pool_skip_predicate` — instance-level predicate honored.
  - `test_redis_model_pool_skip_predicate_blocks_all` — bounded sweep on the model pool.
  - `test_redis_model_pool_skip_predicate_specific_bypasses` — explicit checkouts ignore predicate.
  - `test_redis_model_pool_skip_predicate_default_none` — regression guard.
- Run: `bin/run_tests --agent -t test_helpers.redis_pools` — all 23 pass.

### Docs Updated
- `docs/django_developer/helpers/redis.md` — new `Conditional checkout — skip_predicate` subsections under both `RedisBasePool` and `RedisModelPool`.
- `CHANGELOG.md` — v1.1.0 unreleased entry.

### Security Review
Pending — security-review agent will run post-commit.

### Follow-up
- Consider exposing a "skipped due to predicate" metric if observability gaps appear; deferred to a future request.
