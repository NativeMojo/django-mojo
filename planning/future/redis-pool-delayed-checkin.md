# Redis Pool — Delayed Checkin via Deferred-Returns Sorted Set

**Type**: request
**Status**: open
**Date**: 2026-05-03
**Priority**: medium

## Description

Add a new, additive surface to the Redis pool classes — `checkin_after(str_id, delay)` / `return_instance_after(instance, delay)` — that schedules a member to become available again after `delay` seconds, without changing any existing behaviour. The pool maintains a per-pool "deferred returns" sorted set keyed by `available_at` timestamp; on every `get_next_available` / `get_next_instance` call, matured entries are promoted from the sorted set into the available list before the normal pop runs.

The motivating case is rate-limited resources where the consumer knows the cooldown window at the moment of checkin: instead of returning the member immediately and teaching the pool to skip it via a `skip_predicate` for the next N seconds, the consumer says *"hold this for N seconds, then make it available."* The pool stays trivially simple (no scheduling logic in `get_next_*`), no `skip_predicate` is required for the cooldown case, and no async worker / background process is needed.

## Motivation

The existing `skip_predicate` (and the in-flight retry-after extension in [redis-pool-retry-after-predicate.md](redis-pool-retry-after-predicate.md)) solve the eligibility problem from the wrong end: the member is returned to the available list *immediately on checkin*, then every subsequent `get_next_available` has to ask "is this one still cooling?" via the predicate. The pool ends up doing TTL-aware scheduling because the member came back too early.

If checkin can say "hold this for N seconds before making it available," the predicate becomes unnecessary for the entire cooldown family of cases:

- The pool doesn't need to know about cooldown TTL keys.
- `get_next_available` doesn't need to evaluate any predicate per candidate.
- Consumers don't maintain a parallel TTL keyspace just to express "not yet."
- The existing checkin/checkout flow stays untouched — predicate-less callers see no change.

This is an additive, opt-in surface. Existing methods (`checkin`, `return_instance`, `get_next_available`, `get_next_instance`) keep their current signatures and behaviour exactly. The retry-after predicate work proceeds independently — the two features are complementary (predicate covers state-driven eligibility that isn't delay-shaped, e.g. maintenance flags or external readiness; delayed checkin covers the "I know exactly when this is ready" case).

## Proposal — Lazy Promotion via Sorted Set

A new Redis key per pool: `<pool_key>:deferred` — a sorted set with `score = available_at_unix_seconds` and `member = str_id`.

### New methods

**`RedisBasePool.checkin_after(str_id, delay, allow_duplicate=False)`** — schedule the member to become available again after `delay` seconds.

- If `delay <= 0`, behaves identically to `checkin(str_id, allow_duplicate)`.
- Otherwise: `ZADD <pool>:deferred GT score=now+delay member=str_id` (the `GT` flag — Redis 6.2+ — keeps the latest checkin "winning" if the same id is scheduled twice with different delays).
- Member must already be in `all_items_set_key` (otherwise return `False`, same gate as `checkin`).
- The duplicate-protection check (`allow_duplicate=False`) covers both the available list and the deferred set — if the id is already pending in either, return `False`.

**`RedisBasePool.checkout_item_after(timeout=None, hold_after_use=None, raise_on_timeout=True)`** — context manager that calls `checkin_after(str_id, hold_after_use)` on exit instead of `checkin`. If `hold_after_use is None`, falls back to immediate `checkin` (so the manager can be used uniformly without forcing every checkout to be delayed).

**`RedisModelPool.return_instance_after(instance, delay, allow_duplicate=False)`** — same as `checkin_after` but takes an instance (matches `return_instance` shape).

**`RedisModelPool.checkout_instance_after(timeout=None, hold_after_use=None)`** — context manager analogue.

### Promotion on every pull

`get_next_available` (and therefore `get_next_instance`) gains a single new line at the top, before any existing logic:

```python
self._promote_matured_deferred()
```

```python
def _promote_matured_deferred(self):
    """Move every deferred entry whose available_at <= now into the available list head."""
    now = time.time()
    # Atomic pop-by-score via Lua to avoid races between scan and remove
    promoted = self.redis_client.eval(_PROMOTE_LUA, 1, self.deferred_zset_key, now, self.available_list_key)
    return promoted  # count, useful for tests / metrics
```

The Lua snippet (single round-trip, atomic):

```lua
-- KEYS[1] = deferred zset, ARGV[1] = now, ARGV[2] = list key
local ids = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
if #ids == 0 then return 0 end
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
for i = 1, #ids do
    redis.call('LPUSH', ARGV[2], ids[i])
end
return #ids
```

After promotion, the rest of `get_next_available` proceeds unchanged.

### Helper queries

- **`list_deferred()`** → returns `[(str_id, available_at_unix_seconds), ...]` for observability.
- **`list_deferred_instances()`** on the model pool → list of `(instance, available_at)` tuples.
- **`clear_deferred()`** → flushes the deferred set (used by `clear()` / `destroy_pool()`).
- **`remove(str_id, force=False)`** and **`remove_from_pool(...)`** — extended to also `ZREM` from the deferred set so a removed member doesn't get promoted later.

### Interaction with existing surfaces

- **`is_ready()`** unchanged — readiness is still defined by the available list + all-items set.
- **`list_all()`** unchanged — `all_items_set_key` is the source of truth for membership.
- **`list_available()`** unchanged — only returns members currently in the list. Deferred members are explicitly *not* available; `list_deferred()` covers them.
- **`list_checked_out()`** updated — currently `all_items - list_available()`; the new definition is `all_items - list_available() - list_deferred()`. Otherwise deferred members would falsely appear as "checked out."
- **`init_pool()`** unchanged — initialises the available list as today; deferred set starts empty.
- **`destroy_pool()` / `clear()`** — also delete the deferred set key.
- **`add(str_id)`** — unchanged. A new add goes straight into the available list.
- **`skip_predicate`** — orthogonal. Continues to work exactly as today (and after the retry-after extension lands). Consumers may use either or both: delayed checkin for the "I know when" case, predicate for the "I need to evaluate state at pull time" case.

## Acceptance Criteria

- New `<pool_key>:deferred` ZSET keyed by `available_at` (unix seconds), atomically promoted on every `get_next_*` call via Lua.
- `RedisBasePool.checkin_after(str_id, delay, allow_duplicate=False)` and `RedisModelPool.return_instance_after(instance, delay, allow_duplicate=False)` schedule deferred returns.
- `delay <= 0` falls through to immediate `checkin` / `return_instance`.
- Same-id rescheduling: `ZADD GT` semantics — later-scheduled checkin wins if `delay` is larger; earlier wins otherwise. Document this.
- `checkout_item_after` / `checkout_instance_after` context managers honour an optional `hold_after_use` kwarg; falls back to immediate checkin when `None`.
- `list_deferred()` / `list_deferred_instances()` for observability. `clear_deferred()` for teardown.
- `list_checked_out()` excludes both the available list and the deferred set.
- `remove` / `remove_from_pool` also `ZREM` from the deferred set.
- `destroy_pool` / `clear` delete the deferred set.
- `get_next_available` / `get_next_instance` get exactly one new line at the top: `self._promote_matured_deferred()`. No other behavioural changes to those methods. Predicate-less callers, bool-only predicate callers, and (after the retry-after work lands) numeric predicate callers all see the deferred-promotion step transparently.
- The "deferred member appears in the pool early because of clock skew" risk is bounded — `time.time()` (Redis server clock vs Python clock skew is typically < 100ms; document the assumption).
- A worker calling `checkin_after` then crashing leaves the member scheduled correctly (no in-process state). The next `get_next_*` from any worker promotes it on schedule.
- New tests cover: schedule + promote on next pull; same-id rescheduling; `delay=0` immediate path; `remove` cancels deferred entry; `list_deferred` reports correctly; concurrent promotion doesn't double-add (Lua atomicity); coexistence with `skip_predicate`; coexistence with retry-after numeric predicate.
- No regression in existing pool tests — every existing method behaves identically when `checkin_after` is never called.
- CHANGELOG entry under the next release.

## Investigation

### What exists today

- **`RedisBasePool`** ([pool.py:10](mojo/helpers/redis/pool.py:10)) — `pool_key:list` available list (lpush/brpop FIFO), `pool_key:set` membership set.
- **`checkin`** ([pool.py:100](mojo/helpers/redis/pool.py:100)) — duplicate-protected `lpush` to the available list head.
- **`return_instance`** ([pool.py:409](mojo/helpers/redis/pool.py:409)) — model-pool wrapper with the same duplicate protection.
- **`get_next_available`** ([pool.py:169](mojo/helpers/redis/pool.py:169)) — `brpop` with timeout; predicate handling per the existing skip_predicate work and the in-flight retry-after extension.
- **`list_checked_out`** ([pool.py:135](mojo/helpers/redis/pool.py:135)) — `all_items - list_available()`.

The hybrid promote-on-pull design plugs into `get_next_available` at exactly one point (top of method, single Lua call) and is otherwise additive.

### Why hybrid (option 3) over alternatives

Three approaches were considered:

1. **Sorted-set-only pool** — replace the available list entirely with a ZSET of `(available_at, id)`. Cleanest model, but breaks `BRPOP` (no native blocking primitive on ZSETs), changes the data structure of every existing pool, and requires a schema migration for live deployments. Rejected.

2. **Async-job-per-delayed-checkin** — `checkin(member, delay=N)` enqueues a job that does the actual `lpush` after N seconds. Requires `mojo/apps/jobs` infrastructure, adds a worker dependency, fails if the job runner is down or backed up. Heavier than the problem warrants. Rejected.

3. **Hybrid (this proposal)** — keep the existing list+set structure, layer a deferred ZSET, promote opportunistically on every pull. Additive, no schema migration, no async worker, single Lua round-trip per pull, crash-safe (deferred state lives in Redis). Selected.

### Trade-off accepted: lazy promotion

Promotion happens only when `get_next_*` is called. In a quiet pool with no pulls, a deferred member sits in the ZSET past its `available_at` until someone calls `get_next_*`. Implications:

- **Steady-state worker pools (the target use case)**: pulls are constant; promotion is timely.
- **Low-traffic pools**: a delayed member may not show up in `list_available()` for a while after maturing. `list_available()` is only a snapshot — the member becomes available the moment a pull happens.
- **Observability**: `list_deferred()` reports both pending (future) and ripe (matured-but-not-yet-promoted) members. Consumers wanting "what's actually ready right now" should call `_promote_matured_deferred()` explicitly first, then `list_available()`. Document this.

If a future use case demands eager promotion (e.g., dashboards), a small background sweeper can be added later. Out of scope for this request.

### Implementation sketch

```python
# mojo/helpers/redis/pool.py

_PROMOTE_LUA = """
local ids = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
if #ids == 0 then return 0 end
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
for i = 1, #ids do
    redis.call('LPUSH', KEYS[2], ids[i])
end
return #ids
"""

class RedisBasePool:
    def __init__(self, pool_key, default_timeout=30, skip_predicate=None):
        ...
        self.deferred_zset_key = f"{pool_key}:deferred"
        self._promote_script = self.redis_client.register_script(_PROMOTE_LUA)

    def checkin_after(self, str_id, delay, allow_duplicate=False):
        if not isinstance(str_id, str):
            str_id = str(str_id)
        if not self.redis_client.sismember(self.all_items_set_key, str_id):
            return False
        if delay is None or delay <= 0:
            return self.checkin(str_id, allow_duplicate=allow_duplicate)
        if not allow_duplicate:
            available = self.redis_client.lrange(self.available_list_key, 0, -1)
            if str_id.encode() in available or str_id in available:
                return False
            if self.redis_client.zscore(self.deferred_zset_key, str_id) is not None:
                # Already deferred — only update if new available_at is later (GT)
                pass  # ZADD GT handles this atomically below
        available_at = time.time() + delay
        # ZADD GT: only update if new score is greater than existing
        self.redis_client.zadd(self.deferred_zset_key, {str_id: available_at}, gt=True)
        return True

    def _promote_matured_deferred(self):
        return self._promote_script(
            keys=[self.deferred_zset_key, self.available_list_key],
            args=[time.time()],
        )

    def list_deferred(self):
        return [(m.decode() if isinstance(m, bytes) else m, score)
                for m, score in self.redis_client.zrange(
                    self.deferred_zset_key, 0, -1, withscores=True)]

    def clear_deferred(self):
        self.redis_client.delete(self.deferred_zset_key)

    def get_next_available(self, timeout=None, ...):
        self._promote_matured_deferred()
        # ... existing logic unchanged ...

    def remove(self, str_id, force=False):
        # ... existing logic ...
        self.redis_client.zrem(self.deferred_zset_key, str_id)
        return True

    def list_checked_out(self):
        all_items = self.list_all()
        available = set(self.list_available())
        deferred = {m for m, _ in self.list_deferred()}
        return all_items - available - deferred

    def clear(self):
        self.redis_client.delete(self.available_list_key)
        self.redis_client.delete(self.all_items_set_key)
        self.redis_client.delete(self.deferred_zset_key)


class RedisModelPool(RedisBasePool):
    def return_instance_after(self, instance, delay, allow_duplicate=False):
        return self.checkin_after(str(instance.pk), delay, allow_duplicate=allow_duplicate)

    def list_deferred_instances(self):
        entries = self.list_deferred()
        if not entries:
            return []
        instances_by_pk = {str(i.pk): i for i in self.model_cls.objects.filter(
            pk__in=[e[0] for e in entries])}
        return [(instances_by_pk[pk], at) for pk, at in entries if pk in instances_by_pk]

    @contextmanager
    def checkout_instance_after(self, timeout=None, hold_after_use=None):
        instance = self.get_next_instance(timeout)
        if instance is None:
            raise RuntimeError("No instances available in pool")
        try:
            yield instance
        finally:
            if hold_after_use is not None and hold_after_use > 0:
                self.return_instance_after(instance, hold_after_use)
            else:
                self.return_instance(instance)
```

### Edge cases

- **Same id checked in twice with different delays** — `ZADD GT` keeps the later (larger) score. So `checkin_after(x, 5); checkin_after(x, 30)` → final available_at is `now+30`. Document.
- **Same id checked in then immediately checked-in-immediate** — `checkin(x)` doesn't touch the deferred set. The id ends up in both: the available list (immediate) AND the deferred set (pending). Promotion will then re-add it later. To avoid the double-add, `checkin` should also `ZREM` from the deferred set as a cleanup step. (Trivial extension; document and add to the implementation.)
- **`checkin_after(x, 5)` then `remove(x)`** — `remove` `ZREM`s from deferred; member never promoted. Correct.
- **Clock skew between Redis server and Python** — promotion uses `time.time()` from Python, scoring uses `time.time()` from Python. Both are the same clock per call. No skew within a single worker. Across workers there can be drift — bounded by NTP sync (typically < 100ms). Document.
- **Worker crash after `checkin_after`** — state is in Redis, not in-process. Next pull from any worker promotes correctly. No recovery needed.
- **Pool-wide `clear()` while members are deferred** — `clear()` deletes all three keys (list, set, zset). Deferred entries are dropped, same as available items. Correct.
- **`get_specific_instance(x)` while x is deferred** — `get_specific_instance` uses `LREM` on the available list. If x is in the deferred zset and not in the list, the removal returns 0 → `get_specific_instance` returns False (member not currently checkout-able). Caller can either wait or call a new `get_specific_instance_after_promotion()` helper if the use case demands it. **Open question**: is the explicit-checkout admin path expected to bypass deferred state? Recommendation: no — if an admin needs to force access, `force_remove_from_deferred(x)` then `get_specific_instance(x)` is a clearer two-step.
- **Coexistence with `skip_predicate`** — promotion happens before predicate evaluation. A member promoted into the list will be evaluated by the predicate as today. If the consumer uses `checkin_after`, they probably don't need the predicate; both can coexist for hybrid scenarios.
- **Coexistence with retry-after numeric predicate** (the in-flight extension) — same answer: promotion then predicate path. The deferred zset is for "I know exactly when," the predicate is for "I need to evaluate at pull time." They compose cleanly.
- **Empty pool with deferred members** — `get_next_available` calls `_promote_matured_deferred()` (no-op if nothing matured), then runs `brpop` on an empty list. If something matures during the `brpop` wait it's NOT visible (brpop blocks on the list, not the zset). The consumer needs to either wait for the next pull cycle or set a `default_timeout` shorter than the typical cooldown. **Document this**: blocking `brpop` does not observe deferred maturity. For tight cooldown windows, callers should poll with a short timeout.

## Open Questions

- **Promotion eagerness for low-traffic pools**: should we offer an opt-in background sweeper (a tiny `mojo/apps/jobs` periodic task) for deployments that need ripe-but-not-yet-pulled members to surface without a pull? Recommendation: defer until a real consumer asks. Lazy promotion is simpler and covers the steady-state case.

- **`brpop` blocking does not observe deferred maturity** (last bullet above). Should `get_next_available` switch to a poll-then-sleep loop when the available list is empty AND the deferred zset has entries with `available_at <= deadline`? This is essentially the retry-after predicate path applied to the deferred set. **Recommendation**: yes — when the deferred zset has entries that will mature within the caller's timeout, switch from `brpop` to a `time.sleep(min(soonest_available_at - now, deadline - now, 1.0))` loop with `_promote_matured_deferred` on each wake. This makes the wallclock contract from the retry-after predicate work apply uniformly to delayed-checkin too.

- **Should `checkin_after` accept an absolute `available_at` timestamp** as an alternative to `delay`? Recommendation: no for v1 — seconds-from-now is the natural unit for cooldown. Can add later if a real use case wants to schedule against an external clock.

- **Should the promotion script also drop entries that point to ids no longer in `all_items_set_key`** (i.e., the member was `remove`'d but somehow still in the deferred zset)? Recommendation: no — `remove` already `ZREM`s. Belt-and-suspenders cleanup adds latency to every pull for no real benefit.

- **Metric for promotion count / time-spent-deferred**: useful for tuning cooldown windows. Recommendation: defer to a follow-up; pool internals stay simple.

## Out of Scope

- Replacing the available list with a sorted set (option 1).
- Async-job-based deferred returns (option 2).
- Per-pool background sweeper for eager promotion.
- Absolute-timestamp checkin (use `delay` for v1).
- Metrics / observability hooks beyond `list_deferred()`.
- Changes to `get_specific_*` admin paths beyond the existing remove-from-deferred extension.
